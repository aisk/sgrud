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
from collections.abc import Hashable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field

from .format import short_path
from .models import Frame, Task, Thread, ThreadStatus

FunctionKey = tuple[str, str]  # (funcname, filename)
StackKey = tuple[FunctionKey, ...]  # outermost function first

#: The marker frame the unwinder inserts where a garbage collection is running.
GC_KEY: FunctionKey = ("<GC>", "~")

#: ``wall`` counts every thread that has a Python stack. ``gil`` counts only
#: the thread holding the GIL, which is where CPU time goes in CPython.
#: ``async`` samples asyncio tasks instead of threads: every task counts,
#: suspended ones included, with its coroutine stack joined to the stacks of
#: the tasks awaiting it.
MODES = ("wall", "gil", "async")


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


@dataclass(frozen=True, slots=True)
class GCSite:
    """A function that was running when a garbage collection started."""

    funcname: str
    filename: str
    #: Samples in which a collection triggered from this function was running.
    samples: int
    #: Relative to all samples of the selected threads.
    percent: float


@dataclass(slots=True)
class CallNode:
    """One node of the merged call tree built by :meth:`Hotspots.call_tree`.

    Frame nodes are keyed by ``(funcname, filename)``. When the tree spans
    several threads the root's children are thread nodes keyed by tid.
    """

    name: str
    filename: str
    key: Hashable
    tid: int | None = None
    #: Samples in which this node was on the stack.
    total: int = 0
    #: Samples in which this node was the innermost frame.
    self_samples: int = 0
    children: dict[Hashable, CallNode] = field(default_factory=dict)

    @property
    def synthetic(self) -> bool:
        return self.tid is None and (self.filename == "~" or self.name.startswith("<"))

    def child(self, key: Hashable, name: str, filename: str, tid: int | None = None) -> CallNode:
        node = self.children.get(key)
        if node is None:
            node = self.children[key] = CallNode(name, filename, key, tid)
        return node

    def add_stack(self, stack: StackKey, count: int) -> None:
        """Merge one outermost-first stack seen ``count`` times under this node."""
        self.total += count
        node = self
        for key in stack:
            node = node.child(key, key[0], key[1])
            node.total += count
        node.self_samples += count

    def walk(self, depth: int = 0) -> Iterator[tuple[CallNode, int]]:
        yield self, depth
        for child in self.children.values():
            yield from child.walk(depth + 1)


