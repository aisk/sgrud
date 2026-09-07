import json
import subprocess
import sys
import time

import pytest
from conftest import HAS_THREAD_STATS, TARGET, spawn_sleeper, spawn_target

from sgrud import AttachError, Monitor, ProcessExited, SgrudError, ThreadStatus
from sgrud.format import format_snapshot


def test_process_section(snapshot, monitor):
    p = snapshot.process
    assert p.pid == monitor.pid
    assert p.memory.rss > 1024 * 1024
    if sys.platform != "win32":  # Windows reports the commit charge as vms
        assert p.memory.vms >= p.memory.rss
    assert p.num_threads >= 3
    assert p.uptime > 0
    assert p.cpu_percent is not None and p.cpu_percent >= 0
    assert "target_app.py" in " ".join(p.cmdline)
    assert not snapshot.errors, snapshot.errors


def test_threads_have_names_status_and_stacks(snapshot, monitor):
    by_frame = {t.frames[0].funcname: t for t in snapshot.threads if t.frames}
    assert {"busy_loop", "idle_loop"} <= set(by_frame)
    main = [t for t in snapshot.threads if t.is_main]
    assert len(main) == 1
    assert snapshot.threads[0].is_main
    if sys.platform.startswith("linux"):
        assert main[0].tid == snapshot.process.pid  # only Linux numbers threads like pids

    busy = by_frame["busy_loop"]
    assert busy.frames[0].filename.endswith("target_app.py")
    assert busy.frames[0].lineno is not None
    idle = by_frame["idle_loop"]
    if HAS_THREAD_STATS:
        assert busy.name == "busy" and idle.name == "idle"
        assert busy.cpu_percent is not None and busy.cpu_percent > 5
        assert idle.cpu_percent is not None and idle.cpu_percent < 5
    else:
        assert busy.cpu_percent is None

    # The sleeping thread wakes every 0.2s and can be caught in state R at the
    # sampling instant, so give the flag a few chances to read as off-CPU.
    # Only Linux reports per-thread scheduler state, elsewhere it is unknown.
    idle_statuses = [idle.status]
    for _ in range(5):
        if any(not (s & ThreadStatus.ON_CPU) for s in idle_statuses):
            break
        time.sleep(0.05)
        again = {t.tid: t for t in monitor.snapshot(tasks=False, gc=False).threads}
        idle_statuses.append(again[idle.tid].status)
    assert any(not (s & ThreadStatus.ON_CPU) for s in idle_statuses), idle_statuses
    if sys.platform.startswith("linux"):
        assert not any(s & ThreadStatus.UNKNOWN for s in idle_statuses), idle_statuses

    # A thread sleeping inside time.sleep sits under a <native> marker frame.
    assert any(f.synthetic and f.funcname == "<native>" for f in idle.frames)


def test_asyncio_tasks_tree(snapshot):
    names = {t.name for t in snapshot.tasks}
    assert {"branch-0", "branch-1", "branch-2"} <= names
    leaves = [t for t in snapshot.tasks if t.frames and t.frames[0].funcname == "sleep"]
    assert len(leaves) >= 6
    children = snapshot.task_children()
    branch0 = next(t for t in snapshot.tasks if t.name == "branch-0")
    main_tid = next(t.tid for t in snapshot.threads if t.is_main)
    assert len(children[branch0.id]) == 2
    for leaf in children[branch0.id]:
        assert leaf.parent_ids == (branch0.id,)
        assert leaf.thread_id == main_tid
    assert any(t.name == "Task-1" for t in children[None])


def test_gc_stats(monitor):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snap = monitor.snapshot(stacks=False, tasks=False)
        gen0 = snap.gc[0]
        if gen0.collections > 0:
            break
        time.sleep(0.2)
    assert len(snap.gc) == 3
    assert gen0.generation == 0
    assert gen0.collections > 0
    assert gen0.history and gen0.history[0].generation == 0
    assert gen0.history[0].duration >= 0
    assert gen0.history[0].index == gen0.collections
    # The target's clock and ours are the same clock.
    assert 0 <= gen0.history[0].age < 60
    assert gen0.history[0].heap_size > 0
    time.sleep(0.1)
    again = monitor.snapshot(stacks=False, tasks=False).gc[0]
    assert again.rate is not None and again.rate >= 0
    assert again.time_share is not None and 0 <= again.time_share < 1
    assert len(again.history) >= len(gen0.history)


