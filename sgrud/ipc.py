"""What the target has open and shares with other processes.

Everything here is read from ``/proc`` on Linux and from psutil elsewhere,
without touching the target. The sources and what they cost:

- ``/proc/<pid>/fd``: one readlink and one lstat per descriptor, about
  20 microseconds each. Pipes, sockets, shared memory and files are told
  apart by the link text.
- ``/proc/locks``: every file lock in the system, a few lines each.
  The target's own locks are the ones carrying its pid, and a ``->``
  line with its pid means it is blocked waiting for the lock above.
- ``/proc/<pid>/maps``: the ``/dev/shm`` mappings, which is where POSIX
  shared memory and the semaphores behind ``multiprocessing`` live.
- ``/proc/<pid>/task/<tid>/syscall``: the system call a thread is
  blocked in with its arguments, which names the descriptor a thread
  waits on. Needs the same access as reading the target's memory.
- The descriptor tables of the target's parent and children, to say
  which of them hold the other end of a pipe.

Only facts are reported. Whether a ``futex`` wait is the GIL or a user
lock, or a ``read`` on a pipe is a stuck ``multiprocessing.Queue``, is for
the reader to conclude from the Python stack next to it.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Callable, Iterable
from typing import Any

import psutil

from .models import IPC, FileLock, OpenFile, SharedMapping, Syscall

LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"

#: Descriptors resolved per process. A server holding thousands of
#: sockets gets the lowest numbered ones listed and the rest counted.
MAX_FILES = 1000
#: Processes whose descriptor tables are searched for shared pipes and sockets.
MAX_RELATED = 64

# -- system calls --------------------------------------------------------

# Numbers on x86_64 and on the asm-generic table shared by aarch64,
# riscv64 and loongarch64. None where an architecture lacks the call.
# Listed are the calls a thread plausibly blocks in, plus the ones that
# show up during a read of a busy thread.
_SYSCALLS: dict[str, tuple[int | None, int | None]] = {
    "read": (0, 63),
    "write": (1, 64),
    "close": (3, 57),
    "poll": (7, None),
    "mmap": (9, 222),
    "munmap": (11, 215),
    "ioctl": (16, 29),
    "pread64": (17, 67),
    "pwrite64": (18, 68),
    "readv": (19, 65),
    "writev": (20, 66),
    "select": (23, None),
    "sched_yield": (24, 124),
    "madvise": (28, 233),
    "pause": (34, None),
    "nanosleep": (35, 101),
    "connect": (42, 203),
    "accept": (43, 202),
    "sendto": (44, 206),
    "recvfrom": (45, 207),
    "sendmsg": (46, 211),
    "recvmsg": (47, 212),
    "wait4": (61, 260),
    "semop": (65, 193),
    "msgsnd": (69, 189),
    "msgrcv": (70, 188),
    "fcntl": (72, 25),
    "flock": (73, 32),
    "fsync": (74, 82),
    "fdatasync": (75, 83),
    "rt_sigtimedwait": (128, 137),
    "rt_sigsuspend": (130, 133),
    "futex": (202, 98),
    "semtimedop": (220, 192),
    "clock_nanosleep": (230, 115),
    "epoll_wait": (232, None),
    "epoll_ctl": (233, 21),
    "mq_timedsend": (242, 182),
    "mq_timedreceive": (243, 183),
    "waitid": (247, 95),
    "openat": (257, 56),
    "pselect6": (270, 72),
    "ppoll": (271, 73),
    "epoll_pwait": (281, 22),
    "accept4": (288, 242),
    "io_uring_enter": (426, 426),
    "epoll_pwait2": (441, 441),
    "futex_waitv": (449, 449),
    "futex_wake": (454, 454),
    "futex_wait": (455, 455),
}

#: Calls whose first argument is a descriptor.
_FD_CALLS = frozenset(
    {
        "read",
        "write",
        "close",
        "ioctl",
        "pread64",
        "pwrite64",
        "readv",
        "writev",
        "connect",
        "accept",
        "sendto",
        "recvfrom",
        "sendmsg",
        "recvmsg",
        "fcntl",
        "flock",
        "fsync",
        "fdatasync",
        "epoll_wait",
        "epoll_pwait",
        "epoll_pwait2",
        "accept4",
        "io_uring_enter",
    }
)

_GENERIC_ARCHES = {"aarch64", "arm64", "riscv64", "loongarch64"}


def _syscall_names() -> dict[int, str]:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        column = 0
    elif machine in _GENERIC_ARCHES:
        column = 1
    else:
        return {}
    out: dict[int, str] = {}
    for name, nums in _SYSCALLS.items():
        number = nums[column]
        if number is not None:
            out[number] = name
    return out


#: System call number to name for this machine, empty on an architecture
#: without a table, where calls are reported by number only.
SYSCALL_NAMES: dict[int, str] = _syscall_names() if LINUX else {}


def decode_syscall(raw: str, files: dict[int, OpenFile] | None = None) -> Syscall | None:
    """Interpret one ``/proc/<pid>/task/<tid>/syscall`` line.

    The line is ``running`` for a thread on a CPU, ``-1 sp pc`` for one
    blocked outside a system call (a page fault, a signal) and otherwise
    the number, six arguments, stack pointer and program counter. Returns
    None for the first two and for an empty line.
    """
    fields = raw.split()
    if len(fields) < 2 or not fields[0].lstrip("-").isdigit():
        return None
    number = int(fields[0])
    if number < 0:
        return None
    args = tuple(int(a, 16) for a in fields[1:7])
    name = SYSCALL_NAMES.get(number, f"syscall {number}")
    fd = -1
    target = ""
    if name in _FD_CALLS and args:
        fd = args[0]
        if files is not None:
            f = files.get(fd)
            if f is not None:
                target = f.target
    return Syscall(name=name, number=number, args=args, fd=fd, target=target)


# -- descriptors -----------------------------------------------------------


def _kind(target: str) -> str:
    if target.startswith("pipe:["):
        return "pipe"
    if target.startswith("socket:["):
        return "socket"
    if target.startswith("anon_inode:"):
        return "anon"
    if target.startswith(("/dev/shm/", "/memfd:")):
        return "shm"
    if target.startswith("/"):
        return "file"
    return "other"


def _inode(target: str) -> int:
    lo = target.find("[")
    if lo < 0 or not target.endswith("]"):
        return 0
    try:
        return int(target[lo + 1 : -1])
    except ValueError:
        return 0


def _linux_files(pid: int, limit: int = MAX_FILES) -> tuple[list[OpenFile], int]:
    """(descriptors, total count) from ``/proc/<pid>/fd``."""
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except FileNotFoundError as e:
        raise ProcessLookupError(pid) from e
    fds = sorted(int(n) for n in names if n.isdigit())
    out: list[OpenFile] = []
    for fd in fds[:limit]:
        link = f"/proc/{pid}/fd/{fd}"
        try:
            target = os.readlink(link)
            perm = os.lstat(link).st_mode
        except FileNotFoundError:
            continue  # closed between the listing and now
        mode = ("r" if perm & 0o400 else "") + ("w" if perm & 0o200 else "")
        out.append(
            OpenFile(fd=fd, kind=_kind(target), target=target, mode=mode, inode=_inode(target))
        )
    return out, len(fds)


def _linux_inodes(pid: int, wanted: set[int], limit: int = MAX_FILES) -> set[int]:
    """The pipe and socket inodes of ``wanted`` that ``pid`` has open."""
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return set()
    found: set[int] = set()
    for n in names[:limit]:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{n}")
        except OSError:
            continue
        if target.startswith(("pipe:[", "socket:[")):
            inode = _inode(target)
            if inode in wanted:
                found.add(inode)
    return found


def _shared_with(files: list[OpenFile], related: Iterable[int]) -> list[OpenFile]:
    wanted = {f.inode for f in files if f.inode}
    if not wanted:
        return files
    holders: dict[int, list[int]] = {}
    for pid in list(related)[:MAX_RELATED]:
        for inode in _linux_inodes(pid, wanted):
            holders.setdefault(inode, []).append(pid)
    if not holders:
        return files
    import dataclasses

    return [
        dataclasses.replace(f, shared_with=tuple(holders[f.inode])) if f.inode in holders else f
        for f in files
    ]


def connections(proc: psutil.Process) -> list[Any]:
    """psutil's socket list for ``proc``, empty when it cannot be read.

    This is the expensive read of the section, a few milliseconds, since
    psutil walks the system wide ``/proc/net`` tables. Callers that
    refresh often pass a cached copy to :func:`read_ipc`.
    """
    try:
        return list(proc.net_connections(kind="all"))
    except psutil.NoSuchProcess as e:
        raise ProcessLookupError(proc.pid) from e
    except psutil.Error, OSError:
        return []


def _with_connections(files: list[OpenFile], conns: Iterable[Any]) -> list[OpenFile]:
    """Fill in the addresses of the socket descriptors from psutil's list.

    Sockets psutil knows that are not in ``files`` yet are added, which
    is every socket on macOS and Windows. Windows has no descriptor
    numbers at all, so its entries carry fd -1.
    """
    import dataclasses

    by_fd: dict[int, OpenFile] = {f.fd: f for f in files}
    out = list(files)
    for c in conns:
        fd = -1 if c.fd is None else c.fd
        if fd < 0 and LINUX:
            continue
        family = _family(c.family, c.type)
        local, remote = _addr(c.laddr), _addr(c.raddr)
        status = "" if c.status == psutil.CONN_NONE else c.status
        if fd in by_fd:
            out[out.index(by_fd[fd])] = dataclasses.replace(
                by_fd[fd], kind="socket", family=family, local=local, remote=remote, status=status
            )
        else:
            out.append(
                OpenFile(
                    fd=fd,
                    kind="socket",
                    target=f"{family} socket",
                    family=family,
                    local=local,
                    remote=remote,
                    status=status,
                )
            )
    return out


def _family(family, socktype) -> str:
    import socket

    if family == socket.AF_UNIX:
        return "unix"
    v6 = "6" if family == socket.AF_INET6 else ""
    if socktype == socket.SOCK_STREAM:
        return "tcp" + v6
    if socktype == socket.SOCK_DGRAM:
        return "udp" + v6
    return "raw" + v6


def _addr(addr) -> str:
    if not addr:
        return ""
    if isinstance(addr, str):
        return addr
    host, port = addr[0], addr[1]
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


# -- locks and mappings ----------------------------------------------------


def _linux_locks(pid: int, files: list[OpenFile]) -> list[FileLock]:
    """The target's locks from ``/proc/locks``, paths resolved through its fd table."""
    try:
        with open("/proc/locks") as f:
            text = f.read()
    except OSError:
        return []
    paths: dict[str, str] | None = None

    def path_of(inode: str) -> str:
        nonlocal paths
        if paths is None:
            paths = {}
            for of in files:
                if of.kind != "file":
                    continue
                try:
                    st = os.stat(f"/proc/{pid}/fd/{of.fd}")
                except OSError:
                    continue
                key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
                paths.setdefault(key, of.target)
        return paths.get(inode, "")

    return parse_locks(text, pid, path_of)


