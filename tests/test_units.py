"""Tests that need no target process."""

import math
import sys
from typing import Any

import pytest

from sgrud.format import format_task_tree, human_bytes, human_duration
from sgrud.models import Awaiter, Frame, Memory, Process, Snapshot, Task, ThreadStatus
from sgrud.remote import GCRecord, build_gc


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


def _process(**overrides):
    fields: dict[str, Any] = dict(
        pid=1,
        exe="python",
        cmdline=("python",),
        state="S",
        num_threads=1,
        memory=Memory(1 << 20, 0, 0, 0, 0, 0),
        user_time=0,
        system_time=0,
        uptime=0,
        cpu_percent=None,
    )
    return Process(**(fields | overrides))


def _snapshot(tasks):
    return Snapshot(timestamp=0, process=_process(), tasks=tuple(tasks))


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
    return GCRecord(
        generation=gen,
        interpreter_id=0,
        index=collections,
        started_at=ts,
        stopped_at=ts + 1,
        collected=collected,
        uncollectable=0,
        candidates=candidates,
        duration=duration,
        heap_size=heap,
    )


def test_build_gc_from_ring_records_with_cumulative_counters():
    raw = [
        # gen0 ring, unordered as the ring index moves
        _gc_item(0, 3, collected=30, candidates=300, duration=0.3, heap=13, ts=30),
        _gc_item(0, 1, collected=10, candidates=100, duration=0.1, heap=11, ts=10),
        _gc_item(0, 2, collected=15, candidates=200, duration=0.2, heap=12, ts=20),
        # gen1 never collected, so it has no records at all
        # gen2 ring wrapped: only collections 7 and 8 remain
        _gc_item(2, 8, collected=80, duration=0.8, ts=80),
        _gc_item(2, 7, collected=70, duration=0.7, ts=70),
    ]
    gens = build_gc(raw, now_ns=41, rates={0: (2.0, 0.01)})
    assert [g.generation for g in gens] == [0, 1, 2]

    g0 = gens[0]
    assert (g0.collections, g0.collected, g0.heap_size) == (3, 30, 13)
    assert math.isclose(g0.total_duration, 0.3)
    assert math.isclose(g0.mean_duration, 0.1)
    assert (g0.rate, g0.time_share) == (2.0, 0.01)
    assert [c.index for c in g0.history] == [3, 2, 1]
    assert [c.started_at for c in g0.history] == [30, 20, 10]
    assert [c.collected for c in g0.history] == [15, 5, 10]
    assert [c.candidates for c in g0.history] == [100, 100, 100]
    assert [c.survivors for c in g0.history] == [85, 95, 90]
    assert all(math.isclose(c.duration, 0.1) for c in g0.history)
    assert [c.age for c in g0.history] == pytest.approx([10e-9, 20e-9, 30e-9])

    assert gens[1].collections == 0 and gens[1].history == ()
    assert gens[1].rate is None

    g2 = gens[2]
    assert g2.collections == 8
    assert g2.history[0].collected == 10
    # Oldest surviving entry has no predecessor, so its deltas are unknown.
    assert g2.history[1].collected == -1
    assert g2.history[1].survivors == -1
    assert math.isnan(g2.history[1].duration)
    assert math.isnan(build_gc(raw)[0].history[0].age)


def test_gc_tracker_accumulates_history_and_rates():
    from sgrud.monitor import _GCTracker

    tracker = _GCTracker(limit=3)
    first = tracker.update([_gc_item(0, 1, collected=1, duration=0.1, ts=10)], now=1.0, now_ns=20)
    assert first[0].rate is None and first[0].time_share is None
    assert [c.index for c in first[0].history] == [1]

    # The ring moved on: collection 2 was overwritten before we read it.
    second = tracker.update(
        [
            _gc_item(0, 4, collected=4, duration=0.4, ts=40),
            _gc_item(0, 3, collected=3, duration=0.3, ts=30),
        ],
        now=3.0,
        now_ns=50,
    )
    g0 = second[0]
    assert g0.collections == 4
    assert g0.rate == pytest.approx(1.5)  # 3 collections in 2 seconds
    assert g0.time_share == pytest.approx(0.15)  # 0.3 s of 2 s
    # Records from both reads are kept, capped at the limit, newest first.
    assert [c.index for c in g0.history] == [4, 3, 1]
    assert [c.collected for c in g0.history] == [1, -1, 1]

    third = tracker.update([_gc_item(0, 4, collected=4, duration=0.4, ts=40)], now=4.0, now_ns=60)
    assert third[0].rate == 0.0 and third[0].time_share == 0.0
    assert third[0].history[0].age == pytest.approx(19e-9)


