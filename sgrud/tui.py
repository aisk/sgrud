"""Textual front end over :class:`sgrud.Monitor`.

The app owns a Monitor, takes a snapshot every ``interval`` seconds on the
event loop (a snapshot costs well under a millisecond) and pushes the result
into the widgets. All rendering works from :class:`~sgrud.models.Snapshot`
only, so the UI can be driven by recorded snapshots in tests.
"""

from __future__ import annotations

import math
import os
import zlib
from collections import deque
from collections.abc import Hashable
from dataclasses import dataclass

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Region
from textual.message import Message
from textual.widget import Widget
from textual.widgets import (
    DataTable,
    Footer,
    Label,
    Select,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
    Tree,
)

from .errors import ProcessExited, SgrudError
from .export import Recorder
from .format import (
    describe_file,
    human_bytes,
    human_count,
    human_duration,
    human_rate,
    ipc_summary,
    memory_rows,
    percent,
    short_path,
)
from .models import GCCollection, Snapshot, Task, Thread, ThreadStatus
from .monitor import Monitor
from .probe import ProbeResult
from .profile import CallNode, Hotspots
from .remote import MODES
from .sampler import Sampler

HISTORY = 120

Path = tuple[Hashable, ...]


def _status_text(status: ThreadStatus) -> Text:
    label = status.describe()
    if status & ThreadStatus.HAS_EXCEPTION:
        style = "bold red"
    elif status & ThreadStatus.HAS_GIL:
        style = "bold green"
    elif status & ThreadStatus.GIL_REQUESTED:
        style = "yellow"
    elif status & ThreadStatus.ON_CPU:
        style = "cyan"
    else:
        style = "dim"
    return Text(label, style=style)


def _latest_collection(snap: Snapshot) -> GCCollection | None:
    """The most recent collection of any generation."""
    latest = None
    for g in snap.gc:
        if g.history and (latest is None or g.history[0].stopped_at > latest.stopped_at):
            latest = g.history[0]
    return latest


_KIND_STYLES = {"pipe": "yellow", "socket": "green", "shm": "magenta", "anon": "dim"}


def _waiting_threads(snap: Snapshot) -> dict[int, tuple[Thread, str]]:
    """Descriptor to (thread blocked on it, name of the call)."""
    out: dict[int, tuple[Thread, str]] = {}
    for t in snap.threads:
        if t.syscall is not None and t.syscall.fd >= 0:
            out.setdefault(t.syscall.fd, (t, t.syscall.name))
    return out


def _syscall_text(t: Thread) -> str:
    if t.syscall is None:
        return ""
    return t.syscall.name if t.syscall.fd < 0 else f"{t.syscall.name}(fd {t.syscall.fd})"


def _syscall_note(t: Thread, snap: Snapshot) -> str:
    """One line on the call a thread is blocked in and what its descriptor is."""
    sc = t.syscall
    if sc is None:
        return ""
    note = f"in {sc.describe()}"
    if snap.ipc is None or sc.fd < 0:
        return note
    f = snap.ipc.file(sc.fd)
    if f is None:
        return note
    if f.kind == "socket":
        note = f"in {sc.name}(fd {sc.fd} {describe_file(f)})"
    if f.shared_with:
        note += f", shared with pid {', '.join(map(str, f.shared_with))}"
    others = snap.ipc.same_object(sc.fd)
    if others:
        note += ", also fd " + ", ".join(f"{o.fd} ({o.mode})" for o in others) + " here"
    return note


def _top_frame(frames) -> str:
    for f in frames:
        if not f.synthetic:
            return f"{f.funcname}  {short_path(f.filename)}:{f.lineno}"
    return frames[0].funcname if frames else ""


class Summary(Static):
    """One line process summary shown above the tabs."""

    def update_from(
        self,
        snap: Snapshot,
        interval: float,
        paused: bool,
        exited: ProcessExited | None = None,
    ) -> None:
        p = snap.process
        m = p.memory
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(f" pid {p.pid} ", "bold")
        text.append(os.path.basename(p.exe) or "?")
        text.append(f"  up {human_duration(p.uptime)}")
        text.append(f"  cpu {percent(p.cpu_percent).strip()}%", "cyan")
        text.append(f"  rss {human_bytes(m.rss)}", "magenta")
        text.append(f"  vms {human_bytes(m.vms)}")
        text.append(f"  threads {p.num_threads}")
        text.append(f"  tasks {len(snap.tasks)}")
        share = snap.gc_time_share
        if share is not None:
            text.append(f"  gc {share * 100:.1f}%", "bold red" if snap.collecting else "")
        elif snap.gc:
            text.append(f"  gc0 {snap.gc[0].collections}")
        text.append(f"  every {interval:g}s")
        if exited is not None:
            code = "" if exited.returncode is None else f" code {exited.returncode}"
            text.append(f"  EXITED{code}", "bold white on red")
        elif paused:
            text.append("  PAUSED", "bold yellow")
        for section, err in snap.errors.items():
            if section == "attach":
                continue  # shown in the banner
            text.append(f"  !{section}: {err.splitlines()[0]}", "red")
        # Last so it is what gets cut off on a narrow terminal.
        text.append("  " + " ".join(p.cmdline), "dim")
        self.update(text)


