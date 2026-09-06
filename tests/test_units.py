"""Tests that need no target process."""

import math
import sys
from types import SimpleNamespace

import pytest

from sgrud.format import format_task_tree, human_bytes, human_duration
from sgrud.models import Awaiter, Frame, Memory, Process, Snapshot, Task, ThreadStatus
from sgrud.remote import _convert_gc


def test_thread_status_describe():
    assert ThreadStatus.NONE.describe() == "idle"
    s = ThreadStatus.MAIN_THREAD | ThreadStatus.HAS_GIL | ThreadStatus.ON_CPU
    assert s.describe() == "main,gil,cpu"
    assert (ThreadStatus.GIL_REQUESTED).describe() == "wait-gil"


def test_frame_synthetic():
    assert Frame("<native>", "~").synthetic
    assert not Frame("f", "a.py", lineno=3).synthetic
    assert Frame("f", "a.py", lineno=3).format() == "f (a.py:3)"


def test_human_units():
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(3 * 1024**2) == "3.0 MiB"
    assert human_duration(0.00005) == "50µs"
    assert human_duration(0.25) == "250.0ms"
    assert human_duration(90) == "1m30s"
    assert human_duration(float("nan")) == "?"


def _snapshot(tasks):
    proc = Process(1, "python", ("python",), "S", 1, Memory(0, 0, 0, 0, 0, 0), 0, 0, 0, None)
    return Snapshot(timestamp=0, process=proc, tasks=tuple(tasks))


def test_task_tree_roots_and_children():
    root = Task(1, "root", 100, (Frame("main", "a.py", 1),))
    a = Task(2, "a", 100, (Frame("work", "a.py", 5),), (Awaiter(1),))
    b = Task(3, "b", 100, (Frame("work", "a.py", 5),), (Awaiter(1),))
    orphan = Task(4, "orphan", 100, (), (Awaiter(999),))  # parent not in snapshot
    snap = _snapshot([root, a, b, orphan])
    children = snap.task_children()
    assert [t.name for t in children[None]] == ["root", "orphan"]
    assert [t.name for t in children[1]] == ["a", "b"]
    text = "\n".join(format_task_tree(snap))
    assert "├─ root" in text
    assert "│  ├─ a" in text and "│  └─ b" in text
    assert "└─ orphan" in text


def test_task_tree_handles_cycles():
    a = Task(1, "a", 100, (), (Awaiter(2),))
    b = Task(2, "b", 100, (), (Awaiter(1),))
    lines = format_task_tree(_snapshot([a, b]))
    assert lines == ["(all tasks are in await cycles)"]


def _gc_item(gen, collections, collected=0, candidates=0, duration=0.0, heap=0, ts=0):
    return SimpleNamespace(
        gen=gen,
        iid=0,
        ts_start=ts,
        ts_stop=ts + 1,
        collections=collections,
        collected=collected,
        uncollectable=0,
        candidates=candidates,
        heap_size=heap,
        duration=duration,
    )


def test_convert_gc_ring_buffer_with_cumulative_counters():
    raw = [
        # gen0 ring, unordered as the ring index moves, one empty slot
        _gc_item(0, 3, collected=30, candidates=300, duration=0.3, heap=13, ts=30),
        _gc_item(0, 1, collected=10, candidates=100, duration=0.1, heap=11, ts=10),
        _gc_item(0, 2, collected=15, candidates=200, duration=0.2, heap=12, ts=20),
        _gc_item(0, 0),
        # gen1 never collected
        _gc_item(1, 0),
        _gc_item(1, 0),
        # gen2 ring wrapped: only collections 7 and 8 remain
        _gc_item(2, 8, collected=80, duration=0.8, ts=80),
        _gc_item(2, 7, collected=70, duration=0.7, ts=70),
    ]
    gens = _convert_gc(raw)
    assert [g.generation for g in gens] == [0, 1, 2]

    g0 = gens[0]
    assert (g0.collections, g0.collected, g0.heap_size) == (3, 30, 13)
    assert math.isclose(g0.total_duration, 0.3)
    assert [c.started_at for c in g0.history] == [30, 20, 10]
    assert [c.collected for c in g0.history] == [15, 5, 10]
    assert [c.candidates for c in g0.history] == [100, 100, 100]
    assert all(math.isclose(c.duration, 0.1) for c in g0.history)

    assert gens[1].collections == 0 and gens[1].history == ()

    g2 = gens[2]
    assert g2.collections == 8
    assert g2.history[0].collected == 10
    # Oldest surviving entry has no predecessor, so its deltas are unknown.
    assert g2.history[1].collected == -1
    assert math.isnan(g2.history[1].duration)


def test_looks_like_python_heuristic():
    import os

    from conftest import spawn_sleeper

    from sgrud import osproc

    assert osproc.looks_like_python(os.getpid())
    proc = spawn_sleeper(2)
    try:
        assert not osproc.looks_like_python(proc.pid)
    finally:
        proc.kill()
        proc.wait()


def test_web_command_carries_terminal_options():
    from sgrud.cli import build_parser
    from sgrud.web import web_command

    args = build_parser().parse_args(
        ["top", "run", "--web", "--no-gc", "--no-native", "-n", "0.5", "--mode", "gil"]
    )
    argv = web_command(args, 4321)
    assert argv[:3] == [sys.executable, "-m", "sgrud"]
    assert argv[3] == "4321"
    assert argv[4:] == ["-n", "0.5", "--rate", "100.0", "--mode", "gil", "--no-gc", "--no-native"]


def test_web_flags_default_to_loopback():
    from sgrud.cli import build_parser

    args = build_parser().parse_args(["top", "123"])
    assert not args.web
    assert (args.host, args.port) == ("127.0.0.1", 8000)


@pytest.mark.asyncio
async def test_web_page_urls_follow_the_host_header():
    from aiohttp.test_utils import TestClient, TestServer

    from sgrud.web import _Server

    server = _Server("true", host="0.0.0.0", port=8000)
    async with TestClient(TestServer(await server._make_app())) as client:
        resp = await client.get("/", headers={"Host": "box.example:9000"})
        html = await resp.text()
    assert 'src="http://box.example:9000/static/js/textual.js"' in html
    assert 'websocket-url="ws://box.example:9000/ws"' in html
