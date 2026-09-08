"""Tests that need no target process."""

import dataclasses
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
        state="sleeping",
        num_threads=1,
        memory=Memory(rss=1 << 20, vms=0, hwm=0, swap=0, data=0, shared=0),
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
    from sgrud.models import Cgroup, MemoryLimits

    p = _process()
    rows = dict(memory_rows(p))
    assert rows["rss"] == [("", "1.0 MiB")]
    assert "brk" not in rows and "uss" not in rows
    assert rows["faults"][0] == ("", "-")
    assert "limits" not in rows and "cgroup" not in rows

    rich = _process(
        memory=Memory(
            rss=1 << 20, vms=2 << 20, hwm=3 << 20, swap=0, data=0, shared=0, uss=512 << 10, brk=4096
        ),
        fault_rate=12.0,
        limits=MemoryLimits(address_space=4 << 30, oom_score=5),
        cgroup=Cgroup(path="/box", memory_limit=1 << 30, memory_usage=1 << 29),
    )
    lines = format_memory(rich)
    assert lines[0] == "rss=1.0 MiB  peak 3.0 MiB"
    assert "uss=512.0 KiB" in lines
    assert "brk=4.0 KiB" in lines
    assert "faults=12.0/s  major -  total 0" in lines
    assert format_memory(_process(fault_rate=0.0, major_fault_rate=250.0))[-1] == (
        "faults=0/s  major 250/s  total 0"
    )
    assert "limits=address space 4.0 GiB  oom score 5" in lines
    assert "cgroup=/box  memory 512.0 MiB of 1.0 GiB  used 50%" in lines


def test_cgroup_parsing(tmp_path):
    from sgrud.osproc import (
        _cgroup_counters,
        _CgroupFiles,
        _cpu_list_size,
        _cpu_quota,
        _linux_cgroup,
    )

    assert _cpu_quota("max 100000\n") == 0.0
    assert _cpu_quota("150000 100000\n") == 1.5
    assert _cpu_quota("") == 0.0
    assert _cpu_list_size("0-3,8,10-11") == 7
    assert _cpu_list_size("5") == 1
    assert _cpu_list_size("") == 0
    assert _cgroup_counters("nr_periods 10\nnr_throttled 3\nbad x\n") == {
        "nr_periods": 10,
        "nr_throttled": 3,
    }

    (tmp_path / "memory.max").write_text("1073741824\n")
    (tmp_path / "memory.high").write_text("max\n")
    (tmp_path / "memory.current").write_text("536870912\n")
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    (tmp_path / "cpu.stat").write_text(
        "usage_usec 5\nnr_periods 40\nnr_throttled 4\nthrottled_usec 2500000\n"
    )
    (tmp_path / "memory.events").write_text("low 0\nhigh 2\nmax 9\noom 1\noom_kill 1\n")
    (tmp_path / "pids.max").write_text("max\n")
    (tmp_path / "pids.current").write_text("17\n")
    files = _CgroupFiles(
        "/box",
        str(tmp_path / "memory.max"),
        str(tmp_path / "memory.high"),
        str(tmp_path / "memory.current"),
        str(tmp_path),
    )
    cg = _linux_cgroup(files)
    assert cg.path == "/box"
    assert (cg.memory_limit, cg.memory_high, cg.memory_usage) == (1 << 30, 0, 1 << 29)
    assert cg.memory_percent == 50.0
    assert cg.cpu_quota == 2.0
    assert (cg.periods, cg.throttled, cg.throttled_time) == (40, 4, 2.5)
    assert (cg.oom_kills, cg.limit_hits, cg.high_hits) == (1, 9, 2)
    assert (cg.pids_max, cg.pids_current) == (0, 17)
    assert cg.throttled_percent is None

    # A v1 cgroup has the memory figures only, a missing directory nothing at all.
    (tmp_path / "memory.limit_in_bytes").write_text("9223372036854771712\n")
    (tmp_path / "memory.usage_in_bytes").write_text("4096\n")
    v1 = _linux_cgroup(
        _CgroupFiles(
            "/v1",
            str(tmp_path / "memory.limit_in_bytes"),
            "",
            str(tmp_path / "memory.usage_in_bytes"),
        )
    )
    assert (v1.path, v1.memory_limit, v1.memory_usage, v1.memory_percent) == ("/v1", 0, 4096, None)
    assert v1.cpu_quota == 0.0 and v1.oom_kills == -1
    assert _linux_cgroup(None).path == ""
    empty = _linux_cgroup(_CgroupFiles("/", "", "", "", str(tmp_path / "gone")))
    assert empty.oom_kills == -1 and empty.cpu_quota == 0.0 and empty.pids_current == 0


def test_throttled_share():
    from sgrud.monitor import _RateSample, _share

    assert _share(None, (10.0, 1.0)) is None
    prev = _RateSample(0.0, (10.0, 1.0))
    assert _share(prev, (10.0, 1.0)) is None  # no period elapsed, no quota
    assert _share(prev, (20.0, 3.0)) == 20.0
    assert _share(prev, (20.0, 30.0)) == 100.0


def test_cgroup_rows():
    from sgrud.format import format_memory, memory_rows
    from sgrud.models import Cgroup

    assert "cgroup" not in dict(memory_rows(_process(cgroup=Cgroup(path="/"))))
    rows = dict(memory_rows(_process(cgroup=Cgroup(path="/user.slice"))))
    assert rows["cgroup"] == [("", "/user.slice")]
    busy = _process(
        cgroup=Cgroup(
            path="/docker/abc",
            cpu_quota=1.5,
            periods=100,
            throttled=12,
            throttled_time=0.75,
            throttled_percent=12.0,
            oom_kills=1,
            limit_hits=0,
            high_hits=3,
            pids_max=512,
            pids_current=34,
        ),
        cpus_allowed=2,
    )
    lines = format_memory(busy)
    assert lines[-2] == "limits=cpus 2"
    assert lines[-1] == (
        "cgroup=/docker/abc  cpu 1.5 cores  throttled 12%  throttled time 750.0ms"
        "  oom kills 1  high hits 3  pids 34 of 512"
    )


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


