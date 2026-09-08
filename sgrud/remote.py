"""Thin adapter over CPython's private ``_remote_debugging`` module.

This is the only file that imports ``_remote_debugging``. Everything it
returns is converted to the dataclasses in :mod:`sgrud.models` so the rest
of the package is insulated from changes to the private API.

Requires the 3.15 API (thread status flags, GC stats, native frames).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import osproc
from .errors import AttachError, NotSupported, ProcessExited
from .models import (
    Awaiter,
    Frame,
    GCCollection,
    GCGeneration,
    Task,
    ThreadStatus,
)

try:
    import _remote_debugging as _rd
except ImportError as e:  # pragma: no cover
    raise NotSupported(
        "this Python build has no _remote_debugging module "
        "(needs CPython >= 3.15 with remote debugging enabled)"
    ) from e

# _remote_debugging is a private module, so pin down the exact shape we
# rely on and fail loudly if a newer CPython moves things around.
_REQUIRED_ATTRS = ("RemoteUnwinder", "GCMonitor", "THREAD_STATUS_HAS_GIL", "is_python_process")
_REQUIRED_FIELDS = {
    "InterpreterInfo": ("interpreter_id", "threads"),
    "ThreadInfo": ("thread_id", "status", "frame_info"),
    "FrameInfo": ("filename", "location", "funcname"),
    "LocationInfo": ("lineno", "end_lineno", "col_offset", "end_col_offset"),
    "AwaitedInfo": ("thread_id", "awaited_by"),
    "TaskInfo": ("task_id", "task_name", "coroutine_stack", "awaited_by"),
    "CoroInfo": ("call_stack", "task_name"),
    "GCStatsInfo": (
        "gen",
        "iid",
        "ts_start",
        "ts_stop",
        "collections",
        "collected",
        "uncollectable",
        "candidates",
        "heap_size",
        "duration",
    ),
}


def _check_api() -> None:
    running = sys.version.split()[0]
    for name in _REQUIRED_ATTRS:
        if not hasattr(_rd, name):
            raise NotSupported(
                f"_remote_debugging lacks {name}; sgrud needs the CPython 3.15 API "
                f"(running {running})"
            )
    for type_name, fields in _REQUIRED_FIELDS.items():
        have = getattr(getattr(_rd, type_name, None), "__match_args__", ())
        missing = [f for f in fields if f not in have]
        if missing:
            raise NotSupported(
                f"_remote_debugging.{type_name} lacks fields {missing} on Python {running}; "
                "sgrud needs updating for this interpreter"
            )


_check_api()


def is_python_process(pid: int) -> bool:
    try:
        return bool(_rd.is_python_process(pid))
    except Exception:
        return False


def child_pids(pid: int, recursive: bool = True) -> list[int]:
    try:
        return list(_rd.get_child_pids(pid, recursive=recursive))
    except Exception:
        return []


def interpreter_pid(pid: int) -> int | None:
    """``pid`` if it is a CPython process, else its only CPython child.

    A Windows venv's ``python.exe`` is a launcher that runs the real
    interpreter as a child process, and wrapper scripts do the same
    elsewhere. Returns None when neither is an interpreter.
    """
    if is_python_process(pid):
        return pid
    pythons = [c for c in child_pids(pid, recursive=False) if is_python_process(c)]
    return pythons[0] if len(pythons) == 1 else None


def _frame(f: Any) -> Frame:
    loc = f.location
    if loc is None:
        return Frame(funcname=f.funcname, filename=f.filename)
    return Frame(
        funcname=f.funcname,
        filename=f.filename,
        lineno=loc.lineno,
        end_lineno=loc.end_lineno,
        col_offset=loc.col_offset,
        end_col_offset=loc.end_col_offset,
    )


def _frames(frames: Iterable[Any]) -> tuple[Frame, ...]:
    return tuple(_frame(f) for f in frames)


def permission_hint() -> str:
    """Explain why reading another process's memory failed and how to fix it."""
    hint = "reading the target's memory was denied."
    if osproc.MACOS:
        return hint + (
            " macOS only lets root read another process's memory, so run sgrud with sudo."
        )
    if osproc.WINDOWS:
        return hint + " Run sgrud as the same user as the target, or as administrator."
    scope = osproc.ptrace_scope()
    if scope:
        hint += (
            f" kernel.yama.ptrace_scope is {scope}: only child processes can be "
            "inspected. Run sgrud with sudo, grant CAP_SYS_PTRACE, or start the "
            "target through `sgrud run`."
        )
    else:
        hint += " Run sgrud as the same user as the target, or with sudo."
    return hint