class StackPanel(Static):
    """Shows the frames of whatever is selected in the current tab.

    ``stale`` marks frames that belong to a thread or task which is no
    longer present in the latest snapshot.
    """

    last_title: str = ""
    last_frames: tuple = ()
    stale: bool = False

    def show(self, title: str, frames, *, stale: bool = False, note: str = "") -> None:
        """``note`` is a line under the title, for what the thread is blocked in."""
        self.last_title = title
        self.last_frames = tuple(frames)
        self.stale = stale
        header = Text(title, style="bold red" if stale else "bold")
        if note:
            header = Group(header, Text(note, style="dim" if stale else "cyan"))
        if not self.last_frames:
            self.update(Group(header, Text("  (no Python frames)", style="dim")))
            return
        table = Table.grid(padding=(0, 2))
        table.add_column(
            no_wrap=True, overflow="ellipsis", min_width=24, style="dim" if stale else ""
        )
        table.add_column(no_wrap=True, overflow="ellipsis", style="dim")
        for f in self.last_frames:
            where = "" if f.synthetic else f"{short_path(f.filename)}:{f.lineno}"
            table.add_row(f.funcname, where)
        self.update(Group(header, table))


_FLAME_COLORS = (
    "#d9432f",
    "#e8622e",
    "#f07f2f",
    "#f39a33",
    "#f5b23a",
    "#f7c948",
    "#e0993a",
    "#cc5a2b",
)
_FLAME_THREAD = "black on #7aa6c2"
_FLAME_SYNTHETIC = "black on #9e9e9e"
_FLAME_CURSOR = "bold white on #1f5fbf"


def _flame_style(node: CallNode) -> str:
    if node.tid is not None:
        return _FLAME_THREAD
    if node.synthetic:
        return _FLAME_SYNTHETIC
    return f"black on {_FLAME_COLORS[zlib.crc32(node.filename.encode()) % len(_FLAME_COLORS)]}"


@dataclass(slots=True)
class _Cell:
    node: CallNode
    path: Path
    depth: int
    x: int
    width: int


