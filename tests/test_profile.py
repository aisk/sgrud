import pstats
import time

import pytest

from sgrud.errors import SgrudError
from sgrud.export import Recorder, guess_format
from sgrud.format import format_hotspots, format_read_stats
from sgrud.models import Awaiter, Frame, Task, ThreadStatus
from sgrud.profile import Hotspots, task_stacks
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
    busy_tid = next(t.tid for t in snap.threads if t.frames and t.frames[0].funcname == "busy_loop")
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
    hot.add(
        {
            1: (0, ThreadStatus.HAS_GIL, (_f("running"),)),
            2: (0, ThreadStatus.NONE, (_f("waiting"),)),
        }
    )
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


def test_call_tree_groups_threads_and_keeps_recursion():
    hot = Hotspots()
    hot.add_frames({1: (_f("inner"), _f("outer"), _f("main")), 2: (_f("work"),)})
    hot.add_frames({1: (_f("outer"), _f("main"))})
    hot.add_frames({1: (_f("rec"), _f("rec"), _f("main"))})

    root = hot.call_tree(names={1: "main", 2: "worker"})
    assert root.total == 4
    assert [c.name for c in root.children.values()] == ["main [1]", "worker [2]"]
    t1 = root.children[1]
    assert t1.tid == 1 and t1.total == 3
    main = t1.children[("main", "a.py")]
    assert main.total == 3 and main.self_samples == 0
    outer = main.children[("outer", "a.py")]
    assert outer.total == 2 and outer.self_samples == 1
    assert outer.children[("inner", "a.py")].self_samples == 1
    rec = main.children[("rec", "a.py")]
    # Recursion stays nested in the tree, unlike the per-function totals.
    assert rec.total == 1 and rec.children[("rec", "a.py")].self_samples == 1
    assert sum(1 for _ in root.walk()) == 9

    only = hot.call_tree(thread=2)
    assert only.tid == 2 and only.total == 1
    assert list(only.children) == [("work", "a.py")]
    assert hot.call_tree(thread=99).total == 0


def test_folded_output():
    hot = Hotspots()
    hot.add_frames({1: (_f("inner"), _f("outer")), 2: (_f("<native>", "~"),)})
    hot.add_frames({1: (_f("inner"), _f("outer"))})
    assert hot.folded(names={1: "main"}) == [
        "main [1];outer (a.py);inner (a.py) 2",
        "thread 2;<native> 1",
    ]
    assert hot.folded(thread=1) == ["outer (a.py);inner (a.py) 2"]


def test_task_stacks_join_leaf_to_root():
    root = Task(1, "root", 100, (_f("main"),))
    branch = Task(2, "branch", 100, (_f("gather"), _f("branch")), (Awaiter(1),))
    leaf_a = Task(3, "a", 100, (_f("sleep"), _f("leaf")), (Awaiter(2),))
    leaf_b = Task(4, "b", 100, (_f("sleep"), _f("leaf")), (Awaiter(2),))
    other = Task(5, "other", 200, (_f("serve"),), (Awaiter(99),))  # unknown parent

    stacks = dict(task_stacks([root, branch, leaf_a, leaf_b, other]))
    assert set(stacks) == {100, 200}  # keyed by thread; leaves only
    names = [f.funcname for f in task_stacks([root, branch, leaf_a])[0][1]]
    assert names == [
        "sleep",
        "leaf",
        "<task a>",
        "gather",
        "branch",
        "<task branch>",
        "main",
        "<task root>",
    ]
    assert [f.funcname for f in stacks[200]] == ["serve", "<task other>"]
    assert all(f.synthetic for f in stacks[200] if f.funcname.startswith("<task"))


def test_task_stacks_survive_cycles():
    a = Task(1, "a", 100, (_f("a"),), (Awaiter(2),))
    b = Task(2, "b", 100, (_f("b"),), (Awaiter(1),))
    # Both tasks are awaited, so neither is a leaf and nothing is emitted.
    assert task_stacks([a, b]) == []
    c = Task(3, "c", 100, (_f("c"),), (Awaiter(1),))
    ((tid, frames),) = task_stacks([a, b, c])
    assert [f.funcname for f in frames] == ["c", "<task c>", "a", "<task a>", "b", "<task b>"]


