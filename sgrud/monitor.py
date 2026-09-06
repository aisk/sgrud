"""High level entry point: attach to (or spawn) a process and take snapshots."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from . import procfs
from .errors import AttachError, ProcessExited
from .models import Process, Snapshot, Thread, ThreadStatus
from .remote import RemoteInspector, is_python_process, permission_hint


@dataclass(slots=True)
class _CpuSample:
    wall: float
    cpu: float


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
        cache_frames: bool = True,
    ):
        procfs.ensure_supported()
        self.pid = pid
        self._child = child
        self._inspector: RemoteInspector | None = None
        #: Why the target's memory cannot be read, or None for full access.
        #: In limited mode only /proc based data (memory, CPU, thread names)
        #: is available; stacks, tasks, GC and hotspots are not.
        self.limited: str | None = None
        # The unwinder keeps per-call caches, so serialize access to it when a
        # background Sampler and the UI thread share one Monitor.
        self._lock = threading.Lock()
        self._inspector_opts = dict(
            native=native_frames, gc_markers=gc_markers, cache_frames=cache_frames
        )
        self._proc_cpu: _CpuSample | None = None
        self._thread_cpu: dict[int, _CpuSample] = {}
        self._boot_uptime = procfs.uptime()
        self._boot_wall = time.time()
        try:
            self._start = procfs.read_stat(pid).starttime
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
        """
        procfs.ensure_supported()
        if not os.path.isdir(f"/proc/{pid}"):
            raise ProcessExited(pid)
        if not procfs.can_read_memory(pid):
            # is_python_process() also needs to read memory and would report
            # a perfectly good interpreter as "not Python", so check first.
            hint = permission_hint()
            if require_full or not procfs.looks_like_python(pid):
                raise AttachError(pid, "permission denied", hint)
            monitor = cls(pid, **options)
            monitor.limited = hint
            return monitor
        if not is_python_process(pid):
            exe = ""
            try:
                exe = procfs.exe(pid)
            except ProcessLookupError:
                raise ProcessExited(pid) from None
            raise AttachError(
                pid,
                f"{exe or 'process'} does not look like a CPython interpreter",
                "sgrud can only inspect CPython processes of the same version as itself.",
            )
        monitor = cls(pid, **options)
        deadline = time.monotonic() + retry
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
        before the first attach attempt.
        """
        child = subprocess.Popen(list(argv))
        deadline = time.monotonic() + max(settle, 0.05) + 5.0
        time.sleep(settle)
        last: Exception | None = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise ProcessExited(child.pid, child.returncode)
            try:
                monitor = cls(child.pid, child=child, **options)
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
        if self._inspector is not None:
            self._inspector.close()
            self._inspector = None
        if self._child is not None and kill_child and self._child.poll() is None:
            self._child.kill()
            self._child.wait()

    def __enter__(self) -> Monitor:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get_inspector(self) -> RemoteInspector:
        if self.limited is not None:
            raise AttachError(self.pid, "limited mode", self.limited)
        with self._lock:
            if self._inspector is None:
                self._inspector = RemoteInspector(self.pid, **self._inspector_opts)
            return self._inspector

    def sample_stacks(self) -> dict[int, tuple[int, ThreadStatus, tuple]]:
        """Read only the Python stacks, as fast as possible.

        Returns a mapping of OS thread id to (interpreter_id, status, frames).
        This is what a profiler wants to call hundreds of times per second.
        Raises ProcessExited when the target is gone.
        """
        inspector = self._get_inspector()
        with self._lock:
            try:
                return inspector.stacks()
            except ProcessExited:
                raise
            except Exception:
                self._check_alive()
                raise

    def _check_alive(self) -> None:
        if self._child is not None and self._child.poll() is not None:
            raise ProcessExited(self.pid, self._child.returncode)
        if not os.path.isdir(f"/proc/{self.pid}"):
            raise ProcessExited(self.pid)

    # -- sampling ------------------------------------------------------

    def _cpu_percent(self, key: int | None, now: float, cpu_seconds: float) -> float | None:
        store = self._thread_cpu
        prev = self._proc_cpu if key is None else store.get(key)
        sample = _CpuSample(now, cpu_seconds)
        if key is None:
            self._proc_cpu = sample
        else:
            store[key] = sample
        if prev is None or now <= prev.wall:
            return None
        return max(0.0, (cpu_seconds - prev.cpu) / (now - prev.wall) * 100.0)

    def snapshot(
        self,
        *,
        stacks: bool = True,
        tasks: bool = True,
        gc: bool = True,
    ) -> Snapshot:
        """Collect one snapshot. Sections that fail are reported in ``errors``."""
        self._check_alive()
        now = time.monotonic()
        errors: dict[str, str] = {}
        try:
            stat = procfs.read_stat(self.pid)
            memory = procfs.read_memory(self.pid)
            tids = procfs.list_tids(self.pid)
            cmd = procfs.cmdline(self.pid)
            exe = procfs.exe(self.pid)
        except ProcessLookupError as e:
            raise ProcessExited(self.pid) from e

        uptime = (self._boot_uptime + (time.time() - self._boot_wall)) - self._start
        process = Process(
            pid=self.pid,
            exe=exe,
            cmdline=cmd,
            state=stat.state,
            num_threads=stat.num_threads,
            memory=memory,
            user_time=stat.utime,
            system_time=stat.stime,
            uptime=max(uptime, 0.0),
            cpu_percent=self._cpu_percent(None, now, stat.utime + stat.stime),
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
                    remote = inspector.stacks()
            except ProcessExited:
                raise
            except Exception as e:
                errors["stacks"] = f"{type(e).__name__}: {e}"

        threads: list[Thread] = []
        for tid in tids:
            try:
                tstat = procfs.read_stat(self.pid, tid)
            except ProcessLookupError:
                continue  # thread finished between listing and reading
            interp, status, frames = remote.get(tid, (0, ThreadStatus.UNKNOWN, ()))
            if tid in remote:
                # In wall mode the unwinder does not check CPU state and
                # flags it UNKNOWN. /proc already told us, so fill it in.
                status &= ~ThreadStatus.UNKNOWN
                if tstat.state == "R":
                    status |= ThreadStatus.ON_CPU
            threads.append(
                Thread(
                    tid=tid,
                    name=procfs.thread_name(self.pid, tid) or tstat.comm,
                    interpreter_id=interp,
                    status=status,
                    state=tstat.state,
                    user_time=tstat.utime,
                    system_time=tstat.stime,
                    cpu_percent=self._cpu_percent(tid, now, tstat.utime + tstat.stime),
                    frames=frames,
                )
            )
        for gone in set(self._thread_cpu) - set(tids):
            del self._thread_cpu[gone]

        task_list = ()
        if tasks and inspector is not None:
            try:
                with self._lock:
                    task_list = inspector.tasks()
            except ProcessExited:
                raise
            except Exception as e:
                errors["tasks"] = f"{type(e).__name__}: {e}"

        gc_list = ()
        if gc and inspector is not None:
            try:
                with self._lock:
                    gc_list = inspector.gc()
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
            errors=errors,
        )

    def stream(self, interval: float = 1.0, **kwargs) -> Iterator[Snapshot]:
        """Yield snapshots forever, ``interval`` seconds apart, until the target exits."""
        while True:
            started = time.monotonic()
            yield self.snapshot(**kwargs)
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
