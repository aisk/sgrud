"""Plain text rendering of snapshots, shared by the CLI and usable in tests."""

from __future__ import annotations

import math
import os
from collections.abc import Iterable

from .models import Frame, Process, Snapshot, Task, Thread


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def human_duration(seconds: float) -> str:
    if math.isnan(seconds):
        return "?"
    if seconds < 1e-3:
        return f"{seconds * 1e6:.0f}µs"
    if seconds < 1:
        return f"{seconds * 1e3:.1f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{sec:02d}s"


def short_path(path: str, keep: int = 2) -> str:
    if not path or path.startswith("<"):
        return path
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-keep:]) if len(parts) > keep else path


def percent(value: float | None) -> str:
    return "  -  " if value is None else f"{value:5.1f}"


def format_frames(frames: Iterable[Frame], indent: str = "    ") -> list[str]:
    lines = []
    for f in frames:
        if f.synthetic:
            lines.append(f"{indent}{f.funcname}")
        else:
            lines.append(f"{indent}{f.funcname}  {short_path(f.filename)}:{f.lineno}")
    return lines


def format_thread(t: Thread, *, frames: bool = True, max_frames: int | None = None) -> list[str]:
    head = (
        f"[{t.tid}] {t.name:<16} {t.status.describe():<12} state={t.state} "
        f"cpu={percent(t.cpu_percent)}%  utime={t.user_time:.2f}s stime={t.system_time:.2f}s"
    )
    lines = [head]
    if frames:
        shown = t.frames if max_frames is None else t.frames[:max_frames]
        lines.extend(format_frames(shown))
        if max_frames is not None and len(t.frames) > max_frames:
            lines.append(f"    ... {len(t.frames) - max_frames} more")
    return lines


def format_task_tree(snap: Snapshot) -> list[str]:
    children = snap.task_children()
    by_id = {t.id: t for t in snap.tasks}
    lines: list[str] = []

    def label(t: Task) -> str:
        top = t.frames[0].funcname if t.frames else "?"
        chain = " <- ".join(f.funcname for f in t.frames[:4])
        return f"{t.name} (0x{t.id:x}) [tid {t.thread_id}] {chain or top}"

    def walk(t: Task, prefix: str, last: bool, seen: set[int]) -> None:
        branch = "└─ " if last else "├─ "
        lines.append(f"{prefix}{branch}{label(t)}")
        if t.id in seen:
            lines.append(f"{prefix}{'   ' if last else '│  '}(cycle)")
            return
        seen = seen | {t.id}
        kids = children.get(t.id, [])
        for i, k in enumerate(kids):
            walk(k, prefix + ("   " if last else "│  "), i == len(kids) - 1, seen)

    roots = children.get(None, [])
    for i, root in enumerate(roots):
        walk(root, "", i == len(roots) - 1, set())
    if not roots and by_id:
        lines.append("(all tasks are in await cycles)")
    return lines


def human_rate(value: float | None, unit: str = "/s") -> str:
    if value is None:
        return "-"
    if value == 0 or value >= 100:
        return f"{value:.0f}{unit}"
    return f"{value:.1f}{unit}" if value >= 1 else f"{value:.2f}{unit}"


def human_count(n: int) -> str:
    return "?" if n < 0 else f"{n:,}"


def memory_rows(p: Process) -> list[tuple[str, list[tuple[str, str]]]]:
    """Labelled groups of memory figures, leaving out what the platform lacks.

    Each group is ``(label, [(name, value), ...])`` with the first entry
    of a group being the headline figure. Used by the text dump and the TUI.
    """
    m = p.memory
    lim = p.limits

    def group(label: str, *pairs: tuple[str, int | None]) -> tuple[str, list[tuple[str, str]]]:
        return label, [(name, human_bytes(value)) for name, value in pairs if value]

    rows = [
        group(
            "rss",
            ("", m.rss),
            ("peak", m.hwm),
            ("anon", m.anon),
            ("file", m.file),
            ("shmem", m.shmem),
        ),
        group("uss", ("", m.uss), ("pss", m.pss), ("swap", m.swap)),
        group("vms", ("", m.vms), ("peak", m.peak_vms), ("data", m.data), ("shared", m.shared)),
        group("brk", ("", m.brk), ("anon mapped", m.anon_mapped), ("huge pages", m.huge)),
    ]
    faults: list[tuple[str, str]] = [
        ("", human_rate(p.fault_rate)),
        ("major", human_rate(p.major_fault_rate)),
        ("total", human_count(p.page_faults)),
    ]
    rows.append(("faults", faults))
    limits: list[tuple[str, str]] = []
    if lim.cgroup_limit:
        pct = lim.cgroup_percent
        limits.append(
            ("cgroup", f"{human_bytes(lim.cgroup_usage)} of {human_bytes(lim.cgroup_limit)}")
        )
        if pct is not None:
            limits.append(("used", f"{pct:.0f}%"))
        if lim.cgroup_high:
            limits.append(("high", human_bytes(lim.cgroup_high)))
    elif lim.cgroup_usage:
        limits.append(("cgroup", human_bytes(lim.cgroup_usage)))
    if lim.address_space:
        limits.append(("address space", human_bytes(lim.address_space)))
    if lim.oom_score >= 0:
        limits.append(("oom score", str(lim.oom_score)))
    rows.append(("limits", limits))
    return [(label, pairs) for label, pairs in rows if pairs]


