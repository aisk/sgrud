# sgrud

[![CI](https://github.com/aisk/sgrud/actions/workflows/ci.yml/badge.svg)](https://github.com/aisk/sgrud/actions/workflows/ci.yml)

**English** | [简体中文](docs/i18n/README.zh-CN.md) | [日本語](docs/i18n/README.ja.md) | [한국어](docs/i18n/README.ko.md) | [Tiếng Việt](docs/i18n/README.vi.md) | [Français](docs/i18n/README.fr.md) | [Deutsch](docs/i18n/README.de.md)

sgrud (from Scottish Gaelic *sgrùd*, meaning "inspection" or "examination")
is a diagnostic tool for inspecting running Python processes. Attach to a
CPython process and watch its memory, CPU, threads, asyncio tasks, stacks
and garbage collector without slowing it down.

sgrud never stops or instruments the target. It reads interpreter state
straight out of process memory through CPython 3.15's `_remote_debugging`
module (the machinery behind the Tachyon profiler and `python -m asyncio ps`)
and pairs it with what the OS reports for memory and CPU accounting. A
snapshot of every thread's stack costs tens of microseconds and nothing on
the target side.

Requires CPython 3.15 or newer on Linux, macOS or Windows. The target must
run the same major.minor version as sgrud itself. Linux gets the full
picture, see [Platforms](#platforms) for what the others lack.

![The Threads tab, showing each thread's state, CPU usage and current Python stack](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*The Threads tab: every thread with its state, CPU share and live Python stack.*

## Usage

```
sgrud PID                           interactive terminal interface
sgrud run -- python app.py          start the target as a child and inspect it
sgrud PID --web                     the same interface served to a browser

sgrud dump PID                      one text snapshot
sgrud dump PID -n 0.5               keep printing every 0.5 s until the target exits
sgrud dump PID --json               one JSON object per line
sgrud dump run -- python app.py     `run -- CMD` works in place of a pid everywhere

sgrud profile PID                   sample stacks for 5 s, print the hottest functions
sgrud profile PID -d 30 --mode gil  sample for 30 s, count only the thread holding the GIL
sgrud profile PID --mode async      sample asyncio tasks instead of threads
sgrud profile PID --folded          collapsed stacks for flamegraph.pl or speedscope
sgrud profile PID -o out.html       write a flame graph, or .json / .pstats / .txt / .jsonl / a directory
sgrud profile PID -o out.bin        record for `python -m profiling.sampling replay`
sgrud PID --record out.bin          the interface, recording every sample it takes

sgrud probe PID                     run a script inside the target: gc thresholds, allocator, threads
sgrud probe PID -t 10               also count the tracked objects by type, ten most common
```

`--no-stacks`, `--no-tasks`, `--no-gc`, `--no-children` and `--no-ipc` drop
sections you do not need from the interface or the dump.

`examples/demo_app.py` puts something on every tab. It runs busy and blocked
threads, an asyncio task tree, `multiprocessing` workers, pipes, sockets,
shared memory and a file lock a child process holds. Start it and point
sgrud at the pid it prints.

### Sampling modes

- **wall**: every thread with a Python stack counts, so a sleeping thread
  weighs as much as a busy one.
- **gil**: only the GIL holder counts. This answers "where does the CPU go".
- **cpu**: only threads the OS has on a core count, so C code that released
  the GIL still counts and a thread waiting for the GIL does not.
- **exception**: only threads handling an exception count, which shows
  where exceptions are raised and how far they travel before being caught.
- **async**: samples asyncio tasks instead of thread stacks, since a
  coroutine parked in an `await` is on no thread's stack. Each leaf task
  becomes one stack: its own frames, a `<task NAME>` marker, then the frames
  of each task awaiting it up to the root. Every task counts, running or
  suspended, so this answers "what are my tasks waiting on". It is slower
  per sample than reading a stack, so expect a lower achieved rate.

### Output formats

`profile -o PATH` writes the samples in a format of the standard library's
`profiling.sampling` (the Tachyon profiler) instead of printing a table.
The extension picks the format: `.html` is a flame graph, `.json` a Firefox
Profiler document, `.pstats` loads into `pstats.Stats`, `.txt` is collapsed
stacks, `.jsonl` one sample per line and a directory gets a source heat map.
`.bin` is Tachyon's binary format, which
`python -m profiling.sampling replay` converts into any of the others later.
`--baseline old.bin` makes the flame graph a differential one against an
earlier recording, and `--opcodes` records the bytecode instruction of
every frame for the formats that show it. `sgrud PID --record out.bin`
does the same recording under the interface, across mode switches.

### TUI

| Key | Action |
| --- | --- |
| `1`-`7`, `tab`, `shift+tab` | switch tabs |
| `p` / `r` | pause / refresh |
| `+` / `-` | change the refresh interval |
| `q` | quit |
| `f` | thread filter (Hotspots and Flame) |
| `m` | cycle the sampling mode (Hotspots and Flame) |
| `x` | probe the target (GC) |
| `c` | clear samples (Hotspots and Flame) |
| `s` | toggle self/total ordering (Hotspots) |
| `enter` / `backspace` / `esc` | zoom in / out / reset (Flame) |

Arrow keys move through the current tab's content right away. Hotspots and
Flame share one background sampler (`--rate`, default 100 Hz) that keeps
running while you look at other tabs. The flame graph grows from the bottom
and gives each thread its own block on the first row, so an idle thread
shows up as a tall column instead of being mixed into the others.

The GC tab shows the share of wall time spent collecting, collections per
second, the number of tracked objects and a history of collections. The target
only keeps its last 11 young and 3 old collections, so the monitor accumulates
every record it has seen. While the sampler runs the tab also names the
functions collections were triggered from, which is where the allocation churn
is. The Process tab breaks memory down as far as the platform allows, see
[Platforms](#platforms), and lists the target's child processes with their
CPU and memory, marking the ones that are Python interpreters, so a
`multiprocessing` pool or a worker started by a supervisor is one glance
away. Any of them can be inspected with a second `sgrud PID`.

The IPC tab is for the process that hangs: it lists every descriptor the
target has open, pipes, sockets with their addresses and state, shared
memory, files, and for each pipe which of the target's parent and children
hold the other end. Above the table are the descriptor count against its
limit, the shared memory segments and `multiprocessing` semaphores mapped,
and the file locks the target holds or, in red, is blocked waiting for and
by which pid. On Linux the Threads tab adds the system call a sleeping
thread sits in and the descriptor it is on, `read(fd 4)` say, and the IPC
table names the thread next to that descriptor. Only what the kernel
reports is shown: a `futex` wait is a lock or the GIL, and the Python stack
next to it says which.

![The Tasks tab, showing the asyncio task tree with what each task is awaiting](https://github.com/user-attachments/assets/e8f1e9b0-2d8c-4b39-b67a-b9ba3ae2fa1d)

*The Tasks tab: asyncio tasks as a tree, each with the coroutine frames it is parked in.*

![The Hotspots tab, listing the functions with the most CPU samples](https://github.com/user-attachments/assets/32c75ac9-e279-41e8-83a2-b7e2970275e4)

*The Hotspots tab: functions ranked by self and total samples from the background sampler.*

![The Flame tab, showing a flame graph of the sampled stacks](https://github.com/user-attachments/assets/b78cfbec-3619-45a6-a2a9-5bc57f2e45f3)

*The Flame tab: the same samples as a flame graph, one block per thread on the first row.*

### Web

`--web` serves the same interface to a browser through
[textual-serve](https://github.com/Textualize/textual-serve), which is an
optional dependency, so install `sgrud[web]` to get it. It listens on
`http://127.0.0.1:8000` unless `--host` and `--port` say otherwise. Every
browser tab gets its own copy of the interface attached to the same
target. There is no authentication, so keep it on localhost or behind
something that provides one. With `run -- CMD` on Linux the target is
started allowing any process of the same user to read it, since the
browser sessions are not its parent.

### Probing

Everything above reads the target's memory from outside. `sgrud probe`
is the one exception: it uses `sys.remote_exec` to have the target's main
thread run a short script at its next safe point, which reports what the
interpreter does not export in memory. That is the GC thresholds and the
counters they are compared to, whether the collector is enabled, how many
objects are frozen or in `gc.garbage`, the allocator's block count, module
and thread counts, with `-t N` a histogram of tracked objects by type, and
a tracemalloc snapshot when the target already has tracing on. `x` on the
GC tab runs the same probe. It costs the target a few milliseconds on its
main thread, more with a type histogram, and it waits for that thread to
reach a safe point, so a main thread stuck in C code makes it time out.
sgrud never runs it on its own.

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
    print(snap.gc[0].rate, snap.gc_time_share, snap.gc[0].history[:1])
    print(snap.process.memory.anon, snap.process.fault_rate, snap.process.limits)
    print([(c.pid, c.python, c.rss) for c in snap.children])
    print(snap.ipc.num_fds, snap.ipc.locks, [(f.fd, f.kind, f.target) for f in snap.ipc.files])
    print([(t.name, t.syscall.describe()) for t in snap.threads if t.syscall])
    print(snap.to_dict())  # JSON friendly
    result = m.probe(types=5)  # runs code in the target, see Probing
    print(result.gc_threshold, result.gc_count, result.types)
```

`Monitor.stream(interval)` yields snapshots until the target exits, then
raises `ProcessExited`. CPU percentages need two snapshots, so the first
one reports `None`.

For profiling, `Sampler` runs `Monitor.sample_stacks()` in a background
thread and feeds a `Hotspots` aggregator:

```python
from sgrud.sampler import Sampler

from sgrud.export import Recorder

flame = Recorder("profile.html", interval=1 / 500)
with Sampler(monitor, rate=500, mode="gil", recorders=[flame]) as sampler:
    time.sleep(5)
sampler.close()  # writes profile.html
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()  # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## Platforms

Stacks, asyncio tasks, GC and the profiler come from `_remote_debugging`
and behave the same everywhere. Process and thread accounting comes from
the OS through psutil, and that is where the platforms differ.

- **Linux** reports everything, including per-thread scheduler state, which is
  what marks a thread as on CPU in wall mode, and the full memory picture: the
  anonymous and file backed parts of rss, USS and PSS, the brk heap and
  anonymous mappings, transparent huge pages, page fault rates, the cgroup
  memory limit and the OOM score. It is also the only platform with the full
  IPC picture: pipes and their other ends, shared memory, file locks and the
  system call each thread is blocked in (which needs the same access as
  reading memory, and a call table sgrud has for x86_64, aarch64, riscv64
  and loongarch64; elsewhere calls show by number).
- **Windows** has thread names and per-thread CPU time but no scheduler state,
  so threads read as `?` instead of `cpu` / `idle` in wall mode. Memory is
  `rss`, `vms`, the peak working set, private bytes, USS and the page fault
  rate. IPC is the handle count, open files and sockets, without descriptor
  numbers.
- **macOS** cannot match OS threads to the interpreter's thread ids, so
  threads show without names or CPU figures. Memory is `rss`, `vms`, USS and
  the page fault rate. IPC is the descriptor count, open files and sockets.
  Reading another process's memory needs root, so run sgrud with `sudo`.

## Permissions

Memory, CPU and thread names come from the OS and work for any process
you own. Everything else reads the target's memory. On Linux that needs
ptrace rights, and the default `kernel.yama.ptrace_scope=1` only grants
those for child processes. Without them sgrud attaches in limited mode and
shows a banner explaining what is missing. To get everything, start the
target through `sgrud run -- ...`, run sgrud with `sudo`, grant
`CAP_SYS_PTRACE`, or relax Yama for the session:

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

On macOS only root can read another process's memory, so use `sudo`. On
Windows any process of the same user works, others need an administrator.

Pass `require_full=True` to `Monitor.attach` to fail instead of degrading.
A target started with `-X disable-remote-debug` can still be inspected,
since that flag only disables code injection. The one thing it blocks is
`sgrud probe`.

## Development

```
uv sync
uv run pytest
```

The tests spawn `tests/target_app.py` and inspect it, so they exercise the
real attach path. The Textual app is tested headlessly through its pilot.
