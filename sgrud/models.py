"""Plain data model for a snapshot of a remote Python process.

Everything here is a frozen dataclass so snapshots can be compared, cached,
serialized with :func:`dataclasses.asdict` and consumed by any front end
(CLI, TUI, tests) without touching ``_remote_debugging`` or psutil.
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
    #: Scheduler state spelled like psutil does it (running, sleeping,
    #: disk-sleep, ...). Only Linux reports it per thread; "" elsewhere.
    state: str
    user_time: float
    system_time: float
    #: CPU usage since the previous snapshot in percent of one core.
    #: ``None`` for the first snapshot of a monitor.
    cpu_percent: float | None
    #: Leaf frame first.
    frames: tuple[Frame, ...] = ()
    #: The system call the thread is blocked in, ``None`` when it is
    #: running, not in one, or the platform does not say (Linux only).
    syscall: Syscall | None = None

    @property
    def is_main(self) -> bool:
        return bool(self.status & ThreadStatus.MAIN_THREAD)


@dataclass(frozen=True, slots=True)
class Syscall:
    """A system call a thread is blocked in, from ``/proc/<pid>/task/<tid>/syscall``.

    Only the facts the kernel reports: the call and its arguments. ``fd``
    is the descriptor the call operates on for the calls that take one
    (read, write, recv, poll on one descriptor, flock, ...), -1 otherwise.
    ``target`` names what the descriptor is, resolved from the fd table
    (``pipe:[1234]``, a path, ``socket:[5678]``), "" when unknown.
    """

    name: str
    number: int
    args: tuple[int, ...] = ()
    fd: int = -1
    target: str = ""

    def describe(self) -> str:
        """``read(fd 12 pipe:[48453])`` or just ``futex``."""
        if self.fd < 0:
            return self.name
        what = f"fd {self.fd}"
        if self.target:
            what += f" {self.target}"
        return f"{self.name}({what})"


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
    """One garbage collection of a generation.

    ``collected``, ``uncollectable`` and ``candidates`` are -1 and
    ``duration`` is NaN when the previous collection of the generation was
    never observed, since the target only keeps cumulative counters.
    """

    generation: int
    interpreter_id: int
    #: Ordinal of the collection within its generation, 1 for the first.
    index: int
    #: Raw ``time.perf_counter_ns()`` readings on the target's clock.
    started_at: int
    stopped_at: int
    duration: float
    collected: int
    uncollectable: int
    candidates: int
    #: Objects tracked by the GC when the collection started.
    heap_size: int
    #: Seconds between the end of the collection and the snapshot.
    age: float = float("nan")

    @property
    def survivors(self) -> int:
        """Candidates that survived the collection, -1 when unknown."""
        if self.candidates < 0 or self.collected < 0:
            return -1
        return self.candidates - self.collected


@dataclass(frozen=True, slots=True)
class GCGeneration:
    generation: int
    collections: int
    collected: int
    uncollectable: int
    total_duration: float
    #: Objects tracked by the GC when the last collection started.
    heap_size: int
    #: Collections per second since the previous snapshot, ``None`` for the
    #: first snapshot of a monitor.
    rate: float | None = None
    #: Fraction of wall time the target spent in collections of this
    #: generation since the previous snapshot, ``None`` for the first.
    time_share: float | None = None
    #: Most recent first. A monitor accumulates this across snapshots, so
    #: it can reach back further than the target's own history ring.
    history: tuple[GCCollection, ...] = ()

    @property
    def mean_duration(self) -> float:
        return self.total_duration / self.collections if self.collections else 0.0


@dataclass(frozen=True, slots=True)
class Memory:
    """Process memory figures in bytes.

    Fields the platform does not report are 0. Linux reports everything.
    macOS has ``rss``, ``vms`` and ``uss``. Windows has those plus ``hwm``
    and ``data`` (private bytes), and its ``vms`` is the commit charge,
    which can be smaller than ``rss``.
    """

    rss: int
    vms: int
    #: Peak RSS.
    hwm: int
    swap: int
    data: int
    shared: int
    #: Unique set size: pages no other process maps. What the process
    #: would give back if it exited.
    uss: int = 0
    #: Proportional set size: rss with shared pages split between sharers.
    pss: int = 0
    #: Parts of rss: anonymous memory (the allocators), file mappings
    #: (the interpreter binary, extension modules) and shared memory.
    anon: int = 0
    file: int = 0
    shmem: int = 0
    #: The brk heap, where glibc malloc puts small blocks. Python objects
    #: over 512 bytes and raw allocations land here.
    brk: int = 0
    #: Anonymous private mappings outside the brk heap and the main stack:
    #: pymalloc arenas, large mallocs and thread stacks.
    anon_mapped: int = 0
    #: Anonymous memory backed by transparent huge pages.
    huge: int = 0
    #: Peak virtual size.
    peak_vms: int = 0


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    """Ceilings the process runs under. 0 where there is none or the platform does not say."""

    #: cgroup memory limit, throttle threshold and current usage, in
    #: bytes. Linux only, and only when the process is in a cgroup that
    #: sets them, which is what containers do.
    cgroup_limit: int = 0
    cgroup_high: int = 0
    cgroup_usage: int = 0
    #: ``RLIMIT_AS``, the address space ceiling. Linux only.
    address_space: int = 0
    #: The kernel's OOM killer score, -1 when unknown. Linux only.
    oom_score: int = -1

    @property
    def cgroup_percent(self) -> float | None:
        if self.cgroup_limit <= 0:
            return None
        return 100.0 * self.cgroup_usage / self.cgroup_limit


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
    #: Page faults since the process started. Major faults (those that
    #: had to read from disk) are counted in both figures. Windows reports
    #: no major faults separately.
    page_faults: int = 0
    major_faults: int = 0
    #: Faults per second since the previous snapshot, ``None`` for the
    #: first snapshot. Minor faults are the allocators touching new pages,
    #: so this moves before rss does.
    fault_rate: float | None = None
    major_fault_rate: float | None = None
    limits: MemoryLimits = MemoryLimits()


@dataclass(frozen=True, slots=True)
class ChildProcess:
    """A descendant of the target: a worker of a pool, a subprocess, a shell."""

    pid: int
    parent_pid: int
    #: The executable's base name.
    name: str
    cmdline: tuple[str, ...]
    #: Whether it is a CPython interpreter sgrud could attach to. False
    #: also when its memory cannot be read.
    python: bool
    state: str
    rss: int
    num_threads: int
    user_time: float
    system_time: float
    #: Seconds since the child started.
    uptime: float
    #: CPU usage since the previous snapshot in percent of one core,
    #: ``None`` the first time the child is seen.
    cpu_percent: float | None = None


@dataclass(frozen=True, slots=True)
class OpenFile:
    """One entry of the target's descriptor table."""

    fd: int
    #: One of ``pipe``, ``socket``, ``shm``, ``file``, ``anon``, ``other``.
    #: ``shm`` is POSIX shared memory under ``/dev/shm``, ``anon`` an
    #: anonymous inode (eventfd, epoll, timerfd, signalfd).
    kind: str
    #: What the descriptor refers to: a path, ``pipe:[inode]``,
    #: ``socket:[inode]``, ``anon_inode:[eventfd]``.
    target: str
    #: ``r``, ``w`` or ``rw``. "" where the platform does not say.
    mode: str = ""
    #: The pipe or socket inode, 0 for anything else.
    inode: int = 0
    #: Pids of the target's parent and descendants that have the same
    #: pipe or socket open, for a pipe the ones holding the other end
    #: included. Only Linux resolves this.
    shared_with: tuple[int, ...] = ()
    #: For a socket, its addresses as psutil reports them: ``tcp``,
    #: ``udp`` or ``unix`` plus local address, remote address and state.
    family: str = ""
    local: str = ""
    remote: str = ""
    status: str = ""


