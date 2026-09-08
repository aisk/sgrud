# sgrud

[English](../../README.md) | **简体中文** | [日本語](README.ja.md) | [한국어](README.ko.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

sgrud（源自苏格兰盖尔语 *sgrùd*，意为“检视”或“审查”）是一个用于观察正在运行的 Python 进程的诊断工具。附加到一个 CPython 进程上，即可查看它的内存、CPU、线程、asyncio 任务、调用栈和垃圾回收器，而不会拖慢目标进程。

sgrud 从不暂停或插桩目标进程。它通过 CPython 3.15 的 `_remote_debugging` 模块（Tachyon 性能分析器和 `python -m asyncio ps` 背后的机制）直接从进程内存中读取解释器状态，并通过 psutil 结合操作系统提供的内存、CPU、线程和打开文件的信息。抓取一次所有线程的调用栈只需几十微秒，目标进程侧则没有任何开销。

![Threads 标签页，显示每个线程的状态、CPU 占用和当前 Python 调用栈](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*Threads 标签页：每个线程的状态、CPU 占比和实时 Python 调用栈。*

## 功能

- **Process**：在平台允许的范围内拆分内存，还有 CPU、缺页、资源限制、cgroup 的配额和节流情况，以及子进程列表，其中的 Python 解释器会被标出。
- **Threads**：每个线程的状态、CPU 占比、实时 Python 调用栈，在 Linux 上还有它阻塞在哪个系统调用上。
- **Tasks**：asyncio 任务树，每个任务附带它停留的协程帧。
- **GC**：回收占用的时间、回收速率、被追踪的对象数、回收历史以及触发回收的函数。
- **Hotspots** 和 **Flame**：后台采样分析器，支持 wall、GIL、CPU、异常和 asyncio 任务模式，以表格或火焰图展示。
- **IPC**：打开的描述符、管道及其另一端的持有者、socket、共享内存和文件锁，给挂住的进程准备。
- `sgrud dump` 以文本或 JSON 输出同样的内容，`sgrud profile` 采样一段时间并写出任意 Tachyon 格式，`sgrud probe` 向目标询问仅靠读内存无法得到的信息。`--web` 把界面提供给浏览器。
- `Monitor` 类返回普通的 dataclass，因此这一切都可以作为库使用。

## 安装

```
pip install sgrud
pip install "sgrud[web]"    # 加上 --web 支持
```

`uv tool install sgrud` 和 `pipx install sgrud` 也可以。sgrud 需要 CPython 3.15 或更新版本，支持 Linux、macOS 和 Windows。目标进程必须运行与 sgrud 自身相同的 major.minor 版本。

内存、CPU 和线程名对你拥有的任何进程都可用。读取解释器状态在 Linux 上需要 ptrace 权限，macOS 上需要 root，Windows 上需要同一用户。没有这些权限时，sgrud 会以受限模式附加，并说明缺少了什么。最简单的办法是通过 sgrud 启动目标，其它方式见 [权限](../reference.md#permissions)。

## 用法

```
sgrud PID                           交互式终端界面
sgrud run -- python app.py          以子进程方式启动目标并观察它
sgrud PID --web                     同一界面，改为在浏览器中打开

sgrud dump PID                      输出一次文本快照，加 --json 输出 JSON
sgrud profile PID -d 30 --mode gil  采样持有 GIL 的线程 30 秒，打印最热的函数
sgrud profile PID -o out.html       改为写出火焰图
sgrud probe PID                     在目标里运行一段脚本：gc 阈值、分配器、线程
```

`examples/demo_app.py` 让每个标签页都有内容。启动它，再把 sgrud 指向它打印出的 pid。

在 Python 中使用：

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.frames[:1])
```

[参考文档](../reference.md)涵盖每个命令和选项、采样模式和输出格式、TUI 的按键和标签页、库的用法、各平台提供的信息以及如何获得权限。

## 开发

```
uv sync
uv run pytest
```

测试会启动 `tests/target_app.py` 并对它进行观察，因此覆盖了真实的附加路径。Textual 应用通过其 pilot 进行无头测试。