def access_hint(pid: int) -> str | None:
    """Check that the target's memory is readable. Returns why not, or None.

    Anything other than a permission problem (not a Python process, still
    starting up) counts as readable and is left for the real attach to
    report.
    """
    readable = osproc.can_read_memory(pid)
    if readable is None:
        try:
            _rd.RemoteUnwinder(pid)
        except PermissionError:
            readable = False
        except ProcessLookupError as e:
            raise ProcessExited(pid) from e
        except Exception:
            pass
    if readable is False:
        return permission_hint()
    return None


def _translate_attach_error(pid: int, exc: BaseException) -> AttachError:
    text = str(exc)
    hint = None
    transient = False
    if "different Python version" in text or "version" in text.lower():
        hint = (
            f"sgrud runs on Python {sys.version.split()[0]} and can only attach to "
            "a target of the same major.minor (pre-release builds must match exactly)."
        )
    elif isinstance(exc, PermissionError) or "PyRuntime" in text:
        if isinstance(exc, PermissionError) or osproc.can_read_memory(pid) is False:
            hint = permission_hint()
        else:
            hint = "is the target really a CPython process with remote debugging enabled?"
            transient = True
    else:
        # Anything else (no interpreter state yet, torn reads) is most likely
        # the target still initialising.
        transient = True
    return AttachError(pid, text, hint, transient=transient)


#: The sampling modes, in the order the TUI cycles through them.
#:
#: ``wall`` counts every thread that has a Python stack. ``gil`` counts only
#: the thread holding the GIL, which is where CPU time goes in CPython.
#: ``cpu`` counts threads the OS has on a core, so C code that released the
#: GIL still counts and a thread waiting for the GIL does not.
#: ``exception`` counts only threads handling an exception, to show where
#: exceptions are raised and caught. ``async`` samples asyncio tasks
#: instead of threads: every task counts, suspended ones included, with
#: its coroutine stack joined to the stacks of the tasks awaiting it.
MODES = ("wall", "gil", "cpu", "exception", "async")

#: The unwinder mode behind each stack sampling mode. The unwinder drops
#: the threads that do not match, so the choice of threads is made in C.
#: ``async`` is not here: it reads the task graph through a ``wall``
#: unwinder, see :func:`unwinder_mode`.
UNWINDER_MODES = {"wall": 0, "cpu": 1, "gil": 2, "exception": 4}


