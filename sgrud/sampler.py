"""Background stack sampler feeding a :class:`~sgrud.profile.Hotspots`.

Runs in a plain thread so it works with or without the Textual UI. The
Monitor serializes access to the remote unwinder, so the UI can keep taking
snapshots while the sampler runs.
"""

from __future__ import annotations

import threading
import time

from .errors import ProcessExited, SgrudError
from .monitor import Monitor
from .profile import Hotspots


class Sampler:
    def __init__(
        self,
        monitor: Monitor,
        hotspots: Hotspots | None = None,
        *,
        rate: float = 100.0,
        mode: str = "wall",
    ):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.monitor = monitor
        self.hotspots = hotspots if hotspots is not None else Hotspots(mode)
        self.rate = rate
        self.errors = 0
        self.last_error: str | None = None
        #: Set when the target went away while sampling.
        self.exited: ProcessExited | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> Sampler:
        if self.running:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sgrud-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def __enter__(self) -> Sampler:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _sample(self) -> None:
        # The mode is read on every sample so the UI can switch it live.
        if self.hotspots.mode == "async":
            self.hotspots.add_tasks(self.monitor.sample_tasks())
        else:
            self.hotspots.add(self.monitor.sample_stacks())

    def _run(self) -> None:
        period = 1.0 / self.rate
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                self._sample()
            except ProcessExited as e:
                self.exited = e
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
                self._stop.wait(delay)
            else:
                # We fell behind. Do not try to catch up with a burst.
                next_at = time.monotonic()
