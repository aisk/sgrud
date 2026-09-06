"""Plain data model for a snapshot of a remote Python process.

Everything here is a frozen dataclass so snapshots can be compared, cached,
serialized with :func:`dataclasses.asdict` and consumed by any front end
(CLI, TUI, tests) without touching ``_remote_debugging`` or ``/proc``.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any


class ThreadStatus(enum.IntFlag):
    """Bit flags reported by ``_remote_debugging`` for each thread."""

    NONE = 0
    HAS_GIL = 1 << 0
    ON_CPU = 1 << 1
    UNKNOWN = 1 << 2
    GIL_REQUESTED = 1 << 3
    HAS_EXCEPTION = 1 << 4
    MAIN_THREAD = 1 << 5

    def describe(self) -> str:
        """Short human readable label such as ``"gil,cpu"``."""
        parts = []
        if self & ThreadStatus.MAIN_THREAD:
            parts.append("main")
        if self & ThreadStatus.HAS_GIL:
            parts.append("gil")
        elif self & ThreadStatus.GIL_REQUESTED:
            parts.append("wait-gil")
        if self & ThreadStatus.ON_CPU:
            parts.append("cpu")
        if self & ThreadStatus.HAS_EXCEPTION:
            parts.append("exc")
        if self & ThreadStatus.UNKNOWN:
            parts.append("?")
        return ",".join(parts) or "idle"


@dataclass(frozen=True, slots=True)
class Frame:
    """One Python stack frame, or a synthetic ``<native>`` / ``<GC>`` marker."""

    funcname: str
    filename: str
    lineno: int | None = None
    end_lineno: int | None = None
    col_offset: int | None = None
    end_col_offset: int | None = None

    @property
    def synthetic(self) -> bool:
        return self.lineno is None

    def format(self) -> str:
        if self.synthetic:
            return self.funcname
        return f"{self.funcname} ({self.filename}:{self.lineno})"


@dataclass(frozen=True, slots=True)
class Thread:
    """An OS thread that belongs to the target interpreter."""

    tid: int
    name: str
    interpreter_id: int
    status: ThreadStatus
    #: Kernel scheduler state letter from ``/proc`` (R, S, D, ...), or "".
    state: str
    user_time: float
    system_time: float
    #: CPU usage since the previous snapshot in percent of one core.
    #: ``None`` for the first snapshot of a monitor.
    cpu_percent: float | None
    #: Leaf frame first.
    frames: tuple[Frame, ...] = ()

    @property
    def is_main(self) -> bool:
        return bool(self.status & ThreadStatus.MAIN_THREAD)


@dataclass(frozen=True, slots=True)
class Awaiter:
    """A task waiting on another task, with the frames of the awaiting coroutine."""

    task_id: int
    frames: tuple[Frame, ...] = ()


@dataclass(frozen=True, slots=True)
class Task:
    """An asyncio task discovered in the target process."""

    id: int
    name: str
    thread_id: int
    #: Coroutine call stack, leaf first.
    frames: tuple[Frame, ...] = ()
    awaited_by: tuple[Awaiter, ...] = ()

    @property
    def parent_ids(self) -> tuple[int, ...]:
        return tuple(a.task_id for a in self.awaited_by)


@dataclass(frozen=True, slots=True)
class GCCollection:
    """One garbage collection recorded in the target's GC history ring."""

    generation: int
    interpreter_id: int
    started_at: int
    stopped_at: int
    duration: float
    collected: int
    uncollectable: int
    candidates: int
    heap_size: int


@dataclass(frozen=True, slots=True)
class GCGeneration:
    generation: int
    collections: int
    collected: int
    uncollectable: int
    total_duration: float
    heap_size: int
    #: Most recent first.
    history: tuple[GCCollection, ...] = ()


@dataclass(frozen=True, slots=True)
class Memory:
    """Process memory figures from ``/proc/<pid>/status``, in bytes."""

    rss: int
    vms: int
    hwm: int
    swap: int
    data: int
    shared: int


@dataclass(frozen=True, slots=True)
class Process:
    pid: int
    exe: str
    cmdline: tuple[str, ...]
    state: str
    num_threads: int
    memory: Memory
    user_time: float
    system_time: float
    #: Seconds since the process started.
    uptime: float
    #: CPU usage since the previous snapshot in percent of one core.
    cpu_percent: float | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A consistent-as-practical picture of the target at one instant."""

    timestamp: float
    process: Process
    threads: tuple[Thread, ...] = ()
    tasks: tuple[Task, ...] = ()
    gc: tuple[GCGeneration, ...] = ()
    #: Sections that could not be collected, mapped to the error text.
    errors: dict[str, str] = field(default_factory=dict)

    def thread(self, tid: int) -> Thread | None:
        for t in self.threads:
            if t.tid == tid:
                return t
        return None

    def task(self, task_id: int) -> Task | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def task_children(self) -> dict[int | None, list[Task]]:
        """Map parent task id (``None`` for roots) to its child tasks."""
        known = {t.id for t in self.tasks}
        children: dict[int | None, list[Task]] = {}
        for t in self.tasks:
            parents = [p for p in t.parent_ids if p in known] or [None]
            for p in parents:
                children.setdefault(p, []).append(t)
        return children

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