def test_memory_details(monitor):
    first = monitor.snapshot(stacks=False, tasks=False, gc=False).process
    time.sleep(0.1)
    p = monitor.snapshot(stacks=False, tasks=False, gc=False).process
    m = p.memory
    assert m.rss > 0 and m.vms > 0
    assert p.page_faults >= first.page_faults > 0
    assert p.fault_rate is not None and p.fault_rate >= 0
    if sys.platform != "win32":
        assert m.uss > 0  # psutil reads it on macOS too
    if sys.platform == "linux":
        assert 0 < m.anon < m.rss and m.file > 0
        assert m.anon + m.file + m.shmem == m.rss
        assert m.pss >= m.uss > 0
        assert m.brk > 0 and m.anon_mapped > 0
        assert m.peak_vms >= m.vms
        assert p.limits.oom_score >= 0
    else:
        assert p.limits.oom_score == -1


def test_partial_snapshots_are_cheap(monitor):
    snap = monitor.snapshot(stacks=False, tasks=False, gc=False)
    assert snap.tasks == () and snap.gc == ()
    assert all(t.frames == () for t in snap.threads)
    assert all(t.status == ThreadStatus.UNKNOWN for t in snap.threads)


def test_snapshot_serialises_to_json(snapshot):
    text = json.dumps(snapshot.to_dict(), default=str)
    data = json.loads(text)
    assert data["process"]["pid"] == snapshot.process.pid
    assert data["threads"][0]["frames"] is not None


def test_format_snapshot_mentions_everything(snapshot):
    text = format_snapshot(snapshot)
    assert "busy" in text and "busy_loop" in text
    assert "branch-0" in text
    assert "gen0" in text
    assert "rss=" in text


def test_stack_sampling_is_fast(monitor):
    monitor.snapshot()
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        monitor.snapshot(tasks=False, gc=False)
    per_sample = (time.perf_counter() - t0) / n
    # psutil's thread listing on Windows snapshots every thread on the system.
    budget = 0.02 if sys.platform == "win32" else 0.005
    assert per_sample < budget, f"{per_sample * 1e6:.0f}us per snapshot"


def test_stream_stops_when_target_exits():
    proc = spawn_target("--exit-after", "0.6")
    try:
        with Monitor.attach(proc.pid) as m:
            count = 0
            with pytest.raises(ProcessExited):
                for _ in m.stream(0.1, tasks=False, gc=False):
                    count += 1
            assert count >= 1
    finally:
        proc.kill()
        proc.wait()


def test_spawn_child():
    with Monitor.spawn([sys.executable, str(TARGET)]) as m:
        assert m.child is not None
        snap = m.snapshot()
        assert snap.process.pid == m.pid
        assert snap.threads
    assert m.child.poll() is not None


def test_spawn_reports_early_exit():
    with pytest.raises(ProcessExited) as info:
        Monitor.spawn([sys.executable, "-c", "import sys; sys.exit(7)"], settle=0.5)
    assert info.value.returncode == 7


def test_attach_to_non_python_process():
    proc = spawn_sleeper()
    try:
        with pytest.raises(AttachError) as info:
            Monitor.attach(proc.pid)
        assert "CPython" in str(info.value)
    finally:
        proc.kill()
        proc.wait()


def test_attach_to_missing_pid():
    with pytest.raises(ProcessExited):
        Monitor.attach(2**22 - 1)


