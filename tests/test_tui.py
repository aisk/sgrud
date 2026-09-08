import asyncio
from typing import cast

import pytest
from textual.widgets import DataTable, Select, Static, TabbedContent, Tabs, Tree

from sgrud import Monitor
from sgrud.osproc import HAS_THREAD_STATS
from sgrud.tui import SgrudApp, StackPanel, Summary

pytestmark = pytest.mark.asyncio


async def test_tui_renders_snapshot(monitor):
    app = SgrudApp(monitor, interval=0.2)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.5)
        await pilot.pause()
        assert app.snapshot is not None
        table = app.query_one("#threads-table", DataTable)
        assert table.row_count == len(app.snapshot.threads)
        if HAS_THREAD_STATS:
            names = {t.name for t in app.snapshot.threads}
            assert {"busy", "idle"} <= names

        stack = app.query_one("#thread-stack", StackPanel)
        assert stack.last_title.startswith("[")
        assert stack.last_frames
        # Focus starts in the content, never on the tab bar.
        assert table.has_focus
        assert not app.query_one(Tabs).can_focus

        await pilot.press("2")
        await pilot.pause()
        tree = app.query_one("#tasks-tree", Tree)
        labels = [str(n.label) for n in tree.root.children]
        assert any("branch-0" in label for label in labels)
        assert tree.has_focus

        tabs = app.query_one("#tabs", TabbedContent)
        await pilot.press("tab")
        assert tabs.active == "gc"
        assert app.query_one("#gc-table", DataTable).has_focus
        await pilot.press("x")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if app.probe_result is not None:
                break
        await pilot.pause()
        assert app.probe_result is not None and app.probe_result.types
        assert "threshold" in str(app.query_one("#probe-info", Static).render())
        await pilot.press("shift+tab")
        assert tabs.active == "tasks"
        # space belongs to the tree, p pauses.
        await pilot.press("space")
        assert not app.paused
        await pilot.press("p")
        assert app.paused
        await pilot.press("+")
        assert app.interval == pytest.approx(0.1)
        await pilot.press("=")
        assert app.interval == pytest.approx(0.1)
        await pilot.press("-")
        assert app.interval == pytest.approx(0.2)
        await pilot.press("q")


def _fake_snapshot(threads, pid=42):
    from sgrud.models import Frame, Memory, Process, Snapshot, Thread, ThreadStatus

    proc = Process(
        pid=pid,
        exe="python",
        cmdline=("python",),
        state="sleeping",
        num_threads=len(threads),
        memory=Memory(rss=0, vms=0, peak_rss=0, swap=0, data=0, shared=0),
        user_time=0,
        system_time=0,
        uptime=1,
        cpu_percent=1.0,
    )
    ts = tuple(
        Thread(
            tid=tid,
            name=name,
            interpreter_id=0,
            status=ThreadStatus.NONE,
            state="sleeping",
            user_time=0,
            system_time=0,
            cpu_percent=0.0,
            frames=(Frame(f"fn_{name}", "a.py", 1),),
        )
        for tid, name in threads
    )
    return Snapshot(timestamp=0, process=proc, threads=ts)


class _NoMonitor:
    """Stands in for a Monitor when the test drives apply_snapshot itself."""

    pid = 42
    limited = None

    def snapshot(self, **kw):
        from sgrud import ProcessExited

        raise ProcessExited(42)

    def close(self):
        pass


async def test_tui_ipc_tab(monitor):
    app = SgrudApp(monitor, interval=0.2)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await asyncio.sleep(0.5)
        await pilot.pause()
        assert app.snapshot is not None and app.snapshot.ipc is not None
        await pilot.press("7")
        await pilot.pause()
        tabs = app.query_one("#tabs", TabbedContent)
        assert tabs.active == "ipc"
        table = app.query_one("#ipc-table", DataTable)
        assert table.has_focus
        assert table.row_count == len(app.snapshot.ipc.files)
        info = str(app.query_one("#ipc-info", Static).render())
        assert info.startswith("fds")
        await pilot.press("1")
        await pilot.pause()
        threads = app.query_one("#threads-table", DataTable)
        assert "syscall" in [str(c.label) for c in threads.columns.values()]


