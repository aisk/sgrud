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
from .export import Recorder
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
from .profile import Hotspots
from .sampler import Sampler

__all__ = [
    "AttachError",
    "Awaiter",
    "Cgroup",
    "ChildProcess",
    "FileLock",
    "Frame",
    "GCCollection",
    "GCGeneration",
    "Hotspots",
    "IPC",
    "Memory",
    "MemoryLimits",
    "Monitor",
    "NotSupported",
    "OpenFile",
    "Process",
    "ProcessExited",
    "Recorder",
    "Sampler",
    "SgrudError",
    "SharedMapping",
    "Snapshot",
    "Syscall",
    "Task",
    "Thread",
    "ThreadStatus",
]
