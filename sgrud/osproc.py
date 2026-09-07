"""Process statistics from the operating system.

psutil supplies the process level figures (memory, CPU times, command line,
executable, state) on Linux, macOS and Windows. Per-thread data goes past
what psutil exposes, so it is platform specific:

- Linux: one read of ``/proc/<pid>/task/<tid>/stat`` gives CPU times, the
  scheduler state and the thread name. ``/proc/<pid>/status``,
  ``smaps_rollup``, ``maps`` and ``stat`` add the memory breakdown and
  page faults, and the cgroup files add the container's memory limit,
  CPU quota and throttling, OOM kills and pid limit. Together those reads
  cost well under a millisecond.
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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import psutil

from .errors import NotSupported
from .models import IPC, Cgroup, Memory, MemoryLimits

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
#: How often the socket addresses of the ipc section are refreshed. psutil
#: parses the system wide ``/proc/net`` tables for them, a few milliseconds.
CONNECTIONS_INTERVAL = 1.0


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
    cgroup: Cgroup = Cgroup()
    cpus_allowed: int = 0


@dataclass(frozen=True, slots=True)
class ChildStat:
    pid: int
    ppid: int
    name: str
    cmdline: tuple[str, ...]
    state: str
    rss: int
    num_threads: int
    utime: float
    stime: float
    #: Epoch seconds at which the child started.
    start_time: float


@dataclass(frozen=True, slots=True)
class ThreadStat:
    tid: int
    name: str
    #: Scheduler state, "" when the platform does not report one.
    state: str
    utime: float
    stime: float
    #: The raw ``/proc/<pid>/task/<tid>/syscall`` line, "" where unreadable
    #: or unsupported. See :func:`sgrud.ipc.decode_syscall`.
    syscall: str = ""


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
        self._connections: tuple[float, list[Any]] = (float("-inf"), [])
        self._cgroup: _CgroupFiles | None = _linux_cgroup_files(pid) if LINUX else None

    def is_running(self) -> bool:
        """Whether the process exists and its pid has not been recycled."""
        return self._proc.is_running()

    def parent_pid(self) -> int:
        try:
            return self._proc.ppid()
        except psutil.NoSuchProcess as e:
            raise ProcessLookupError(self.pid) from e
        except psutil.Error:
            return 0

    def ipc(self, related: Iterable[int] = ()) -> IPC:
        """Open descriptors, locks and shared memory, see :func:`sgrud.ipc.read_ipc`.

        Socket addresses are refreshed at most every :data:`CONNECTIONS_INTERVAL`.
        """
        from .ipc import connections, read_ipc

        def cached(proc: psutil.Process) -> list[Any]:
            import time

            now = time.monotonic()
            stamp, value = self._connections
            if now - stamp >= CONNECTIONS_INTERVAL:
                value = connections(proc)
                self._connections = (now, value)
            return value

        return read_ipc(self._proc, related, cached)

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
        cgroup, cpus_allowed = Cgroup(), 0
        if LINUX:
            memory, faults, major = _linux_memory(self.pid, mem)
            limits = _linux_limits(self.pid, self._proc, self._cgroup)
            cgroup = _linux_cgroup(self._cgroup)
            cpus_allowed = _linux_cpus_allowed(self.pid)
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
            cgroup=cgroup,
            cpus_allowed=cpus_allowed,
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

    def children(self) -> list[ChildStat]:
        """Every live descendant, parents before children. Zombies are left out."""
        try:
            procs = self._proc.children(recursive=True)
        except psutil.NoSuchProcess as e:
            raise ProcessLookupError(self.pid) from e
        out: list[ChildStat] = []
        for p in procs:
            try:
                with p.oneshot():
                    state = p.status()
                    if state == psutil.STATUS_ZOMBIE:
                        continue
                    cpu = p.cpu_times()
                    out.append(
                        ChildStat(
                            pid=p.pid,
                            ppid=p.ppid(),
                            name=_optional(p.name, ""),
                            cmdline=tuple(_optional(p.cmdline, ())),
                            state=state,
                            rss=p.memory_info().rss,
                            num_threads=_optional(p.num_threads, 0),
                            utime=cpu.user,
                            stime=cpu.system,
                            start_time=p.create_time(),
                        )
                    )
            except psutil.NoSuchProcess, psutil.ZombieProcess:
                continue
            except psutil.AccessDenied:
                # A setuid child, say. Still worth listing.
                out.append(ChildStat(p.pid, self.pid, "", (), "", 0, 0, 0.0, 0.0, 0.0))
        return out

    def threads(self, *, syscalls: bool = True) -> dict[int, ThreadStat]:
        """OS threads keyed by the id ``_remote_debugging`` uses for them.

        Empty where the platform cannot provide matching ids (macOS).
        ``syscalls`` asks for the system call each sleeping thread is in,
        which Linux reports when the target's memory is readable.
        """
        if LINUX:
            return _linux_threads(self.pid, syscalls)
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
    #: The cgroup's path as ``/proc/<pid>/cgroup`` spells it.
    path: str
    limit: str
    high: str
    usage: str
    #: The cgroup's directory on a v2 hierarchy, where the cpu, memory
    #: event and pid files live. "" on v1, which keeps them elsewhere.
    v2: str = ""


def _linux_cgroup_files(pid: int) -> _CgroupFiles | None:
    """Paths of the memory limit files of the process's cgroup, if any.

    Handles cgroup v2 (one ``0::/path`` line) and v1 (a line naming the
    ``memory`` controller). Resolved once per process; a process rarely
    moves between cgroups. Returns None when the cgroup's directory is
    not visible, which is the case for a container watched from outside
    its cgroup namespace. The root cgroup has no limit files, so reading
    them yields 0 like an unlimited cgroup.
    """
    try:
        text = _read(f"/proc/{pid}/cgroup")
    except ProcessLookupError:
        return None
    for line in text.splitlines():
        _, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if controllers == "":
            base = "/sys/fs/cgroup" + path.rstrip("/")
            files = _CgroupFiles(
                path, f"{base}/memory.max", f"{base}/memory.high", f"{base}/memory.current", base
            )
        elif "memory" in controllers.split(","):
            base = "/sys/fs/cgroup/memory" + path.rstrip("/")
            files = _CgroupFiles(
                path, f"{base}/memory.limit_in_bytes", "", f"{base}/memory.usage_in_bytes"
            )
        else:
            continue
        # cgroup.controllers exists in every v2 cgroup, the root included,
        # and tells a v2 mount from the unified hierarchy of a hybrid
        # layout that has no controllers of its own.
        marker = f"{base}/cgroup.controllers" if files.v2 else base
        if os.path.exists(marker):
            return files
    return None


def _cgroup_text(path: str) -> str:
    """A cgroup file's content, "" where it does not exist or cannot be read."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _cgroup_counters(text: str) -> dict[str, int]:
    """The ``key value`` lines of a cgroup stat file."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            out[key] = int(value)
    return out


def _cpu_quota(text: str) -> float:
    """Cores from a ``cpu.max`` line, ``$QUOTA $PERIOD`` or ``max $PERIOD``."""
    quota, _, period = text.strip().partition(" ")
    if not quota.isdigit() or not period.isdigit() or int(period) == 0:
        return 0.0
    return int(quota) / int(period)


def _pids_max(text: str) -> int:
    text = text.strip()
    return int(text) if text.isdigit() else 0


def _linux_cgroup(files: _CgroupFiles | None) -> Cgroup:
    """The cgroup's CPU quota, throttling, memory events and pid figures.

    Only cgroup v2 keeps these in the cgroup's own directory. Missing
    files, a controller not enabled for this cgroup say, leave the
    figures at their unknown values.
    """
    if files is None:
        return Cgroup()
    if not files.v2:
        return Cgroup(path=files.path)
    base = files.v2
    cpu = _cgroup_counters(_cgroup_text(f"{base}/cpu.stat"))
    events = _cgroup_counters(_cgroup_text(f"{base}/memory.events"))
    return Cgroup(
        path=files.path,
        cpu_quota=_cpu_quota(_cgroup_text(f"{base}/cpu.max")),
        periods=cpu.get("nr_periods", 0),
        throttled=cpu.get("nr_throttled", 0),
        throttled_time=cpu.get("throttled_usec", 0) / 1e6,
        oom_kills=events.get("oom_kill", -1),
        limit_hits=events.get("max", -1),
        high_hits=events.get("high", -1),
        pids_max=_pids_max(_cgroup_text(f"{base}/pids.max")),
        pids_current=_pids_max(_cgroup_text(f"{base}/pids.current")),
    )


def _cpu_list_size(text: str) -> int:
    """How many CPUs a list like ``0-3,8,10-11`` names."""
    count = 0
    for part in text.strip().split(","):
        low, _, high = part.partition("-")
        if not low.isdigit():
            continue
        count += int(high) - int(low) + 1 if high.isdigit() else 1
    return count


def _linux_cpus_allowed(pid: int) -> int:
    """The size of the affinity mask, from ``Cpus_allowed_list`` in status."""
    try:
        text = _read(f"/proc/{pid}/status")
    except ProcessLookupError:
        return 0
    for line in text.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return _cpu_list_size(line.partition(":")[2])
    return 0


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


def _linux_threads(pid: int, syscalls: bool = True) -> dict[int, ThreadStat]:
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
        state = _LINUX_STATES.get(fields[0], fields[0])
        syscall = ""
        if state != "running" and syscalls:
            # Needs ptrace access like reading memory does. Denied reads
            # come back as PermissionError, so stop asking after the first.
            try:
                with open(f"/proc/{pid}/task/{tid}/syscall") as f:
                    syscall = f.read().strip()
            except PermissionError:
                syscalls = False
            except OSError:
                pass
        out[tid] = ThreadStat(
            tid=tid,
            name=raw[lparen + 1 : rparen],
            state=state,
            utime=int(fields[11]) / _CLK_TCK,
            stime=int(fields[12]) / _CLK_TCK,
            syscall=syscall,
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
