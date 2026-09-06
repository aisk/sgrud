"""Process statistics from the operating system.

psutil supplies the process level figures (memory, CPU times, command line,
executable, state) on Linux, macOS and Windows. Per-thread data goes past
what psutil exposes, so it is platform specific:

- Linux: one read of ``/proc/<pid>/task/<tid>/stat`` gives CPU times, the
  scheduler state and the thread name, and ``/proc/<pid>/status`` adds
  peak RSS and swap.
- Windows: psutil lists threads with CPU times and ``GetThreadDescription``
  adds the name. Windows has no cheap per-thread scheduler state.
- macOS: psutil numbers threads by index rather than by the id that
  ``_remote_debugging`` reports, so there are no per-thread figures at all
  and the thread list comes from the interpreter instead.

All functions raise :class:`ProcessLookupError` when the process has gone
away so callers can translate that into :class:`sgrud.errors.ProcessExited`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psutil

from .errors import NotSupported
from .models import Memory

LINUX = sys.platform.startswith("linux")
MACOS = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"

#: Whether :meth:`ProcessStats.threads` returns anything on this platform.
HAS_THREAD_STATS = not MACOS

_CLK_TCK = os.sysconf("SC_CLK_TCK") if LINUX else 100

# Linux scheduler state letters, spelled the way psutil spells process states.
_LINUX_STATES = {
    "R": psutil.STATUS_RUNNING,
    "S": psutil.STATUS_SLEEPING,
    "D": psutil.STATUS_DISK_SLEEP,
    "T": psutil.STATUS_STOPPED,
    "t": psutil.STATUS_TRACING_STOP,
    "Z": psutil.STATUS_ZOMBIE,
    "X": psutil.STATUS_DEAD,
    "x": psutil.STATUS_DEAD,
    "K": "wake-kill",
    "W": psutil.STATUS_WAKING,
    "P": psutil.STATUS_PARKED,
    "I": psutil.STATUS_IDLE,
}


def ensure_supported() -> None:
    if not (LINUX or MACOS or WINDOWS):
        raise NotSupported(f"sgrud supports Linux, macOS and Windows, not {sys.platform}")


@dataclass(frozen=True, slots=True)
class ProcessStat:
    exe: str
    cmdline: tuple[str, ...]
    state: str
    utime: float
    stime: float
    num_threads: int
    memory: Memory


@dataclass(frozen=True, slots=True)
class ThreadStat:
    tid: int
    name: str
    #: Scheduler state, "" when the platform does not report one.
    state: str
    utime: float
    stime: float


def pid_exists(pid: int) -> bool:
    return psutil.pid_exists(pid)


def _optional(fn: Callable[[], Any], default: Any) -> Any:
    """Call ``fn`` and swallow the failures that leave the process alive."""
    try:
        return fn()
    except psutil.AccessDenied, psutil.ZombieProcess:
        return default


class ProcessStats:
    """A handle on one process that is cheap to query repeatedly."""

    def __init__(self, pid: int):
        self.pid = pid
        try:
            self._proc = psutil.Process(pid)
            #: Epoch seconds at which the process started.
            self.start_time: float = self._proc.create_time()
        except psutil.NoSuchProcess as e:
            raise ProcessLookupError(pid) from e

    def is_running(self) -> bool:
        """Whether the process exists and its pid has not been recycled."""
        return self._proc.is_running()

    def process(self) -> ProcessStat:
        p = self._proc
        try:
            with p.oneshot():
                state = p.status()
                if state == psutil.STATUS_ZOMBIE:
                    raise ProcessLookupError(self.pid)
                cpu = p.cpu_times()
                mem = p.memory_info()
                num_threads = p.num_threads()
                exe = _optional(p.exe, "")
                cmdline = tuple(_optional(p.cmdline, ()))
        except psutil.NoSuchProcess as e:
            raise ProcessLookupError(self.pid) from e
        return ProcessStat(
            exe=exe,
            cmdline=cmdline,
            state=state,
            utime=cpu.user,
            stime=cpu.system,
            num_threads=num_threads,
            memory=_memory(self.pid, mem),
        )

    def threads(self) -> dict[int, ThreadStat]:
        """OS threads keyed by the id ``_remote_debugging`` uses for them.

        Empty where the platform cannot provide matching ids (macOS).
        """
        if LINUX:
            return _linux_threads(self.pid)
        if not HAS_THREAD_STATS:
            return {}
        try:
            raw = self._proc.threads()
        except psutil.NoSuchProcess as e:
            raise ProcessLookupError(self.pid) from e
        except psutil.AccessDenied:
            return {}
        return {
            t.id: ThreadStat(t.id, _windows_thread_name(t.id), "", t.user_time, t.system_time)
            for t in raw
        }


def _memory(pid: int, mem: Any) -> Memory:
    if LINUX:
        hwm, swap = _linux_status_memory(pid)
        return Memory(
            rss=mem.rss, vms=mem.vms, hwm=hwm, swap=swap, data=mem.data, shared=mem.shared
        )
    if WINDOWS:
        return Memory(
            rss=mem.rss, vms=mem.vms, hwm=mem.peak_wset, swap=0, data=mem.private, shared=0
        )
    return Memory(rss=mem.rss, vms=mem.vms, hwm=0, swap=0, data=0, shared=0)


def exe(pid: int) -> str:
    try:
        return psutil.Process(pid).exe()
    except psutil.NoSuchProcess as e:
        raise ProcessLookupError(pid) from e
    except psutil.AccessDenied:
        return ""


def looks_like_python(pid: int) -> bool:
    """Cheap guess from the executable name and mapped libraries.

    Used when the target's memory cannot be read, which is what the
    authoritative check in ``_remote_debugging`` needs.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    name = os.path.basename(_optional(proc.exe, "")).lower()
    if name.startswith("python"):
        return True
    try:
        for mapping in proc.memory_maps():
            base = os.path.basename(mapping.path).lower()
            if "python" in base and any(ext in base for ext in (".so", ".dll", ".dylib")):
                return True
    except psutil.Error:
        pass
    return False


