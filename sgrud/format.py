"""Plain text rendering of snapshots, shared by the CLI and usable in tests."""

from __future__ import annotations

import math
import os
from collections.abc import Iterable

from .models import Frame, Snapshot, Task, Thread


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


def format_gc(snap: Snapshot) -> list[str]:
    lines = []
    for g in snap.gc:
        lines.append(
            f"gen{g.generation}: {g.collections} collections, {g.collected} collected, "
            f"{g.uncollectable} uncollectable, total {human_duration(g.total_duration)}, "
            f"heap {g.heap_size}"
        )
        for c in g.history[:3]:
            lines.append(
                f"    last: {human_duration(c.duration)} collected={c.collected} "
                f"candidates={c.candidates} heap={c.heap_size}"
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
    m = p.memory
    lines = [
        f"pid {p.pid}  {os.path.basename(p.exe) or '?'}  {' '.join(p.cmdline)[:80]}",
        f"state={p.state} threads={p.num_threads} uptime={human_duration(p.uptime)} "
        f"cpu={percent(p.cpu_percent)}%  utime={p.user_time:.2f}s stime={p.system_time:.2f}s",
        f"rss={human_bytes(m.rss)} vms={human_bytes(m.vms)} hwm={human_bytes(m.hwm)} "
        f"swap={human_bytes(m.swap)} data={human_bytes(m.data)} shared={human_bytes(m.shared)}",
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
