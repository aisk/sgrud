# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | **한국어** | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

sgrud(스코틀랜드 게일어 *sgrùd*에서 유래했으며 "검사(檢査)" 또는 "조사(調査)"를 뜻합니다)는
실행 중인 Python 프로세스를 들여다보기 위한 진단 도구입니다. CPython 프로세스에
붙어서 메모리, CPU, 스레드, asyncio 태스크, 스택, 가비지 컬렉터를 대상 프로세스를
느리게 만들지 않고 관찰할 수 있습니다.

sgrud는 대상을 멈추거나 계측 코드를 주입하지 않습니다. CPython 3.15의
`_remote_debugging` 모듈(Tachyon 프로파일러와 `python -m asyncio ps`의 기반이 되는
장치)을 통해 프로세스 메모리에서 인터프리터 상태를 직접 읽어 오고, 메모리와 CPU
집계를 위해 OS가 보고하는 정보를 함께 사용합니다. 모든 스레드의 스택을 한 번
스냅샷하는 데 수십 마이크로초가 들며 대상 쪽에는 아무런 비용이 없습니다.

CPython 3.15 이상이 필요하며 Linux, macOS, Windows에서 동작합니다. 대상은 sgrud
자체와 같은 major.minor 버전으로 실행되어야 합니다. Linux에서는 모든 정보를 얻을
수 있고, 다른 플랫폼과의 차이는 [플랫폼](#플랫폼)을 참고하십시오.

![Threads 탭. 각 스레드의 상태, CPU 사용량, 현재 Python 스택을 표시](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*Threads 탭: 각 스레드의 상태, CPU 점유율, 실시간 Python 스택.*

## 사용법

```
sgrud PID                           대화형 터미널 인터페이스
sgrud run -- python app.py          대상을 자식 프로세스로 시작하고 검사
sgrud PID --web                     같은 화면을 브라우저로 제공

sgrud dump PID                      텍스트 스냅샷 한 번
sgrud dump PID -n 0.5               대상이 종료될 때까지 0.5초마다 계속 출력
sgrud dump PID --json               한 줄에 JSON 객체 하나
sgrud dump run -- python app.py     `run -- CMD`는 어디서든 pid 대신 사용 가능

sgrud profile PID                   5초 동안 스택을 샘플링하고 가장 뜨거운 함수를 출력
sgrud profile PID -d 30 --mode gil  30초 동안 샘플링하되 GIL을 쥔 스레드만 집계
sgrud profile PID --mode async      스레드 대신 asyncio 태스크를 샘플링
sgrud profile PID --folded          flamegraph.pl이나 speedscope용 접힌 스택
```

`--no-stacks`, `--no-tasks`, `--no-gc`를 사용하면 필요 없는 섹션을 인터페이스나
덤프에서 제외할 수 있습니다.

### 샘플링 모드

- **wall**: Python 스택을 가진 모든 스레드를 집계하므로 잠들어 있는 스레드도
  바쁜 스레드와 같은 비중을 가집니다.
- **gil**: GIL을 쥔 스레드만 집계합니다. "CPU가 어디에 쓰이는가"에 답합니다.
- **async**: 스레드 스택 대신 asyncio 태스크를 샘플링합니다. `await`에서 멈춰
  있는 코루틴은 어떤 스레드의 스택에도 없기 때문입니다. 각 리프 태스크가 하나의
  스택이 되며, 자신의 프레임, `<task NAME>` 마커, 그 다음 루트까지 이 태스크를
  기다리는 각 태스크의 프레임 순으로 이어집니다. 실행 중이든 중단 상태이든 모든
  태스크를 집계하므로 "내 태스크들이 무엇을 기다리고 있는가"에 답합니다. 스택을
  읽는 것보다 샘플당 비용이 크므로 실제 달성되는 샘플링 속도는 더 낮습니다.

### TUI

| 키 | 동작 |
| --- | --- |
| `1`-`6`, `tab`, `shift+tab` | 탭 전환 |
| `p` / `r` | 일시 정지 / 새로 고침 |
| `+` / `-` | 새로 고침 간격 변경 |
| `q` | 종료 |
| `f` | 스레드 필터 (Hotspots 및 Flame) |
| `m` | wall/gil/async 모드 순환 (Hotspots 및 Flame) |
| `c` | 샘플 지우기 (Hotspots 및 Flame) |
| `s` | self/total 정렬 전환 (Hotspots) |
| `enter` / `backspace` / `esc` | 확대 / 축소 / 초기화 (Flame) |

화살표 키는 현재 탭의 내용을 바로 이동합니다. Hotspots와 Flame은 하나의 백그라운드
샘플러(`--rate`, 기본 100 Hz)를 공유하며, 다른 탭을 보고 있는 동안에도 계속
동작합니다. 플레임 그래프는 아래에서 위로 자라며 첫 번째 행에서 각 스레드에 고유한
블록을 배정하므로, 유휴 스레드는 다른 스레드와 섞이지 않고 높은 기둥으로 나타납니다.

![Tasks 탭. asyncio 태스크 트리와 각 태스크가 기다리는 대상을 표시](https://github.com/user-attachments/assets/e8f1e9b0-2d8c-4b39-b67a-b9ba3ae2fa1d)

*Tasks 탭: asyncio 태스크 트리. 각 태스크가 멈춰 있는 코루틴 프레임을 함께 표시.*

![Hotspots 탭. CPU 샘플이 가장 많은 함수 목록](https://github.com/user-attachments/assets/32c75ac9-e279-41e8-83a2-b7e2970275e4)

*Hotspots 탭: 백그라운드 샘플러의 self / total 샘플 수로 정렬한 함수.*

![Flame 탭. 샘플링한 스택의 플레임 그래프](https://github.com/user-attachments/assets/b78cfbec-3619-45a6-a2a9-5bc57f2e45f3)

*Flame 탭: 같은 샘플을 플레임 그래프로 표시. 첫 번째 행은 스레드마다 한 블록.*

### Web

`--web`을 주면 같은 화면을 [textual-serve](https://github.com/Textualize/textual-serve)를
통해 브라우저로 제공합니다. 선택 의존성이므로 `sgrud[web]`을 설치하세요. 기본으로
`http://127.0.0.1:8000`에서 대기하며 `--host`와 `--port`로 바꿀 수 있습니다.
브라우저 탭마다 같은 대상에 붙는 독립된 화면이 하나씩 생깁니다. 인증이 없으므로
localhost에만 두거나 인증을 제공하는 무언가 뒤에 두세요.
Linux에서 `run -- CMD`를 쓰면 브라우저 세션이 대상의 부모가 아니므로, 대상은
같은 사용자의 어떤 프로세스든 읽을 수 있도록 시작됩니다.

## 라이브러리

TUI는 프런트엔드일 뿐입니다. 모든 것은 `Monitor`에서 나오며, 단순한 frozen
dataclass를 반환합니다:

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

`Monitor.stream(interval)`은 대상이 종료될 때까지 스냅샷을 생성한 뒤
`ProcessExited`를 발생시킵니다. CPU 백분율은 두 개의 스냅샷이 필요하므로 첫 번째
스냅샷에서는 `None`을 보고합니다.

프로파일링의 경우 `Sampler`가 백그라운드 스레드에서 `Monitor.sample_stacks()`를
실행하고 `Hotspots` 집계기에 결과를 공급합니다:

```python
from sgrud.sampler import Sampler

with Sampler(monitor, rate=500, mode="gil") as sampler:
    time.sleep(5)
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()  # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## 플랫폼

스택, asyncio 태스크, GC, 프로파일러는 `_remote_debugging`에서 가져오므로 어디서나
같게 동작합니다. 프로세스와 스레드 집계는 psutil을 통해 OS에서 가져오며,
플랫폼별 차이는 여기에 있습니다.

- **Linux**는 스레드별 스케줄러 상태를 포함해 모든 것을 보고합니다. wall 모드에서
  스레드가 CPU 위에 있는지 표시하는 것이 바로 이 상태입니다.
- **Windows**는 스레드 이름과 스레드별 CPU 시간은 있지만 스케줄러 상태가 없어서,
  wall 모드에서 스레드가 `cpu` / `idle` 대신 `?`로 표시됩니다. swap과 공유
  메모리는 보고되지 않습니다.
- **macOS**는 OS 스레드를 인터프리터의 스레드 id와 맞출 수 없으므로 스레드에
  이름과 CPU 수치가 없고, 메모리는 `rss` / `vms`만 보고됩니다. 다른 프로세스의
  메모리를 읽으려면 root가 필요하므로 sgrud를 `sudo`로 실행하십시오.

## 권한

메모리, CPU, 스레드 이름은 OS에서 가져오므로 자신이 소유한 모든 프로세스에
대해 동작합니다. 그 외의 모든 것은 대상의 메모리를 읽습니다. Linux에서는 ptrace
권한이 필요한데, 기본값인 `kernel.yama.ptrace_scope=1`은 자식 프로세스에 대해서만
이 권한을 허용합니다. 권한이 없으면 sgrud는 제한 모드로 붙고 무엇이 빠져 있는지
설명하는 배너를 표시합니다. 모든 기능을 사용하려면 `sgrud run -- ...`으로 대상을
시작하거나, `sudo`로 sgrud를 실행하거나, `CAP_SYS_PTRACE`를 부여하거나, 해당
세션 동안 Yama를 완화하십시오:

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

macOS에서는 root만 다른 프로세스의 메모리를 읽을 수 있으므로 `sudo`를 사용하십시오.
Windows에서는 같은 사용자의 프로세스는 그대로 동작하고, 다른 사용자의 프로세스는
관리자 권한이 필요합니다.

`Monitor.attach`에 `require_full=True`를 넘기면 기능을 축소하는 대신 실패합니다.
`-X disable-remote-debug`로 시작된 대상도 검사할 수 있습니다. 그 플래그는 코드
주입만 비활성화하는데, sgrud는 코드 주입을 사용하지 않기 때문입니다.

## 개발

```
uv sync
uv run pytest
```

테스트는 `tests/target_app.py`를 실행하고 검사하므로 실제 attach 경로를 그대로
거칩니다. Textual 앱은 pilot을 통해 헤드리스로 테스트됩니다.
