"""Failure paths that must preserve recordings and protect probe files."""

import contextlib
import importlib
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sgrud import cli
from sgrud.errors import ProcessExited, SgrudError
from sgrud.export import Recorder
from sgrud.models import Frame, OpenFile
from sgrud.profile import Hotspots
from sgrud.remote import RawSample
from sgrud.sampler import Sampler


@pytest.mark.parametrize("timeout", [False, True])
def test_probe_private_directory_and_cleanup(monkeypatch, timeout):
    module = importlib.import_module("sgrud.probe")
    scripts = []

    def remote_exec(pid, script):
        path = Path(script)
        scripts.append(path)
        if os.name != "nt":
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert path.is_file()
        if not timeout:
            raise RuntimeError("refused")

    monkeypatch.setattr(module.sys, "remote_exec", remote_exec)
    with pytest.raises(SgrudError, match="within|refused"):
        module.probe(os.getpid(), timeout=0)
    directory = scripts[0].parent
    # After a timeout the target may still read the script, so the
    # directory stays, marked for the script to remove once it has run.
    assert directory.exists() == timeout
    assert (directory / "abandoned").exists() == timeout
    # Run late, the script must neither fail in the target nor leave the
    # directory or a result behind, whether or not the directory is there.
    exec(
        module._SCRIPT,
        {
            "TYPES": 0,
            "ALLOCATIONS": 0,
            "RESULT": str(directory / "result.json"),
            "ABANDONED": str(directory / "abandoned"),
        },
    )
    assert not directory.exists()


def test_profile_interrupt_exports_collected_samples(monkeypatch, tmp_path):
    monitor = MagicMock()
    monitor.limited = None
    monitor.read_stats.return_value = {}
    sampler = MagicMock()
    sampler.exited = None
    sampler.errors = 0
    recorder = MagicMock()
    monkeypatch.setattr(cli, "open_monitor", lambda *a, **kw: monitor)
    monkeypatch.setattr("sgrud.sampler.Sampler", lambda *a, **kw: sampler)
    monkeypatch.setattr("sgrud.export.Recorder", lambda *a, **kw: recorder)

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupt)
    assert cli.main(["profile", "123", "-o", str(tmp_path / "out.html")]) == 0
    sampler.close.assert_called_once()


def test_recorder_rejects_mixed_modes(tmp_path):
    recorder = Recorder(str(tmp_path / "out.bin"), interval=0.01, mode="gil")
    try:
        with pytest.raises(SgrudError, match="cannot record"):
            recorder.collect(RawSample("async", ()))
        assert recorder.samples == 0
    finally:
        recorder.close()


@pytest.mark.parametrize("options", [["--mode", "async"], ["--rate", "0"], ["--web"]])
def test_invalid_recording_rejected_before_attach(monkeypatch, options):
    monitor = MagicMock()
    monkeypatch.setattr(cli, "open_monitor", monitor)
    assert cli.main(["123", "--record", "unused.bin", *options]) == 1
    monitor.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership")
def test_root_probe_grants_only_target_access(monkeypatch):
    module = importlib.import_module("sgrud.probe")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    process = MagicMock()
    process.uids.return_value.effective = 1234
    monkeypatch.setattr(module.psutil, "Process", lambda pid: process)
    ownership = []
    monkeypatch.setattr(module.os, "chown", lambda *args: ownership.append(args))

    def remote_exec(pid, script):
        assert ownership == [(script, 1234, -1), (str(Path(script).parent), 1234, -1)]
        raise RuntimeError("refused")

    monkeypatch.setattr(module.sys, "remote_exec", remote_exec)
    with pytest.raises(SgrudError, match="refused"):
        module.probe(123)


def test_failed_attach_keeps_an_old_recording(monkeypatch, tmp_path):
    old = tmp_path / "old.bin"
    old.write_bytes(b"precious data")

    def refuse(*args, **kwargs):
        raise SgrudError("process 999999 has exited")

    monkeypatch.setattr(cli, "open_monitor", refuse)
    assert cli.main(["profile", "999999", "-o", str(old)]) == 1
    assert old.read_bytes() == b"precious data"


