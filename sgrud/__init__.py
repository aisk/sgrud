"""sgrud: inspect a running CPython process from the outside.

Quick start::

    from sgrud import Monitor

    with Monitor.attach(pid) as m:
        snap = m.snapshot()
        print(snap.process.memory.rss, snap.process.cpu_percent)
        for t in snap.threads:
            print(t.tid, t.name, t.status.describe(), t.frames[:1])
"""

from .errors import AttachError, NotSupported, ProcessExited, SgrudError
from .models import (
    IPC,
    Awaiter,
    Cgroup,
    ChildProcess,
    FileLock,
    Frame,
    GCCollection,
    GCGeneration,
    Memory,
    MemoryLimits,
    OpenFile,
    Process,
    SharedMapping,
    Snapshot,
    Syscall,
    Task,
    Thread,
    ThreadStatus,
)
from .monitor import Monitor

__all__ = [
    "AttachError",
    "Awaiter",
    "Cgroup",
    "ChildProcess",
    "FileLock",
    "Frame",
    "GCCollection",
    "GCGeneration",
    "IPC",
    "Memory",
    "MemoryLimits",
    "Monitor",
    "NotSupported",
    "OpenFile",
    "Process",
    "ProcessExited",
    "SgrudError",
    "SharedMapping",
    "Snapshot",
    "Syscall",
    "Task",
    "Thread",
    "ThreadStatus",
]