def can_read_memory(pid: int) -> bool | None:
    """Whether the target's memory is readable, or None when only attaching can tell.

    On Linux a denied ptrace (Yama ``ptrace_scope``, missing
    ``CAP_SYS_PTRACE``) does not surface as a PermissionError from
    ``_remote_debugging``, it just fails to find the interpreter, so probe
    ``/proc/<pid>/mem`` directly. macOS and Windows raise PermissionError
    from the attach itself.
    """
    if not LINUX:
        return None
    try:
        with open(f"/proc/{pid}/maps") as maps:
            first = maps.readline()
        start = int(first.split("-", 1)[0], 16)
        fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        try:
            os.pread(fd, 1, start)
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def ptrace_scope() -> int | None:
    """Linux Yama ``ptrace_scope`` setting, or None where it does not apply."""
    if not LINUX:
        return None
    try:
        return int(_read("/proc/sys/kernel/yama/ptrace_scope").strip())
    except OSError, ValueError:
        return None


# -- Linux -------------------------------------------------------------


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError) as e:
        raise ProcessLookupError(path) from e


def _linux_status_memory(pid: int) -> tuple[int, int]:
    """(VmHWM, VmSwap) in bytes, which psutil's cheap memory_info() lacks."""
    hwm = swap = 0
    for line in _read(f"/proc/{pid}/status").splitlines():
        key, _, rest = line.partition(":")
        if key == "VmHWM":
            hwm = int(rest.split()[0]) * 1024
        elif key == "VmSwap":
            swap = int(rest.split()[0]) * 1024
    return hwm, swap


def _linux_threads(pid: int) -> dict[int, ThreadStat]:
    try:
        names = os.listdir(f"/proc/{pid}/task")
    except FileNotFoundError as e:
        raise ProcessLookupError(pid) from e
    out: dict[int, ThreadStat] = {}
    for name in names:
        if not name.isdigit():
            continue
        tid = int(name)
        try:
            raw = _read(f"/proc/{pid}/task/{tid}/stat")
        except ProcessLookupError:
            continue  # thread finished between listing and reading
        # comm may contain spaces and parens, so split around the last ')'.
        lparen = raw.index("(")
        rparen = raw.rindex(")")
        fields = raw[rparen + 2 :].split()
        # fields[0] is field 3 (state) of the documented layout.
        out[tid] = ThreadStat(
            tid=tid,
            name=raw[lparen + 1 : rparen],
            state=_LINUX_STATES.get(fields[0], fields[0]),
            utime=int(fields[11]) / _CLK_TCK,
            stime=int(fields[12]) / _CLK_TCK,
        )
    return out


# -- Windows -----------------------------------------------------------

_kernel32: Any = None


def _windows_thread_name(tid: int) -> str:
    """The name set through ``SetThreadDescription``, which threading uses."""
    global _kernel32
    try:
        import ctypes
        from ctypes import wintypes

        if _kernel32 is None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # ty: ignore[unresolved-attribute]
            k32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.OpenThread.restype = wintypes.HANDLE
            k32.GetThreadDescription.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.LPWSTR))
            k32.GetThreadDescription.restype = ctypes.c_long  # HRESULT
            k32.LocalFree.argtypes = (wintypes.HLOCAL,)
            k32.LocalFree.restype = wintypes.HLOCAL
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            k32.CloseHandle.restype = wintypes.BOOL
            _kernel32 = k32
        k32 = _kernel32
        thread_query_limited_information = 0x0800
        handle = k32.OpenThread(thread_query_limited_information, False, tid)
        if not handle:
            return ""
        try:
            buf = wintypes.LPWSTR()
            if k32.GetThreadDescription(handle, ctypes.byref(buf)) < 0:
                return ""
            try:
                return buf.value or ""
            finally:
                k32.LocalFree(ctypes.cast(buf, ctypes.c_void_p))
        finally:
            k32.CloseHandle(handle)
    except OSError, AttributeError, ValueError:
        return ""
