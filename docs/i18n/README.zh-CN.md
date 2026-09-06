# sgrud

[English](../../README.md) | **简体中文** | [日本語](README.ja.md) | [한국어](README.ko.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

sgrud（源自苏格兰盖尔语 *sgrùd*，意为“检视”或“审查”）是一个用于观察正在运行的
Python 进程的诊断工具。附加到一个 CPython 进程上，即可查看它的内存、CPU、线程、
asyncio 任务、调用栈和垃圾回收器，而不会拖慢目标进程。

sgrud 从不暂停或插桩目标进程。它通过 CPython 3.15 的 `_remote_debugging` 模块
（Tachyon 性能分析器和 `python -m asyncio ps` 背后的机制）直接从进程内存中读取
解释器状态，并结合操作系统提供的信息来统计内存和 CPU。抓取一次所有线程的调用栈
只需几十微秒，目标进程侧则没有任何开销。

需要 CPython 3.15 或更新版本，支持 Linux、macOS 和 Windows。目标进程必须运行与
sgrud 自身相同的 major.minor 版本。Linux 上信息最完整，其它平台的差异见
[平台](#平台)。

## 用法

```
sgrud PID                           交互式终端界面
sgrud run -- python app.py          以子进程方式启动目标并观察它

sgrud dump PID                      输出一次文本快照
sgrud dump PID -n 0.5               每 0.5 秒打印一次，直到目标退出
sgrud dump PID --json               每行输出一个 JSON 对象
sgrud dump run -- python app.py     任何需要 pid 的地方都可以用 `run -- CMD` 代替

sgrud profile PID                   采样调用栈 5 秒，打印最热的函数
sgrud profile PID -d 30 --mode gil  采样 30 秒，只统计持有 GIL 的线程
sgrud profile PID --mode async      采样 asyncio 任务而不是线程
sgrud profile PID --folded          输出折叠栈，供 flamegraph.pl 或 speedscope 使用
```

`--no-stacks`、`--no-tasks` 和 `--no-gc` 可以从界面或 dump 输出中去掉你不需要的部分。

### 采样模式

- **wall**：所有拥有 Python 调用栈的线程都计入，因此一个休眠线程和一个忙碌线程的
  权重相同。
- **gil**：只计入持有 GIL 的线程。它回答的是“CPU 时间花在了哪里”。
- **async**：采样 asyncio 任务而不是线程栈，因为停在 `await` 上的协程不在任何线程的
  栈上。每个叶子任务对应一个栈：先是它自己的帧，然后是一个 `<task NAME>` 标记，
  再往上是每个等待它的任务的帧，直到根任务。无论运行中还是挂起的任务都会计入，
  所以它回答的是“我的任务都在等什么”。这种模式每次采样比读取一个栈更慢，
  因此实际采样率会偏低。

### TUI

| 按键 | 操作 |
| --- | --- |
| `1`-`6`、`tab`、`shift+tab` | 切换标签页 |
| `p` / `r` | 暂停 / 刷新 |
| `+` / `-` | 调整刷新间隔 |
| `q` | 退出 |
| `f` | 线程过滤（Hotspots 和 Flame） |
| `m` | 循环切换 wall/gil/async 模式（Hotspots 和 Flame） |
| `c` | 清空采样（Hotspots 和 Flame） |
| `s` | 切换 self/total 排序（Hotspots） |
| `enter` / `backspace` / `esc` | 放大 / 缩小 / 重置（Flame） |

方向键可以直接在当前标签页的内容中移动。Hotspots 和 Flame 共用一个后台采样器
（`--rate`，默认 100 Hz），在你查看其他标签页时它也会持续运行。火焰图自底向上生长，
第一行为每个线程分配一个独立的块，因此空闲线程会显示为一根高高的柱子，
而不是混在其他线程里。

## 作为库使用

TUI 只是一个前端。所有数据都来自 `Monitor`，它返回普通的冻结 dataclass：

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()  # snapshot(stacks=..., tasks=..., gc=...)
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.cpu_percent, t.frames[:1])
    for task in snap.tasks:
        print(task.name, task.parent_ids, [f.funcname for f in task.frames])
    print(snap.gc[0].collections, snap.gc[0].history[:1])
    print(snap.to_dict())  # JSON friendly
```

`Monitor.stream(interval)` 会持续产出快照，直到目标退出，然后抛出 `ProcessExited`。
CPU 百分比需要两次快照才能算出，所以第一次快照中报告为 `None`。

要做性能分析，`Sampler` 会在后台线程中运行 `Monitor.sample_stacks()`，
并把结果送入 `Hotspots` 聚合器：

```python
from sgrud.sampler import Sampler

with Sampler(monitor, rate=500, mode="gil") as sampler:
    time.sleep(5)
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()  # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## 平台

调用栈、asyncio 任务、GC 和性能分析都来自 `_remote_debugging`，在各平台上表现
一致。进程和线程的统计信息通过 psutil 从操作系统获取，平台差异都在这里。

- **Linux** 提供全部信息，包括线程的调度状态，wall 模式下正是靠它标记线程是否
  在 CPU 上。
- **Windows** 有线程名和线程级 CPU 时间，但没有调度状态，所以 wall 模式下线程
  显示为 `?` 而不是 `cpu` / `idle`。不报告 swap 和共享内存。
- **macOS** 无法把操作系统线程和解释器的线程 id 对应起来，所以线程没有名字和
  CPU 数据，内存只有 `rss` / `vms`。读取其它进程的内存需要 root，请用 `sudo`
  运行 sgrud。

## 权限

内存、CPU 和线程名来自操作系统，对你拥有的任何进程都可用。其余信息都需要读取
目标进程的内存。在 Linux 上这需要 ptrace 权限，而默认的 `kernel.yama.ptrace_scope=1`
只对子进程授予这些权限。没有这些权限时，sgrud 会以受限模式附加，并显示一条横幅说明
缺少了什么。要获得完整功能，可以通过 `sgrud run -- ...` 启动目标，用 `sudo`
运行 sgrud，授予 `CAP_SYS_PTRACE`，或者在当前会话中放宽 Yama 限制：

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

macOS 上只有 root 能读取其它进程的内存，请使用 `sudo`。Windows 上同一用户的
进程都可以，其它用户的进程需要管理员权限。

给 `Monitor.attach` 传入 `require_full=True` 可以让它在权限不足时直接失败，
而不是降级运行。使用 `-X disable-remote-debug` 启动的目标仍然可以被观察，
因为该选项只禁用代码注入，而 sgrud 并不使用代码注入。

## 开发

```
uv sync
uv run pytest
```

测试会启动 `tests/target_app.py` 并对它进行观察，因此覆盖了真实的附加路径。
Textual 应用通过其 pilot 进行无头测试。