def test_hotspots_async_mode_counts_each_task():
    hot = Hotspots("async")
    root = Task(1, "root", 100, (_f("main"),))
    leaves = [Task(i, f"w{i}", 100, (_f("sleep"), _f("work")), (Awaiter(1),)) for i in (2, 3)]
    hot.add_tasks([root, *leaves])
    hot.add_tasks([root, *leaves])
    assert hot.samples == 2
    assert hot.thread_ids == [100]
    rows = {r.funcname: r for r in hot.rows(thread=100)}
    # Two task stacks per sample, so four thread-samples in total.
    assert rows["sleep"].self_samples == 4 and rows["sleep"].self_percent == 100.0
    assert rows["main"].total_percent == 100.0 and rows["main"].self_samples == 0
    assert rows["<task w2>"].total_percent == 50.0
    tree = hot.call_tree(thread=100)
    assert list(tree.children) == [("<task root>", "~")]


def test_sampler_async_mode_sees_sleeping_tasks(monitor):
    with Sampler(monitor, rate=100, mode="async") as sampler:
        time.sleep(0.6)
    hot = sampler.hotspots
    assert hot.samples > 5, (hot.samples, sampler.last_error)
    assert sampler.exited is None
    rows = {r.funcname: r for r in hot.rows(sort="total")}
    assert "busy_loop" not in rows
    assert rows["sleep"].self_percent > 50
    assert rows["leaf"].total_percent > 50
    assert rows["<task branch-0>"].total_samples > 0
    # main() creates the branches without awaiting them, so it is a leaf of
    # its own next to the six sleeping leaves, roughly one stack in seven.
    # It only runs to churn objects twice a second, so almost never on top.
    assert rows["main"].self_percent < 5
    assert 5 < rows["main"].total_percent < 25
    assert rows["<task Task-1>"].total_samples == rows["main"].total_samples


def test_sampler_cpu_mode_counts_only_running_threads(monitor):
    with Sampler(monitor, rate=300, mode="cpu") as sampler:
        time.sleep(0.5)
    rows = {r.funcname: r for r in sampler.hotspots.rows()}
    assert "busy_loop" in rows, sampler.last_error
    # The sleepers wake briefly every 200ms, so a stray sample of them is fine.
    for name in ("idle_loop", "except_loop"):
        assert name not in rows or rows[name].total_percent < 5, rows[name]
    assert rows["busy_loop"].self_percent > 80


def test_sampler_exception_mode_sees_handlers(monitor):
    with Sampler(monitor, rate=300, mode="exception") as sampler:
        time.sleep(0.5)
    rows = {r.funcname: r for r in sampler.hotspots.rows()}
    assert "except_loop" in rows, sampler.last_error
    assert "busy_loop" not in rows and "idle_loop" not in rows
    assert rows["except_loop"].total_percent == 100.0


def test_hotspots_cpu_and_exception_modes_filter_by_status():
    sample = {
        1: (0, ThreadStatus.ON_CPU, (_f("running"),)),
        2: (0, ThreadStatus.HAS_EXCEPTION, (_f("handling"),)),
        3: (0, ThreadStatus.NONE, (_f("waiting"),)),
    }
    cpu, exc = Hotspots("cpu"), Hotspots("exception")
    cpu.add(sample)
    exc.add(sample)
    assert [r.funcname for r in cpu.rows()] == ["running"]
    assert [r.funcname for r in exc.rows()] == ["handling"]