async def test_tui_marks_vanished_thread_stale():
    app = SgrudApp(cast(Monitor, _NoMonitor()), interval=60)
    async with app.run_test(size=(100, 30)) as pilot:
        # The first refresh hits ProcessExited; reset so we can drive it by hand.
        app.exited = None
        app.apply_snapshot(_fake_snapshot([(42, "main"), (43, "worker")]))
        await pilot.pause()
        app._selected_tid = 43
        app.apply_snapshot(_fake_snapshot([(42, "main"), (43, "worker")]))
        stack = app.query_one("#thread-stack", StackPanel)
        assert stack.last_title.startswith("[43] worker") and not stack.stale

        app.apply_snapshot(_fake_snapshot([(42, "main")]))
        await pilot.pause()
        assert stack.stale
        assert "thread gone" in stack.last_title
        assert stack.last_frames[0].funcname == "fn_worker"
        table = app.query_one("#threads-table", DataTable)
        assert table.row_count == 1


async def test_tui_reports_process_exit():
    from conftest import spawn_target

    proc = spawn_target("--exit-after", "0.5")
    try:
        monitor = Monitor.attach(proc.pid)
        app = SgrudApp(monitor, interval=0.1)
        async with app.run_test(size=(100, 30)) as pilot:
            deadline = asyncio.get_event_loop().time() + 5
            while app.exited is None and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.1)
            await pilot.pause()
            assert app.exited is not None
            assert app.snapshot is not None, "last snapshot must be kept"
            assert "EXITED" in str(app.query_one(Summary).content)
            assert app.query_one("#thread-stack", StackPanel).stale
            await pilot.press("space")
            assert not app.paused
            await pilot.press("q")
    finally:
        proc.kill()
        proc.wait()


async def test_tui_hotspots_tab(monitor, tmp_path):
    record = tmp_path / "session.bin"
    app = SgrudApp(monitor, interval=0.2, sample_rate=200, record=str(record))
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.sleep(0.8)
        await pilot.pause()
        await pilot.press("5")
        await pilot.pause()
        assert app.sampler is not None and app.sampler.running
        assert app.hotspots.samples > 20
        table = app.query_one("#hot-table", DataTable)
        assert table.row_count > 0
        funcs = [str(table.get_row_at(i)[4]) for i in range(min(table.row_count, 5))]
        assert "busy_loop" in funcs
        await pilot.press("s")
        assert app.hot_sort == "total"

        # f opens the thread filter, a choice hands focus back to the table.
        assert table.has_focus
        await pilot.press("f")
        await pilot.pause()
        select = app.query_one("#hot-filter", Select)
        assert select.expanded
        await pilot.press("down", "enter")
        await pilot.pause()
        assert not select.expanded
        assert app.hot_thread == app.snapshot.threads[0].tid
        assert table.has_focus
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert table.has_focus

        before = app.hotspots.samples
        await pilot.press("c")
        await pilot.pause()
        assert app.hotspots.samples < before / 2
        await pilot.press("m")
        assert app.sample_mode == "gil" and app.sampler.mode == "gil"
        await pilot.press("q")
    assert not app.sampler.running
    # The recording survives the mode switch and is closed on exit.
    assert record.stat().st_size > 0


async def test_tui_flame_tab(monitor):
    from sgrud.tui import FlameGraph

    app = SgrudApp(monitor, interval=0.2, sample_rate=200)
    async with app.run_test(size=(120, 40)) as pilot:
        await asyncio.sleep(0.8)
        await pilot.pause()
        await pilot.press("6")
        await pilot.pause()
        graph = app.query_one(FlameGraph)
        assert graph.has_focus
        assert graph.root is not None and graph.root.total > 20
        cells = graph.cells()
        names = {c.node.name for c in cells}
        assert "busy_loop" in names
        threads = [c for c in cells if c.node.tid is not None]
        if HAS_THREAD_STATS:
            assert any(c.node.name.startswith("busy [") for c in threads)
        assert all(c.width >= 1 for c in cells)

        # Walk up from the root into the widest thread, then zoom in on it.
        await pilot.press("up")
        assert len(graph.cursor) == 1
        assert graph.cursor_node is not None and graph.cursor_node.tid is not None
        await pilot.press("enter")
        assert graph.zoom == graph.cursor
        assert graph.cells()[0].node.tid is not None
        await pilot.press("backspace")
        assert graph.zoom == ()
        status = str(app.query_one("#flame-status", Static).content)
        assert "total" in status

        # The thread filter is shared with the Hotspots tab.
        assert app.snapshot is not None
        busy = next(
            t.tid for t in app.snapshot.threads if t.frames and t.frames[0].funcname == "busy_loop"
        )
        app.query_one("#flame-filter", Select).value = busy
        await pilot.pause()
        assert app.hot_thread == busy
        assert app.query_one("#hot-filter", Select).value == busy
        assert graph.root.tid == busy
        assert all(c.node.tid is None or c.depth == 0 for c in graph.cells())

        before = app.hotspots.samples
        await pilot.press("c")
        await pilot.pause()
        assert app.hotspots.samples < before / 2
        await pilot.press("q")
