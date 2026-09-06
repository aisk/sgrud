import time

from sgrud.format import format_hotspots
from sgrud.models import Frame, ThreadStatus
from sgrud.profile import Hotspots
from sgrud.sampler import Sampler


def _f(name, file="a.py"):
    return Frame(name, file, 1)


def test_hotspots_self_and_total():
    hot = Hotspots()
    # thread 1: leaf "inner" called from "outer" twice, then "outer" alone once
    hot.add_frames({1: (_f("inner"), _f("outer"), _f("main"))})
    hot.add_frames({1: (_f("inner"), _f("outer"), _f("main")), 2: (_f("work"),)})
    hot.add_frames({1: (_f("outer"), _f("main"))})
    assert hot.samples == 3
    assert hot.thread_ids == [1, 2]

    rows = {r.funcname: r for r in hot.rows(thread=1)}
    assert rows["inner"].self_samples == 2 and rows["inner"].total_samples == 2
    assert rows["outer"].self_samples == 1 and rows["outer"].total_samples == 3
    assert rows["main"].self_samples == 0 and rows["main"].total_samples == 3
    assert rows["main"].total_percent == 100.0
    assert [r.funcname for r in hot.rows(thread=1)][:2] == ["inner", "outer"]
    assert [r.funcname for r in hot.rows(thread=1, sort="total")][0] in ("outer", "main")

    merged = {r.funcname: r for r in hot.rows()}
    # 4 thread-samples in total (3 for thread 1, 1 for thread 2)
    assert merged["work"].self_percent == 25.0
    assert hot.rows(thread=99) == []


def test_hotspots_recursion_counts_once():
    hot = Hotspots()
    hot.add_frames({1: (_f("rec"), _f("rec"), _f("rec"), _f("main"))})
    row = hot.rows(thread=1)[0]
    assert row.funcname == "rec"
    assert row.total_samples == 1 and row.total_percent == 100.0


def test_hotspots_reset_and_format():
    hot = Hotspots()
    hot.add_frames({1: (_f("x"),)})
    text = format_hotspots(hot.rows(), samples=hot.samples)
    assert "x" in text and "1 samples" in text
    hot.reset()
    assert hot.samples == 0 and hot.rows() == []
    assert "(no samples)" in format_hotspots(hot.rows(), samples=0)


def test_sampler_finds_busy_loop(monitor):
    with Sampler(monitor, rate=300) as sampler:
        time.sleep(0.6)
    hot = sampler.hotspots
    assert hot.samples > 50, hot.samples
    assert sampler.exited is None
    snap = monitor.snapshot(tasks=False, gc=False)
    busy_tid = next(t.tid for t in snap.threads if t.name == "busy")
    top = hot.rows(thread=busy_tid)[0]
    assert top.funcname == "busy_loop"
    assert top.self_percent > 50
    # The sampler and the UI-style snapshot share the unwinder safely.
    assert not snap.errors


def test_sampler_notices_exit():
    import sys

    from conftest import TARGET

    from sgrud import Monitor

    m = Monitor.spawn([sys.executable, str(TARGET), "--exit-after", "0.4"])
    with m, Sampler(m, rate=100) as sampler:
        deadline = time.monotonic() + 5
        while sampler.exited is None and time.monotonic() < deadline:
            time.sleep(0.05)
    assert sampler.exited is not None
    assert not sampler.running


def test_hotspots_gil_mode_filters_by_status():
    hot = Hotspots("gil")
    hot.add({
        1: (0, ThreadStatus.HAS_GIL, (_f("running"),)),
        2: (0, ThreadStatus.NONE, (_f("waiting"),)),
    })
    names = [r.funcname for r in hot.rows()]
    assert names == ["running"]
    assert hot.thread_ids == [1]


def test_sampler_gil_mode_ignores_sleepers(monitor):
    with Sampler(monitor, rate=300, mode="gil") as sampler:
        time.sleep(0.5)
    rows = {r.funcname: r for r in sampler.hotspots.rows()}
    assert "busy_loop" in rows
    assert "idle_loop" not in rows
    assert rows["busy_loop"].self_percent > 80
