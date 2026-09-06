"""Textual front end over :class:`sgrud.Monitor`.

The app owns a Monitor, takes a snapshot every ``interval`` seconds on the
event loop (a snapshot costs well under a millisecond) and pushes the result
into the widgets. All rendering works from :class:`~sgrud.models.Snapshot`
only, so the UI can be driven by recorded snapshots in tests.
"""

from __future__ import annotations

import os
from collections import deque

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from .errors import ProcessExited, SgrudError
from .format import format_frames, human_bytes, human_duration, percent, short_path
from .models import Snapshot, Task, Thread, ThreadStatus
from .monitor import Monitor

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

    def update_from(self, snap: Snapshot, interval: float, paused: bool) -> None:
        p = snap.process
        m = p.memory
        text = Text()
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
        if paused:
            text.append("  PAUSED", "bold yellow")
        for section, err in snap.errors.items():
            text.append(f"  !{section}: {err.splitlines()[0]}", "red")
        self.update(text)


class StackPanel(Static):
    """Shows the frames of whatever is selected in the current tab."""

    last_title: str = ""
    last_frames: tuple = ()

    def show(self, title: str, frames) -> None:
        self.last_title = title
        self.last_frames = tuple(frames)
        lines = format_frames(frames, indent="  ")
        body = "\n".join(lines) if lines else "  (no Python frames)"
        self.update(Text.assemble((title + "\n", "bold"), body))


class SgrudApp(App[int]):
    TITLE = "sgrud"
    CSS = """
    Summary { height: 1; background: $primary-background; }
    #tabs { height: 1fr; }
    #left { width: 3fr; }
    .stack { width: 2fr; border-left: solid $secondary; padding: 0 1; }
    DataTable { height: 1fr; }
    Tree { height: 1fr; }
    #memhist, #cpuhist { height: 3; }
    .hist-label { color: $text-muted; }
    #procinfo { padding: 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("space", "toggle_pause", "Pause"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("+", "faster", "Faster"),
        Binding("-", "slower", "Slower"),
        Binding("1", "tab('threads')", "Threads", show=False),
        Binding("2", "tab('tasks')", "Tasks", show=False),
        Binding("3", "tab('gc')", "GC", show=False),
        Binding("4", "tab('process')", "Process", show=False),
    ]

    def __init__(
        self,
        monitor: Monitor,
        *,
        interval: float = 1.0,
        stacks: bool = True,
        tasks: bool = True,
        gc: bool = True,
    ):
        super().__init__()
        self.monitor = monitor
        self.interval = interval
        self.sections = dict(stacks=stacks, tasks=tasks, gc=gc)
        self.paused = False
        self.snapshot: Snapshot | None = None
        self.rss_history: deque[int] = deque(maxlen=HISTORY)
        self.cpu_history: deque[float] = deque(maxlen=HISTORY)
        self._timer = None
        self._selected_tid: int | None = None
        self._selected_task: int | None = None

    # -- layout --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Summary(id="summary")
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
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#threads-table", DataTable)
        table.add_columns("tid", "name", "status", "st", "cpu%", "utime", "where")
        gc = self.query_one("#gc-table", DataTable)
        gc.add_columns("gen", "collections", "collected", "uncollectable", "total time", "heap")
        hist = self.query_one("#gc-history", DataTable)
        hist.add_columns("gen", "duration", "collected", "candidates", "heap")
        self.refresh_snapshot()
        self._timer = self.set_interval(self.interval, self.refresh_snapshot)

    # -- actions -------------------------------------------------------

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.snapshot:
            self.query_one(Summary).update_from(self.snapshot, self.interval, self.paused)

    def action_refresh_now(self) -> None:
        self.refresh_snapshot(force=True)

    def action_faster(self) -> None:
        self._set_interval(max(0.1, self.interval / 2))

    def action_slower(self) -> None:
        self._set_interval(min(30.0, self.interval * 2))

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def _set_interval(self, value: float) -> None:
        self.interval = value
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_interval(self.interval, self.refresh_snapshot)
        if self.snapshot:
            self.query_one(Summary).update_from(self.snapshot, self.interval, self.paused)

    # -- data flow -----------------------------------------------------

    def refresh_snapshot(self, force: bool = False) -> None:
        if self.paused and not force:
            return
        try:
            snap = self.monitor.snapshot(**self.sections)
        except ProcessExited as e:
            self.notify(str(e), severity="warning", timeout=10)
            self.paused = True
            self.sub_title = str(e)
            return
        except SgrudError as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        self.apply_snapshot(snap)

    def apply_snapshot(self, snap: Snapshot) -> None:
        """Render a snapshot. Public so tests and replays can drive the UI."""
        self.snapshot = snap
        self.rss_history.append(snap.process.memory.rss)
        if snap.process.cpu_percent is not None:
            self.cpu_history.append(snap.process.cpu_percent)
        self.sub_title = " ".join(snap.process.cmdline)[:60]
        self.query_one(Summary).update_from(snap, self.interval, self.paused)
        self._update_threads(snap)
        self._update_tasks(snap)
        self._update_gc(snap)
        self._update_process(snap)

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
                    table.update_cell(key, col, value)
            else:
                table.add_row(*cells, key=key)
        for key in [k for k in table.rows if k.value not in seen]:
            table.remove_row(key)
        if self._selected_tid is None and snap.threads:
            self._selected_tid = snap.threads[0].tid
        self._show_thread(snap.thread(self._selected_tid) if self._selected_tid else None)

    def _show_thread(self, t: Thread | None) -> None:
        panel = self.query_one("#thread-stack", StackPanel)
        if t is None:
            panel.show("no thread selected", ())
            return
        panel.show(f"[{t.tid}] {t.name}  {t.status.describe()}", t.frames)

    @on(DataTable.RowHighlighted, "#threads-table")
    def _thread_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or self.snapshot is None:
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
        if task is None:
            panel.show("select a task", ())
            return
        panel.show(f"{task.name} (0x{task.id:x})", task.frames)

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
                f"cpu      {percent(p.cpu_percent).strip()}%   user {p.user_time:.2f}s   sys {p.system_time:.2f}s",
                f"rss      {human_bytes(m.rss)}   peak {human_bytes(m.hwm)}",
                f"vms      {human_bytes(m.vms)}   data {human_bytes(m.data)}",
                f"shared   {human_bytes(m.shared)}   swap {human_bytes(m.swap)}",
                f"threads  {p.num_threads}",
            ]
        )
        self.query_one("#procinfo", Static).update(info)


def run_tui(monitor: Monitor, **options) -> int:
    app = SgrudApp(monitor, **options)
    try:
        app.run()
    finally:
        monitor.close()
    return 0
