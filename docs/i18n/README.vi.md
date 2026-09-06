# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | **Tiếng Việt** | [Français](README.fr.md) | [Deutsch](README.de.md)

sgrud (bắt nguồn từ tiếng Gael Scotland *sgrùd*, nghĩa là "kiểm tra (檢查)" hay
"khảo sát (考察)") là một công cụ chẩn đoán dùng để quan sát các tiến trình Python
đang chạy. Gắn vào một tiến trình CPython và theo dõi bộ nhớ, CPU, các thread,
các task asyncio, stack và bộ thu gom rác của nó mà không làm nó chậm đi.

sgrud không bao giờ dừng hay chèn mã vào tiến trình đích. Nó đọc trạng thái
của trình thông dịch trực tiếp từ bộ nhớ tiến trình thông qua module
`_remote_debugging` của CPython 3.15 (cơ chế đứng sau profiler Tachyon và
`python -m asyncio ps`) và kết hợp với `/proc` để thống kê bộ nhớ và CPU.
Một ảnh chụp stack của mọi thread chỉ tốn vài chục micro giây và không tốn
gì ở phía tiến trình đích.

Yêu cầu Linux và CPython 3.15 trở lên. Tiến trình đích phải chạy cùng phiên
bản major.minor với chính sgrud.

## Cách dùng

```
sgrud PID                           giao diện terminal tương tác
sgrud run -- python app.py          khởi chạy tiến trình đích như tiến trình con rồi quan sát nó

sgrud dump PID                      một ảnh chụp dạng văn bản
sgrud dump PID -n 0.5               in liên tục mỗi 0.5 s cho đến khi tiến trình đích thoát
sgrud dump PID --json               mỗi dòng một đối tượng JSON
sgrud dump run -- python app.py     `run -- CMD` dùng được thay cho pid ở mọi nơi

sgrud profile PID                   lấy mẫu stack trong 5 s, in ra các hàm nóng nhất
sgrud profile PID -d 30 --mode gil  lấy mẫu trong 30 s, chỉ đếm thread đang giữ GIL
sgrud profile PID --mode async      lấy mẫu các task asyncio thay vì thread
sgrud profile PID --folded          stack dạng gộp cho flamegraph.pl hoặc speedscope
```

`--no-stacks`, `--no-tasks` và `--no-gc` bỏ bớt những phần bạn không cần
khỏi giao diện hoặc bản dump.

### Các chế độ lấy mẫu

- **wall**: mọi thread có stack Python đều được tính, nên một thread đang
  ngủ cũng nặng ngang một thread đang bận.
- **gil**: chỉ tính thread đang giữ GIL. Chế độ này trả lời câu hỏi "CPU
  đang đi đâu".
- **async**: lấy mẫu các task asyncio thay vì stack của thread, vì một
  coroutine đang đứng chờ ở `await` không nằm trên stack của thread nào cả.
  Mỗi task lá trở thành một stack: các frame của chính nó, một dấu
  `<task NAME>`, rồi đến các frame của từng task đang chờ nó cho tới gốc.
  Mọi task đều được tính, dù đang chạy hay đang tạm dừng, nên chế độ này
  trả lời câu hỏi "các task của tôi đang chờ gì". Mỗi mẫu tốn thời gian hơn
  so với đọc một stack, nên tần suất thực tế sẽ thấp hơn.

### TUI

| Phím | Hành động |
| --- | --- |
| `1`-`6`, `tab`, `shift+tab` | chuyển tab |
| `p` / `r` | tạm dừng / làm mới |
| `+` / `-` | thay đổi khoảng làm mới |
| `q` | thoát |
| `f` | lọc theo thread (Hotspots và Flame) |
| `m` | luân chuyển chế độ wall/gil/async (Hotspots và Flame) |
| `c` | xóa các mẫu (Hotspots và Flame) |
| `s` | đổi thứ tự sắp xếp self/total (Hotspots) |
| `enter` / `backspace` / `esc` | phóng to / thu nhỏ / đặt lại (Flame) |

Các phím mũi tên di chuyển ngay trong nội dung của tab hiện tại. Hotspots và
Flame dùng chung một bộ lấy mẫu chạy nền (`--rate`, mặc định 100 Hz) tiếp
tục hoạt động khi bạn xem các tab khác. Biểu đồ flame mọc từ dưới lên và
dành cho mỗi thread một khối riêng ở hàng đầu tiên, nên một thread nhàn rỗi
sẽ hiện thành một cột cao thay vì bị trộn lẫn với các thread khác.

## Thư viện

TUI chỉ là lớp giao diện. Mọi thứ đều đến từ `Monitor`, vốn trả về các
dataclass frozen thuần túy:

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

`Monitor.stream(interval)` sinh ra các ảnh chụp liên tiếp cho đến khi tiến
trình đích thoát, rồi ném ra `ProcessExited`. Phần trăm CPU cần hai ảnh chụp
để tính, nên ảnh chụp đầu tiên báo `None`.

Để profiling, `Sampler` chạy `Monitor.sample_stacks()` trong một thread nền
và đưa kết quả vào bộ tổng hợp `Hotspots`:

```python
from sgrud.sampler import Sampler

with Sampler(monitor, rate=500, mode="gil") as sampler:
    time.sleep(5)
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()  # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## Quyền hạn

Bộ nhớ, CPU và tên thread lấy từ `/proc` và hoạt động với mọi tiến trình
thuộc về bạn. Những thứ còn lại đọc bộ nhớ của tiến trình đích, việc này cần
quyền ptrace, và giá trị mặc định `kernel.yama.ptrace_scope=1` chỉ cấp quyền
đó cho các tiến trình con. Nếu thiếu quyền, sgrud gắn vào ở chế độ hạn chế
và hiện một banner giải thích những gì đang thiếu. Để có đầy đủ mọi thứ, hãy
khởi chạy tiến trình đích qua `sgrud run -- ...`, chạy sgrud với `sudo`, cấp
`CAP_SYS_PTRACE`, hoặc nới lỏng Yama cho phiên hiện tại:

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

Truyền `require_full=True` cho `Monitor.attach` để báo lỗi thay vì chạy ở
chế độ hạn chế. Tiến trình đích khởi chạy với `-X disable-remote-debug` vẫn
có thể được quan sát, vì cờ đó chỉ tắt việc chèn mã, điều mà sgrud không
dùng đến.

## Phát triển

```
uv sync
uv run pytest
```

Các bài kiểm thử khởi chạy `tests/target_app.py` rồi quan sát nó, nên chúng
chạy qua đường gắn kết thật. Ứng dụng Textual được kiểm thử ở chế độ headless
thông qua pilot của nó.