@dataclass(frozen=True, slots=True)
class FileLock:
    """A file lock the target holds or is waiting for, from ``/proc/locks``. Linux only."""

    #: ``flock``, ``posix`` (fcntl record lock) or ``ofd`` (open file description lock).
    kind: str
    #: ``read`` or ``write``.
    mode: str
    #: The locked file as the target has it open, "" when it does not
    #: (a lock inherited across exec, say) so only the inode is known.
    path: str
    #: ``major:minor:inode`` as the kernel spells it.
    inode: str
    #: Byte range, ``end`` -1 for the end of file.
    start: int = 0
    end: int = -1
    #: True when the target is blocked waiting for this lock, in which
    #: case ``holder`` is the pid holding it. Otherwise the target holds
    #: it and ``holder`` is the target itself, or -1 for an OFD lock which
    #: the kernel does not attribute to a pid.
    waiting: bool = False
    holder: int = -1


@dataclass(frozen=True, slots=True)
class SharedMapping:
    """A shared memory object mapped by the target, from ``/proc/<pid>/maps``. Linux only.

    ``multiprocessing.shared_memory`` segments are ``/dev/shm/psm_*``
    and ``multiprocessing`` locks, semaphores and queues each map one
    ``/dev/shm/sem.*`` page that is unlinked right after creation.
    """

    path: str
    size: int
    #: ``shm`` for shared memory, ``sem`` for a POSIX semaphore.
    kind: str
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class IPC:
    """What the target has open and shares with other processes."""

    #: Descriptors in use and the soft ``RLIMIT_NOFILE`` (0 when unknown).
    #: Windows counts handles instead.
    num_fds: int
    max_fds: int = 0
    #: The descriptor table, ordered by fd. On Linux every descriptor up
    #: to :data:`sgrud.ipc.MAX_FILES` is listed; elsewhere psutil supplies
    #: regular files and sockets only.
    files: tuple[OpenFile, ...] = ()
    locks: tuple[FileLock, ...] = ()
    mappings: tuple[SharedMapping, ...] = ()
    #: True where :attr:`files` can only ever hold regular files and
    #: sockets (macOS and Windows), so :attr:`num_fds` does not bound it.
    partial: bool = False

    @property
    def truncated(self) -> bool:
        """Whether descriptors were cut off the end of :attr:`files`."""
        return not self.partial and 0 < len(self.files) < self.num_fds

    def counts(self) -> dict[str, int]:
        """Descriptors per kind, in the order the kinds are worth reading."""
        out: dict[str, int] = {}
        for f in self.files:
            out[f.kind] = out.get(f.kind, 0) + 1
        order = ("pipe", "socket", "shm", "file", "anon", "other")
        return {k: out[k] for k in order if k in out}

    def file(self, fd: int) -> OpenFile | None:
        for f in self.files:
            if f.fd == fd:
                return f
        return None

    def same_object(self, fd: int) -> tuple[OpenFile, ...]:
        """Other descriptors of the target that refer to the same pipe or socket.

        For a pipe that is the process's own copy of the other end, or a
        duplicate of the same end.
        """
        f = self.file(fd)
        if f is None or not f.inode:
            return ()
        return tuple(o for o in self.files if o.inode == f.inode and o.fd != fd)

    @property
    def semaphores(self) -> int:
        return sum(m.kind == "sem" for m in self.mappings)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A consistent-as-practical picture of the target at one instant."""

    timestamp: float
    process: Process
    threads: tuple[Thread, ...] = ()
    tasks: tuple[Task, ...] = ()
    gc: tuple[GCGeneration, ...] = ()
    #: Every descendant of the target, parents before children.
    children: tuple[ChildProcess, ...] = ()
    #: Open descriptors, locks and shared memory, ``None`` when not collected.
    ipc: IPC | None = None
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

    @property
    def collecting(self) -> tuple[Thread, ...]:
        """Threads that are inside a garbage collection right now."""
        return tuple(
            t for t in self.threads if any(f.synthetic and f.funcname == "<GC>" for f in t.frames)
        )

    @property
    def gc_time_share(self) -> float | None:
        """Fraction of wall time spent in the GC since the previous snapshot."""
        shares = [g.time_share for g in self.gc if g.time_share is not None]
        return sum(shares) if shares else None

    @property
    def gc_rate(self) -> float | None:
        """Collections per second, all generations, since the previous snapshot."""
        rates = [g.rate for g in self.gc if g.rate is not None]
        return sum(rates) if rates else None

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
