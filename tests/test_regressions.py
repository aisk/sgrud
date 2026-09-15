"""Failure paths that must preserve recordings and protect probe files."""

import importlib
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sgrud import cli
from sgrud.errors import SgrudError
from sgrud.export import Recorder
from sgrud.remote import RawSample


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
    assert not scripts[0].parent.exists()
    # A script already loaded when the caller times out must not recreate
    # results or fail in the target when its directory has disappeared.
    exec(
        module._SCRIPT,
        {"TYPES": 0, "ALLOCATIONS": 0, "RESULT": str(scripts[0].parent / "result.json")},
    )
    assert not scripts[0].parent.exists()


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