def unwinder_mode(mode: str) -> str:
    """The :data:`UNWINDER_MODES` entry that serves sampling ``mode``."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    return "wall" if mode == "async" else mode


@dataclass(frozen=True, slots=True)
class RawSample:
    """One read of the target as ``_remote_debugging`` returned it.

    ``data`` is opaque to the rest of sgrud. It is what the standard
    library's ``profiling.sampling`` collectors consume, so a sample can be
    handed to them unchanged, see :mod:`sgrud.export`. :meth:`stacks` and
    :meth:`tasks` convert it to sgrud's own types, depending on ``mode``.
    """

    #: One of :data:`MODES`.
    mode: str
    data: Any

    def stacks(self) -> dict[int, tuple[int, ThreadStatus, tuple[Frame, ...]]]:
        """Map OS thread id to (interpreter_id, status, frames leaf-first)."""
        if self.mode == "async":
            raise ValueError("an async sample holds tasks, not thread stacks")
        return _convert_stacks(self.data)

    def tasks(self) -> tuple[Task, ...]:
        if self.mode != "async":
            raise ValueError("only an async sample holds tasks")
        return _convert_tasks(self.data)


def _convert_stacks(result: Any) -> dict[int, tuple[int, ThreadStatus, tuple[Frame, ...]]]:
    out: dict[int, tuple[int, ThreadStatus, tuple[Frame, ...]]] = {}
    for interp in result:
        for t in interp.threads:
            out[t.thread_id] = (
                interp.interpreter_id,
                ThreadStatus(t.status),
                _frames(t.frame_info),
            )
    return out


def _convert_tasks(result: Any) -> tuple[Task, ...]:
    tasks: list[Task] = []
    for awaited in result:
        for t in awaited.awaited_by:
            frames = tuple(_frame(f) for coro in t.coroutine_stack for f in coro.call_stack)
            awaiters = tuple(
                Awaiter(task_id=int(a.task_name), frames=_frames(a.call_stack))
                for a in t.awaited_by
                if isinstance(a.task_name, int)
            )
            tasks.append(
                Task(
                    id=t.task_id,
                    name=str(t.task_name),
                    thread_id=awaited.thread_id,
                    frames=frames,
                    awaited_by=awaiters,
                )
            )
    return tuple(tasks)


def _no_asyncio(exc: BaseException) -> bool:
    """Whether reading tasks failed because the target never imported asyncio."""
    text = str(exc)
    return isinstance(exc, RuntimeError) and ("AsyncioDebug" in text or "asyncio" in text.lower())


class RemoteInspector:
    """Reads stacks, asyncio tasks and GC stats from another process.

    ``mode`` is one of :data:`UNWINDER_MODES`. Only a ``wall`` inspector
    returns every thread, and it is the one that can read the task graph.
    ``opcodes`` makes each raw frame carry the bytecode instruction being
    executed, which only the ``profiling.sampling`` collectors use.
    """

    def __init__(
        self,
        pid: int,
        *,
        native: bool = True,
        gc_markers: bool = True,
        cache_frames: bool = True,
        mode: str = "wall",
        opcodes: bool = False,
    ):
        if mode not in UNWINDER_MODES:
            raise ValueError(f"mode must be one of {tuple(UNWINDER_MODES)}")
        self.pid = pid
        self.mode = mode
        try:
            self._unwinder = _rd.RemoteUnwinder(
                pid,
                all_threads=True,
                mode=UNWINDER_MODES[mode],
                skip_non_matching_threads=mode != "wall",
                native=native,
                gc=gc_markers,
                opcodes=opcodes,
                cache_frames=cache_frames,
                stats=True,
            )
        except ProcessLookupError as e:
            raise ProcessExited(pid) from e
        except (PermissionError, RuntimeError, OSError) as e:
            raise _translate_attach_error(pid, e) from e
        self._gc_monitor: Any = None
        self._gc_error: str | None = None

    def _guard(self, exc: BaseException) -> BaseException:
        """Turn a failure mid-read into ProcessExited if the target died."""
        if isinstance(exc, ProcessLookupError):
            return ProcessExited(self.pid)
        if not osproc.pid_exists(self.pid):
            return ProcessExited(self.pid)
        return exc

    def sample(self, retries: int = 5, *, tasks: bool = False) -> RawSample:
        """One read of the stacks in this inspector's mode, or of the task graph.

        The target keeps running while its frames are read, so a thread
        that is pushing or popping a frame at that instant makes the read
        fail, and one such thread fails the whole sample. On a target
        whose threads call functions in a tight loop that is every other
        read. A failed read is retried at once up to ``retries`` times,
        which costs a few microseconds each and gets almost all of them.
        ``python -m asyncio ps`` retries its task reads the same way.

        With ``tasks`` the read is ``get_all_awaited_by`` and the sample
        is an ``async`` one. A target that never imported asyncio yields
        an empty sample rather than an error.
        """
        if tasks and self.mode != "wall":
            raise ValueError("tasks are read through a wall inspector")
        unwinder = self._live()
        mode = "async" if tasks else self.mode
        for attempt in range(retries + 1):
            try:
                if tasks:
                    result = unwinder.get_all_awaited_by()
                else:
                    result = unwinder.get_stack_trace()
                return RawSample(mode, result)
            except ProcessLookupError as e:
                raise ProcessExited(self.pid) from e
            except Exception as e:
                if tasks and _no_asyncio(e):
                    return RawSample(mode, ())
                if attempt == retries:
                    raise self._guard(e) from e
        raise AssertionError("unreachable")

    def stats(self) -> dict[str, Any]:
        """The unwinder's own counters: memory reads, bytes, cache hit rates."""
        try:
            return dict(self._live().get_stats())
        except Exception:
            return {}

    def gc_records(self) -> tuple[GCRecord, ...]:
        """The raw contents of the target's GC history rings, empty slots dropped."""
        if self._gc_error is not None:
            raise RuntimeError(self._gc_error)
        if self._gc_monitor is None:
            try:
                self._gc_monitor = _rd.GCMonitor(self.pid)
            except Exception as e:
                self._gc_error = str(e)
                raise self._guard(e) from e
        try:
            raw = self._gc_monitor.get_gc_stats()
        except Exception as e:
            raise self._guard(e) from e
        return tuple(
            GCRecord(
                generation=item.gen,
                interpreter_id=item.iid,
                index=item.collections,
                started_at=item.ts_start,
                stopped_at=item.ts_stop,
                collected=item.collected,
                uncollectable=item.uncollectable,
                candidates=item.candidates,
                duration=item.duration,
                heap_size=item.heap_size,
            )
            for item in raw
            if item.collections > 0
        )

    def _live(self):
        if self._unwinder is None:
            raise RuntimeError("inspector is closed")
        return self._unwinder

    def close(self) -> None:
        self._unwinder = None
        self._gc_monitor = None