def format_memory(p: Process) -> list[str]:
    lines = []
    for label, pairs in memory_rows(p):
        parts = [f"{name} {value}".strip() for name, value in pairs]
        lines.append(f"{label}={parts[0]}" + "".join(f"  {part}" for part in parts[1:]))
    return lines


def format_gc(snap: Snapshot) -> list[str]:
    lines = []
    share = snap.gc_time_share
    if share is not None:
        lines.append(f"time in gc {share * 100:.2f}%  {human_rate(snap.gc_rate)} collections")
    for t in snap.collecting:
        lines.append(f"collecting now in thread {t.tid} {t.name}".rstrip())
    for g in snap.gc:
        line = (
            f"gen{g.generation}: {g.collections} collections, {g.collected} collected, "
            f"{g.uncollectable} uncollectable, total {human_duration(g.total_duration)}, "
            f"mean {human_duration(g.mean_duration)}, heap {human_count(g.heap_size)}"
        )
        if g.rate is not None and g.time_share is not None:
            line += f", {human_rate(g.rate)}, {g.time_share * 100:.2f}% of time"
        lines.append(line)
        for c in g.history[:3]:
            ago = "" if math.isnan(c.age) else f"{human_duration(max(c.age, 0.0))} ago, "
            lines.append(
                f"    #{c.index}: {ago}{human_duration(c.duration)} "
                f"collected={human_count(c.collected)} survivors={human_count(c.survivors)} "
                f"heap={human_count(c.heap_size)}"
            )
    return lines


def format_snapshot(
    snap: Snapshot,
    *,
    threads: bool = True,
    frames: bool = True,
    tasks: bool = True,
    gc: bool = True,
    max_frames: int | None = None,
) -> str:
    p = snap.process
    lines = [
        f"pid {p.pid}  {os.path.basename(p.exe) or '?'}  {' '.join(p.cmdline)[:80]}",
        f"state={p.state} threads={p.num_threads} uptime={human_duration(p.uptime)} "
        f"cpu={percent(p.cpu_percent)}%  utime={p.user_time:.2f}s stime={p.system_time:.2f}s",
        *format_memory(p),
    ]
    if threads:
        lines.append("")
        lines.append(f"threads ({len(snap.threads)}):")
        for t in snap.threads:
            lines.extend(format_thread(t, frames=frames, max_frames=max_frames))
    if tasks:
        lines.append("")
        lines.append(f"asyncio tasks ({len(snap.tasks)}):")
        lines.extend(format_task_tree(snap))
    if gc:
        lines.append("")
        lines.append("gc:")
        lines.extend(format_gc(snap))
    for section, err in snap.errors.items():
        lines.append(f"! {section}: {err}")
    return "\n".join(lines)


def format_hotspots(
    rows, *, samples: int, rate: float | None = None, mode: str = "wall", limit: int = 25
) -> str:
    """Render hotspot rows (see :meth:`sgrud.profile.Hotspots.rows`) as a table."""
    head = f"hotspots ({mode}): {samples} samples"
    if rate:
        head += f" at {rate:.0f}/s"
    lines = [head, f"{'self%':>6} {'total%':>7} {'self':>7} {'total':>7}  function  file"]
    for r in rows[:limit]:
        lines.append(
            f"{r.self_percent:6.1f} {r.total_percent:7.1f} {r.self_samples:7d} "
            f"{r.total_samples:7d}  {r.funcname}  {short_path(r.filename)}"
        )
    if not rows:
        lines.append("(no samples)")
    return "\n".join(lines)
