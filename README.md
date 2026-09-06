# sgrud

Attach to a running CPython process and watch its memory, CPU, threads,
asyncio tasks, stacks and garbage collector without slowing it down.

sgrud never stops or instruments the target. It reads the interpreter's
state straight out of process memory through CPython 3.15's
`_remote_debugging` module (the same machinery behind the Tachyon sampling
profiler and `python -m asyncio ps`) and pairs it with `/proc` for memory
and CPU accounting. A full snapshot of stacks for every thread costs tens
of microseconds on the sgrud side and nothing on the target side.

Requires Linux and CPython 3.15 or newer. The target must run the same
major.minor version as sgrud itself.

## Usage

```
sgrud PID                      one text snapshot
sgrud PID -n 0.5               keep printing every 0.5 s until the target exits
sgrud PID --json               one JSON object per line, easy to pipe elsewhere
sgrud run -- python app.py     start the target as a child and inspect it
sgrud tui PID                  interactive terminal interface
sgrud tui run -- python app.py
```

`--no-stacks`, `--no-tasks` and `--no-gc` drop sections you do not need.

```
sgrud PID --profile 5            sample stacks for 5 s, print the hottest functions
sgrud PID --profile 5 --mode gil count only the thread holding the GIL
sgrud PID --profile 5 --folded   collapsed stacks for flamegraph.pl or speedscope
```

Keys inside the TUI: `1`-`6` switch tabs, `space` pauses, `r` refreshes,
`+` and `-` change the refresh interval, `q` quits. On the Hotspots and
Flame tabs `m` toggles wall/gil mode and `c` clears the samples, and on
Hotspots `s` toggles self/total ordering. Both tabs are fed by one
background sampler (`--rate`, default 100 Hz) that keeps running while
you look at the other tabs, and share the thread filter.

The Flame tab draws the sampled stacks as a flame graph with the root at
the bottom. With all threads selected each thread is a block of its own
on the first row, so a sleeping thread shows up as a tall idle column
rather than being mixed into the others. Arrow keys move a cursor between
frames, `enter` zooms into the frame under the cursor, `backspace` zooms
out one level and `esc` resets the zoom.

In wall mode every thread with a Python stack counts, so a sleeping thread
weighs as much as a busy one. In gil mode only the GIL holder counts, which
answers "where does the CPU time go" for CPython code.

## Library

The TUI is only a front end. Everything comes from `Monitor`, which
returns plain frozen dataclasses:

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:          # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()                  # snapshot(stacks=..., tasks=..., gc=...)
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.cpu_percent, t.frames[:1])
    for task in snap.tasks:
        print(task.name, task.parent_ids, [f.funcname for f in task.frames])
    print(snap.gc[0].collections, snap.gc[0].history[:1])
    print(snap.to_dict())                # JSON friendly
```

`Monitor.stream(interval)` yields snapshots until the target exits, at which
point it raises `ProcessExited`. CPU percentages need two snapshots, so the
first one reports `None`.

For profiling, `Sampler` runs `Monitor.sample_stacks()` in a background
thread and feeds a `Hotspots` aggregator:

```python
from sgrud.sampler import Sampler

with Sampler(monitor, rate=500, mode="gil") as sampler:
    time.sleep(5)
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()        # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## Permissions

Memory, CPU, thread names and per-thread CPU come from `/proc` and work
for any process you own. Stacks, GIL state, asyncio tasks, GC statistics
and hotspots need to read the target's memory, which needs ptrace rights.
With the default `kernel.yama.ptrace_scope=1` that is only granted for
child processes.

Without those rights sgrud still attaches in limited mode and shows what it
can, with a banner explaining what is missing. To get everything, either
start the target through `sgrud run -- ...`, run sgrud with `sudo`, grant
`CAP_SYS_PTRACE`, or relax Yama for the session:

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

Pass `require_full=True` to `Monitor.attach` to fail instead of degrading.

A target started with `-X disable-remote-debug` can still be inspected.
That flag only disables code injection, which sgrud does not use.

## Development

```
uv sync
uv run pytest
```

The tests spawn `tests/target_app.py` and inspect it, so they exercise the
real attach path. The Textual app is tested headlessly through its pilot.
