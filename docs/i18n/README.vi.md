# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | **Tiếng Việt** | [Français](README.fr.md) | [Deutsch](README.de.md)

sgrud (bắt nguồn từ tiếng Gael Scotland *sgrùd*, nghĩa là "kiểm tra (檢查)" hay "khảo sát (考察)") là một công cụ chẩn đoán dùng để quan sát các tiến trình Python đang chạy. Gắn vào một tiến trình CPython và theo dõi bộ nhớ, CPU, các thread, các task asyncio, stack và bộ thu gom rác của nó mà không làm nó chậm đi.

sgrud không bao giờ dừng hay chèn mã vào tiến trình đích. Nó đọc trạng thái của trình thông dịch trực tiếp từ bộ nhớ tiến trình thông qua module `_remote_debugging` của CPython 3.15 (cơ chế đứng sau profiler Tachyon và `python -m asyncio ps`) và kết hợp với những gì hệ điều hành báo cáo qua psutil về bộ nhớ, CPU, thread và các tệp đang mở. Một ảnh chụp stack của mọi thread chỉ tốn vài chục micro giây và không tốn gì ở phía tiến trình đích.

![Tab Threads, hiển thị trạng thái, mức dùng CPU và stack Python hiện tại của từng thread](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*Tab Threads: mỗi thread với trạng thái, phần CPU và stack Python đang chạy.*

## Tính năng

- **Process**: bộ nhớ được tách chi tiết đến mức nền tảng cho phép, CPU, lỗi trang, giới hạn, hạn mức và việc bóp băng thông của cgroup, cùng các tiến trình con với những tiến trình là trình thông dịch Python được đánh dấu.
- **Threads**: mọi thread với trạng thái, phần CPU, stack Python đang chạy và, trên Linux, system call mà nó đang bị chặn.
- **Tasks**: cây task asyncio, mỗi task kèm các frame coroutine nó đang dừng.
- **GC**: thời gian dành cho thu gom, tốc độ thu gom, số đối tượng đang được theo dõi, lịch sử các lần thu gom và các hàm đã kích hoạt chúng.
- **Hotspots** và **Flame**: một profiler lấy mẫu chạy nền với các chế độ wall, GIL, CPU, exception và task asyncio, hiển thị dưới dạng bảng hoặc biểu đồ flame.
- **IPC**: các descriptor đang mở, pipe và ai đang giữ đầu bên kia, socket, bộ nhớ chia sẻ và khóa tệp, dành cho tiến trình bị treo.
- `sgrud dump` in ra cùng nội dung đó dưới dạng văn bản hoặc JSON, `sgrud profile` lấy mẫu trong một khoảng thời gian rồi ghi ra bất kỳ định dạng Tachyon nào, còn `sgrud probe` hỏi mục tiêu những gì chỉ đọc bộ nhớ không thể cho thấy. `--web` đưa giao diện lên trình duyệt.
- Một lớp `Monitor` trả về các dataclass thuần túy, nên tất cả đều dùng được dưới dạng thư viện.

## Cài đặt

```
pip install sgrud
pip install "sgrud[web]"    # thêm --web
```

`uv tool install sgrud` và `pipx install sgrud` cũng dùng được. sgrud cần CPython 3.15 trở lên trên Linux, macOS hoặc Windows, và tiến trình đích phải chạy cùng phiên bản major.minor với chính sgrud.

Bộ nhớ, CPU và tên thread hoạt động với mọi tiến trình thuộc về bạn. Đọc trạng thái của trình thông dịch cần quyền ptrace trên Linux, root trên macOS và cùng người dùng trên Windows. Nếu thiếu quyền, sgrud gắn vào ở chế độ hạn chế và cho biết những gì đang thiếu. Cách đơn giản nhất để có đầy đủ mọi thứ là khởi chạy tiến trình đích qua sgrud, xem [Quyền hạn](../reference.md#permissions) để biết các cách khác.

## Cách dùng

```
sgrud PID                           giao diện terminal tương tác
sgrud run -- python app.py          khởi chạy tiến trình đích như tiến trình con rồi quan sát nó
sgrud PID --web                     cùng giao diện đó nhưng mở trong trình duyệt

sgrud dump PID                      một ảnh chụp dạng văn bản, --json để ra JSON
sgrud profile PID -d 30 --mode gil  lấy mẫu thread đang giữ GIL trong 30 s, in ra các hàm nóng nhất
sgrud profile PID -o out.html       ghi flame graph thay vì in bảng
sgrud probe PID                     chạy một script bên trong mục tiêu: ngưỡng gc, bộ cấp phát, thread
```

`examples/demo_app.py` cho mọi tab đều có nội dung. Khởi động nó rồi trỏ sgrud vào pid mà nó in ra.

Từ Python:

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.frames[:1])
```

[Tài liệu tham khảo](../reference.md) trình bày mọi lệnh và tùy chọn, các chế độ lấy mẫu và định dạng đầu ra, phím và tab của TUI, thư viện, những gì mỗi nền tảng báo cáo và cách lấy quyền.

## Phát triển

```
uv sync
uv run pytest
```

Các bài kiểm thử khởi chạy `tests/target_app.py` rồi quan sát nó, nên chúng chạy qua đường gắn kết thật. Ứng dụng Textual được kiểm thử ở chế độ headless thông qua pilot của nó.