def test_attach_without_ptrace_falls_back_to_limited_mode(tmp_path):
    import os
    import signal
    import subprocess

    from sgrud import osproc

    if not osproc.LINUX or osproc.ptrace_scope() != 1 or os.geteuid() == 0:
        pytest.skip("needs Linux with kernel.yama.ptrace_scope=1 and a non-root user")
    # Double fork so the target is reparented away from us and is no longer a
    # descendant, which is what ptrace_scope=1 keys on. The launcher hands the
    # grandchild's pid over through a file to avoid holding any pipe open.
    pidfile = tmp_path / "pid"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, pathlib;"
            " p = subprocess.Popen([sys.executable, sys.argv[1]], start_new_session=True,"
            " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
            " pathlib.Path(sys.argv[2]).write_text(str(p.pid))",
            str(TARGET),
            str(pidfile),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=10,
    )
    pid = int(pidfile.read_text())
    try:
        time.sleep(0.5)
        with pytest.raises(AttachError) as info:
            Monitor.attach(pid, require_full=True)
        assert "ptrace_scope" in str(info.value)
        assert "CPython" not in str(info.value)

        # Without require_full we still get everything the OS can tell us.
        with Monitor.attach(pid) as m:
            assert m.limited is not None and "ptrace_scope" in m.limited
            m.snapshot(stacks=False, tasks=False, gc=False)
            time.sleep(0.15)
            snap = m.snapshot()
        assert snap.process.memory.rss > 0
        assert snap.process.cpu_percent is not None
        names = {t.name for t in snap.threads}
        assert {"busy", "idle"} <= names
        busy = next(t for t in snap.threads if t.name == "busy")
        assert busy.cpu_percent is not None and busy.cpu_percent > 5
        assert busy.frames == () and busy.status == ThreadStatus.UNKNOWN
        assert snap.tasks == () and snap.gc == ()
        assert "ptrace_scope" in snap.errors["attach"]
        assert set(snap.errors) == {"attach"}
    finally:
        os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="Yama ptrace_scope is Linux only")