def test_raw_samples_convert_and_count_reads(monitor):
    sample = monitor.sample()
    stacks = sample.stacks()
    assert any(frames and frames[0].funcname == "busy_loop" for _, _, frames in stacks.values())
    with pytest.raises(ValueError):
        sample.tasks()
    tasks = monitor.sample("async")
    assert any(t.name == "branch-0" for t in tasks.tasks())
    stats = monitor.read_stats()
    assert stats["memory_reads"] > 0 and stats["memory_bytes_read"] > 0
    assert format_read_stats(stats).startswith("read ")
    assert format_read_stats({}) == ""
    assert monitor.read_stats("cpu") == {} or monitor.read_stats("cpu")["memory_reads"] >= 0


def test_recorders_write_every_format(monitor, tmp_path):
    import _remote_debugging

    paths = {
        "binary": tmp_path / "p.bin",
        "flamegraph": tmp_path / "f.html",
        "gecko": tmp_path / "g.json",
        "pstats": tmp_path / "p.pstats",
        "collapsed": tmp_path / "c.txt",
        "jsonl": tmp_path / "j.jsonl",
        "heatmap": tmp_path / "heat",
    }
    recorders = [Recorder(str(path), interval=1 / 200) for path in paths.values()]
    assert [r.format for r in recorders] == list(paths)
    with Sampler(monitor, rate=200, recorders=recorders) as sampler:
        time.sleep(0.5)
    sampler.close()
    assert sampler.recorders == []
    assert recorders[0].samples == sampler.hotspots.samples > 20
    for fmt, path in paths.items():
        assert path.exists(), fmt
    assert (paths["heatmap"] / "index.html").exists()
    assert "busy_loop" in paths["collapsed"].read_text()
    assert "busy_loop" in paths["flamegraph"].read_text()

    reader = _remote_debugging.BinaryReader(str(paths["binary"]))
    count = reader.get_info()["sample_count"]
    reader.close()
    # The file counts one sample per thread stack.
    assert isinstance(count, int) and count >= recorders[0].samples
    profile = pstats.Stats(str(paths["pstats"])).get_stats_profile()
    assert "busy_loop" in profile.func_profiles

    diff = Recorder(str(tmp_path / "diff.html"), interval=1 / 200, baseline=str(paths["binary"]))
    with Sampler(monitor, rate=200, recorders=[diff]) as sampler:
        time.sleep(0.3)
    sampler.close()
    assert (tmp_path / "diff.html").exists()


def test_recorder_rejects_what_cannot_work(tmp_path):
    assert guess_format("x.HTML") == "flamegraph"
    assert guess_format(str(tmp_path)) == "heatmap"
    with pytest.raises(SgrudError, match="--format"):
        guess_format("x.foo")
    with pytest.raises(SgrudError, match="task stacks"):
        Recorder(str(tmp_path / "a.bin"), interval=0.01, mode="async")
    with pytest.raises(SgrudError, match="baseline"):
        Recorder(str(tmp_path / "a.json"), interval=0.01, baseline="old.bin")
    with pytest.raises(SgrudError, match="does not exist"):
        Recorder(str(tmp_path / "a.html"), interval=0.01, baseline=str(tmp_path / "old.bin"))
    with pytest.raises(SgrudError, match="unknown format"):
        Recorder(str(tmp_path / "a.html"), "svg", interval=0.01)


def test_torn_reads_are_retried(monitor, monkeypatch):
    """A read the target tears by moving its frames is retried at once."""
    from sgrud.remote import RemoteInspector

    inspector = RemoteInspector(monitor.pid)
    real = inspector._unwinder
    assert real is not None
    calls = 0

    class Flaky:
        def get_stack_trace(self):
            nonlocal calls
            calls += 1
            if calls % 3:
                raise RuntimeError("Failed to parse initial frame in chain")
            return real.get_stack_trace()

    monkeypatch.setattr(inspector, "_unwinder", Flaky())
    assert inspector.sample().stacks()
    assert calls == 3
    with pytest.raises(RuntimeError, match="initial frame"):
        inspector.sample(retries=1)
    assert calls == 5
    assert inspector.sample(retries=0).stacks()
    assert calls == 6
