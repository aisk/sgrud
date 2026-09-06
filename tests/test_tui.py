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
