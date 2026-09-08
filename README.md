# sgrud

[![CI](https://github.com/aisk/sgrud/actions/workflows/ci.yml/badge.svg)](https://github.com/aisk/sgrud/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/sgrud)](https://pypi.org/project/sgrud/)

**English** | [简体中文](docs/i18n/README.zh-CN.md) | [日本語](docs/i18n/README.ja.md) | [한국어](docs/i18n/README.ko.md) | [Tiếng Việt](docs/i18n/README.vi.md) | [Français](docs/i18n/README.fr.md) | [Deutsch](docs/i18n/README.de.md)

sgrud (from Scottish Gaelic *sgrùd*, meaning "inspection" or "examination") is a diagnostic tool for inspecting running Python processes. Attach to a CPython process and watch its memory, CPU, threads, asyncio tasks, stacks and garbage collector without slowing it down.

sgrud never stops or instruments the target. It reads interpreter state straight out of process memory through CPython 3.15's `_remote_debugging` module (the machinery behind the Tachyon profiler and `python -m asyncio ps`) and pairs it with what the OS reports through psutil for memory, CPU, threads and open files. A snapshot of every thread's stack costs tens of microseconds and nothing on the target side.

![The Threads tab, showing each thread's state, CPU usage and current Python stack](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*The Threads tab: every thread with its state, CPU share and live Python stack.*

## Features

- **Process**: memory broken down as far as the platform allows, CPU, page faults, limits, the cgroup's quota and throttling, and the child processes with the Python interpreters among them marked.
- **Threads**: every thread with its state, CPU share, live Python stack and, on Linux, the system call it is blocked in.
- **Tasks**: the asyncio task tree, each task with the coroutine frames it is parked in.
- **GC**: time spent collecting, collection rate, tracked objects, a history of collections and the functions that triggered them.
- **Hotspots** and **Flame**: a background sampling profiler with wall, GIL, CPU, exception and asyncio task modes, shown as a table or a flame graph.
- **IPC**: open descriptors, pipes and who holds their other ends, sockets, shared memory and file locks, for the process that hangs.
- `sgrud dump` prints the same as text or JSON, `sgrud profile` samples for a while and writes any Tachyon format, and `sgrud probe` asks the target for what memory alone cannot show. `--web` serves the interface to a browser.
- A `Monitor` class that returns plain dataclasses, so all of it is available as a library.

## Installation

```
pip install sgrud
pip install "sgrud[web]"    # adds --web
```

`uv tool install sgrud` and `pipx install sgrud` work too. sgrud needs CPython 3.15 or newer on Linux, macOS or Windows, and the target must run the same major.minor version as sgrud itself.

Memory, CPU and thread names work for any process you own. Reading the interpreter's state needs ptrace rights on Linux, root on macOS and the same user on Windows. Without them sgrud attaches in limited mode and says what is missing. The simplest way to get everything is to start the target through sgrud, see [Permissions](docs/reference.md#permissions) for the other ways.

## Usage

```
sgrud PID                           interactive terminal interface
sgrud run -- python app.py          start the target as a child and inspect it
sgrud PID --web                     the same interface served to a browser

sgrud dump PID                      one text snapshot, --json for JSON
sgrud profile PID -d 30 --mode gil  sample the GIL holder for 30 s, print the hottest functions
sgrud profile PID -o out.html       write a flame graph instead
sgrud probe PID                     run a script inside the target: gc thresholds, allocator, threads
```

`examples/demo_app.py` puts something on every tab. Start it and point sgrud at the pid it prints.

From Python:

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.frames[:1])
```

The [reference](docs/reference.md) covers every command and option, the sampling modes and output formats, the TUI's keys and tabs, the library, what each platform reports and how to get the permissions.

## Development

```
uv sync
uv run pytest
```

The tests spawn `tests/target_app.py` and inspect it, so they exercise the real attach path. The Textual app is tested headlessly through its pilot.