_LOCK_KINDS = {"FLOCK": "flock", "POSIX": "posix", "OFDLCK": "ofd", "LEASE": "lease"}


def parse_locks(text: str, pid: int, path_of: Callable[[str], str]) -> list[FileLock]:
    """The locks of ``pid`` in the text of ``/proc/locks``.

    Each line is ``id: KIND ADVISORY|MANDATORY READ|WRITE pid maj:min:ino
    start end``. A line whose second field is ``->`` is a process blocked
    waiting for the lock of the same id above it. ``path_of`` turns the
    inode spelling into a path, "" when unknown.
    """
    out: list[FileLock] = []
    holder_of: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 8:
            continue
        lock_id = fields[0].rstrip(":")
        waiting = fields[1] == "->"
        if waiting:
            fields = fields[:1] + fields[2:]
        if len(fields) < 8:
            continue
        kind_raw, _, mode_raw, pid_raw, inode = fields[1:6]
        try:
            owner = int(pid_raw)
            start = int(fields[6])
            end = -1 if fields[7] == "EOF" else int(fields[7])
        except ValueError:
            continue
        if not waiting:
            holder_of[lock_id] = owner
        # OFD locks are attributed to no pid. Report them on files the
        # target has open, which is the most the kernel lets us say.
        if kind_raw == "OFDLCK":
            mine = owner == -1 and bool(path_of(inode))
        else:
            mine = owner == pid
        if not mine:
            continue
        out.append(
            FileLock(
                kind=_LOCK_KINDS.get(kind_raw, kind_raw.lower()),
                mode=mode_raw.lower(),
                path=path_of(inode),
                inode=inode,
                start=start,
                end=end,
                waiting=waiting,
                holder=holder_of.get(lock_id, -1) if waiting else owner,
            )
        )
    return out


