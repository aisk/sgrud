"""Record samples in the formats of the standard library's ``profiling.sampling``.

The Tachyon profiler that ships with CPython 3.15 reads the same raw
samples sgrud does, so its collectors can consume sgrud's samples
unchanged. A :class:`Recorder` wraps one of them: feed it every
:class:`~sgrud.remote.RawSample` the :class:`~sgrud.sampler.Sampler`
takes and it writes a flame graph, a Firefox Profiler document, a pstats
file, a source heat map or Tachyon's binary format, which
``python -m profiling.sampling replay`` turns into any of the others
later, or into a differential flame graph against a second recording.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import time
from typing import Any

from .errors import SgrudError
from .remote import RawSample

#: Output formats and the file extension each one is recognised by.
FORMATS = {
    "binary": "bin",
    "flamegraph": "html",
    "gecko": "json",
    "pstats": "pstats",
    "collapsed": "txt",
    "heatmap": "",
    "jsonl": "jsonl",
}

_BY_EXTENSION = {ext: fmt for fmt, ext in FORMATS.items() if ext}


def guess_format(path: str) -> str:
    """The format a path implies: by extension, or ``heatmap`` for a directory."""
    if os.path.isdir(path):
        return "heatmap"
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if not ext:
        return "heatmap"
    try:
        return _BY_EXTENSION[ext]
    except KeyError:
        known = ", ".join(f".{e}" for e in _BY_EXTENSION)
        raise SgrudError(
            f"cannot tell the output format from {path!r}, use one of {known} or pass --format"
        ) from None


def check_output(
    path: str, format: str | None = None, *, mode: str = "cpu", baseline: str | None = None
) -> str:
    """The format a :class:`Recorder` would write, or SgrudError if it cannot.

    Touches no file, so arguments can be checked before there is anything
    to record.
    """
    format = format or guess_format(path)
    if format not in FORMATS:
        raise SgrudError(f"unknown format {format!r}, use one of {', '.join(FORMATS)}")
    if format == "binary" and mode == "async":
        raise SgrudError("the binary format holds thread stacks, not task stacks")
    if baseline is not None:
        if format != "flamegraph":
            raise SgrudError("a baseline only makes sense for a flamegraph")
        if not os.path.exists(baseline):
            raise SgrudError(f"baseline {baseline!r} does not exist")
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise SgrudError(f"cannot write {path!r}: directory {parent!r} does not exist")
    if not os.access(parent, os.W_OK):
        raise SgrudError(f"cannot write {path!r}: directory {parent!r} is not writable")
    return format


class Recorder:
    """Feeds raw samples to one ``profiling.sampling`` collector.

    ``interval`` is the intended seconds between samples, which the
    formats use to turn counts into time. ``mode`` is the sampling mode
    the samples were taken in. ``baseline`` is a binary recording to
    compare against, which turns ``flamegraph`` into a differential one.
    ``opcodes`` says the samples carry opcodes, which gecko turns into
    interval markers. The binary format is written as samples arrive, the others on
    :meth:`close`.
    """

    def __init__(
        self,
        path: str,
        format: str | None = None,
        *,
        interval: float,
        mode: str = "cpu",
        baseline: str | None = None,
        opcodes: bool = False,
    ):
        self.path = path
        self.format = check_output(path, format, mode=mode, baseline=baseline)
        self.mode = mode
        self.interval = interval
        self.samples = 0
        self.failed = 0
        self.started = time.monotonic()
        self._collector = _make_collector(
            self.format, path, interval, baseline, mode=mode, opcodes=opcodes
        )

    def collect(self, sample: RawSample) -> None:
        if sample.mode != self.mode:
            raise SgrudError(f"cannot record {sample.mode} samples in a {self.mode} recording")
        self._collector.collect(sample.data)
        self.samples += 1

    def collect_failed(self) -> None:
        self._collector.collect_failed_sample()
        self.failed += 1

    def close(self) -> None:
        """Write the output. The collectors narrate progress, which is swallowed."""
        elapsed = time.monotonic() - self.started
        rate = self.samples / elapsed if elapsed > 0 else 0.0
        attempts = self.samples + self.failed
        if hasattr(self._collector, "set_stats"):
            self._collector.set_stats(
                int(self.interval * 1_000_000),
                elapsed,
                rate,
                100.0 * self.failed / attempts if attempts else 0.0,
                max(0.0, 100.0 * (1 - self.samples * self.interval / elapsed)) if elapsed else 0.0,
                mode=_mode_value(self.mode),
            )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self._collector.export(None if self.format == "binary" else self.path)


def _stdlib(module: str, name: str) -> Any:
    """An attribute of ``profiling.sampling``, which has no type stubs yet."""
    return getattr(importlib.import_module(f"profiling.sampling.{module}"), name)


_COLLECTORS = {
    "flamegraph": ("stack_collector", "FlamegraphCollector"),
    "gecko": ("gecko_collector", "GeckoCollector"),
    "pstats": ("pstats_collector", "PstatsCollector"),
    "collapsed": ("stack_collector", "CollapsedStackCollector"),
    "heatmap": ("heatmap_collector", "HeatmapCollector"),
    "jsonl": ("jsonl_collector", "JsonlCollector"),
}


def _mode_value(mode: str) -> int:
    """The ``profiling.sampling`` constant for a sampling mode, wall if it has none."""
    names = _stdlib("constants", "PROFILING_MODE_NAMES")
    modes = {name: value for value, name in names.items()}
    return modes.get(mode, modes["wall"])


def _make_collector(
    format: str, path: str, interval: float, baseline: str | None, *, mode: str, opcodes: bool
) -> Any:
    usec = max(int(interval * 1_000_000), 1)
    if format == "binary":
        return _stdlib("binary_collector", "BinaryCollector")(path, usec)
    if baseline is not None:
        cls = _stdlib("stack_collector", "DiffFlamegraphCollector")
        return cls(usec, baseline_binary_path=baseline)
    module, name = _COLLECTORS[format]
    if format == "gecko":
        return _stdlib(module, name)(usec, opcodes=opcodes)
    if format == "jsonl":
        return _stdlib(module, name)(usec, mode=_mode_value(mode))
    return _stdlib(module, name)(usec)