def test_allow_ptrace_lets_a_sibling_inspect_a_run_target():
    import subprocess

    from sgrud.web import allow_ptrace

    child = subprocess.Popen(allow_ptrace([sys.executable, str(TARGET)]), stdout=subprocess.PIPE)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == b"READY"
        sibling = subprocess.run(
            [sys.executable, "-m", "sgrud", "dump", str(child.pid), "--no-gc"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        child.kill()
        child.wait()
    assert sibling.returncode == 0, sibling.stderr
    assert "limited mode" not in sibling.stderr
    assert "busy_loop" in sibling.stdout


def test_children_are_listed_and_marked_python():
    import psutil
    from conftest import spawn_target

    from sgrud.format import format_children

    proc = spawn_target("--children")
    try:
        with Monitor.attach(proc.pid) as m:
            deadline = time.monotonic() + 5
            while True:
                snap = m.snapshot(stacks=False, tasks=False, gc=False)
                kids = {c.pid: c for c in snap.children}
                pythons = [c for c in kids.values() if c.python]
                if len(kids) >= 3 and len(pythons) >= 2 or time.monotonic() > deadline:
                    break
                time.sleep(0.1)
            assert "children" not in snap.errors, snap.errors
            assert len(kids) >= 3, kids
            assert len(pythons) == 2, kids
            others = [c for c in kids.values() if not c.python]
            assert others and all(c.rss > 0 for c in kids.values())
            # The grandchild hangs off the child, both under the target.
            grandchild = next(c for c in pythons if c.parent_pid != proc.pid)
            assert grandchild.parent_pid in kids and kids[grandchild.parent_pid].python
            assert list(kids).index(grandchild.parent_pid) < list(kids).index(grandchild.pid)
            time.sleep(0.15)
            again = m.snapshot(stacks=False, tasks=False, gc=False)
            assert all(c.cpu_percent is not None for c in again.children)
            lines = format_children(again.children)
            assert len(lines) == len(again.children)
            assert any(line.startswith("    [") for line in lines)  # the grandchild is indented
            assert m.snapshot(children=False).children == ()
    finally:
        # The children would outlive the target and keep its stdout pipe open.
        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
        proc.kill()
        proc.wait()


def test_ipc_section_lists_descriptors_locks_and_waits():
    import psutil
    from conftest import spawn_target

    from sgrud.format import format_ipc
    from sgrud.ipc import SYSCALL_NAMES

    proc = spawn_target("--ipc")
    try:
        assert proc.stdout is not None
        _, pipe_fd, port, lock_path, segment, child_pid = proc.stdout.readline().split()
        assert proc.stdout.readline().strip() == b"READY"
        with Monitor.attach(proc.pid) as m:
            snap = m.snapshot(stacks=False, tasks=False, gc=False)
            assert "ipc" not in snap.errors, snap.errors
            ipc = snap.ipc
            assert ipc is not None and ipc.num_fds > 0 and not ipc.truncated
            listener = next(
                f for f in ipc.files if f.kind == "socket" and f.local.endswith(f":{port.decode()}")
            )
            assert listener.family == "tcp" and listener.status == "LISTEN"
            lines = format_ipc(snap)
            assert any("listen" in line and port.decode() in line for line in lines)
            if sys.platform == "win32":
                return
            lock_file = next(f for f in ipc.files if f.target == lock_path.decode())
            assert lock_file.kind == "file" and lock_file.mode == "w"
            if not sys.platform.startswith("linux"):
                return
            # A pipe whose read end a thread sits in, its write end in the same process.
            pipe = ipc.file(int(pipe_fd))
            assert pipe is not None and pipe.kind == "pipe" and pipe.mode == "r"
            assert [o.mode for o in ipc.same_object(pipe.fd)] == ["w"]
            if SYSCALL_NAMES:
                reader = next(t for t in snap.threads if t.name == "pipereader")
                assert reader.syscall is not None, reader
                assert reader.syscall.name == "read" and reader.syscall.fd == pipe.fd
                assert reader.syscall.target == pipe.target
                assert any(f"<- thread {reader.tid} pipereader in read" in line for line in lines)
            # The child's stdin is a pipe the target holds the write end of.
            stdin = next(f for f in ipc.files if int(child_pid) in f.shared_with)
            assert stdin.kind == "pipe" and stdin.mode == "w"
            # The flock, resolved to its path.
            (lock,) = [lk for lk in ipc.locks if lk.path == lock_path.decode()]
            assert lock.kind == "flock" and lock.mode == "write" and not lock.waiting
            assert lock.holder == proc.pid
            assert any(line.startswith("lock     flock write") for line in lines)
            # The shared memory segment, as a descriptor and as a mapping.
            shm = [f for f in ipc.files if f.kind == "shm"]
            assert shm and all(f.target.endswith(segment.decode()) for f in shm)
            (mapping,) = ipc.mappings
            assert mapping.kind == "shm" and mapping.size >= 4096
            assert ipc.counts()["pipe"] >= 3
            assert m.snapshot(ipc=False).ipc is None
            assert all(t.syscall is None for t in m.snapshot(ipc=False, stacks=False).threads)
    finally:
        # The target goes first, then its children get EOF on their pipes
        # and exit, and the resource tracker among them unlinks the segment.
        children = psutil.Process(proc.pid).children(recursive=True)
        proc.kill()
        proc.wait()
        for child in psutil.wait_procs(children, timeout=5)[1]:
            child.kill()
        import contextlib
        import os

        with contextlib.suppress(OSError, NameError):
            os.unlink(lock_path.decode())


def test_probe_runs_inside_the_target(monitor):
    from sgrud.format import format_probe

    result = monitor.probe(types=5)
    assert result.gc_threshold[0] > 0 and len(result.gc_count) == 3
    assert result.gc_enabled and result.gc_frozen == 0
    assert result.allocated_blocks > 1000 and result.modules > 10
    assert {"MainThread", "busy", "idle"} <= set(result.thread_names)
    assert result.tracked > 1000 and len(result.types) == 5
    assert result.types[0].count >= result.types[1].count
    assert "dict" in {t.name for t in result.types}
    assert not result.tracing and result.allocations == ()
    assert 0 < result.elapsed < result.round_trip
    text = format_probe(result)
    assert "threshold" in text and "most common types" in text
    json.dumps(result.to_dict())
    plain = monitor.probe()
    assert plain.tracked == -1 and plain.types == ()


def test_probe_is_refused_when_remote_debugging_is_off():
    proc = subprocess.Popen(
        [sys.executable, "-X", "disable-remote-debug", str(TARGET)], stdout=subprocess.PIPE
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == b"READY"
        with Monitor.attach(proc.pid) as m:
            # Reading memory still works, only code injection is refused.
            assert m.snapshot(tasks=False, gc=False).threads
            with pytest.raises(SgrudError, match="disable-remote-debug"):
                m.probe()
    finally:
        proc.kill()
        proc.wait()
