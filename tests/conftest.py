import pathlib
import subprocess
import sys
import time

import pytest

from sgrud import Monitor

TARGET = pathlib.Path(__file__).with_name("target_app.py")


def spawn_target(*extra: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, str(TARGET), *extra],
        stdout=subprocess.PIPE,
    )
    if not extra:
        line = proc.stdout.readline()
        assert line.strip() == b"READY", line
    return proc


@pytest.fixture(scope="module")
def target():
    proc = spawn_target()
    yield proc
    proc.kill()
    proc.wait()


@pytest.fixture(scope="module")
def monitor(target):
    with Monitor.attach(target.pid) as m:
        # Prime CPU counters so later snapshots carry percentages.
        m.snapshot(stacks=False, tasks=False, gc=False)
        time.sleep(0.15)
        yield m


@pytest.fixture(scope="module")
def snapshot(monitor):
    return monitor.snapshot()