class FlameGraph(Widget, can_focus=True):
    """Draws a :class:`~sgrud.profile.CallNode` tree as a flame graph.

    The root sits on the bottom row and callees stack upwards, one row per
    depth, each cell as wide as its share of the zoomed root's samples. A
    keyboard cursor replaces mouse hover: arrows move between cells, enter
    zooms into the cursor cell, backspace zooms out one level.
    """

    DEFAULT_CSS = "FlameGraph { height: auto; }"
    BINDINGS = [
        Binding("left", "move('left')", "Prev", show=False),
        Binding("right", "move('right')", "Next", show=False),
        Binding("up", "move('up')", "Callee", show=False),
        Binding("down", "move('down')", "Caller", show=False),
        Binding("enter", "zoom", "Zoom"),
        Binding("backspace", "zoom_out", "Zoom out"),
        Binding("escape", "zoom_reset", "Reset zoom", show=False),
    ]

    class Changed(Message):
        """Cursor or zoom moved."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.root: CallNode | None = None
        #: Keys from the root down to the node shown at the bottom row.
        self.zoom: Path = ()
        #: Keys from the root down to the cell the cursor is on.
        self.cursor: Path = ()
        self._cells_cache: tuple[int, list[_Cell]] | None = None

    # -- data ----------------------------------------------------------

    def set_tree(self, root: CallNode) -> None:
        self.root = root
        self.zoom = self._existing(self.zoom)
        self.cursor = self._existing(self.cursor)
        if self.cursor[: len(self.zoom)] != self.zoom:
            self.cursor = self.zoom
        self._cells_cache = None
        self.refresh(layout=True)
        self.post_message(self.Changed())

    def node_at(self, path: Path) -> CallNode | None:
        node = self.root
        for key in path:
            if node is None:
                return None
            node = node.children.get(key)
        return node

    def _existing(self, path: Path) -> Path:
        node = self.root
        for i, key in enumerate(path):
            if node is None or key not in node.children:
                return path[:i]
            node = node.children[key]
        return path

    @property
    def cursor_node(self) -> CallNode | None:
        return self.node_at(self.cursor)

    # -- layout --------------------------------------------------------

    def cells(self, width: int | None = None) -> list[_Cell]:
        width = width or self.size.width
        if self._cells_cache is not None and self._cells_cache[0] == width:
            return self._cells_cache[1]
        cells: list[_Cell] = []
        zoomed = self.node_at(self.zoom)
        if zoomed is not None and zoomed.total > 0 and width > 0:
            self._place(cells, zoomed, self.zoom, 0, 0.0, float(width))
        self._cells_cache = (width, cells)
        return cells

    def _place(
        self, cells: list[_Cell], node: CallNode, path: Path, depth: int, x0: float, x1: float
    ) -> None:
        start, end = round(x0), round(x1)
        if end - start < 1:
            return
        cells.append(_Cell(node, path, depth, start, end - start))
        scale = (x1 - x0) / node.total
        x = x0
        for child in sorted(node.children.values(), key=lambda c: (-c.total, c.name)):
            w = child.total * scale
            self._place(cells, child, path + (child.key,), depth + 1, x, x + w)
            x += w

    def _rows(self, width: int | None = None) -> int:
        cells = self.cells(width)
        return max((c.depth for c in cells), default=0) + 1

    def get_content_width(self, container, viewport) -> int:
        return container.width

    def get_content_height(self, container, viewport, width: int) -> int:
        return self._rows(width)

    def render(self):
        width = self.size.width
        cells = self.cells(width)
        if not cells:
            return Text("(no samples yet)", style="dim")
        rows = self._rows(width)
        lines = [Text(no_wrap=True, overflow="crop") for _ in range(rows)]
        fill = [0] * rows
        for cell in sorted(cells, key=lambda c: (c.depth, c.x)):
            line = lines[cell.depth]
            if cell.x > fill[cell.depth]:
                line.append(" " * (cell.x - fill[cell.depth]))
            name = cell.node.name
            label = (" " + name) if cell.width >= 3 else name
            style = _FLAME_CURSOR if cell.path == self.cursor else _flame_style(cell.node)
            line.append(label[: cell.width].ljust(cell.width), style)
            fill[cell.depth] = cell.x + cell.width
        return Group(*reversed(lines))

    # -- navigation ----------------------------------------------------

    def _cursor_cell(self) -> _Cell | None:
        for cell in self.cells():
            if cell.path == self.cursor:
                return cell
        return None

    def action_move(self, direction: str) -> None:
        cells = self.cells()
        current = self._cursor_cell()
        if current is None:
            if not cells:
                return
            self._set_cursor(cells[0])
            return
        target: _Cell | None = None
        if direction in ("left", "right"):
            siblings = sorted(
                (c for c in cells if c.depth == current.depth and c.path[:-1] == current.path[:-1]),
                key=lambda c: c.x,
            )
            i = siblings.index(current) + (1 if direction == "right" else -1)
            if 0 <= i < len(siblings):
                target = siblings[i]
        elif direction == "up":
            kids = [
                c for c in cells if c.depth == current.depth + 1 and c.path[:-1] == current.path
            ]
            if kids:
                target = max(kids, key=lambda c: c.width)
        elif len(current.path) > len(self.zoom):
            target = next((c for c in cells if c.path == current.path[:-1]), None)
        if target is not None:
            self._set_cursor(target)

    def _set_cursor(self, cell: _Cell) -> None:
        self.cursor = cell.path
        self.refresh()
        self.post_message(self.Changed())
        rows = self._rows()
        region = Region(cell.x, rows - 1 - cell.depth, cell.width, 1)
        parent = self.parent
        if isinstance(parent, Widget):
            parent.scroll_to_region(region.translate(self.virtual_region.offset), animate=False)

    def action_zoom(self) -> None:
        self._set_zoom(self.cursor)

    def action_zoom_out(self) -> None:
        if self.zoom:
            self._set_zoom(self.zoom[:-1])

    def action_zoom_reset(self) -> None:
        self._set_zoom(())

    def _set_zoom(self, path: Path) -> None:
        if path == self.zoom:
            return
        self.zoom = path
        if self.cursor[: len(path)] != path:
            self.cursor = path
        self._cells_cache = None
        self.refresh(layout=True)
        self.post_message(self.Changed())


class ThreadFilter(Select[int]):
    """Thread selector that tells the app when its dropdown closes.

    The app moves focus back to the tab's content on :class:`Closed`, so
    picking a thread (or cancelling with escape) never leaves the user
    parked on the filter.
    """

    class Closed(Message):
        def __init__(self, select: ThreadFilter) -> None:
            super().__init__()
            self.select = select

        @property
        def control(self) -> ThreadFilter:
            return self.select

    def watch_expanded(self, expanded: bool) -> None:
        if not expanded:
            self.post_message(self.Closed(self))


class SgrudApp(App[int]):
    TITLE = "sgrud"
    CSS = """
    Summary { height: 1; background: $primary-background; }
    #banner { height: auto; padding: 0 1; background: $warning 30%; color: $text; }
    #tabs { height: 1fr; }
    #left { width: 3fr; }
    .stack { width: 2fr; border-left: solid $secondary; padding: 0 1; }
    DataTable { height: 1fr; }
    Tree { height: 1fr; }
    #memhist, #cpuhist, #faulthist, #heaphist, #gchist { height: 3; }
    #gc-info { height: 2; padding: 0 1; }
    #probe-info { height: auto; padding: 0 1; color: $text-muted; }
    #gc-table { height: auto; }
    #hot-bar, #flame-bar { height: 3; }
    #hot-filter, #flame-filter { width: 40; }
    #hot-info, #flame-info { padding: 1 2; color: $text-muted; }
    #flame-scroll { height: 1fr; align-vertical: bottom; }
    #flame-status { height: 2; padding: 0 1; border-top: solid $secondary; }
    .hist-label { color: $text-muted; }
    #procinfo { padding: 1; height: auto; }
    #children-label { padding: 0 1; color: $text-muted; }
    #children-table { height: 1fr; }
    #ipc-info { padding: 1; height: auto; }
    #ipc-table { height: 1fr; }
    """
    # Focus always lives in the active tab's content, never on the tab bar,
    # so the arrow keys drive tables, trees and the flame graph while tab
    # switching has keys of its own. ``tab`` takes priority over Textual's
    # focus cycling because no pane has more than one main widget; the
    # thread filter is reached with ``f`` instead.
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("+,=", "faster", "Faster", key_display="+"),
        Binding("-", "slower", "Slower"),
        Binding("1", "tab('threads')", "Tabs", key_display="1-7 tab"),
        Binding("2", "tab('tasks')", "Tasks", show=False),
        Binding("3", "tab('gc')", "GC", show=False),
        Binding("4", "tab('process')", "Process", show=False),
        Binding("5", "tab('hotspots')", "Hotspots", show=False),
        Binding("6", "tab('flame')", "Flame", show=False),
        Binding("7", "tab('ipc')", "IPC", show=False),
        Binding("tab", "next_tab", "Next tab", show=False, priority=True),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
        Binding("f", "focus_filter", "Filter"),
        Binding("s", "toggle_sort", "Sort self/total"),
        Binding("c", "clear_hotspots", "Clear samples"),
        Binding("m", "toggle_mode", "Mode"),
        Binding("x", "probe", "Probe target"),
    ]

    #: The widget that gets focus when a tab becomes active.
    TAB_CONTENT = {
        "threads": "#threads-table",
        "tasks": "#tasks-tree",
        "gc": "#gc-table",
        "process": "#children-table",
        "hotspots": "#hot-table",
        "flame": "#flame-graph",
        "ipc": "#ipc-table",
    }

    def __init__(
        self,
        monitor: Monitor,
        *,
        interval: float = 1.0,
        stacks: bool = True,
        tasks: bool = True,
        gc: bool = True,
        children: bool = True,
        ipc: bool = True,
        sample_rate: float = 100.0,
        sample_mode: str = "wall",
        record: str | None = None,
    ):
        """``record`` is a path for a binary recording of every sample taken."""
        super().__init__()
        self.monitor = monitor
        self.hotspots = Hotspots()
        #: The sampling mode, kept here too so it shows without a sampler.
        self.sample_mode = sample_mode
        self.sampler: Sampler | None = None
        if sample_rate > 0 and monitor.limited is None:
            recorders = []
            if record:
                recorders.append(Recorder(record, "binary", interval=1 / sample_rate))
            self.sampler = Sampler(
                monitor, self.hotspots, rate=sample_rate, mode=sample_mode, recorders=recorders
            )
        self.hot_sort = "self"
        self.hot_thread: int | None = None
        self._hot_options: tuple[int, ...] = ()
        self.interval = interval
        self.sections = dict(stacks=stacks, tasks=tasks, gc=gc, children=children, ipc=ipc)
        self.paused = False
        #: Set once the target has gone away. The last snapshot is kept.
        self.exited: ProcessExited | None = None
        self.snapshot: Snapshot | None = None
        self.rss_history: deque[int] = deque(maxlen=HISTORY)
        self.cpu_history: deque[float] = deque(maxlen=HISTORY)
        self.fault_history: deque[float] = deque(maxlen=HISTORY)
        self.heap_history: deque[int] = deque(maxlen=HISTORY)
        self.gc_history: deque[float] = deque(maxlen=HISTORY)
        self._timer = None
        self._selected_tid: int | None = None
        self._selected_task: int | None = None
        self._last_thread: Thread | None = None
        self._last_task: Task | None = None
        self._probing = False
        #: The last answer from the target, see :meth:`action_probe`.
        self.probe_result: ProbeResult | None = None

    # -- layout --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Summary(id="summary")
        banner = Static("", id="banner")
        banner.display = self.monitor.limited is not None
        if self.monitor.limited is not None:
            banner.update(
                Text.assemble(
                    ("LIMITED MODE  ", "bold"),
                    "stacks, tasks, GC and hotspots are unavailable: ",
                    self.monitor.limited,
                )
            )
        yield banner
        with TabbedContent(id="tabs"):
            with TabPane("Threads", id="threads"):
                with Horizontal():
                    with Vertical(id="left"):
                        yield DataTable(id="threads-table", cursor_type="row", zebra_stripes=True)
                    yield StackPanel("", id="thread-stack", classes="stack")
            with TabPane("Tasks", id="tasks"):
                with Horizontal():
                    with Vertical(id="left"):
                        yield Tree("asyncio", id="tasks-tree")
                    yield StackPanel("", id="task-stack", classes="stack")
            with TabPane("GC", id="gc"):
                yield Static("", id="gc-info")
                yield Static(
                    Text(
                        "x runs a probe inside the target: gc thresholds and counts, "
                        "allocator blocks, object types",
                        "dim",
                    ),
                    id="probe-info",
                )
                yield Label("tracked objects", classes="hist-label")
                yield Sparkline([], id="heaphist")
                yield Label("time in gc %", classes="hist-label")
                yield Sparkline([], id="gchist")
                yield DataTable(id="gc-table", cursor_type="row")
                history = DataTable(id="gc-history", cursor_type="none")
                history.can_focus = False
                yield history
            with TabPane("Process", id="process"):
                yield Label("rss", classes="hist-label")
                yield Sparkline([], id="memhist")
                yield Label("cpu %", classes="hist-label")
                yield Sparkline([], id="cpuhist")
                yield Label("page faults /s", classes="hist-label")
                yield Sparkline([], id="faulthist")
                yield Static("", id="procinfo")
                yield Static("", id="children-label")
                yield DataTable(id="children-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Hotspots", id="hotspots"):
                with Horizontal(id="hot-bar"):
                    yield ThreadFilter(
                        [("all threads", -1)], value=-1, allow_blank=False, id="hot-filter"
                    )
                    yield Static("", id="hot-info")
                yield DataTable(id="hot-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Flame", id="flame"):
                with Horizontal(id="flame-bar"):
                    yield ThreadFilter(
                        [("all threads", -1)], value=-1, allow_blank=False, id="flame-filter"
                    )
                    yield Static("", id="flame-info")
                with VerticalScroll(id="flame-scroll", can_focus=False):
                    yield FlameGraph(id="flame-graph")
                yield Static("", id="flame-status")
            with TabPane("IPC", id="ipc"):
                yield Static("", id="ipc-info")
                yield DataTable(id="ipc-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#threads-table", DataTable)
        table.add_columns("tid", "name", "status", "st", "cpu%", "utime", "syscall", "where")
        gc = self.query_one("#gc-table", DataTable)
        gc.add_columns(
            "gen", "collections", "rate", "time%", "mean", "collected", "uncollectable", "objects"
        )
        hist = self.query_one("#gc-history", DataTable)
        hist.add_columns("gen", "#", "ago", "duration", "collected", "survivors", "objects")
        hot = self.query_one("#hot-table", DataTable)
        hot.add_columns("self%", "total%", "self", "total", "function", "file")
        kids = self.query_one("#children-table", DataTable)
        kids.add_columns("pid", "kind", "state", "cpu%", "rss", "threads", "command")
        ipc = self.query_one("#ipc-table", DataTable)
        ipc.add_columns("fd", "kind", "mode", "object", "shared with", "thread")
        self.query_one("#flame-scroll", VerticalScroll).anchor()
        self.query_one(Tabs).can_focus = False
        self._focus_content()
        if self.sampler is not None:
            self.sampler.start()
        self.refresh_snapshot()
        self._timer = self.set_interval(self.interval, self.refresh_snapshot)

    def on_unmount(self) -> None:
        if self.sampler is not None:
            self.sampler.close()

    # -- actions -------------------------------------------------------

    def _update_summary(self) -> None:
        if self.snapshot:
            self.query_one(Summary).update_from(
                self.snapshot, self.interval, self.paused, self.exited
            )

    def action_toggle_pause(self) -> None:
        if self.exited:
            return
        self.paused = not self.paused
        self._update_summary()

    def action_refresh_now(self) -> None:
        self.refresh_snapshot(force=True)

    def action_faster(self) -> None:
        self._set_interval(max(0.1, self.interval / 2))

    def action_slower(self) -> None:
        self._set_interval(min(30.0, self.interval * 2))

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def action_next_tab(self) -> None:
        self.query_one(Tabs).action_next_tab()

    def action_previous_tab(self) -> None:
        self.query_one(Tabs).action_previous_tab()

    def action_focus_filter(self) -> None:
        active = self.query_one("#tabs", TabbedContent).active
        select = self.query_one(f"#{active}").query_one(ThreadFilter)
        select.focus()
        select.action_show_overlay()

    def _focus_content(self) -> None:
        active = self.query_one("#tabs", TabbedContent).active
        selector = self.TAB_CONTENT.get(active)
        if selector is None:
            self.screen.set_focus(None)
        else:
            self.query_one(selector).focus()

    @on(ThreadFilter.Closed)
    def _filter_closed(self, event: ThreadFilter.Closed) -> None:
        # Select re-focuses itself after a choice or escape. Only take the
        # focus away in that case, not when the user clicked elsewhere.
        if event.select.has_focus:
            self._focus_content()

    #: Keys that only make sense (and only show in the footer) on some tabs.
    TAB_ACTIONS = {
        "focus_filter": ("hotspots", "flame"),
        "toggle_sort": ("hotspots",),
        "clear_hotspots": ("hotspots", "flame"),
        "toggle_mode": ("hotspots", "flame"),
        "probe": ("gc",),
    }

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        tabs = self.TAB_ACTIONS.get(action)
        if tabs is not None:
            return self.query_one("#tabs", TabbedContent).active in tabs
        return True

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        self.refresh_bindings()
        if event.pane.id == "flame" and self.snapshot:
            # The tree is only rebuilt while the tab is visible.
            self._update_flame(self.snapshot)
        self._focus_content()

    def action_toggle_sort(self) -> None:
        self.hot_sort = "total" if self.hot_sort == "self" else "self"
        if self.snapshot:
            self._update_hotspots(self.snapshot)

    def action_toggle_mode(self) -> None:
        self.sample_mode = MODES[(MODES.index(self.sample_mode) + 1) % len(MODES)]
        if self.sampler is not None:
            self.sampler.mode = self.sample_mode
        # Samples are not comparable across modes, so start over.
        self.action_clear_hotspots()

    def action_probe(self) -> None:
        """Run the probe in a thread and show the answer on the GC tab."""
        if self.exited or self._probing:
            return
        if self.monitor.limited is not None:
            self.notify("probing needs access to the target's memory", severity="error")
            return
        self._probing = True
        self.query_one("#probe-info", Static).update(Text("probing...", "dim"))
        self.run_worker(self._run_probe, thread=True, exclusive=True, group="probe")

    def _run_probe(self) -> None:
        try:
            result = self.monitor.probe(types=8)
        except SgrudError as e:
            self.call_from_thread(self._show_probe, None, str(e))
        else:
            self.call_from_thread(self._show_probe, result, None)

    def _show_probe(self, result: ProbeResult | None, error: str | None) -> None:
        self._probing = False
        self.probe_result = result
        info = self.query_one("#probe-info", Static)
        if result is None:
            info.update(Text(f"probe failed: {error}", "red"))
            return
        text = Text()
        thr = "/".join(str(n) for n in result.gc_threshold)
        cnt = "/".join(str(n) for n in result.gc_count)
        text.append("probe  ", "bold")
        text.append(f"threshold {thr}  count {cnt}  ")
        if result.gc_enabled:
            text.append("enabled")
        else:
            text.append("DISABLED", "red")
        text.append(
            f"  frozen {human_count(result.gc_frozen)}  garbage {human_count(result.gc_garbage)}"
            f"  blocks {human_count(result.allocated_blocks)}  modules {result.modules}"
        )
        if result.tracing:
            text.append(f"  tracemalloc {human_bytes(result.tracemalloc_traced)}", "cyan")
        text.append(f"  ({human_duration(result.elapsed)} in target)", "dim")
        if result.types:
            text.append("\ntypes  ", "bold")
            text.append(
                "  ".join(f"{t.name} {human_count(t.count)}" for t in result.types)
                + f"  of {human_count(result.tracked)} tracked"
            )
        info.update(text)

    def action_clear_hotspots(self) -> None:
        self.hotspots.reset()
        if self.snapshot:
            self._update_hotspots(self.snapshot)
            self._update_flame(self.snapshot)

    @on(Select.Changed, "#hot-filter, #flame-filter")
    def _hot_filter_changed(self, event: Select.Changed) -> None:
        value = event.value
        self.hot_thread = value if isinstance(value, int) and value != -1 else None
        # The Hotspots and Flame tabs share one thread filter.
        for select in self.query(Select):
            if select is not event.select and select.value != value:
                select.value = value
        if self.snapshot:
            self._update_hotspots(self.snapshot)
            self._update_flame(self.snapshot)

    def _set_interval(self, value: float) -> None:
        self.interval = value
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_interval(self.interval, self.refresh_snapshot)
        self._update_summary()

    # -- data flow -----------------------------------------------------

    def refresh_snapshot(self, force: bool = False) -> None:
        if self.exited or (self.paused and not force):
            return
        try:
            snap = self.monitor.snapshot(**self.sections)
        except ProcessExited as e:
            self.mark_exited(e)
            return
        except SgrudError as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        self.apply_snapshot(snap)

    def mark_exited(self, exc: ProcessExited) -> None:
        """Freeze the UI on the last snapshot and announce the exit."""
        self.exited = exc
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self.sampler is not None:
            self.sampler.stop()
        self.notify(str(exc), severity="warning", timeout=10)
        self._update_summary()
        self._show_thread(None)
        self._show_task(None)

    def apply_snapshot(self, snap: Snapshot) -> None:
        """Render a snapshot. Public so tests and replays can drive the UI."""
        self.snapshot = snap
        self.rss_history.append(snap.process.memory.rss)
        if snap.process.cpu_percent is not None:
            self.cpu_history.append(snap.process.cpu_percent)
        if snap.process.fault_rate is not None:
            self.fault_history.append(snap.process.fault_rate)
        latest = _latest_collection(snap)
        if latest is not None:
            self.heap_history.append(latest.heap_size)
        if snap.gc_time_share is not None:
            self.gc_history.append(snap.gc_time_share * 100)
        self._update_summary()
        self._update_threads(snap)
        self._update_tasks(snap)
        self._update_gc(snap)
        self._update_process(snap)
        self._update_ipc(snap)
        self._update_hotspots(snap)
        if self.query_one("#tabs", TabbedContent).active == "flame":
            self._update_flame(snap)

    def _update_threads(self, snap: Snapshot) -> None:
        table = self.query_one("#threads-table", DataTable)
        seen = set()
        for t in snap.threads:
            key = str(t.tid)
            seen.add(key)
            cells = (
                str(t.tid),
                t.name,
                _status_text(t.status),
                t.state,
                percent(t.cpu_percent),
                f"{t.user_time + t.system_time:.2f}",
                _syscall_text(t),
                _top_frame(t.frames),
            )
            if key in table.rows:
                for col, value in zip(table.columns, cells, strict=True):
                    table.update_cell(key, col, value, update_width=True)
            else:
                table.add_row(*cells, key=key)
        for key in [k for k in table.rows if k.value not in seen]:
            table.remove_row(key)
        if self._selected_tid is None and snap.threads:
            self._selected_tid = snap.threads[0].tid
        self._show_thread(snap.thread(self._selected_tid) if self._selected_tid else None)

    def _show_thread(self, t: Thread | None) -> None:
        panel = self.query_one("#thread-stack", StackPanel)
        if t is not None:
            self._last_thread = t
            note = ""
            if t.syscall is not None and self.snapshot is not None:
                note = _syscall_note(t, self.snapshot)
            panel.show(f"[{t.tid}] {t.name}  {t.status.describe()}", t.frames, note=note)
            return
        last = self._last_thread
        if last is None:
            panel.show("no thread selected", ())
        else:
            reason = "process exited" if self.exited else "thread gone"
            panel.show(
                f"[{last.tid}] {last.name}  ({reason}, last seen stack)", last.frames, stale=True
            )

    @on(DataTable.RowHighlighted, "#threads-table")
    def _thread_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None or self.snapshot is None:
            return
        self._selected_tid = int(event.row_key.value)
        self._show_thread(self.snapshot.thread(self._selected_tid))

    def _update_tasks(self, snap: Snapshot) -> None:
        tree = self.query_one("#tasks-tree", Tree)
        children = snap.task_children()
        tree.root.label = f"asyncio ({len(snap.tasks)} tasks)"
        cursor_line = tree.cursor_line
        tree.clear()

        def add(parent, task: Task, seen: frozenset[int]) -> None:
            label = Text.assemble(
                (task.name, "bold"),
                f"  {task.frames[0].funcname if task.frames else '?'}",
                (f"  tid {task.thread_id}", "dim"),
            )
            kids = children.get(task.id, [])
            node = parent.add(label, data=task.id, expand=True, allow_expand=bool(kids))
            if task.id in seen:
                node.add_leaf("(cycle)")
                return
            for k in kids:
                add(node, k, seen | {task.id})

        for root in children.get(None, []):
            add(tree.root, root, frozenset())
        tree.root.expand()
        if cursor_line >= 0:
            tree.cursor_line = min(cursor_line, tree.last_line)
        if self._selected_task is None and snap.tasks:
            roots = children.get(None, [])
            self._selected_task = (roots[0] if roots else snap.tasks[0]).id
        self._show_task(snap.task(self._selected_task) if self._selected_task else None)

    def _show_task(self, task: Task | None) -> None:
        panel = self.query_one("#task-stack", StackPanel)
        if task is not None:
            self._last_task = task
            panel.show(f"{task.name} (0x{task.id:x})", task.frames)
            return
        last = self._last_task
        if last is None:
            panel.show("select a task", ())
        else:
            reason = "process exited" if self.exited else "task finished"
            panel.show(
                f"{last.name} (0x{last.id:x})  ({reason}, last seen stack)", last.frames, stale=True
            )

    @on(Tree.NodeHighlighted, "#tasks-tree")
    def _task_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if isinstance(event.node.data, int) and self.snapshot is not None:
            self._selected_task = event.node.data
            self._show_task(self.snapshot.task(self._selected_task))

    def _update_gc(self, snap: Snapshot) -> None:
        self.query_one("#heaphist", Sparkline).data = list(self.heap_history)
        self.query_one("#gchist", Sparkline).data = list(self.gc_history)
        self.query_one("#gc-info", Static).update(self._gc_info(snap))
        table = self.query_one("#gc-table", DataTable)
        table.clear()
        hist = self.query_one("#gc-history", DataTable)
        hist.clear()
        for g in snap.gc:
            table.add_row(
                str(g.generation),
                str(g.collections),
                human_rate(g.rate),
                "-" if g.time_share is None else f"{g.time_share * 100:.2f}",
                human_duration(g.mean_duration),
                str(g.collected),
                str(g.uncollectable),
                human_count(g.heap_size),
            )
        rows = sorted(
            (c for g in snap.gc for c in g.history), key=lambda c: c.stopped_at, reverse=True
        )
        for c in rows[:50]:
            hist.add_row(
                str(c.generation),
                str(c.index),
                "?" if math.isnan(c.age) else human_duration(max(c.age, 0.0)),
                human_duration(c.duration),
                human_count(c.collected),
                human_count(c.survivors),
                human_count(c.heap_size),
            )

    def _gc_info(self, snap: Snapshot) -> Text:
        info = Text(no_wrap=True, overflow="ellipsis")
        share = snap.gc_time_share
        if share is None:
            info.append("waiting for a second snapshot", "dim")
        else:
            info.append(f"time in gc {share * 100:.2f}%", "bold")
            info.append(f"  {human_rate(snap.gc_rate)} collections")
        latest = _latest_collection(snap)
        if latest is not None:
            info.append(f"  {human_count(latest.heap_size)} tracked objects")
            anon = snap.process.memory.anon or snap.process.memory.uss
            if anon and latest.heap_size:
                info.append(f"  {human_bytes(anon // latest.heap_size)} anon per object")
        for t in snap.collecting:
            info.append(f"  COLLECTING in {t.name or t.tid}", "bold red")
        info.append("\n")
        if self.sampler is None:
            info.append("trigger sites need the sampler (--rate)", "dim")
        else:
            gc_samples, samples, sites = self.hotspots.gc_sites(limit=4)
            if not samples:
                info.append("waiting for samples", "dim")
            else:
                info.append(f"in gc {100 * gc_samples / samples:.1f}% of {samples} samples")
                if sites:
                    info.append("  triggered from ", "dim")
                    info.append(", ".join(f"{site.funcname} {site.percent:.1f}%" for site in sites))
        return info

    def _update_process(self, snap: Snapshot) -> None:
        self.query_one("#memhist", Sparkline).data = list(self.rss_history)
        self.query_one("#cpuhist", Sparkline).data = list(self.cpu_history)
        self.query_one("#faulthist", Sparkline).data = list(self.fault_history)
        p = snap.process
        lines = [
            f"exe      {p.exe}",
            f"cmdline  {' '.join(p.cmdline)}",
            f"state    {p.state}    uptime {human_duration(p.uptime)}    threads {p.num_threads}",
            f"cpu      {percent(p.cpu_percent).strip()}%"
            f"   user {p.user_time:.2f}s   sys {p.system_time:.2f}s",
        ]
        for label, pairs in memory_rows(p):
            cells = [f"{name} {value}".strip() for name, value in pairs]
            lines.append(f"{label:<8} " + "   ".join(cells))
        self.query_one("#procinfo", Static).update("\n".join(lines))
        self._update_children(snap)

    def _update_children(self, snap: Snapshot) -> None:
        label = self.query_one("#children-label", Static)
        table = self.query_one("#children-table", DataTable)
        if not snap.children:
            label.update(Text("no child processes", "dim"))
            table.display = False
            return
        pythons = sum(c.python for c in snap.children)
        label.update(f"children: {len(snap.children)}, {pythons} python")
        table.display = True
        depth = {c.pid: 0 for c in snap.children}
        for c in snap.children:
            depth[c.pid] = depth.get(c.parent_pid, -1) + 1
        table.clear()
        for c in snap.children:
            cmd = " ".join(c.cmdline) or c.name or "?"
            table.add_row(
                str(c.pid),
                Text("python", "cyan") if c.python else Text("other", "dim"),
                c.state or "?",
                percent(c.cpu_percent),
                human_bytes(c.rss),
                str(c.num_threads),
                "  " * depth[c.pid] + cmd,
                key=str(c.pid),
            )

    def _update_ipc(self, snap: Snapshot) -> None:
        info = self.query_one("#ipc-info", Static)
        table = self.query_one("#ipc-table", DataTable)
        ipc = snap.ipc
        if ipc is None:
            err = snap.errors.get("ipc")
            info.update(Text(err or "ipc section disabled (--no-ipc)", "dim"))
            table.display = False
            return
        text = Text()
        for i, line in enumerate(ipc_summary(ipc)):
            if i:
                text.append("\n")
            text.append(line, "bold red" if "WAITING" in line else "")
        info.update(text)
        table.display = True
        waiting = _waiting_threads(snap)
        table.clear()
        for i, f in enumerate(ipc.files):
            blocked = ""
            if f.fd in waiting:
                t, name = waiting[f.fd]
                blocked = Text(f"{t.name or t.tid} in {name}", "cyan")
            table.add_row(
                "-" if f.fd < 0 else str(f.fd),
                Text(f.kind, _KIND_STYLES.get(f.kind, "")),
                f.mode or "-",
                describe_file(f),
                ", ".join(map(str, f.shared_with)),
                blocked,
                key=f"{i}:{f.fd}:{f.target}",
            )

    def _sync_thread_filters(self, snap: Snapshot) -> None:
        tids = tuple(t.tid for t in snap.threads)
        if tids == self._hot_options:
            return
        self._hot_options = tids
        options = [("all threads", -1)] + [(f"{t.name} [{t.tid}]", t.tid) for t in snap.threads]
        keep = self.hot_thread if self.hot_thread in tids else -1
        if keep == -1:
            self.hot_thread = None
        for select in self.query(Select):
            select.set_options(options)
            select.value = keep

    def _hot_info(self) -> Text:
        hot = self.hotspots
        info = Text()
        if self.monitor.limited is not None:
            info.append("unavailable without memory access, see banner", "yellow")
        elif self.sampler is None:
            info.append("sampling disabled (--rate 0)", "dim")
        else:
            state = "stopped" if not self.sampler.running else "sampling"
            info.append(f"{state} at {hot.rate():.0f}/s, {hot.samples} samples")
            if self.sampler.errors:
                info.append(f", {self.sampler.errors} failed", "yellow")
        info.append(f"  mode: {self.sample_mode}", "cyan")
        return info

    def _update_hotspots(self, snap: Snapshot) -> None:
        self._sync_thread_filters(snap)
        rows = self.hotspots.rows(thread=self.hot_thread, sort=self.hot_sort, limit=200)
        info = self._hot_info()
        info.append(f"  sort: {self.hot_sort}", "cyan")
        self.query_one("#hot-info", Static).update(info)
        table = self.query_one("#hot-table", DataTable)
        table.clear()
        for r in rows:
            table.add_row(
                f"{r.self_percent:5.1f}",
                f"{r.total_percent:5.1f}",
                str(r.self_samples),
                str(r.total_samples),
                Text(r.funcname, style="dim" if r.synthetic else ""),
                short_path(r.filename),
            )

    def _update_flame(self, snap: Snapshot) -> None:
        self._sync_thread_filters(snap)
        self.query_one("#flame-info", Static).update(self._hot_info())
        names = {t.tid: t.name for t in snap.threads}
        tree = self.hotspots.call_tree(thread=self.hot_thread, names=names)
        self.query_one(FlameGraph).set_tree(tree)

    @on(FlameGraph.Changed)
    def _update_flame_status(self) -> None:
        graph = self.query_one(FlameGraph)
        root, node = graph.root, graph.cursor_node
        status = Text(no_wrap=True, overflow="ellipsis")
        if root is None or node is None or root.total == 0:
            status.append("waiting for samples", "dim")
        else:
            status.append(node.name, "bold")
            if node.tid is None and node.filename and not node.synthetic:
                status.append(f"  {short_path(node.filename)}", "dim")
            share = 100 / root.total
            status.append(f"  self {node.self_samples} ({node.self_samples * share:.1f}%)")
            status.append(f"  total {node.total} ({node.total * share:.1f}%)")
        status.append("\n")
        if graph.zoom:
            crumbs = []
            for i in range(1, len(graph.zoom) + 1):
                zoomed = graph.node_at(graph.zoom[:i])
                crumbs.append(zoomed.name if zoomed else "?")
            status.append("zoom: " + " > ".join(crumbs), "cyan")
            status.append("  backspace out, esc reset", "dim")
        else:
            status.append("arrows move, enter zoom", "dim")
        self.query_one("#flame-status", Static).update(status)


def run_tui(monitor: Monitor, **options) -> int:
    app = SgrudApp(monitor, **options)
    try:
        app.run()
    finally:
        monitor.close()
    return 0
