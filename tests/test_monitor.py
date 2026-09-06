import json
import sys
import time

import pytest

from sgrud import AttachError, Monitor, ProcessExited, ThreadStatus
from sgrud.format import format_snapshot

from conftest import TARGET, spawn_target


def test_process_section(snapshot, target):
    p = snapshot.process
    assert p.pid == target.pid
    assert p.memory.rss > 1024 * 1024
    assert p.memory.vms >= p.memory.rss
    assert p.num_threads >= 3
    assert p.uptime > 0
    assert p.cpu_percent is not None and p.cpu_percent >= 0
    assert "target_app.py" in " ".join(p.cmdline)
    assert not snapshot.errors, snapshot.errors


def test_threads_have_names_status_and_stacks(snapshot):
    by_name = {t.name: t for t in snapshot.threads}
    assert {"busy", "idle"} <= set(by_name)
    main = [t for t in snapshot.threads if t.is_main]
    assert len(main) == 1
    assert main[0].tid == snapshot.process.pid

    busy = by_name["busy"]
    assert busy.frames[0].funcname == "busy_loop"
    assert busy.frames[0].filename.endswith("target_app.py")
    assert busy.frames[0].lineno is not None
    assert busy.cpu_percent is not None and busy.cpu_percent > 5

    idle = by_name["idle"]
    assert idle.frames[0].funcname == "idle_loop"
    assert idle.cpu_percent is not None and idle.cpu_percent < 5
    assert not (idle.status & ThreadStatus.ON_CPU)

    # A thread sleeping inside time.sleep sits under a <native> marker frame.
    assert any(f.synthetic and f.funcname == "<native>" for f in idle.frames)


def test_asyncio_tasks_tree(snapshot):
    names = {t.name for t in snapshot.tasks}
    assert {"branch-0", "branch-1", "branch-2"} <= names
    leaves = [t for t in snapshot.tasks if t.frames and t.frames[0].funcname == "sleep"]
    assert len(leaves) >= 6
    children = snapshot.task_children()
    branch0 = next(t for t in snapshot.tasks if t.name == "branch-0")
    assert len(children[branch0.id]) == 2
    for leaf in children[branch0.id]:
        assert leaf.parent_ids == (branch0.id,)
        assert leaf.thread_id == snapshot.process.pid
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
    assert per_sample < 0.005, f"{per_sample * 1e6:.0f}us per snapshot"


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
        assert snap.process.pid == m.child.pid
        assert any(t.name == "busy" for t in snap.threads) or snap.threads
    assert m.child.poll() is not None


def test_spawn_reports_early_exit():
    with pytest.raises(ProcessExited) as info:
        Monitor.spawn([sys.executable, "-c", "import sys; sys.exit(7)"], settle=0.5)
    assert info.value.returncode == 7


def test_attach_to_non_python_process():
    import subprocess

    proc = subprocess.Popen(["sleep", "5"])
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

    from sgrud import procfs

    if procfs.ptrace_scope() != 1 or os.geteuid() == 0:
        pytest.skip("needs kernel.yama.ptrace_scope=1 and a non-root user")
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

        # Without require_full we still get everything /proc can tell us.
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
