# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | **한국어** | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

sgrud(스코틀랜드 게일어 *sgrùd*에서 유래했으며 "검사(檢査)" 또는 "조사(調査)"를 뜻합니다)는 실행 중인 Python 프로세스를 들여다보기 위한 진단 도구입니다. CPython 프로세스에 붙어서 메모리, CPU, 스레드, asyncio 태스크, 스택, 가비지 컬렉터를 대상 프로세스를 느리게 만들지 않고 관찰할 수 있습니다.

sgrud는 대상을 멈추거나 계측 코드를 주입하지 않습니다. CPython 3.15의 `_remote_debugging` 모듈(Tachyon 프로파일러와 `python -m asyncio ps`의 기반이 되는 장치)을 통해 프로세스 메모리에서 인터프리터 상태를 직접 읽어 오고, 메모리, CPU, 스레드, 열린 파일에 대해 OS가 psutil을 통해 보고하는 정보를 함께 사용합니다. 모든 스레드의 스택을 한 번 스냅샷하는 데 수십 마이크로초가 들며 대상 쪽에는 아무런 비용이 없습니다.

![Threads 탭. 각 스레드의 상태, CPU 사용량, 현재 Python 스택을 표시](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*Threads 탭: 각 스레드의 상태, CPU 점유율, 실시간 Python 스택.*

## 기능

- **Process**: 플랫폼이 허용하는 만큼 나누어 보여 주는 메모리, CPU, 페이지 폴트, 한도, cgroup의 할당량과 스로틀링, 그리고 그중 Python 인터프리터를 표시한 자식 프로세스 목록.
- **Threads**: 모든 스레드의 상태, CPU 점유율, 실시간 Python 스택, 그리고 Linux에서는 블록되어 있는 시스템 호출.
- **Tasks**: asyncio 태스크 트리. 각 태스크가 멈춰 있는 코루틴 프레임을 함께 표시.
- **GC**: 수집에 쓴 시간, 수집 속도, 추적 중인 객체 수, 수집 이력과 수집을 유발한 함수.
- **Hotspots**와 **Flame**: wall, GIL, CPU, exception, asyncio 태스크 모드를 갖춘 백그라운드 샘플링 프로파일러. 표 또는 플레임 그래프로 표시.
- **IPC**: 열린 디스크립터, 파이프와 그 반대쪽 끝을 쥔 프로세스, 소켓, 공유 메모리, 파일 잠금. 멈춘 프로세스를 위한 탭.
- `sgrud dump`는 같은 내용을 텍스트나 JSON으로 출력하고, `sgrud profile`은 일정 시간 샘플링해 Tachyon의 어떤 형식으로든 저장하며, `sgrud probe`는 메모리만으로는 알 수 없는 것을 대상에게 직접 물어봅니다. `--web`은 같은 화면을 브라우저로 제공합니다.
- 단순한 dataclass를 반환하는 `Monitor` 클래스가 있어 이 모든 것을 라이브러리로도 쓸 수 있습니다.

## 설치

```
pip install sgrud
pip install "sgrud[web]"    # --web 추가
```

`uv tool install sgrud`와 `pipx install sgrud`도 됩니다. sgrud는 Linux, macOS, Windows에서 CPython 3.15 이상이 필요하며, 대상은 sgrud 자체와 같은 major.minor 버전으로 실행되어야 합니다.

메모리, CPU, 스레드 이름은 자신이 소유한 모든 프로세스에 대해 동작합니다. 인터프리터 상태를 읽으려면 Linux에서는 ptrace 권한, macOS에서는 root, Windows에서는 같은 사용자여야 합니다. 권한이 없으면 sgrud는 제한 모드로 붙고 무엇이 빠져 있는지 알려 줍니다. 모든 기능을 얻는 가장 간단한 방법은 sgrud를 통해 대상을 시작하는 것이며, 다른 방법은 [권한](../reference.md#permissions)을 참고하십시오.

## 사용법

```
sgrud PID                           대화형 터미널 인터페이스
sgrud run -- python app.py          대상을 자식 프로세스로 시작하고 검사
sgrud PID --web                     같은 화면을 브라우저로 제공

sgrud dump PID                      텍스트 스냅샷 한 번, --json이면 JSON
sgrud profile PID -d 30 --mode gil  30초 동안 GIL을 쥔 스레드를 샘플링하고 가장 뜨거운 함수를 출력
sgrud profile PID -o out.html       대신 플레임 그래프를 저장
sgrud probe PID                     대상 안에서 스크립트 실행: gc 임계값, 할당기, 스레드
```

`examples/demo_app.py`는 모든 탭에 볼거리를 채워 주는 대상 프로그램입니다. 실행한 뒤 출력되는 pid에 sgrud를 붙이면 됩니다.

Python에서:

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.frames[:1])
```

[참조 문서](../reference.md)는 모든 명령과 옵션, 샘플링 모드와 출력 형식, TUI의 키와 탭, 라이브러리, 각 플랫폼이 보고하는 내용, 권한을 얻는 방법을 다룹니다.

## 개발

```
uv sync
uv run pytest
```

테스트는 `tests/target_app.py`를 실행하고 검사하므로 실제 attach 경로를 그대로 거칩니다. Textual 앱은 pilot을 통해 헤드리스로 테스트됩니다.
