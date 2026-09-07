"""The demo in examples/ keeps something on every tab."""

import pathlib
import subprocess
import sys
import time

import psutil

from sgrud import Monitor

DEMO = pathlib.Path(__file__).resolve().parent.parent / "examples" / "demo_app.py"


def test_demo_app_has_something_on_every_tab():
    proc = subprocess.Popen([sys.executable, str(DEMO), "--seconds", "60"], stdout=subprocess.PIPE)
    children: list[psutil.Process] = []
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.startswith(b"sgrud demo running as pid"), line
        with Monitor.attach(proc.pid) as m:
            wanted_threads = {"simulate", "wait_for_lock", "read_pipe", "read_socket", "churn"}
            wanted_tasks = {"supervisor", "consumer-2", "quiet-1", "lock-waiter", "feeder"}
            deadline = time.monotonic() + 10
            while True:
                snap = m.snapshot()
                stacks = {t.tid: {f.funcname for f in t.frames} for t in snap.threads}
                tasks = {t.name: t for t in snap.tasks}
                pythons = [c for c in snap.children if c.python]
                ready = (
                    all(any(name in s for s in stacks.values()) for name in wanted_threads)
                    and wanted_tasks <= set(tasks)
                    and len(pythons) >= 2
                    and len(snap.children) > len(pythons)
                )
                if ready or time.monotonic() > deadline:
                    break
                time.sleep(0.2)
            assert ready, (stacks, set(tasks), snap.children)
            assert not snap.errors, snap.errors

            # The task tree: consumers hang off the supervisor, which hangs off main.
            assert tasks["consumer-2"].parent_ids == (tasks["supervisor"].id,)
            assert tasks["supervisor"].parent_ids == tasks["quiet-1"].parent_ids

            ipc = snap.ipc
            assert ipc is not None
            kinds = ipc.counts()
            assert kinds.get("pipe", 0) >= 1 and kinds.get("socket", 0) >= 6, kinds
            assert any(f.kind == "socket" and f.status == "LISTEN" for f in ipc.files)
            if sys.platform.startswith("linux"):
                assert kinds.get("shm", 0) >= 1, kinds
                assert ipc.semaphores >= 1
                # The file lock a child holds, and the thread blocked on it.
                waiting = [lock for lock in ipc.locks if lock.waiting]
                assert waiting and waiting[0].holder in {c.pid for c in snap.children}
                calls = {t.syscall.name for t in snap.threads if t.syscall}
                assert {"read", "flock", "futex"} <= calls, calls
                reader = next(t for t in snap.threads if "read_pipe" in stacks[t.tid])
                assert reader.syscall is not None and reader.syscall.fd >= 0
                assert ipc.file(reader.syscall.fd) is not None
    finally:
        children = psutil.Process(proc.pid).children(recursive=True)
        proc.kill()
        proc.wait()
        # Killed outright, the demo cannot clean up, so its children notice
        # on their own and exit, taking the lock file and the segment along.
        _, alive = psutil.wait_procs(children, timeout=5)
        for child in alive:
            child.kill()
    assert not alive, [c.cmdline() for c in alive]
