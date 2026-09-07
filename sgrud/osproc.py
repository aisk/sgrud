"""Process statistics from the operating system.

psutil supplies the process level figures (memory, CPU times, command line,
executable, state) on Linux, macOS and Windows. Per-thread data goes past
what psutil exposes, so it is platform specific:

- Linux: one read of ``/proc/<pid>/task/<tid>/stat`` gives CPU times, the
  scheduler state and the thread name. ``/proc/<pid>/status``,
  ``smaps_rollup``, ``maps`` and ``stat`` add the memory breakdown and
  page faults, and the cgroup files add the container's memory limit.
  Together those reads cost well under a millisecond.
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
from .models import Memory, MemoryLimits

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


#: How often the unique set size is refreshed on macOS and Windows, where
#: psutil walks every page of the working set to compute it.
USS_INTERVAL = 1.0


@dataclass(frozen=True, slots=True)
class ProcessStat:
    exe: str
    cmdline: tuple[str, ...]
    state: str
    utime: float
    stime: float
    num_threads: int
    memory: Memory
    page_faults: int = 0
    major_faults: int = 0
    limits: MemoryLimits = MemoryLimits()


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
        self._uss: tuple[float, int] = (float("-inf"), 0)
        self._cgroup: _CgroupFiles | None = _linux_cgroup_files(pid) if LINUX else None

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
        if LINUX:
            memory, faults, major = _linux_memory(self.pid, mem)
            limits = _linux_limits(self.pid, self._proc, self._cgroup)
        elif WINDOWS:
            memory = Memory(
                rss=mem.rss,
                vms=mem.vms,
                hwm=mem.peak_wset,
                swap=0,
                data=mem.private,
                shared=0,
                uss=self._uss_cached(),
            )
            faults, major, limits = mem.num_page_faults, 0, MemoryLimits()
        else:
            memory = Memory(
                rss=mem.rss, vms=mem.vms, hwm=0, swap=0, data=0, shared=0, uss=self._uss_cached()
            )
            faults, major, limits = mem.pfaults, mem.pageins, MemoryLimits()
        return ProcessStat(
            exe=exe,
            cmdline=cmdline,
            state=state,
            utime=cpu.user,
            stime=cpu.system,
            num_threads=num_threads,
            memory=memory,
            page_faults=faults,
            major_faults=major,
            limits=limits,
        )

    def _uss_cached(self) -> int:
        """The unique set size, refreshed at most every :data:`USS_INTERVAL`."""
        import time

        now = time.monotonic()
        stamp, value = self._uss
        if now - stamp >= USS_INTERVAL:
            try:
                value = self._proc.memory_full_info().uss
            except psutil.NoSuchProcess as e:
                raise ProcessLookupError(self.pid) from e
            except psutil.Error, OSError:
                value = 0
            self._uss = (now, value)
        return value

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
    memory_maps = getattr(proc, "memory_maps", None)  # psutil has none on macOS
    if memory_maps is None:
        return False
    try:
        for mapping in memory_maps():
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


def _kib_fields(text: str, wanted: tuple[str, ...]) -> dict[str, int]:
    """Values in bytes of the ``Key:  123 kB`` lines named in ``wanted``."""
    out = dict.fromkeys(wanted, 0)
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if sep and key in out:
            out[key] = int(rest.split()[0]) * 1024
    return out


def _linux_memory(pid: int, mem: Any) -> tuple[Memory, int, int]:
    """(Memory, page faults, major faults) from /proc, on top of psutil's statm figures."""
    status = _kib_fields(
        _read(f"/proc/{pid}/status"),
        ("VmHWM", "VmSwap", "VmPeak", "RssAnon", "RssFile", "RssShmem"),
    )
    try:
        rollup = _kib_fields(
            _read(f"/proc/{pid}/smaps_rollup"),
            ("Pss", "Private_Clean", "Private_Dirty", "AnonHugePages"),
        )
    except ProcessLookupError:
        if not os.path.exists(f"/proc/{pid}"):
            raise
        rollup = {}  # kernels before 4.14
    brk, anon_mapped = _linux_maps(_read(f"/proc/{pid}/maps"))
    faults, major = _linux_faults(_read(f"/proc/{pid}/stat"))
    memory = Memory(
        rss=mem.rss,
        vms=mem.vms,
        hwm=status["VmHWM"],
        swap=status["VmSwap"],
        data=mem.data,
        shared=mem.shared,
        uss=rollup.get("Private_Clean", 0) + rollup.get("Private_Dirty", 0),
        pss=rollup.get("Pss", 0),
        anon=status["RssAnon"],
        file=status["RssFile"],
        shmem=status["RssShmem"],
        brk=brk,
        anon_mapped=anon_mapped,
        huge=rollup.get("AnonHugePages", 0),
        peak_vms=status["VmPeak"],
    )
    return memory, faults, major


