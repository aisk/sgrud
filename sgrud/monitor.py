"""High level entry point: attach to (or spawn) a process and take snapshots."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace

from . import osproc
from .errors import AttachError, ProcessExited
from .ipc import decode_syscall
from .models import (
    IPC,
    ChildProcess,
    GCGeneration,
    Process,
    Snapshot,
    Task,
    Thread,
    ThreadStatus,
)
from .remote import (
    GCRecord,
    RawSample,
    RemoteInspector,
    access_hint,
    build_gc,
    interpreter_pid,
    is_python_process,
    unwinder_mode,
)

#: Collections kept per generation across snapshots.
GC_HISTORY = 200


@dataclass(slots=True)
class _RateSample:
    """Cumulative counters read at wall time ``wall``, for differencing."""

    wall: float
    values: tuple[float, ...]


def _rates(
    prev: _RateSample | None, now: float, values: tuple[float, ...]
) -> tuple[float | None, ...]:
    """Per-second change of each value since ``prev``, ``None`` without one."""
    if prev is None or now <= prev.wall:
        return (None,) * len(values)
    elapsed = now - prev.wall
    return tuple(max(0.0, (v - p) / elapsed) for v, p in zip(values, prev.values, strict=True))


def _cpu_percent(
    prev: _RateSample | None, now: float, cpu_seconds: float
) -> tuple[float | None, _RateSample]:
    """CPU use since ``prev`` in percent of one core, and the sample to keep for next time."""
    sample = _RateSample(now, (cpu_seconds,))
    (rate,) = _rates(prev, now, sample.values)
    return (None if rate is None else rate * 100.0), sample


def _share(prev: _RateSample | None, values: tuple[float, ...]) -> float | None:
    """Percent the second counter grew relative to the first since ``prev``.

    ``None`` without a previous sample or when the first counter stood still.
    """
    if prev is None:
        return None
    total = values[0] - prev.values[0]
    if total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (values[1] - prev.values[1]) / total))


class _GCTracker:
    """Accumulates GC ring records across reads and derives rates.

    The target keeps only the last few collections per generation (11 for
    the young generation, 3 for the others), so at a one second refresh a
    busy process has already overwritten most of them. Keeping every record
    seen gives a continuous history, and resolves the per-collection
    figures of records whose predecessor was read earlier.
    """

    def __init__(self, limit: int = GC_HISTORY) -> None:
        self.limit = limit
        self._records: dict[int, dict[int, GCRecord]] = {}
        self._prev: dict[int, _RateSample] = {}

    def update(
        self, records: Iterable[GCRecord], *, now: float, now_ns: int
    ) -> tuple[GCGeneration, ...]:
        latest: dict[int, GCRecord] = {}
        for r in records:
            slots = self._records.setdefault(r.generation, {})
            slots[r.index] = r
            if r.generation not in latest or r.index > latest[r.generation].index:
                latest[r.generation] = r
        for slots in self._records.values():
            if len(slots) > self.limit:
                for index in sorted(slots)[: len(slots) - self.limit]:
                    del slots[index]
        rates: dict[int, tuple[float | None, float | None]] = {}
        for gen, r in latest.items():
            sample = _RateSample(now, (float(r.index), r.duration))
            rate, share = _rates(self._prev.get(gen), now, sample.values)
            self._prev[gen] = sample
            rates[gen] = (rate, share)
        # A generation with no collection yet is not among the records at
        # all, so it gets no sample and its rates stay unknown until the
        # second snapshot after its first collection.
        return build_gc(
            (r for slots in self._records.values() for r in slots.values()),
            now_ns=now_ns,
            rates=rates,
        )


class Monitor:
    """Collects :class:`~sgrud.models.Snapshot` objects for one target process.

    Use :meth:`attach` for a running pid or :meth:`spawn` to start the
    target as a child, which sidesteps ``ptrace_scope`` restrictions.
    Snapshots are cheap (tens of microseconds for the stacks) so calling
    :meth:`snapshot` several times per second is fine.
    """

    def __init__(
        self,
        pid: int,
        *,
        child: subprocess.Popen[bytes] | None = None,
        native_frames: bool = True,
        gc_markers: bool = True,
        cache_frames: bool | None = None,
        opcodes: bool = False,
    ):
        """``cache_frames`` defaults to the opposite of ``gc_markers``.

        The unwinder's frame cache returns the cached stack whenever the
        frame addresses are unchanged, and a running collection does not
        change them, so with the cache on the ``<GC>`` marker never shows
        up (CPython 3.15). Reading a stack without the cache costs a few
        microseconds more.

        ``opcodes`` makes raw samples carry the current bytecode
        instruction of every frame, for the ``profiling.sampling`` formats
        that show it (gecko, heatmap, binary).
        """
        osproc.ensure_supported()
        if cache_frames is None:
            cache_frames = not gc_markers
        self.pid = pid
        self._child = child
        # One unwinder per unwinder mode, created on demand. The unwinder
        # decides which threads a mode includes, so a mode is a property
        # of the unwinder rather than of a read.
        self._inspectors: dict[str, RemoteInspector] = {}
        #: Why the target's memory cannot be read, or None for full access.
        #: In limited mode only what the OS reports (memory, CPU, thread
        #: names) is available; stacks, tasks, GC and hotspots are not.
        self.limited: str | None = None
        # The unwinder keeps per-call caches, so serialize access to it when a
        # background Sampler and the UI thread share one Monitor.
        self._lock = threading.Lock()
        self._inspector_opts = dict(
            native=native_frames, gc_markers=gc_markers, cache_frames=cache_frames, opcodes=opcodes
        )
        self._proc_cpu: _RateSample | None = None
        self._thread_cpu: dict[int, _RateSample] = {}
        self._child_cpu: dict[int, _RateSample] = {}
        # Whether a child is a CPython process. A fresh interpreter says no
        # until it has mapped its runtime, so only a yes is final.
        self._child_python: dict[int, bool] = {}
        self._faults: _RateSample | None = None
        self._throttle: _RateSample | None = None
        self._gc = _GCTracker()
        try:
            self._stats = osproc.ProcessStats(pid)
        except ProcessLookupError as e:
            raise ProcessExited(pid) from e

    @classmethod
    def attach(
        cls, pid: int, *, retry: float = 1.0, require_full: bool = False, **options
    ) -> Monitor:
        """Attach to a running process. Raises AttachError with a hint on failure.

        When the target's memory cannot be read (ptrace restrictions) the
        monitor comes back in limited mode, see :attr:`limited`, unless
        ``require_full`` is set. Transient failures (the interpreter is
        still starting) are retried for up to ``retry`` seconds.

        When ``pid`` is a launcher whose only child is the interpreter (a
        Windows venv's ``python.exe``) the child is attached instead.
        """
        if not osproc.pid_exists(pid):
            raise ProcessExited(pid)
        hint = access_hint(pid)
        if hint is not None:
            # is_python_process() also needs to read memory and would report
            # a perfectly good interpreter as "not Python", so check first.
            if require_full or not osproc.looks_like_python(pid):
                raise AttachError(pid, "permission denied", hint)
            monitor = cls(pid, **options)
            monitor.limited = hint
            return monitor
        deadline = time.monotonic() + retry
        while True:
            target = interpreter_pid(pid)
            if target is not None:
                break
            try:
                exe = osproc.exe(pid)
            except ProcessLookupError:
                raise ProcessExited(pid) from None
            # A launcher named python whose interpreter child has not
            # started yet, or an interpreter that has not loaded its DLL.
            if time.monotonic() < deadline and osproc.looks_like_python(pid):
                time.sleep(0.05)
                continue
            raise AttachError(
                pid,
                f"{exe or 'process'} does not look like a CPython interpreter",
                "sgrud can only inspect CPython processes of the same version as itself.",
            )
        monitor = cls(target, **options)
        while True:
            try:
                monitor._get_inspector()  # fail early with a useful message
                return monitor
            except AttachError as e:
                if not e.transient or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    @classmethod
    def spawn(
        cls,
        argv: Sequence[str],
        *,
        settle: float = 0.2,
        **options,
    ) -> Monitor:
        """Start ``argv`` as a child process and attach to it.

        ``settle`` is how long to wait for the interpreter to initialise
        before the first attach attempt. If the child turns out to be a
        launcher (a Windows venv's ``python.exe``) its interpreter child
        is attached instead, while the launcher stays :attr:`child`.
        """
        child = subprocess.Popen(list(argv))
        deadline = time.monotonic() + max(settle, 0.05) + 5.0
        time.sleep(settle)
        last: Exception | None = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ProcessExited(child.pid, child.returncode)
            try:
                target = interpreter_pid(child.pid)
                if target is None:
                    raise AttachError(child.pid, "no interpreter started yet", transient=True)
                monitor = cls(target, child=child, **options)
                monitor._get_inspector()
                return monitor
            except AttachError as e:
                last = e
                time.sleep(0.05)
        child.kill()
        raise last or AttachError(child.pid, "timed out waiting for the interpreter")

    # -- lifecycle -----------------------------------------------------

    @property
    def child(self) -> subprocess.Popen[bytes] | None:
        return self._child

    def close(self, *, kill_child: bool = True) -> None:
        with self._lock:
            for inspector in self._inspectors.values():
                inspector.close()
            self._inspectors.clear()
        if self._child is not None and kill_child and self._child.poll() is None:
            self._child.kill()
            self._child.wait()

    def __enter__(self) -> Monitor:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get_inspector(self, mode: str = "wall") -> RemoteInspector:
        """The inspector serving sampling ``mode``, built on first use."""
        if self.limited is not None:
            raise AttachError(self.pid, "limited mode", self.limited)
        key = unwinder_mode(mode)
        with self._lock:
            inspector = self._inspectors.get(key)
            if inspector is None:
                inspector = RemoteInspector(self.pid, mode=key, **self._inspector_opts)
                self._inspectors[key] = inspector
            return inspector

    def sample(self, mode: str = "wall", *, retries: int = 5) -> RawSample:
        """One read of the stacks (or, in ``async`` mode, the tasks), unconverted.

        This is what a profiler wants to call hundreds of times per second.
        ``mode`` is one of :data:`sgrud.remote.MODES` and decides which
        threads the read includes. The result converts to sgrud's types
        with :meth:`RawSample.stacks` or :meth:`RawSample.tasks` and can be
        recorded as is, see :mod:`sgrud.export`. A read torn by the target
        changing its frames is retried at once up to ``retries`` times.
        Raises ProcessExited when the target is gone.
        """
        inspector = self._get_inspector(mode)
        with self._lock:
            try:
                return inspector.sample(retries, tasks=mode == "async")
            except ProcessExited:
                raise
            except Exception:
                self._check_alive()
                raise

    def probe(self, *, types: int = 0, allocations: int = 10, timeout: float = 5.0):
        """Run a script inside the target, see :func:`sgrud.probe.probe`.

        This is the one thing sgrud does that touches the target: its main
        thread runs the script at its next safe point. Fails in limited
        mode, since injecting code needs the same access as reading memory.
        """
        from .probe import probe

        if self.limited is not None:
            raise AttachError(self.pid, "limited mode", self.limited)
        self._check_alive()
        return probe(self.pid, types=types, allocations=allocations, timeout=timeout)

    def read_stats(self, mode: str = "wall") -> dict[str, int | float]:
        """Counters of the reader used for ``mode``: memory reads, bytes, cache hits.

        They count every read made in that mode since the monitor was
        opened, so for ``wall`` the snapshots are included. Empty in
        limited mode or before the first read.
        """
        with self._lock:
            inspector = self._inspectors.get(unwinder_mode(mode))
            return inspector.stats() if inspector is not None else {}

    def _check_alive(self) -> None:
        if self._child is not None and self._child.poll() is not None:
            raise ProcessExited(self.pid, self._child.returncode)
        if not self._stats.is_running():
            raise ProcessExited(self.pid)

    # -- sampling ------------------------------------------------------

    def snapshot(
        self,
        *,
        stacks: bool = True,
        tasks: bool = True,
        gc: bool = True,
        children: bool = True,
        ipc: bool = True,
    ) -> Snapshot:
        """Collect one snapshot. Sections that fail are reported in ``errors``."""
        self._check_alive()
        now = time.monotonic()
        errors: dict[str, str] = {}
        try:
            stat = self._stats.process()
            os_threads = self._stats.threads(syscalls=ipc and self.limited is None)
        except ProcessLookupError as e:
            raise ProcessExited(self.pid) from e

        faults = _RateSample(now, (float(stat.page_faults), float(stat.major_faults)))
        fault_rate, major_fault_rate = _rates(self._faults, now, faults.values)
        self._faults = faults
        throttle = _RateSample(now, (float(stat.cgroup.periods), float(stat.cgroup.throttled)))
        cgroup = replace(stat.cgroup, throttled_percent=_share(self._throttle, throttle.values))
        self._throttle = throttle
        cpu_percent, self._proc_cpu = _cpu_percent(self._proc_cpu, now, stat.utime + stat.stime)
        process = Process(
            pid=self.pid,
            exe=stat.exe,
            cmdline=stat.cmdline,
            state=stat.state,
            num_threads=stat.num_threads,
            memory=stat.memory,
            user_time=stat.utime,
            system_time=stat.stime,
            uptime=max(time.time() - self._stats.start_time, 0.0),
            cpu_percent=cpu_percent,
            page_faults=stat.page_faults,
            major_faults=stat.major_faults,
            fault_rate=fault_rate,
            major_fault_rate=major_fault_rate,
            limits=stat.limits,
            cgroup=cgroup,
            cpus_allowed=stat.cpus_allowed,
        )

        remote: dict[int, tuple[int, ThreadStatus, tuple]] = {}
        inspector: RemoteInspector | None = None
        if self.limited is not None:
            errors["attach"] = self.limited
        elif stacks or tasks or gc:
            try:
                inspector = self._get_inspector()
            except AttachError as e:
                errors["attach"] = str(e)
        if stacks and inspector is not None:
            try:
                with self._lock:
                    remote = inspector.sample().stacks()
            except ProcessExited:
                raise
            except Exception as e:
                errors["stacks"] = f"{type(e).__name__}: {e}"

        child_list: tuple[ChildProcess, ...] = ()
        if children:
            try:
                child_list = self._children(now)
            except ProcessLookupError as e:
                raise ProcessExited(self.pid) from e
            except Exception as e:
                errors["children"] = f"{type(e).__name__}: {e}"

        ipc_info: IPC | None = None
        if ipc:
            try:
                related = [self._stats.parent_pid()] + [c.pid for c in child_list]
                ipc_info = self._stats.ipc(pid for pid in related if pid > 1)
            except ProcessLookupError as e:
                raise ProcessExited(self.pid) from e
            except Exception as e:
                errors["ipc"] = f"{type(e).__name__}: {e}"
        files_by_fd = {f.fd: f for f in ipc_info.files} if ipc_info is not None else None

        # Where the OS cannot name threads by the same id as the interpreter
        # (macOS) the interpreter's own thread list is all there is. Main
        # thread first, then the other interpreter threads, then threads the
        # OS knows but the interpreter does not (on Windows those can have
        # the lowest ids).
        def order(tid: int) -> tuple[int, int]:
            if tid not in remote:
                return (2, tid)
            return (0 if remote[tid][1] & ThreadStatus.MAIN_THREAD else 1, tid)

        tids = sorted(os_threads or remote, key=order)
        threads: list[Thread] = []
        for tid in tids:
            tstat = os_threads.get(tid)
            interp, status, frames = remote.get(tid, (0, ThreadStatus.UNKNOWN, ()))
            if tid in remote and tstat is not None and tstat.state:
                # In wall mode the unwinder does not check CPU state and
                # flags it UNKNOWN. The OS already told us, so fill it in.
                status &= ~ThreadStatus.UNKNOWN
                if tstat.state == "running":
                    status |= ThreadStatus.ON_CPU
            cpu_percent = None
            if tstat is not None:
                cpu_percent, self._thread_cpu[tid] = _cpu_percent(
                    self._thread_cpu.get(tid), now, tstat.utime + tstat.stime
                )
            threads.append(
                Thread(
                    tid=tid,
                    name=tstat.name if tstat else "",
                    interpreter_id=interp,
                    status=status,
                    state=tstat.state if tstat else "",
                    user_time=tstat.utime if tstat else 0.0,
                    system_time=tstat.stime if tstat else 0.0,
                    cpu_percent=cpu_percent,
                    frames=frames,
                    syscall=decode_syscall(tstat.syscall, files_by_fd) if tstat else None,
                )
            )
        for gone in set(self._thread_cpu) - set(tids):
            del self._thread_cpu[gone]

        task_list: tuple[Task, ...] = ()
        if tasks and inspector is not None:
            try:
                with self._lock:
                    task_list = inspector.sample(tasks=True).tasks()
            except ProcessExited:
                raise
            except Exception as e:
                errors["tasks"] = f"{type(e).__name__}: {e}"

        gc_list: tuple[GCGeneration, ...] = ()
        if gc and inspector is not None:
            try:
                with self._lock:
                    records = inspector.gc_records()
                    now_ns = time.perf_counter_ns()
                gc_list = self._gc.update(records, now=time.monotonic(), now_ns=now_ns)
            except ProcessExited:
                raise
            except Exception as e:
                errors["gc"] = f"{type(e).__name__}: {e}"

        return Snapshot(
            timestamp=time.time(),
            process=process,
            threads=tuple(threads),
            tasks=task_list,
            gc=gc_list,
            children=child_list,
            ipc=ipc_info,
            errors=errors,
        )

    def _children(self, now: float) -> tuple[ChildProcess, ...]:
        stats = self._stats.children()
        seen = {c.pid for c in stats}
        for gone in set(self._child_cpu) - seen:
            del self._child_cpu[gone]
        for gone in set(self._child_python) - seen:
            del self._child_python[gone]
        out: list[ChildProcess] = []
        for c in stats:
            python = self._child_python.get(c.pid, False)
            if not python:
                # Without memory access fall back to the executable's name.
                python = is_python_process(c.pid) or (
                    self.limited is not None and osproc.looks_like_python(c.pid)
                )
                self._child_python[c.pid] = python
            cpu_percent, self._child_cpu[c.pid] = _cpu_percent(
                self._child_cpu.get(c.pid), now, c.utime + c.stime
            )
            out.append(
                ChildProcess(
                    pid=c.pid,
                    parent_pid=c.ppid,
                    name=c.name,
                    cmdline=c.cmdline,
                    python=python,
                    state=c.state,
                    rss=c.rss,
                    num_threads=c.num_threads,
                    user_time=c.utime,
                    system_time=c.stime,
                    uptime=max(time.time() - c.start_time, 0.0) if c.start_time else 0.0,
                    cpu_percent=cpu_percent,
                )
            )
        return tuple(out)

    def stream(self, interval: float = 1.0, **kwargs) -> Iterator[Snapshot]:
        """Yield snapshots forever, ``interval`` seconds apart, until the target exits."""
        while True:
            started = time.monotonic()
            yield self.snapshot(**kwargs)
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
