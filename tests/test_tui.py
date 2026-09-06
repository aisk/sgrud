import asyncio

import pytest
from textual.widgets import DataTable, Tree

from sgrud.tui import SgrudApp

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
        names = {t.name for t in app.snapshot.threads}
        assert {"busy", "idle"} <= names

        stack = app.query_one("#thread-stack")
        assert stack.last_title.startswith("[")
        assert stack.last_frames

        await pilot.press("2")
        await pilot.pause()
        tree = app.query_one("#tasks-tree", Tree)
        labels = [str(n.label) for n in tree.root.children]
        assert any("branch-0" in label for label in labels)

        await pilot.press("space")
        assert app.paused
        await pilot.press("+")
        assert app.interval == pytest.approx(0.1)
        await pilot.press("q")


def _fake_snapshot(threads, pid=42):
    from sgrud.models import Frame, Memory, Process, Snapshot, Thread, ThreadStatus

    proc = Process(pid, "python", ("python",), "S", len(threads), Memory(0, 0, 0, 0, 0, 0), 0, 0, 1, 1.0)
    ts = tuple(
        Thread(tid, name, 0, ThreadStatus.NONE, "S", 0, 0, 0.0, (Frame(f"fn_{name}", "a.py", 1),))
        for tid, name in threads
    )
    return Snapshot(timestamp=0, process=proc, threads=ts)


class _NoMonitor:
    """Stands in for a Monitor when the test drives apply_snapshot itself."""

    pid = 42

    def snapshot(self, **kw):
        from sgrud import ProcessExited

        raise ProcessExited(42)

    def close(self):
        pass


async def test_tui_marks_vanished_thread_stale():
    app = SgrudApp(_NoMonitor(), interval=60)
    async with app.run_test(size=(100, 30)) as pilot:
        # The first refresh hits ProcessExited; reset so we can drive it by hand.
        app.exited = None
        app.apply_snapshot(_fake_snapshot([(42, "main"), (43, "worker")]))
        await pilot.pause()
        app._selected_tid = 43
        app.apply_snapshot(_fake_snapshot([(42, "main"), (43, "worker")]))
        stack = app.query_one("#thread-stack")
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
    from sgrud import Monitor

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
            assert "exited" in app.sub_title
            assert app.query_one("#thread-stack").stale
            await pilot.press("space")
            assert not app.paused
            await pilot.press("q")
    finally:
        proc.kill()
        proc.wait()


async def test_tui_hotspots_tab(monitor):
    app = SgrudApp(monitor, interval=0.2, sample_rate=200)
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
        before = app.hotspots.samples
        await pilot.press("c")
        await pilot.pause()
        assert app.hotspots.samples < before / 2
        await pilot.press("q")
    assert not app.sampler.running