@dataclass(frozen=True, slots=True)
class GCRecord:
    """One slot of a GC history ring: cumulative counters after collection ``index``.

    ``collected``, ``uncollectable``, ``candidates`` and ``duration`` are
    running totals for the generation. ``heap_size`` is the number of
    tracked objects when the collection started.
    """

    generation: int
    interpreter_id: int
    index: int
    started_at: int
    stopped_at: int
    collected: int
    uncollectable: int
    candidates: int
    duration: float
    heap_size: int


NUM_GENERATIONS = 3


def build_gc(
    records: Iterable[GCRecord],
    *,
    now_ns: int | None = None,
    rates: Mapping[int, tuple[float | None, float | None]] | None = None,
) -> tuple[GCGeneration, ...]:
    """Turn ring records into per-generation totals and a history of deltas.

    Records may span several reads of the ring, see
    :class:`sgrud.monitor._GCTracker`, and every record given ends up in
    the history. Each collection's own figures are the difference to the
    record before it, so a gap (the ring wrapped between reads) leaves
    that collection's figures unknown. The first collection of a
    generation needs no predecessor.

    ``now_ns`` is a ``time.perf_counter_ns()`` reading used to fill in each
    collection's ``age``. ``rates`` maps generation to ``(rate, time_share)``.
    """
    by_gen: dict[int, dict[int, GCRecord]] = {g: {} for g in range(NUM_GENERATIONS)}
    for r in records:
        by_gen.setdefault(r.generation, {})[r.index] = r
    gens: list[GCGeneration] = []
    for gen, slots in sorted(by_gen.items()):
        rate, share = (rates or {}).get(gen, (None, None))
        if not slots:
            gens.append(GCGeneration(gen, 0, 0, 0, 0.0, 0, rate, share))
            continue
        history: list[GCCollection] = []
        for index in sorted(slots, reverse=True):
            cur = slots[index]
            base: GCRecord | _ZeroStats | None
            if index == 1:
                base = _ZERO_STATS
            else:
                base = slots.get(index - 1)
            history.append(
                GCCollection(
                    generation=gen,
                    interpreter_id=cur.interpreter_id,
                    index=index,
                    started_at=cur.started_at,
                    stopped_at=cur.stopped_at,
                    duration=cur.duration - base.duration if base else float("nan"),
                    collected=cur.collected - base.collected if base else -1,
                    uncollectable=cur.uncollectable - base.uncollectable if base else -1,
                    candidates=cur.candidates - base.candidates if base else -1,
                    heap_size=cur.heap_size,
                    age=(now_ns - cur.stopped_at) / 1e9 if now_ns is not None else float("nan"),
                )
            )
        latest = slots[max(slots)]
        gens.append(
            GCGeneration(
                generation=gen,
                collections=latest.index,
                collected=latest.collected,
                uncollectable=latest.uncollectable,
                total_duration=latest.duration,
                heap_size=latest.heap_size,
                rate=rate,
                time_share=share,
                history=tuple(history),
            )
        )
    return tuple(gens)


class _ZeroStats:
    collected = 0
    uncollectable = 0
    candidates = 0
    duration = 0.0


_ZERO_STATS = _ZeroStats()