@dataclass(slots=True)
class _ThreadCounts:
    samples: int = 0
    self_counts: Counter[FunctionKey] = field(default_factory=Counter)
    total_counts: Counter[FunctionKey] = field(default_factory=Counter)
    #: Whole stacks, outermost function first. This is what a flame graph
    #: needs and, unlike the per-function counters, it keeps recursion as is.
    stacks: Counter[StackKey] = field(default_factory=Counter)


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

    def add_tasks(self, tasks: Iterable[Task]) -> None:
        """Record one sample of asyncio tasks, see :func:`task_stacks`."""
        self.add_stacks(task_stacks(tasks))

    def add_frames(self, frames_by_tid: Mapping[int, tuple[Frame, ...]]) -> None:
        self.add_stacks(frames_by_tid.items())

    def add_stacks(self, stacks: Iterable[tuple[int, tuple[Frame, ...]]]) -> None:
        """Record one sample given as ``(tid, frames)`` pairs, leaf frame first.

        A thread may contribute several stacks to one sample, as it does in
        ``async`` mode where each task is a stack of its own. Each stack
        counts as one sample of that thread.
        """
        with self._lock:
            self.samples += 1
            self.last_sample_at = time.monotonic()
            for tid, frames in stacks:
                if not frames:
                    continue
                counts = self._threads.get(tid)
                if counts is None:
                    counts = self._threads[tid] = _ThreadCounts()
                counts.samples += 1
                leaf = frames[0]
                counts.self_counts[(leaf.funcname, leaf.filename)] += 1
                counts.stacks[tuple((f.funcname, f.filename) for f in reversed(frames))] += 1
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

    def gc_sites(
        self, *, thread: int | None = None, limit: int | None = None
    ) -> tuple[int, int, list[GCSite]]:
        """Where garbage collections were triggered from.

        Returns ``(gc_samples, samples, sites)``: how many samples of the
        selected thread(s) were inside a collection, how many samples there
        were in all, and the functions the collections interrupted, most
        frequent first. The triggering function is the frame right outside
        the ``<GC>`` marker, so this answers which allocation sites make the
        collector run. Needs the monitor's ``gc_markers`` option, the default.
        """
        with self._lock:
            if thread is None:
                selected = list(self._threads.values())
            else:
                counts = self._threads.get(thread)
                selected = [counts] if counts else []
            samples = sum(c.samples for c in selected)
            by_site: Counter[FunctionKey] = Counter()
            gc_samples = 0
            for c in selected:
                for stack, n in c.stacks.items():
                    try:
                        i = stack.index(GC_KEY)
                    except ValueError:
                        continue
                    gc_samples += n
                    by_site[stack[i - 1] if i else ("<no Python frame>", "~")] += n
        sites = [
            GCSite(key[0], key[1], n, 100.0 * n / samples) for key, n in by_site.most_common(limit)
        ]
        return gc_samples, samples, sites

    def _stacks(self, thread: int | None) -> list[tuple[int, dict[StackKey, int]]]:
        with self._lock:
            if thread is None:
                return [(tid, dict(c.stacks)) for tid, c in sorted(self._threads.items())]
            counts = self._threads.get(thread)
            return [(thread, dict(counts.stacks))] if counts else []

    def call_tree(
        self, *, thread: int | None = None, names: Mapping[int, str] | None = None
    ) -> CallNode:
        """Merge the sampled stacks into one tree, outermost functions first.

        With ``thread`` set the root's children are that thread's outermost
        functions. Otherwise the root has one child per thread, labelled
        from ``names`` when given, so threads sit side by side in a flame
        graph instead of having their stacks mixed together.
        """
        if thread is None:
            root = CallNode("all threads", "", None)
            for tid, stacks in self._stacks(None):
                node = root.child(tid, _thread_label(tid, names), "", tid)
                for stack, count in stacks.items():
                    node.add_stack(stack, count)
                root.total += node.total
            return root
        root = CallNode(_thread_label(thread, names), "", None, thread)
        for _, stacks in self._stacks(thread):
            for stack, count in stacks.items():
                root.add_stack(stack, count)
        return root

    def folded(
        self, *, thread: int | None = None, names: Mapping[int, str] | None = None
    ) -> list[str]:
        """Collapsed stacks in the ``a;b;c count`` format of flamegraph.pl.

        Without a ``thread`` filter each line starts with the thread label,
        so the resulting graph shows threads side by side like the TUI.
        """
        lines = []
        for tid, stacks in self._stacks(thread):
            prefix = [] if thread is not None else [_thread_label(tid, names)]
            for stack, count in sorted(stacks.items()):
                parts = prefix + [_folded_frame(name, filename) for name, filename in stack]
                lines.append(f"{';'.join(parts)} {count}")
        return lines


def task_stacks(tasks: Iterable[Task]) -> list[tuple[int, tuple[Frame, ...]]]:
    """Join asyncio tasks into linear stacks, one per innermost task.

    A task that no other task awaits through is a leaf. Its stack is its own
    coroutine frames, a synthetic ``<task NAME>`` marker, then the frames of
    the task awaiting it, and so on up to a root task. This mirrors what the
    ``profiling.sampling`` module does in async-aware mode, so a flame graph
    shows ``main -> gather -> worker`` even though the worker task runs on no
    thread's stack. Returns ``(thread_id, frames)`` pairs, leaf frame first.
    A task awaited by several tasks follows the first of them.
    """
    by_id = {t.id: t for t in tasks}
    awaiting: set[int] = set()
    for t in by_id.values():
        awaiting.update(p for p in t.parent_ids if p in by_id)
    stacks: list[tuple[int, tuple[Frame, ...]]] = []
    for leaf in by_id.values():
        if leaf.id in awaiting:
            continue
        frames: list[Frame] = []
        seen: set[int] = set()
        task: Task | None = leaf
        while task is not None and task.id not in seen:
            seen.add(task.id)
            frames.extend(task.frames)
            frames.append(Frame(f"<task {task.name}>", "~"))
            parents = [by_id[p] for p in task.parent_ids if p in by_id]
            task = parents[0] if parents else None
        stacks.append((leaf.thread_id, tuple(frames)))
    return stacks


def _thread_label(tid: int, names: Mapping[int, str] | None) -> str:
    name = names.get(tid) if names else None
    return f"{name} [{tid}]" if name else f"thread {tid}"


def _folded_frame(name: str, filename: str) -> str:
    if filename == "~" or name.startswith("<"):
        return name
    return f"{name} ({short_path(filename)})".replace(";", ",")
