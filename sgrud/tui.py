"""Textual front end over :class:`sgrud.Monitor`.

The app owns a Monitor, takes a snapshot every ``interval`` seconds on the
event loop (a snapshot costs well under a millisecond) and pushes the result
into the widgets. All rendering works from :class:`~sgrud.models.Snapshot`
only, so the UI can be driven by recorded snapshots in tests.
"""

from __future__ import annotations

import os
from collections import deque

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Label,
    Select,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from .errors import ProcessExited, SgrudError
from .format import human_bytes, human_duration, percent, short_path
from .models import Snapshot, Task, Thread, ThreadStatus
from .monitor import Monitor
from .profile import Hotspots
from .sampler import Sampler

HISTORY = 120


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
        if snap.gc:
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

    def show(self, title: str, frames, *, stale: bool = False) -> None:
        self.last_title = title
        self.last_frames = tuple(frames)
        self.stale = stale
        header = Text(title, style="bold red" if stale else "bold")
        if not self.last_frames:
            self.update(Group(header, Text("  (no Python frames)", style="dim")))
            return
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True, overflow="ellipsis", min_width=24,
                         style="dim" if stale else "")
        table.add_column(no_wrap=True, overflow="ellipsis", style="dim")
        for f in self.last_frames:
            where = "" if f.synthetic else f"{short_path(f.filename)}:{f.lineno}"
            table.add_row(f.funcname, where)
        self.update(Group(header, table))


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
    #memhist, #cpuhist { height: 3; }
    #hot-bar { height: 3; }
    #hot-filter { width: 40; }
    #hot-info { padding: 1 2; color: $text-muted; }
    .hist-label { color: $text-muted; }
    #procinfo { padding: 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("space", "toggle_pause", "Pause"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("+", "faster", "Faster"),
        Binding("-", "slower", "Slower"),
        Binding("1", "tab('threads')", "Tabs", key_display="1-5"),
        Binding("2", "tab('tasks')", "Tasks", show=False),
        Binding("3", "tab('gc')", "GC", show=False),
        Binding("4", "tab('process')", "Process", show=False),
        Binding("5", "tab('hotspots')", "Hotspots", show=False),
        Binding("s", "toggle_sort", "Sort self/total"),
        Binding("c", "clear_hotspots", "Clear samples"),
        Binding("m", "toggle_mode", "wall/gil"),
    ]

    def __init__(
        self,
        monitor: Monitor,
        *,
        interval: float = 1.0,
        stacks: bool = True,
        tasks: bool = True,
        gc: bool = True,
        sample_rate: float = 100.0,
        sample_mode: str = "wall",
    ):
        super().__init__()
        self.monitor = monitor
        self.hotspots = Hotspots(sample_mode)
        self.sampler = (
            Sampler(monitor, self.hotspots, rate=sample_rate)
            if sample_rate > 0 and monitor.limited is None
            else None
        )
        self.hot_sort = "self"
        self.hot_thread: int | None = None
        self._hot_options: tuple[int, ...] = ()
        self.interval = interval
        self.sections = dict(stacks=stacks, tasks=tasks, gc=gc)
        self.paused = False
        #: Set once the target has gone away. The last snapshot is kept.
        self.exited: ProcessExited | None = None
        self.snapshot: Snapshot | None = None
        self.rss_history: deque[int] = deque(maxlen=HISTORY)
        self.cpu_history: deque[float] = deque(maxlen=HISTORY)
        self._timer = None
        self._selected_tid: int | None = None
        self._selected_task: int | None = None
        self._last_thread: Thread | None = None
        self._last_task: Task | None = None

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
                yield DataTable(id="gc-table", cursor_type="row")
                yield DataTable(id="gc-history", cursor_type="none")
            with TabPane("Process", id="process"):
                yield Label("rss", classes="hist-label")
                yield Sparkline([], id="memhist")
                yield Label("cpu %", classes="hist-label")
                yield Sparkline([], id="cpuhist")
                yield Static("", id="procinfo")
            with TabPane("Hotspots", id="hotspots"):
                with Horizontal(id="hot-bar"):
                    yield Select(
                        [("all threads", -1)], value=-1, allow_blank=False, id="hot-filter"
                    )
                    yield Static("", id="hot-info")
                yield DataTable(id="hot-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#threads-table", DataTable)
        table.add_columns("tid", "name", "status", "st", "cpu%", "utime", "where")
        gc = self.query_one("#gc-table", DataTable)
        gc.add_columns("gen", "collections", "collected", "uncollectable", "total time", "heap")
        hist = self.query_one("#gc-history", DataTable)
        hist.add_columns("gen", "duration", "collected", "candidates", "heap")
        hot = self.query_one("#hot-table", DataTable)
        hot.add_columns("self%", "total%", "self", "total", "function", "file")
        if self.sampler is not None:
            self.sampler.start()
        self.refresh_snapshot()
        self._timer = self.set_interval(self.interval, self.refresh_snapshot)

    def on_unmount(self) -> None:
        if self.sampler is not None:
            self.sampler.stop()

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

    HOTSPOT_ACTIONS = frozenset({"toggle_sort", "clear_hotspots", "toggle_mode"})

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Hotspot keys only make sense (and only show in the footer) on that tab.
        if action in self.HOTSPOT_ACTIONS:
            return self.query_one("#tabs", TabbedContent).active == "hotspots"
        return True

    @on(TabbedContent.TabActivated)
    def _tab_changed(self) -> None:
        self.refresh_bindings()

    def action_toggle_sort(self) -> None:
        self.hot_sort = "total" if self.hot_sort == "self" else "self"
        if self.snapshot:
            self._update_hotspots(self.snapshot)

    def action_toggle_mode(self) -> None:
        # Samples are not comparable across modes, so start over.
        self.hotspots.mode = "gil" if self.hotspots.mode == "wall" else "wall"
        self.hotspots.reset()
        if self.snapshot:
            self._update_hotspots(self.snapshot)

    def action_clear_hotspots(self) -> None:
        self.hotspots.reset()
        if self.snapshot:
            self._update_hotspots(self.snapshot)

    @on(Select.Changed, "#hot-filter")
    def _hot_filter_changed(self, event: Select.Changed) -> None:
        value = event.value
        self.hot_thread = value if isinstance(value, int) and value != -1 else None
        if self.snapshot:
            self._update_hotspots(self.snapshot)

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
        self._update_summary()
        self._update_threads(snap)
        self._update_tasks(snap)
        self._update_gc(snap)
        self._update_process(snap)
        self._update_hotspots(snap)

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
            panel.show(f"[{t.tid}] {t.name}  {t.status.describe()}", t.frames)
            return
        last = self._last_thread
        if last is None:
            panel.show("no thread selected", ())
        else:
            reason = "process exited" if self.exited else "thread gone"
            panel.show(f"[{last.tid}] {last.name}  ({reason}, last seen stack)",
                       last.frames, stale=True)

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
            panel.show(f"{last.name} (0x{last.id:x})  ({reason}, last seen stack)",
                       last.frames, stale=True)

    @on(Tree.NodeHighlighted, "#tasks-tree")
    def _task_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if isinstance(event.node.data, int) and self.snapshot is not None:
            self._selected_task = event.node.data
            self._show_task(self.snapshot.task(self._selected_task))

    def _update_gc(self, snap: Snapshot) -> None:
        table = self.query_one("#gc-table", DataTable)
        table.clear()
        hist = self.query_one("#gc-history", DataTable)
        hist.clear()
        for g in snap.gc:
            table.add_row(
                str(g.generation),
                str(g.collections),
                str(g.collected),
                str(g.uncollectable),
                human_duration(g.total_duration),
                str(g.heap_size),
            )
        rows = sorted(
            (c for g in snap.gc for c in g.history), key=lambda c: c.stopped_at, reverse=True
        )
        for c in rows[:20]:
            hist.add_row(
                str(c.generation),
                human_duration(c.duration),
                "?" if c.collected < 0 else str(c.collected),
                "?" if c.candidates < 0 else str(c.candidates),
                str(c.heap_size),
            )

    def _update_process(self, snap: Snapshot) -> None:
        self.query_one("#memhist", Sparkline).data = list(self.rss_history)
        self.query_one("#cpuhist", Sparkline).data = list(self.cpu_history)
        p = snap.process
        m = p.memory
        info = "\n".join(
            [
                f"exe      {p.exe}",
                f"cmdline  {' '.join(p.cmdline)}",
                f"state    {p.state}    uptime {human_duration(p.uptime)}",
                f"cpu      {percent(p.cpu_percent).strip()}%"
                f"   user {p.user_time:.2f}s   sys {p.system_time:.2f}s",
                f"rss      {human_bytes(m.rss)}   peak {human_bytes(m.hwm)}",
                f"vms      {human_bytes(m.vms)}   data {human_bytes(m.data)}",
                f"shared   {human_bytes(m.shared)}   swap {human_bytes(m.swap)}",
                f"threads  {p.num_threads}",
            ]
        )
        self.query_one("#procinfo", Static).update(info)


    def _update_hotspots(self, snap: Snapshot) -> None:
        select = self.query_one("#hot-filter", Select)
        tids = tuple(t.tid for t in snap.threads)
        if tids != self._hot_options:
            self._hot_options = tids
            options = [("all threads", -1)] + [
                (f"{t.name} [{t.tid}]", t.tid) for t in snap.threads
            ]
            keep = self.hot_thread if self.hot_thread in tids else -1
            select.set_options(options)
            select.value = keep
            if keep == -1:
                self.hot_thread = None
        hot = self.hotspots
        rows = hot.rows(thread=self.hot_thread, sort=self.hot_sort, limit=200)
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
        info.append(f"  mode: {hot.mode}", "cyan")
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


def run_tui(monitor: Monitor, **options) -> int:
    app = SgrudApp(monitor, **options)
    try:
        app.run()
    finally:
        monitor.close()
    return 0
