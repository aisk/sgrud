"""Readers for the Linux ``/proc`` filesystem.

Only the handful of files sgrud needs are covered. All functions raise
:class:`ProcessLookupError` when the process or thread has gone away so
callers can translate that into :class:`sgrud.errors.ProcessExited`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .errors import NotSupported
from .models import Memory

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def ensure_supported() -> None:
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc"):
        raise NotSupported("sgrud process statistics require Linux /proc")


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError) as e:
        raise ProcessLookupError(path) from e


@dataclass(frozen=True, slots=True)
class StatLine:
    """Selected fields of ``/proc/<pid>/stat`` or ``/proc/<pid>/task/<tid>/stat``."""

    comm: str
    state: str
    utime: float
    stime: float
    num_threads: int
    starttime: float


def read_stat(pid: int, tid: int | None = None) -> StatLine:
    path = f"/proc/{pid}/stat" if tid is None else f"/proc/{pid}/task/{tid}/stat"
    raw = _read(path)
    # comm may contain spaces and parens, so split around the last ')'.
    lparen = raw.index("(")
    rparen = raw.rindex(")")
    comm = raw[lparen + 1 : rparen]
    fields = raw[rparen + 2 :].split()
    # fields[0] is field 3 (state) of the documented layout.
    return StatLine(
        comm=comm,
        state=fields[0],
        utime=int(fields[11]) / _CLK_TCK,
        stime=int(fields[12]) / _CLK_TCK,
        num_threads=int(fields[17]),
        starttime=int(fields[19]) / _CLK_TCK,
    )


def read_memory(pid: int) -> Memory:
    values: dict[str, int] = {}
    for line in _read(f"/proc/{pid}/status").splitlines():
        key, sep, rest = line.partition(":")
        if sep and rest.strip().endswith("kB"):
            values[key] = int(rest.split()[0]) * 1024
    return Memory(
        rss=values.get("VmRSS", 0),
        vms=values.get("VmSize", 0),
        hwm=values.get("VmHWM", 0),
        swap=values.get("VmSwap", 0),
        data=values.get("VmData", 0),
        shared=values.get("RssFile", 0) + values.get("RssShmem", 0),
    )


def list_tids(pid: int) -> list[int]:
    try:
        names = os.listdir(f"/proc/{pid}/task")
    except FileNotFoundError as e:
        raise ProcessLookupError(pid) from e
    return sorted(int(n) for n in names if n.isdigit())


def thread_name(pid: int, tid: int) -> str:
    try:
        return _read(f"/proc/{pid}/task/{tid}/comm").strip()
    except ProcessLookupError:
        return ""


def cmdline(pid: int) -> tuple[str, ...]:
    raw = _read(f"/proc/{pid}/cmdline")
    return tuple(part for part in raw.split("\0") if part)


def exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except (FileNotFoundError, ProcessLookupError) as e:
        raise ProcessLookupError(pid) from e
    except PermissionError:
        return ""


def looks_like_python(pid: int) -> bool:
    """Cheap guess from the executable name and mapped libraries.

    Used when the target's memory cannot be read, which is what the
    authoritative check in ``_remote_debugging`` needs.
    """
    try:
        name = os.path.basename(exe(pid)).lower()
    except ProcessLookupError:
        return False
    if name.startswith("python"):
        return True
    try:
        with open(f"/proc/{pid}/maps") as maps:
            for line in maps:
                if "libpython" in line:
                    return True
    except OSError:
        pass
    return False


def uptime() -> float:
    """Seconds since boot, the reference for ``StatLine.starttime``."""
    return float(_read("/proc/uptime").split()[0])


def can_read_memory(pid: int) -> bool:
    """Whether ``/proc/<pid>/mem`` is readable, which is what attaching needs.

    Yama's ``ptrace_scope`` and ``CAP_SYS_PTRACE`` both surface here.
    """
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
    try:
        return int(_read("/proc/sys/kernel/yama/ptrace_scope").strip())
    except (OSError, ValueError):
        return None