def _linux_maps(text: str) -> tuple[int, int]:
    """(brk heap size, other anonymous private writable mappings) from /proc/<pid>/maps."""
    brk = anon = 0
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5:
            continue
        perms = fields[1]
        path = fields[5].strip() if len(fields) > 5 else ""
        if path == "[heap]":
            lo, _, hi = fields[0].partition("-")
            brk += int(hi, 16) - int(lo, 16)
        elif not path.startswith("/") and path != "[stack]" and "rw" in perms and "p" in perms:
            # inode 0 marks anonymous memory. Named pseudo mappings other
            # than the stack ([anon:...] names, [vvar]) are anonymous too.
            if fields[4] == "0":
                lo, _, hi = fields[0].partition("-")
                anon += int(hi, 16) - int(lo, 16)
    return brk, anon


def _linux_faults(stat: str) -> tuple[int, int]:
    """(minor + major, major) page faults from /proc/<pid>/stat."""
    fields = stat[stat.rindex(")") + 2 :].split()
    # fields[0] is field 3 (state); minflt is field 10, majflt field 12.
    minor, major = int(fields[7]), int(fields[9])
    return minor + major, major


@dataclass(frozen=True, slots=True)
class _CgroupFiles:
    limit: str
    high: str
    usage: str


def _linux_cgroup_files(pid: int) -> _CgroupFiles | None:
    """Paths of the memory limit files of the process's cgroup, if any.

    Handles cgroup v2 (one ``0::/path`` line) and v1 (a line naming the
    ``memory`` controller). Resolved once per process; a process rarely
    moves between cgroups. Returns None when the files are not visible,
    which is the case for the root cgroup and for a container watched
    from outside its cgroup namespace.
    """
    try:
        text = _read(f"/proc/{pid}/cgroup")
    except ProcessLookupError:
        return None
    for line in text.splitlines():
        _, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if controllers == "":
            base = f"/sys/fs/cgroup{path}"
            files = _CgroupFiles(
                f"{base}/memory.max", f"{base}/memory.high", f"{base}/memory.current"
            )
        elif "memory" in controllers.split(","):
            base = f"/sys/fs/cgroup/memory{path}"
            files = _CgroupFiles(
                f"{base}/memory.limit_in_bytes", "", f"{base}/memory.usage_in_bytes"
            )
        else:
            continue
        if os.path.exists(files.limit):
            return files
    return None


def _cgroup_bytes(path: str) -> int:
    """A cgroup byte figure, 0 when unset, unlimited or unreadable."""
    if not path:
        return 0
    try:
        with open(path) as f:
            text = f.read().strip()
    except OSError:
        return 0
    if text == "max":
        return 0
    value = int(text)
    # cgroup v1 spells "unlimited" as PAGE_COUNTER_MAX, a number near 2**63.
    return 0 if value >= 1 << 62 else value


def _linux_limits(pid: int, proc: psutil.Process, cgroup: _CgroupFiles | None) -> MemoryLimits:
    import resource

    address_space = 0
    try:
        soft, _ = proc.rlimit(resource.RLIMIT_AS)
        # RLIM_INFINITY comes back as -1 or as its unsigned spelling.
        address_space = 0 if soft < 0 or soft >= 1 << 62 else soft
    except psutil.NoSuchProcess as e:
        raise ProcessLookupError(pid) from e
    except psutil.Error, OSError:
        pass
    try:
        oom_score = int(_read(f"/proc/{pid}/oom_score").strip())
    except ProcessLookupError, ValueError:
        oom_score = -1
    if cgroup is None:
        return MemoryLimits(address_space=address_space, oom_score=oom_score)
    return MemoryLimits(
        cgroup_limit=_cgroup_bytes(cgroup.limit),
        cgroup_high=_cgroup_bytes(cgroup.high),
        cgroup_usage=_cgroup_bytes(cgroup.usage),
        address_space=address_space,
        oom_score=oom_score,
    )


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
