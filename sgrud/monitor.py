"""High level entry point: attach to (or spawn) a process and take snapshots."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from . import osproc
from .errors import AttachError, ProcessExited
from .models import Process, Snapshot, Task, Thread, ThreadStatus
from .remote import RemoteInspector, access_hint, interpreter_pid


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
        osproc.ensure_supported()
        self.pid = pid
        self._child = child
        self._inspector: RemoteInspector | None = None
        #: Why the target's memory cannot be read, or None for full access.
        #: In limited mode only what the OS reports (memory, CPU, thread
        #: names) is available; stacks, tasks, GC and hotspots are not.
        self.limited: str | None = None
        # The unwinder keeps per-call caches, so serialize access to it when a
        # background Sampler and the UI thread share one Monitor.
        self._lock = threading.Lock()
        self._inspector_opts = dict(
            native=native_frames, gc_markers=gc_markers, cache_frames=cache_frames
        )
        self._proc_cpu: _CpuSample | None = None
        self._thread_cpu: dict[int, _CpuSample] = {}
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
        osproc.ensure_supported()
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

    def sample_tasks(self) -> tuple[Task, ...]:
        """Read only the asyncio tasks, as fast as possible.

        The async-mode counterpart of :meth:`sample_stacks`. Returns an
        empty tuple when the target has not imported asyncio. Raises
        ProcessExited when the target is gone.
        """
        inspector = self._get_inspector()
        with self._lock:
            try:
                return inspector.tasks(retries=1)
            except ProcessExited:
                raise
            except Exception:
                self._check_alive()
                raise

    def _check_alive(self) -> None:
        if self._child is not None and self._child.poll() is not None:
            raise ProcessExited(self.pid, self._child.returncode)
        if not self._stats.is_running():
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
            stat = self._stats.process()
            os_threads = self._stats.threads()
        except ProcessLookupError as e:
            raise ProcessExited(self.pid) from e

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
                cpu_percent = self._cpu_percent(tid, now, tstat.utime + tstat.stime)
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
