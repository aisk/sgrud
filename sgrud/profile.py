"""Aggregate stack samples into per-function hotspot counts.

The aggregator is independent of how samples are obtained: feed it the
mapping returned by :meth:`sgrud.Monitor.sample_stacks` (or the ``threads``
of a :class:`~sgrud.models.Snapshot`) and read back sorted rows. It is safe
to feed from one thread and read from another.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .models import Frame, Thread, ThreadStatus

FunctionKey = tuple[str, str]  # (funcname, filename)

#: ``wall`` counts every thread that has a Python stack. ``gil`` counts only
#: the thread holding the GIL, which is where CPU time goes in CPython.
MODES = ("wall", "gil")


@dataclass(frozen=True, slots=True)
class HotspotRow:
    funcname: str
    filename: str
    #: Samples where this function was the innermost Python frame.
    self_samples: int
    #: Samples where this function appeared anywhere on the stack.
    total_samples: int
    self_percent: float
    total_percent: float

    @property
    def synthetic(self) -> bool:
        return self.filename == "~" or self.funcname.startswith("<")


@dataclass(slots=True)
class _ThreadCounts:
    samples: int = 0
    self_counts: Counter[FunctionKey] | None = None
    total_counts: Counter[FunctionKey] | None = None

    def __post_init__(self) -> None:
        self.self_counts = Counter()
        self.total_counts = Counter()


class Hotspots:
    """Counts self and total samples per function, per thread."""

    def __init__(self, mode: str = "wall") -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self._lock = threading.Lock()
        self._threads: dict[int, _ThreadCounts] = {}
        self.samples = 0
        self.started = time.monotonic()
        self.last_sample_at: float | None = None

    def reset(self) -> None:
        with self._lock:
            self._threads.clear()
            self.samples = 0
            self.started = time.monotonic()
            self.last_sample_at = None

    def _wanted(self, status: object) -> bool:
        if self.mode == "gil":
            return bool(ThreadStatus(status) & ThreadStatus.HAS_GIL)
        return True

    def add(self, stacks: Mapping[int, tuple[int, object, tuple[Frame, ...]]]) -> None:
        """Record one sample from :meth:`Monitor.sample_stacks`."""
        self.add_frames(
            {tid: frames for tid, (_, status, frames) in stacks.items() if self._wanted(status)}
        )

    def add_threads(self, threads: Iterable[Thread]) -> None:
        """Record one sample from the threads of a Snapshot."""
        self.add_frames({t.tid: t.frames for t in threads if t.frames and self._wanted(t.status)})

    def add_frames(self, frames_by_tid: Mapping[int, tuple[Frame, ...]]) -> None:
        with self._lock:
            self.samples += 1
            self.last_sample_at = time.monotonic()
            for tid, frames in frames_by_tid.items():
                if not frames:
                    continue
                counts = self._threads.get(tid)
                if counts is None:
                    counts = self._threads[tid] = _ThreadCounts()
                counts.samples += 1
                leaf = frames[0]
                counts.self_counts[(leaf.funcname, leaf.filename)] += 1
                # Count each function once per sample so recursion does not
                # inflate its total beyond 100 percent.
                seen: set[FunctionKey] = set()
                for f in frames:
                    key = (f.funcname, f.filename)
                    if key not in seen:
                        seen.add(key)
                        counts.total_counts[key] += 1

    @property
    def thread_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._threads)

    def thread_samples(self, tid: int) -> int:
        with self._lock:
            counts = self._threads.get(tid)
            return counts.samples if counts else 0

    def rate(self) -> float:
        """Achieved samples per second since the last reset."""
        elapsed = (self.last_sample_at or self.started) - self.started
        return self.samples / elapsed if elapsed > 0 else 0.0

    def rows(
        self,
        *,
        thread: int | None = None,
        sort: str = "self",
        limit: int | None = None,
    ) -> list[HotspotRow]:
        """Sorted hotspot rows for one thread, or merged over all threads.

        ``sort`` is ``"self"`` or ``"total"``. Percentages are relative to the
        number of samples in which the selected thread(s) had a Python stack.
        """
        with self._lock:
            if thread is None:
                selected = list(self._threads.values())
            else:
                counts = self._threads.get(thread)
                selected = [counts] if counts else []
            self_counts: Counter[FunctionKey] = Counter()
            total_counts: Counter[FunctionKey] = Counter()
            samples = 0
            for counts in selected:
                samples += counts.samples
                self_counts.update(counts.self_counts)
                total_counts.update(counts.total_counts)
        if samples == 0:
            return []
        rows = [
            HotspotRow(
                funcname=key[0],
                filename=key[1],
                self_samples=self_counts.get(key, 0),
                total_samples=total,
                self_percent=100.0 * self_counts.get(key, 0) / samples,
                total_percent=100.0 * total / samples,
            )
            for key, total in total_counts.items()
        ]
        if sort == "total":
            rows.sort(key=lambda r: (-r.total_samples, -r.self_samples, r.funcname))
        else:
            rows.sort(key=lambda r: (-r.self_samples, -r.total_samples, r.funcname))
        return rows[:limit] if limit is not None else rows
