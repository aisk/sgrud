"""Run a small script inside the target to read what memory alone cannot.

Everything else in sgrud reads the target's memory from outside. The
probe is the one exception: it uses :func:`sys.remote_exec` (PEP 768) to
have the target's main thread run a short script at its next safe point,
which writes its findings to a file for sgrud to pick up. That is how the
GC thresholds and counters, the allocator's block count, a histogram of
tracked objects by type and a tracemalloc snapshot become reachable, since
``_Py_DebugOffsets`` exports none of them.

The cost is real: the script runs on the target's main thread, so it
waits for that thread to reach a safe point and takes time from it, a
type histogram walks every tracked object, and a target started with
``-X disable-remote-debug`` refuses it. It is therefore never run unless
asked for explicitly.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from .errors import ProcessExited, SgrudError

#: What the script writes back, as a JSON object.
_SCRIPT = """
import gc, json, os, sys, threading, time
_t0 = time.perf_counter()
_out = {}
try:
    import tracemalloc
    _out["gc"] = {
        "threshold": gc.get_threshold(),
        "count": gc.get_count(),
        "enabled": gc.isenabled(),
        "frozen": gc.get_freeze_count(),
        "garbage": len(gc.garbage),
    }
    _out["allocated_blocks"] = sys.getallocatedblocks()
    _out["modules"] = len(sys.modules)
    _out["threads"] = [t.name for t in threading.enumerate()]
    _out["switch_interval"] = sys.getswitchinterval()
    _out["cwd"] = os.getcwd()
    if TYPES:
        _objects = gc.get_objects()
        _counts = {}
        for _o in _objects:
            _name = type(_o).__name__
            _counts[_name] = _counts.get(_name, 0) + 1
        _out["tracked"] = len(_objects)
        _out["types"] = sorted(_counts.items(), key=lambda kv: -kv[1])[:TYPES]
        del _objects, _counts
    if tracemalloc.is_tracing():
        _stats = tracemalloc.take_snapshot().statistics("lineno")[:ALLOCATIONS]
        _out["tracemalloc"] = {
            "traced": tracemalloc.get_traced_memory()[0],
            "peak": tracemalloc.get_traced_memory()[1],
            "top": [
                (f.traceback[0].filename, f.traceback[0].lineno, f.size, f.count) for f in _stats
            ],
        }
except BaseException as _e:
    _out = {"error": f"{type(_e).__name__}: {_e}"}
_out["elapsed"] = time.perf_counter() - _t0
with open(RESULT + ".tmp", "w") as _f:
    json.dump(_out, _f)
