"""Background stack sampler feeding a :class:`~sgrud.profile.Hotspots`.

Runs in a plain thread so it works with or without the Textual UI. The
Monitor serializes access to the remote unwinder, so the UI can keep taking
snapshots while the sampler runs. Every sample can also go to any number
of :class:`~sgrud.export.Recorder` objects, which write it out in the
formats of the standard library's ``profiling.sampling``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from .errors import ProcessExited, SgrudError
from .export import Recorder
from .monitor import Monitor
from .profile import Hotspots
from .remote import MODES


class Sampler:
    """Samples ``monitor`` ``rate`` times a second in sampling ``mode``.

    ``mode`` is one of :data:`sgrud.remote.MODES` and may be changed while
    the sampler runs, the next sample uses it. Samples of different modes
    are not comparable, so reset :attr:`hotspots` when switching.
    """

    def __init__(
        self,
        monitor: Monitor,
        hotspots: Hotspots | None = None,
        *,
        rate: float = 100.0,
        mode: str = "cpu",
        recorders: Iterable[Recorder] = (),
    ):
        if rate <= 0:
            raise ValueError("rate must be positive")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.monitor = monitor
        self.hotspots = hotspots if hotspots is not None else Hotspots()
        self.mode = mode
        self.rate = rate
        self.recorders = list(recorders)
        self.errors = 0
        self.last_error: str | None = None
        #: Set when the target went away while sampling.
        self.exited: ProcessExited | None = None
        self._stop = threading.Event()
        # Every thread started and not yet seen to finish.
        self._threads: list[threading.Thread] = []
        # Held while a sample is recorded, and to stop, so a stopped
        # thread cannot record next to its successor.
        self._record_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return not self._stop.is_set() and any(t.is_alive() for t in self._threads)

    def start(self) -> Sampler:
        if self.running:
            return self
        # Every thread gets its own event, so one still finishing a slow
        # read keeps its stop and does not sample next to its successor.
        self._stop = threading.Event()
        thread = threading.Thread(
            target=self._run, args=(self._stop,), name="sgrud-sampler", daemon=True
        )
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()
        return self

    def stop(self, timeout: float | None = 2.0) -> None:
        """Stop sampling, waiting up to ``timeout`` seconds for the thread.

        Once this returns no sample is recorded any more, even by a thread
        still in a read after the timeout.
        """
        with self._record_lock:
            self._stop.set()
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        self._threads = [t for t in self._threads if t.is_alive()]

    def __enter__(self) -> Sampler:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def close(self) -> None:
        """Stop sampling and write out every recorder."""
        # A read still in flight after the timeout records nothing, see stop().
        self.stop()
        for recorder in self.recorders:
            recorder.close()
        self.recorders.clear()

    def _sample(self, stop: threading.Event) -> None:
        # The mode is read on every sample so the UI can switch it live.
        mode = self.mode
        try:
            sample = self.monitor.sample(mode)
        except Exception:
            with self._record_lock:
                if not stop.is_set():
                    for recorder in self.recorders:
                        recorder.collect_failed()
            raise
        with self._record_lock:
            if stop.is_set():
                # Stopped during the read, maybe with a successor already
                # sampling into the same recorders.
                return
            if mode == "async":
                self.hotspots.add_tasks(sample.tasks())
            else:
                self.hotspots.add(sample.stacks())
            for recorder in self.recorders:
                recorder.collect(sample)

    def _run(self, stop: threading.Event) -> None:
        period = 1.0 / self.rate
        next_at = time.monotonic()
        while not stop.is_set():
            try:
                self._sample(stop)
            except ProcessExited as e:
                self.exited = e
                self.hotspots.freeze()
                return
            except SgrudError as e:
                self.errors += 1
                self.last_error = str(e)
            except Exception as e:
                # A torn read while the target mutates its frames is normal
                # for a sampling profiler. Count it and carry on.
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
            next_at += period
            delay = next_at - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            else:
                # We fell behind. Do not try to catch up with a burst.
                next_at = time.monotonic()