def _linux_mappings(pid: int) -> list[SharedMapping]:
    try:
        with open(f"/proc/{pid}/maps") as f:
            text = f.read()
    except FileNotFoundError as e:
        raise ProcessLookupError(pid) from e
    sizes: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        path = fields[5].strip()
        if not path.startswith(("/dev/shm/", "/memfd:")):
            continue
        lo, _, hi = fields[0].partition("-")
        sizes[path] = sizes.get(path, 0) + int(hi, 16) - int(lo, 16)
    out = []
    for path, size in sizes.items():
        deleted = path.endswith(" (deleted)")
        name = path[: -len(" (deleted)")] if deleted else path
        kind = "sem" if os.path.basename(name).startswith("sem.") else "shm"
        out.append(SharedMapping(path=name, size=size, kind=kind, deleted=deleted))
    return out


# -- entry point -----------------------------------------------------------


def read_ipc(
    proc: psutil.Process,
    related: Iterable[int] = (),
    connections_of: Callable[[psutil.Process], Iterable[Any]] = connections,
) -> IPC:
    """Collect the :class:`~sgrud.models.IPC` section for ``proc``.

    ``related`` are the pids whose descriptor tables are searched for the
    target's pipes and sockets, typically its parent and children.
    ``connections_of`` supplies the socket list, see :func:`connections`.
    Raises ProcessLookupError when the process is gone.
    """
    pid = proc.pid
    max_fds = 0
    if LINUX:
        import resource

        try:
            soft, _ = proc.rlimit(resource.RLIMIT_NOFILE)
            max_fds = 0 if soft < 0 or soft >= 1 << 62 else soft
        except psutil.NoSuchProcess as e:
            raise ProcessLookupError(pid) from e
        except psutil.Error, OSError:
            pass
        files, num_fds = _linux_files(pid)
        if any(f.kind == "socket" for f in files):
            files = _with_connections(files, connections_of(proc))
        files = _shared_with(files, related)
        return IPC(
            num_fds=num_fds,
            max_fds=max_fds,
            files=tuple(files),
            locks=tuple(_linux_locks(pid, files)),
            mappings=tuple(_linux_mappings(pid)),
        )
    try:
        num_fds = proc.num_handles() if WINDOWS else proc.num_fds()
        opened = proc.open_files()
    except psutil.NoSuchProcess as e:
        raise ProcessLookupError(pid) from e
    except psutil.Error, OSError:
        num_fds, opened = 0, []
    files = [
        OpenFile(fd=-1 if item.fd is None else item.fd, kind="file", target=item.path)
        for item in opened
    ]
    files = _with_connections(files, connections_of(proc))
    files.sort(key=lambda f: (f.fd < 0, f.fd, f.target))
    return IPC(num_fds=num_fds, max_fds=max_fds, files=tuple(files))
