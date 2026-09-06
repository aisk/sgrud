# sgrud

sgrud (from Scottish Gaelic *sgrùd*, meaning "inspection" or "examination")
is a diagnostic tool for inspecting running Python processes. Attach to a
CPython process and watch its memory, CPU, threads, asyncio tasks, stacks
and garbage collector without slowing it down.

sgrud never stops or instruments the target. It reads interpreter state
straight out of process memory through CPython 3.15's `_remote_debugging`
module (the machinery behind the Tachyon profiler and `python -m asyncio ps`)
and pairs it with `/proc` for memory and CPU accounting. A snapshot of every
thread's stack costs tens of microseconds and nothing on the target side.

Requires Linux and CPython 3.15 or newer. The target must run the same
major.minor version as sgrud itself.

## Usage

```
sgrud PID                           one text snapshot
sgrud PID -n 0.5                    keep printing every 0.5 s until the target exits
sgrud PID --json                    one JSON object per line
sgrud run -- python app.py          start the target as a child and inspect it
sgrud tui PID                       interactive terminal interface
sgrud tui run -- python app.py

sgrud PID --profile 5               sample stacks for 5 s, print the hottest functions
sgrud PID --profile 5 --mode gil    count only the thread holding the GIL
sgrud PID --profile 5 --mode async  sample asyncio tasks instead of threads
sgrud PID --profile 5 --folded      collapsed stacks for flamegraph.pl or speedscope
```

`--no-stacks`, `--no-tasks` and `--no-gc` drop sections you do not need.

### Sampling modes

- **wall**: every thread with a Python stack counts, so a sleeping thread
  weighs as much as a busy one.
- **gil**: only the GIL holder counts. This answers "where does the CPU go".
- **async**: samples asyncio tasks instead of thread stacks, since a
  coroutine parked in an `await` is on no thread's stack. Each leaf task
  becomes one stack: its own frames, a `<task NAME>` marker, then the frames
  of each task awaiting it up to the root. Every task counts, running or
  suspended, so this answers "what are my tasks waiting on". It is slower
  per sample than reading a stack, so expect a lower achieved rate.

### TUI

| Key | Action |
| --- | --- |
| `1`-`6`, `tab`, `shift+tab` | switch tabs |
| `p` / `r` | pause / refresh |
| `+` / `-` | change the refresh interval |
| `q` | quit |
| `f` | thread filter (Hotspots and Flame) |
| `m` | cycle wall/gil/async mode (Hotspots and Flame) |
| `c` | clear samples (Hotspots and Flame) |
| `s` | toggle self/total ordering (Hotspots) |
| `enter` / `backspace` / `esc` | zoom in / out / reset (Flame) |

Arrow keys move through the current tab's content right away. Hotspots and
Flame share one background sampler (`--rate`, default 100 Hz) that keeps
running while you look at other tabs. The flame graph grows from the bottom
and gives each thread its own block on the first row, so an idle thread
shows up as a tall column instead of being mixed into the others.

## Library

The TUI is only a front end. Everything comes from `Monitor`, which
returns plain frozen dataclasses:

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()  # snapshot(stacks=..., tasks=..., gc=...)
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.cpu_percent, t.frames[:1])
    for task in snap.tasks:
        print(task.name, task.parent_ids, [f.funcname for f in task.frames])
    print(snap.gc[0].collections, snap.gc[0].history[:1])
    print(snap.to_dict())  # JSON friendly
```

`Monitor.stream(interval)` yields snapshots until the target exits, then
raises `ProcessExited`. CPU percentages need two snapshots, so the first
one reports `None`.

For profiling, `Sampler` runs `Monitor.sample_stacks()` in a background
thread and feeds a `Hotspots` aggregator:

```python
from sgrud.sampler import Sampler

with Sampler(monitor, rate=500, mode="gil") as sampler:
    time.sleep(5)
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()  # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## Permissions

Memory, CPU and thread names come from `/proc` and work for any process
you own. Everything else reads the target's memory, which needs ptrace
rights, and the default `kernel.yama.ptrace_scope=1` only grants those for
child processes. Without them sgrud attaches in limited mode and shows a
banner explaining what is missing. To get everything, start the target
through `sgrud run -- ...`, run sgrud with `sudo`, grant `CAP_SYS_PTRACE`,
or relax Yama for the session:

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

Pass `require_full=True` to `Monitor.attach` to fail instead of degrading.
A target started with `-X disable-remote-debug` can still be inspected,
since that flag only disables code injection, which sgrud does not use.

## Development

```
uv sync
uv run pytest
```

The tests spawn `tests/target_app.py` and inspect it, so they exercise the
real attach path. The Textual app is tested headlessly through its pilot.