@pytest.mark.parametrize("rate", ["0", "-5", "nan"])
def test_profile_rejects_a_rate_that_is_not_positive(monkeypatch, capsys, rate):
    monitor = MagicMock()
    monkeypatch.setattr(cli, "open_monitor", monitor)
    with pytest.raises(SystemExit) as exc:
        cli.main(["profile", "123", "--rate", rate])
    assert exc.value.code == 2
    assert "must be positive" in capsys.readouterr().err
    monitor.assert_not_called()


def test_run_reports_a_missing_command(capsys):
    assert cli.main(["dump", "run", "--", "sgrud-no-such-command"]) == 1
    assert "cannot start 'sgrud-no-such-command'" in capsys.readouterr().err


@pytest.mark.parametrize("error", [KeyboardInterrupt, ValueError])
def test_spawn_kills_the_child_when_attaching_fails(monkeypatch, error):
    from sgrud import monitor as monitor_module

    children = []
    popen = monitor_module.subprocess.Popen

    def spawn(argv):
        children.append(popen(argv))
        return children[-1]

    def fail(pid):
        raise error

    monkeypatch.setattr(monitor_module.subprocess, "Popen", spawn)
    monkeypatch.setattr(monitor_module, "interpreter_pid", fail)
    with pytest.raises(error):
        monitor_module.Monitor.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    assert children[0].poll() is not None


def test_recorders_get_opcodes_and_mode(tmp_path):
    gecko = Recorder(str(tmp_path / "out.json"), interval=0.01, opcodes=True)
    assert gecko._collector.opcodes_enabled
    jsonl = Recorder(str(tmp_path / "out.jsonl"), interval=0.01, mode="gil")
    assert jsonl._collector._mode == 2  # PROFILING_MODE_GIL