os.replace(RESULT + ".tmp", RESULT)
"""


@dataclass(frozen=True, slots=True)
class TypeCount:
    """How many tracked objects of one type the target holds."""

    name: str
    count: int


@dataclass(frozen=True, slots=True)
class Allocation:
    """One line of a tracemalloc snapshot, biggest first."""

    filename: str
    lineno: int
    size: int
    count: int


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the target reported about itself."""

    pid: int
    #: ``gc.get_threshold()``.
    gc_threshold: tuple[int, int, int]
    #: ``gc.get_count()``: allocations minus deallocations since the last
    #: collection of each generation, what the thresholds are compared to.
    gc_count: tuple[int, int, int]
    gc_enabled: bool
    #: Objects moved out of the collector's reach with ``gc.freeze()``.
    gc_frozen: int
    #: ``len(gc.garbage)``.
    gc_garbage: int
    #: ``sys.getallocatedblocks()``: blocks the object allocator holds.
    allocated_blocks: int
    #: ``len(sys.modules)``.
    modules: int
    #: Names from ``threading.enumerate()``.
    thread_names: tuple[str, ...]
    switch_interval: float
    cwd: str
    #: Seconds the script took inside the target.
    elapsed: float
    #: Seconds from asking to the answer arriving.
    round_trip: float
    #: Objects the collector tracks, -1 when no type histogram was asked for.
    tracked: int = -1
    #: The most common types among tracked objects, when asked for.
    types: tuple[TypeCount, ...] = ()
    #: Only when the target has tracemalloc running.
    tracemalloc_traced: int = 0
    tracemalloc_peak: int = 0
    allocations: tuple[Allocation, ...] = ()

    @property
    def tracing(self) -> bool:
        return self.tracemalloc_peak > 0 or bool(self.allocations)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def probe(pid: int, *, types: int = 0, allocations: int = 10, timeout: float = 5.0) -> ProbeResult:
    """Run the probe script in ``pid`` and wait for its answer.

    ``types`` is how many entries the type histogram should have, 0 to skip
    it, which also skips walking every tracked object. ``allocations`` is
    how many tracemalloc lines to bring back when the target is tracing.
    Raises SgrudError when the target refuses or does not answer within
    ``timeout`` seconds, which happens when its main thread is stuck in C
    code or the target was started with ``-X disable-remote-debug``.
    """
    if not hasattr(sys, "remote_exec"):
        raise SgrudError("this Python has no sys.remote_exec, so it cannot probe")
    fd, result = tempfile.mkstemp(prefix="sgrud-probe-", suffix=".json")
    os.close(fd)
    os.unlink(result)  # the script creates it, its presence means done
    script = result[: -len(".json")] + ".py"
    with open(script, "w") as f:
        f.write(f"TYPES = {int(types)}\nALLOCATIONS = {int(allocations)}\nRESULT = {result!r}\n")
        f.write(_SCRIPT)
    started = time.monotonic()
    try:
        try:
            sys.remote_exec(pid, script)
        except ProcessLookupError as e:
            raise ProcessExited(pid) from e
        except PermissionError as e:
            raise SgrudError(f"cannot probe process {pid}: {e}") from e
        except RuntimeError as e:
            hint = ""
            if "not enabled" in str(e):
                hint = " (started with -X disable-remote-debug or PYTHON_DISABLE_REMOTE_DEBUG)"
            raise SgrudError(f"process {pid} refused the probe: {e}{hint}") from e
        except OSError as e:
            raise SgrudError(f"cannot probe process {pid}: {e}") from e
        while not os.path.exists(result):
            if time.monotonic() - started > timeout:
                raise SgrudError(
                    f"process {pid} did not run the probe within {timeout:g}s. Its main "
                    "thread may be blocked in C code, or it stopped responding."
                )
            time.sleep(0.005)
        with open(result) as f:
            data = json.load(f)
    finally:
        for path in (script, result, result + ".tmp"):
            try:
                os.unlink(path)
            except OSError:
                pass
    if "error" in data:
        raise SgrudError(f"the probe failed inside process {pid}: {data['error']}")
    trace = data.get("tracemalloc") or {}
    return ProbeResult(
        pid=pid,
        gc_threshold=_triple(data["gc"]["threshold"]),
        gc_count=_triple(data["gc"]["count"]),
        gc_enabled=bool(data["gc"]["enabled"]),
        gc_frozen=data["gc"]["frozen"],
        gc_garbage=data["gc"]["garbage"],
        allocated_blocks=data["allocated_blocks"],
        modules=data["modules"],
        thread_names=tuple(data["threads"]),
        switch_interval=data["switch_interval"],
        cwd=data["cwd"],
        elapsed=data["elapsed"],
        round_trip=time.monotonic() - started,
        tracked=data.get("tracked", -1),
        types=tuple(TypeCount(name, n) for name, n in data.get("types", ())),
        tracemalloc_traced=trace.get("traced", 0),
        tracemalloc_peak=trace.get("peak", 0),
        allocations=tuple(Allocation(*item) for item in trace.get("top", ())),
    )


def _triple(values: Any) -> tuple[int, int, int]:
    a, b, c = (int(v) for v in values)
    return a, b, c