def test_hotspots_gc_sites():
    from sgrud.profile import Hotspots

    gc = Frame("<GC>", "~")
    alloc = Frame("alloc", "app.py", 1)
    main = Frame("main", "app.py", 2)
    other = Frame("other", "app.py", 3)
    hot = Hotspots()
    hot.add_stacks([(1, (gc, alloc, main))])
    hot.add_stacks([(1, (Frame("finalize", "app.py", 4), gc, alloc, main))])
    hot.add_stacks([(1, (other, main))])
    hot.add_stacks([(2, (gc,))])
    gc_samples, samples, sites = hot.gc_sites()
    assert (gc_samples, samples) == (3, 4)
    assert [(s.funcname, s.samples) for s in sites] == [("alloc", 2), ("<no Python frame>", 1)]
    assert sites[0].percent == pytest.approx(50.0)
    assert hot.gc_sites(thread=1)[:2] == (2, 3)
    assert hot.gc_sites(thread=3) == (0, 0, [])


def test_linux_maps_and_faults_parsing():
    from sgrud.osproc import _linux_faults, _linux_maps

    maps = """\
00400000-00401000 r--p 00000000 08:01 1234       /usr/bin/python3.15
01000000-01100000 rw-p 00000000 00:00 0          [heap]
7f0000000000-7f0000100000 rw-p 00000000 00:00 0
7f0000100000-7f0000101000 ---p 00000000 00:00 0
7f0000200000-7f0000a00000 rw-p 00000000 00:00 0          [anon:thread stack]
7f0000b00000-7f0000b01000 r--p 00000000 00:00 0          [vvar]
7ffc00000000-7ffc00021000 rw-p 00000000 00:00 0          [stack]
"""
    assert _linux_maps(maps) == (0x100000, 0x100000 + 0x800000)
    stat = "1234 (my prog) S 1 1 1 0 -1 4194560 3139 0 7 0 12 3 0 0 20 0 3 0 100 200 50"
    assert _linux_faults(stat) == (3146, 7)


def test_memory_rows_skip_what_the_platform_lacks():
    from sgrud.format import format_memory, memory_rows
    from sgrud.models import MemoryLimits

    p = _process()
    rows = dict(memory_rows(p))
    assert rows["rss"] == [("", "1.0 MiB")]
    assert "brk" not in rows and "uss" not in rows
    assert rows["faults"][0] == ("", "-")
    assert "limits" not in rows

    rich = _process(
        memory=Memory(
            rss=1 << 20, vms=2 << 20, hwm=3 << 20, swap=0, data=0, shared=0, uss=512 << 10, brk=4096
        ),
        fault_rate=12.0,
        limits=MemoryLimits(cgroup_limit=1 << 30, cgroup_usage=1 << 29, oom_score=5),
    )
    lines = format_memory(rich)
    assert lines[0] == "rss=1.0 MiB  peak 3.0 MiB"
    assert "uss=512.0 KiB" in lines
    assert "brk=4.0 KiB" in lines
    assert "faults=12.0/s  major -  total 0" in lines
    assert format_memory(_process(fault_rate=0.0, major_fault_rate=250.0))[-1] == (
        "faults=0/s  major 250/s  total 0"
    )
    assert "limits=cgroup 512.0 MiB of 1.0 GiB  used 50%  oom score 5" in lines


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
    args = build_parser().parse_args(["top", "1", "--no-children"])
    assert web_command(args, 1)[-1] == "--no-children"


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