def test_window_is_kept_once_frozen(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("sgrud.profile.time.monotonic", lambda: now[0])
    hot = Hotspots(window=30.0)
    hot.add_frames({1: (Frame("work", "a.py", 1),)})
    now[0] += 0.01
    hot.add_frames({1: (Frame("work", "a.py", 1),)})
    hot.freeze()
    now[0] += 1000
    assert [r.funcname for r in hot.rows()] == ["work"]
    assert hot.rate() == pytest.approx(100, rel=0.01)


def test_rate_right_after_a_reset(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("sgrud.profile.time.monotonic", lambda: now[0])
    hot = Hotspots()
    for _ in range(100):
        hot.add_frames({1: (Frame("work", "a.py", 1),)})
        now[0] += 0.01
    hot.reset()
    now[0] += 0.000_01
    hot.add_frames({1: (Frame("work", "a.py", 1),)})
    now[0] += 0.000_01
    assert hot.rate() == 0
    now[0] += 0.01
    hot.add_frames({1: (Frame("work", "a.py", 1),)})
    assert hot.rate() == pytest.approx(100, rel=0.01)


def test_folded_escapes_thread_names():
    hot = Hotspots()
    hot.add_frames({1: (Frame("work", "a.py", 1),)})
    [line] = hot.folded(names={1: "a;b"})
    assert line.rsplit(" ", 1)[0].split(";")[0] == "a,b [1]"


def test_restarted_sampler_stops_the_thread_stuck_in_a_read():
    release = threading.Event()
    calls = []

    class SlowMonitor:
        def sample(self, mode):
            calls.append(threading.current_thread())
            release.wait(5)
            raise SgrudError("torn read")

    sampler = Sampler(SlowMonitor(), rate=1000)  # ty: ignore[invalid-argument-type]
    sampler.start()
    while not calls:
        time.sleep(0.001)
    sampler.stop(timeout=0.01)
    assert not sampler.running
    sampler.start()
    release.set()
    stuck = calls[0]
    stuck.join(5)
    assert not stuck.is_alive()
    assert sampler.running
    sampler.close()
    assert not any(t.is_alive() for t in set(calls))


def test_linux_sockets_only_fill_in_listed_descriptors(monkeypatch):
    from sgrud import ipc

    monkeypatch.setattr(ipc, "LINUX", True)
    files = [
        OpenFile(fd=3, kind="socket", target="socket:[1]"),
        OpenFile(fd=4, kind="file", target="/tmp/reused"),
    ]

    def conn(fd):
        return SimpleNamespace(
            fd=fd,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
            laddr=("127.0.0.1", 80),
            raddr=(),
            status="LISTEN",
        )

    out = ipc._with_connections(files, [conn(3), conn(4), conn(5000)])
    assert [(f.fd, f.kind, f.local) for f in out] == [
        (3, "socket", "127.0.0.1:80"),
        (4, "file", ""),
    ]


def test_sampler_freezes_the_window_when_the_target_exits():
    class GoneMonitor:
        def sample(self, mode):
            raise ProcessExited(123)

    hot = Hotspots(window=30.0)
    sampler = Sampler(GoneMonitor(), hot)  # ty: ignore[invalid-argument-type]
    sampler.start()
    sampler.stop(timeout=5)
    assert sampler.exited is not None
    assert hot._frozen_at is not None


def test_stopped_sampler_records_nothing_more(tmp_path):
    release = threading.Event()
    reading = threading.Event()

    class SlowMonitor:
        def sample(self, mode):
            reading.set()
            release.wait(5)
            return RawSample("wall", ())

    recorder = MagicMock()
    sampler = Sampler(SlowMonitor(), recorders=[recorder])  # ty: ignore[invalid-argument-type]
    sampler.start()
    reading.wait(5)
    sampler.stop(timeout=0.01)
    release.set()
    sampler._threads[0].join(5) if sampler._threads else None
    recorder.collect.assert_not_called()
    assert sampler.hotspots.samples == 0


@pytest.mark.parametrize("error", [KeyboardInterrupt, SgrudError])
def test_probe_leaves_the_script_to_a_target_it_was_sent_to(monkeypatch, error):
    module = importlib.import_module("sgrud.probe")
    scripts = []

    def remote_exec(pid, script):
        scripts.append(Path(script))

    def interrupt(seconds):
        raise error("interrupted")

    monkeypatch.setattr(module.sys, "remote_exec", remote_exec)
    monkeypatch.setattr(module.time, "sleep", interrupt)
    with pytest.raises(error):
        module.probe(os.getpid())
    directory = scripts[0].parent
    assert (directory / "abandoned").exists()
    exec(
        module._SCRIPT,
        {
            "TYPES": 0,
            "ALLOCATIONS": 0,
            "RESULT": str(directory / "result.json"),
            "ABANDONED": str(directory / "abandoned"),
        },
    )
    assert not directory.exists()


def test_output_directory_is_checked_before_attaching(monkeypatch, tmp_path, capsys):
    monitor = MagicMock()
    monkeypatch.setattr(cli, "open_monitor", monitor)
    missing = tmp_path / "missing" / "out.bin"
    assert cli.main(["profile", "run", "-o", str(missing), "--", "python"]) == 1
    assert "does not exist" in capsys.readouterr().err
    monitor.assert_not_called()


def test_denied_process_figures_do_not_abort_the_snapshot(monkeypatch):
    import psutil

    from sgrud import osproc

    stats = osproc.ProcessStats(os.getpid())

    def denied():
        raise psutil.AccessDenied(os.getpid())

    # oneshot() primes caches on the real methods, which are replaced here.
    monkeypatch.setattr(stats._proc, "oneshot", contextlib.nullcontext)
    monkeypatch.setattr(stats._proc, "cpu_times", denied)
    monkeypatch.setattr(stats._proc, "memory_info", denied)
    monkeypatch.setattr(stats._proc, "num_threads", denied)
    stat = stats.process()
    assert stat.utime == 0 and stat.memory.rss == 0 and stat.num_threads == 0


def test_monitor_forgets_rate_baselines(monkeypatch):
    from sgrud.monitor import Monitor

    with Monitor.attach(os.getpid()) as m:
        m.snapshot(stacks=False, tasks=False, children=False, ipc=False)
        time.sleep(0.05)
        assert (
            m.snapshot(stacks=False, tasks=False, children=False, ipc=False).process.cpu_percent
            is not None
        )
        m.reset_rates()
        snap = m.snapshot(stacks=False, tasks=False, children=False, ipc=False)
        assert snap.process.cpu_percent is None
