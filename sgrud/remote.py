"""Thin adapter over CPython's private ``_remote_debugging`` module.

This is the only file that imports ``_remote_debugging``. Everything it
returns is converted to the dataclasses in :mod:`sgrud.models` so the rest
of the package is insulated from changes to the private API.

Requires the 3.15 API (thread status flags, GC stats, native frames).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any

from . import procfs
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
        "gen", "iid", "ts_start", "ts_stop", "collections", "collected",
        "uncollectable", "candidates", "heap_size", "duration",
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
    scope = procfs.ptrace_scope()
    if scope:
        hint += (
            f" kernel.yama.ptrace_scope is {scope}: only child processes can be "
            "inspected. Run sgrud with sudo, grant CAP_SYS_PTRACE, or start the "
            "target through `sgrud run`."
        )
    else:
        hint += " Run sgrud as the same user as the target, or with sudo."
    return hint


def _translate_attach_error(pid: int, exc: BaseException) -> AttachError:
    text = str(exc)
    hint = None
    transient = False
    if "different Python version" in text or "version" in text.lower():
        hint = (
            f"sgrud runs on Python {sys.version.split()[0]} and can only attach to "
            "a target of the same major.minor (pre-release builds must match exactly)."
        )
    elif "PyRuntime" in text or "Permission" in type(exc).__name__:
        if not procfs.can_read_memory(pid):
            hint = permission_hint()
        else:
            hint = "is the target really a CPython process with remote debugging enabled?"
            transient = True
    else:
        # Anything else (no interpreter state yet, torn reads) is most likely
        # the target still initialising.
        transient = True
    return AttachError(pid, text, hint, transient=transient)


class RemoteInspector:
    """Reads stacks, asyncio tasks and GC stats from another process."""

    def __init__(
        self,
        pid: int,
        *,
        native: bool = True,
        gc_markers: bool = True,
        cache_frames: bool = True,
        only_active_thread: bool = False,
    ):
        self.pid = pid
        self._only_active = only_active_thread
        try:
            self._unwinder = _rd.RemoteUnwinder(
                pid,
                all_threads=not only_active_thread,
                only_active_thread=only_active_thread,
                native=native,
                gc=gc_markers,
                cache_frames=cache_frames,
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
        try:
            procfs.read_stat(self.pid)
        except ProcessLookupError:
            return ProcessExited(self.pid)
        return exc

    def stacks(self) -> dict[int, tuple[int, ThreadStatus, tuple[Frame, ...]]]:
        """Map OS thread id to (interpreter_id, status, frames leaf-first)."""
        try:
            result = self._unwinder.get_stack_trace()
        except Exception as e:
            raise self._guard(e) from e
        out: dict[int, tuple[int, ThreadStatus, tuple[Frame, ...]]] = {}
        for interp in result:
            for t in interp.threads:
                out[t.thread_id] = (
                    interp.interpreter_id,
                    ThreadStatus(t.status),
                    _frames(t.frame_info),
                )
        return out

    def tasks(self) -> tuple[Task, ...]:
        """All asyncio tasks in the target, across threads."""
        try:
            result = self._unwinder.get_all_awaited_by()
        except RuntimeError as e:
            # asyncio not imported in the target is a normal condition.
            if "AsyncioDebug" in str(e) or "asyncio" in str(e).lower():
                return ()
            raise self._guard(e) from e
        except Exception as e:
            raise self._guard(e) from e
        tasks: list[Task] = []
        for awaited in result:
            for t in awaited.awaited_by:
                frames = tuple(
                    _frame(f) for coro in t.coroutine_stack for f in coro.call_stack
                )
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

    def gc(self) -> tuple[GCGeneration, ...]:
        """Per-generation GC totals plus the recent collection history."""
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
        return _convert_gc(raw)

    def pause(self) -> None:
        self._unwinder.pause_threads()

    def resume(self) -> None:
        self._unwinder.resume_threads()

    def close(self) -> None:
        self._unwinder = None
        self._gc_monitor = None


class _ZeroStats:
    collections = 0
    collected = 0
    uncollectable = 0
    candidates = 0
    duration = 0.0


_ZERO_STATS = _ZeroStats()


def _convert_gc(raw: Iterable[Any]) -> tuple[GCGeneration, ...]:
    by_gen: dict[int, list[Any]] = {}
    for item in raw:
        by_gen.setdefault(item.gen, []).append(item)
    gens: list[GCGeneration] = []
    for gen, items in sorted(by_gen.items()):
        # The target keeps a ring buffer per generation whose counters are
        # cumulative. Empty slots have collections == 0. Sorting by the
        # cumulative counter recovers chronological order.
        used = sorted((i for i in items if i.collections > 0), key=lambda i: i.collections)
        if not used:
            gens.append(GCGeneration(gen, 0, 0, 0, 0.0, 0))
            continue
        latest = used[-1]
        history: list[GCCollection] = []
        prev = None
        for cur in used:
            if cur.collections == 1:
                base = _ZERO_STATS
            elif prev is not None and prev.collections == cur.collections - 1:
                base = prev
            else:
                base = None  # gap in the ring, per-collection deltas unknown
            history.append(
                GCCollection(
                    generation=gen,
                    interpreter_id=cur.iid,
                    started_at=cur.ts_start,
                    stopped_at=cur.ts_stop,
                    duration=cur.duration - base.duration if base else float("nan"),
                    collected=cur.collected - base.collected if base else -1,
                    uncollectable=cur.uncollectable - base.uncollectable if base else -1,
                    candidates=cur.candidates - base.candidates if base else -1,
                    heap_size=cur.heap_size,
                )
            )
            prev = cur
        history.reverse()
        gens.append(
            GCGeneration(
                generation=gen,
                collections=latest.collections,
                collected=latest.collected,
                uncollectable=latest.uncollectable,
                total_duration=latest.duration,
                heap_size=latest.heap_size,
                history=tuple(history),
            )
        )
    return tuple(gens)
