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

Keys inside the TUI: `1`-`4` switch tabs, `space` pauses, `r` refreshes,
`+` and `-` change the refresh interval, `q` quits.

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

## Permissions

Reading another process's memory needs ptrace rights. With the default
`kernel.yama.ptrace_scope=1` only child processes can be inspected, so
either use `sgrud run -- ...`, run sgrud with `sudo`, or grant
`CAP_SYS_PTRACE`. sgrud detects this and prints the fix in the error.

A target started with `-X disable-remote-debug` can still be inspected.
That flag only disables code injection, which sgrud does not use.

## Development

```
uv sync
uv run pytest
```

The tests spawn `tests/target_app.py` and inspect it, so they exercise the
real attach path. The Textual app is tested headlessly through its pilot.