def test_decode_syscall_reports_the_call_and_its_descriptor():
    from sgrud import ipc
    from sgrud.models import OpenFile

    files = {4: OpenFile(fd=4, kind="pipe", target="pipe:[51605]", mode="r", inode=51605)}
    read = ipc.decode_syscall("0 0x4 0x7f 0x1 0x0 0x0 0x0 0x7ffd 0x7fa1", files)
    futex = ipc.decode_syscall("202 0x1 0x189 0x0 0x0 0x0 0xffffffff 0x7ffd 0x7fa1", files)
    if ipc.SYSCALL_NAMES.get(0) == "read":  # x86_64
        assert read is not None and read.name == "read"
        assert read.fd == 4 and read.target == "pipe:[51605]"
        assert read.describe() == "read(fd 4 pipe:[51605])"
        assert futex is not None and futex.name == "futex" and futex.fd == -1
        assert futex.describe() == "futex"
    elif ipc.SYSCALL_NAMES:
        assert read is not None and read.number == 0
    assert ipc.decode_syscall("running", files) is None
    assert ipc.decode_syscall("-1 0x7ffd 0x7fa1", files) is None
    assert ipc.decode_syscall("", files) is None
    unknown = ipc.decode_syscall("9999 0x4 0x0 0x0 0x0 0x0 0x0 0x0 0x0", files)
    assert unknown is not None and unknown.name == "syscall 9999" and unknown.fd == -1


def test_syscall_tables_agree_on_the_calls_that_take_a_descriptor():
    from sgrud import ipc

    assert ipc._FD_CALLS <= set(ipc._SYSCALLS)
    x86 = [n for n, _ in ipc._SYSCALLS.values() if n is not None]
    generic = [n for _, n in ipc._SYSCALLS.values() if n is not None]
    assert len(x86) == len(set(x86)) and len(generic) == len(set(generic))


def test_parse_locks_finds_held_and_waited_locks():
    from sgrud.ipc import parse_locks

    text = """\
1: FLOCK  ADVISORY  WRITE 7288 08:20:775119 0 EOF
2: POSIX  ADVISORY  WRITE 1234 08:20:775200 0 EOF
2: -> POSIX  ADVISORY  WRITE 7288 08:20:775200 0 EOF
3: POSIX  ADVISORY  READ 7288 08:20:775300 100 199
4: OFDLCK ADVISORY  WRITE -1 08:20:775400 0 EOF
5: OFDLCK ADVISORY  WRITE -1 08:20:775500 0 EOF
6: FLOCK  ADVISORY  WRITE 999 08:20:775119 0 EOF
"""
    paths = {"08:20:775119": "/tmp/a.lock", "08:20:775400": "/tmp/d.lock"}
    locks = parse_locks(text, 7288, lambda inode: paths.get(inode, ""))
    assert [(lk.kind, lk.mode, lk.waiting, lk.holder) for lk in locks] == [
        ("flock", "write", False, 7288),
        ("posix", "write", True, 1234),
        ("posix", "read", False, 7288),
        ("ofd", "write", False, -1),
    ]
    assert locks[0].path == "/tmp/a.lock" and locks[1].path == ""
    assert (locks[2].start, locks[2].end) == (100, 199) and locks[0].end == -1
    assert locks[3].path == "/tmp/d.lock"  # the OFD lock on a file nobody has open is left out


def test_ipc_counts_and_same_object():
    from sgrud.format import describe_file, ipc_summary
    from sgrud.models import IPC, FileLock, OpenFile, SharedMapping

    files = (
        OpenFile(fd=3, kind="pipe", target="pipe:[10]", mode="r", inode=10),
        OpenFile(fd=4, kind="pipe", target="pipe:[10]", mode="w", inode=10),
        OpenFile(
            fd=5,
            kind="socket",
            target="socket:[11]",
            mode="rw",
            inode=11,
            family="tcp",
            local="127.0.0.1:80",
            remote="10.0.0.1:5",
            status="ESTABLISHED",
        ),
        OpenFile(fd=6, kind="file", target="/tmp/x", mode="w"),
    )
    ipc = IPC(
        num_fds=40,
        max_fds=1024,
        files=files,
        locks=(FileLock("flock", "write", "/tmp/x", "08:20:1", waiting=True, holder=42),),
        mappings=(
            SharedMapping("/dev/shm/psm_1", 4096, "shm"),
            SharedMapping("/dev/shm/sem.a", 4096, "sem", deleted=True),
        ),
    )
    assert ipc.counts() == {"pipe": 2, "socket": 1, "file": 1}
    assert ipc.truncated  # 40 descriptors, 4 listed
    assert not dataclasses.replace(ipc, partial=True).truncated
    assert [o.fd for o in ipc.same_object(3)] == [4]
    assert ipc.same_object(6) == ()
    assert ipc.semaphores == 1
    assert describe_file(files[2]) == "tcp 127.0.0.1:80 -> 10.0.0.1:5 established"
    lines = ipc_summary(ipc)
    assert lines[0].startswith("fds      40 of 1024 (4%)   pipe 2  socket 1  file 1")
    assert "first 4 listed" in lines[0]
    assert lines[1] == "shared   /dev/shm/psm_1 4.0 KiB   semaphores 1"
    assert lines[2] == "lock     flock write /tmp/x  WAITING for pid 42"
