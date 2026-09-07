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
    Awaiter,
    ChildProcess,
    Frame,
    GCCollection,
    GCGeneration,
    Memory,
    MemoryLimits,
    Process,
    Snapshot,
    Task,
    Thread,
    ThreadStatus,
)
from .monitor import Monitor

__all__ = [
    "AttachError",
    "Awaiter",
    "ChildProcess",
    "Frame",
    "GCCollection",
    "GCGeneration",
    "Memory",
    "MemoryLimits",
    "Monitor",
    "NotSupported",
    "Process",
    "ProcessExited",
    "SgrudError",
    "Snapshot",
    "Task",
    "Thread",
    "ThreadStatus",
]
