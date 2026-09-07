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

![Threads 标签页，显示每个线程的状态、CPU 占用和当前 Python 调用栈](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*Threads 标签页：每个线程的状态、CPU 占比和实时 Python 调用栈。*

## 用法

```
sgrud PID                           交互式终端界面
sgrud run -- python app.py          以子进程方式启动目标并观察它
sgrud PID --web                     同一界面，改为在浏览器中打开

sgrud dump PID                      输出一次文本快照
sgrud dump PID -n 0.5               每 0.5 秒打印一次，直到目标退出
sgrud dump PID --json               每行输出一个 JSON 对象
sgrud dump run -- python app.py     任何需要 pid 的地方都可以用 `run -- CMD` 代替

sgrud profile PID                   采样调用栈 5 秒，打印最热的函数
sgrud profile PID -d 30 --mode gil  采样 30 秒，只统计持有 GIL 的线程
sgrud profile PID --mode async      采样 asyncio 任务而不是线程
sgrud profile PID --folded          输出折叠栈，供 flamegraph.pl 或 speedscope 使用
sgrud profile PID -o out.html       写出火焰图，也可以是 .json / .pstats / .txt / .jsonl / 目录
sgrud profile PID -o out.bin        录制成 `python -m profiling.sampling replay` 可读的格式
sgrud PID --record out.bin          打开界面的同时录制每一个样本

sgrud probe PID                     在目标里运行一段脚本：gc 阈值、分配器、线程
sgrud probe PID -t 10               同时按类型统计被追踪的对象，列出最多的十种
```

`--no-stacks`、`--no-tasks` 和 `--no-gc` 可以从界面或 dump 输出中去掉你不需要的部分。

### 采样模式

- **wall**：所有拥有 Python 调用栈的线程都计入，因此一个休眠线程和一个忙碌线程的
  权重相同。
- **gil**：只计入持有 GIL 的线程。它回答的是“CPU 时间花在了哪里”。
- **cpu**：只计入操作系统正调度在核上的线程，所以释放了 GIL 的 C 代码仍然计入，
  而等待 GIL 的线程不计入。
- **exception**：只计入正在处理异常的线程，用来看异常在哪里抛出、传播多远才被捕获。
- **async**：采样 asyncio 任务而不是线程栈，因为停在 `await` 上的协程不在任何线程的
  栈上。每个叶子任务对应一个栈：先是它自己的帧，然后是一个 `<task NAME>` 标记，
  再往上是每个等待它的任务的帧，直到根任务。无论运行中还是挂起的任务都会计入，
  所以它回答的是“我的任务都在等什么”。这种模式每次采样比读取一个栈更慢，
  因此实际采样率会偏低。

### 输出格式

`profile -o PATH` 把样本写成标准库 `profiling.sampling`（Tachyon 分析器）的格式，
而不是打印表格。扩展名决定格式：`.html` 是火焰图，`.json` 是 Firefox Profiler
文档，`.pstats` 可用 `pstats.Stats` 加载，`.txt` 是折叠栈，`.jsonl` 每行一个样本，
目录则生成源码热力图。`.bin` 是 Tachyon 的二进制格式，之后可以用
`python -m profiling.sampling replay` 转成其他任何格式。`--baseline old.bin`
让火焰图变成与早前录制的差分图，`--opcodes` 为支持的格式记录每个帧当前的
字节码指令。`sgrud PID --record out.bin` 在界面运行期间做同样的录制，切换模式
也不会中断。

### TUI

| 按键 | 操作 |
| --- | --- |
| `1`-`6`、`tab`、`shift+tab` | 切换标签页 |
| `p` / `r` | 暂停 / 刷新 |
| `+` / `-` | 调整刷新间隔 |
| `q` | 退出 |
| `f` | 线程过滤（Hotspots 和 Flame） |
| `m` | 循环切换采样模式（Hotspots 和 Flame） |
| `x` | 探测目标（GC） |
| `c` | 清空采样（Hotspots 和 Flame） |
| `s` | 切换 self/total 排序（Hotspots） |
| `enter` / `backspace` / `esc` | 放大 / 缩小 / 重置（Flame） |

方向键可以直接在当前标签页的内容中移动。Hotspots 和 Flame 共用一个后台采样器
（`--rate`，默认 100 Hz），在你查看其他标签页时它也会持续运行。火焰图自底向上生长，
第一行为每个线程分配一个独立的块，因此空闲线程会显示为一根高高的柱子，
而不是混在其他线程里。

GC 标签页显示回收占用的时间比例、每秒回收次数、被追踪的对象数和回收历史。目标进程自己只保留最近 11 次年轻代和 3 次老年代回收，
所以 monitor 会把见过的记录累积起来。采样器运行时，这里还会列出触发回收的函数，
也就是分配最频繁的地方。Process 标签页在平台允许的范围内拆分内存，见[平台](#平台)，
还会列出目标的子进程及其 CPU 和内存，并标出哪些是 Python 解释器，`multiprocessing`
进程池或者由 supervisor 拉起的 worker 一眼就能看到。再开一个 `sgrud PID` 就能检查其中任何一个。

![Tasks 标签页，以树形显示 asyncio 任务及各任务正在等待的内容](https://github.com/user-attachments/assets/e8f1e9b0-2d8c-4b39-b67a-b9ba3ae2fa1d)

*Tasks 标签页：asyncio 任务树，每个任务附带它停留的协程帧。*

![Hotspots 标签页，列出 CPU 采样最多的函数](https://github.com/user-attachments/assets/32c75ac9-e279-41e8-83a2-b7e2970275e4)

*Hotspots 标签页：按后台采样器的 self 和 total 采样数排列的函数。*

![Flame 标签页，显示采样调用栈的火焰图](https://github.com/user-attachments/assets/b78cfbec-3619-45a6-a2a9-5bc57f2e45f3)

*Flame 标签页：同一批采样绘制成火焰图，第一行每个线程一个块。*

### Web

`--web` 通过 [textual-serve](https://github.com/Textualize/textual-serve)
把同一个界面提供给浏览器。它是可选依赖，安装 `sgrud[web]` 即可获得。默认监听
`http://127.0.0.1:8000`，可用 `--host` 和 `--port` 修改。每个浏览器标签页
都会得到一份独立的界面，连接到同一个目标进程。没有任何鉴权，请只在本机使用，
或者放在有鉴权的反向代理后面。
在 Linux 上使用 `run -- CMD` 时，目标会以允许同一用户的任意进程读取的方式启动，
因为浏览器会话并不是它的父进程。

### 探测

上面的一切都是从外部读目标的内存。`sgrud probe` 是唯一的例外：它用
`sys.remote_exec` 让目标的主线程在下一个安全点运行一段短脚本，报告解释器没有
放进内存里的东西。包括 gc 的阈值和与之比较的计数、回收器是否启用、多少对象被
冻结或落在 `gc.garbage` 里、分配器持有的块数、模块和线程数，加 `-t N` 还有按
类型统计的被追踪对象直方图，目标已经开着 tracemalloc 的话也会带回一份快照。
GC 标签页按 `x` 运行的是同一个探测。它会占用目标主线程几毫秒，带类型直方图时
更多，而且要等主线程到达安全点，主线程卡在 C 代码里时会超时。sgrud 从不自行运行它。

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
    print(snap.gc[0].rate, snap.gc_time_share, snap.gc[0].history[:1])
    print(snap.process.memory.anon, snap.process.fault_rate, snap.process.limits)
    print([(c.pid, c.python, c.rss) for c in snap.children])
    print(snap.to_dict())  # JSON friendly
    result = m.probe(types=5)  # runs code in the target, see Probing
    print(result.gc_threshold, result.gc_count, result.types)
```

`Monitor.stream(interval)` 会持续产出快照，直到目标退出，然后抛出 `ProcessExited`。
CPU 百分比需要两次快照才能算出，所以第一次快照中报告为 `None`。

要做性能分析，`Sampler` 会在后台线程中运行 `Monitor.sample_stacks()`，
并把结果送入 `Hotspots` 聚合器：

```python
from sgrud.sampler import Sampler

from sgrud.export import Recorder

flame = Recorder("profile.html", interval=1 / 500)
with Sampler(monitor, rate=500, mode="gil", recorders=[flame]) as sampler:
    time.sleep(5)
sampler.close()  # writes profile.html
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()  # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## 平台

调用栈、asyncio 任务、GC 和性能分析都来自 `_remote_debugging`，在各平台上表现
一致。进程和线程的统计信息通过 psutil 从操作系统获取，平台差异都在这里。

- **Linux** 提供全部信息，包括线程的调度状态，wall 模式下正是靠它标记线程是否在 CPU 上。
  内存也是完整的，包括 rss 中匿名和文件映射各占多少、USS 和 PSS、
  brk 堆和匿名映射、透明大页、缺页速率、cgroup 内存上限和 OOM 分数。
- **Windows** 有线程名和线程级 CPU 时间，但没有调度状态，所以 wall 模式下线程显示为 `?` 而不是 `cpu` / `idle`。
  内存有 `rss`、`vms`、工作集峰值、私有字节、USS 和缺页速率。
- **macOS** 无法把操作系统线程和解释器的线程 id 对应起来，所以线程没有名字和 CPU 数据。
  内存有 `rss`、`vms`、USS 和缺页速率。读取其它进程的内存需要 root，
  请用 `sudo` 运行 sgrud。

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
因为该选项只禁用代码注入。它唯一挡住的是 `sgrud probe`。

## 开发

```
uv sync
uv run pytest
```

测试会启动 `tests/target_app.py` 并对它进行观察，因此覆盖了真实的附加路径。
Textual 应用通过其 pilot 进行无头测试。
