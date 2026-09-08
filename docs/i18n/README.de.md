# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | **Deutsch**

sgrud (vom schottisch-gälischen *sgrùd*, was „Prüfung“ oder „Untersuchung“ bedeutet) ist ein Diagnosewerkzeug zur Untersuchung laufender Python-Prozesse. Hängen Sie sich an einen CPython-Prozess an und beobachten Sie seinen Speicher, seine CPU, Threads, asyncio-Tasks, Stacks und den Garbage Collector, ohne ihn auszubremsen.

sgrud hält das Ziel nie an und instrumentiert es nicht. Es liest den Interpreterzustand direkt aus dem Prozessspeicher über das Modul `_remote_debugging` von CPython 3.15 (die Mechanik hinter dem Tachyon-Profiler und `python -m asyncio ps`) und kombiniert das mit dem, was das Betriebssystem über psutil zu Speicher, CPU, Threads und offenen Dateien meldet. Ein Schnappschuss der Stacks aller Threads kostet einige Dutzend Mikrosekunden und auf der Zielseite gar nichts.

![Der Threads-Tab mit Zustand, CPU-Auslastung und aktuellem Python-Stack jedes Threads](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*Der Threads-Tab: jeder Thread mit Zustand, CPU-Anteil und aktuellem Python-Stack.*

## Funktionen

- **Process**: der Speicher so weit aufgeschlüsselt, wie es die Plattform erlaubt, CPU, Page Faults, Limits, das CPU-Kontingent und die Drosselung der cgroup sowie die Kindprozesse, wobei die Python-Interpreter darunter markiert werden.
- **Threads**: jeder Thread mit Zustand, CPU-Anteil, aktuellem Python-Stack und, unter Linux, dem Systemaufruf, in dem er blockiert.
- **Tasks**: der asyncio-Taskbaum, jeder Task mit den Coroutine-Frames, in denen er wartet.
- **GC**: die auf Collections entfallende Zeit, die Collection-Rate, verfolgte Objekte, eine Historie der Collections und die Funktionen, aus denen sie ausgelöst wurden.
- **Hotspots** und **Flame**: ein Hintergrund-Sampling-Profiler mit den Modi wall, GIL, CPU, exception und asyncio-Task, dargestellt als Tabelle oder als Flame Graph.
- **IPC**: offene Deskriptoren, Pipes und wer ihre anderen Enden hält, Sockets, Shared Memory und Dateisperren, für den Prozess, der hängt.
- `sgrud dump` gibt dasselbe als Text oder JSON aus, `sgrud profile` tastet eine Weile ab und schreibt jedes Tachyon-Format, und `sgrud probe` fragt das Ziel nach dem, was der Speicher allein nicht zeigen kann. `--web` stellt die Oberfläche im Browser bereit.
- Eine `Monitor`-Klasse, die einfache Dataclasses zurückgibt, sodass all das auch als Bibliothek verfügbar ist.

## Installation

```
pip install sgrud
pip install "sgrud[web]"    # fügt --web hinzu
```

`uv tool install sgrud` und `pipx install sgrud` funktionieren ebenfalls. sgrud braucht CPython 3.15 oder neuer unter Linux, macOS oder Windows, und das Ziel muss dieselbe major.minor-Version wie sgrud selbst verwenden.

Speicher, CPU und Thread-Namen funktionieren für jeden Prozess, der Ihnen gehört. Das Lesen des Interpreterzustands erfordert ptrace-Rechte unter Linux, root unter macOS und denselben Benutzer unter Windows. Ohne sie hängt sich sgrud im eingeschränkten Modus an und sagt, was fehlt. Am einfachsten bekommen Sie alles, indem Sie das Ziel über sgrud starten, siehe [Berechtigungen](../reference.md#permissions) für die anderen Wege.

## Verwendung

```
sgrud PID                           interaktive Terminaloberfläche
sgrud run -- python app.py          das Ziel als Kindprozess starten und untersuchen
sgrud PID --web                     dieselbe Oberfläche im Browser

sgrud dump PID                      ein Textschnappschuss, --json für JSON
sgrud profile PID -d 30 --mode gil  30 s lang den GIL-Inhaber abtasten, die heißesten Funktionen ausgeben
sgrud profile PID -o out.html       stattdessen einen Flame-Graph schreiben
sgrud probe PID                     ein Skript im Ziel ausführen: gc-Schwellen, Allokator, Threads
```

`examples/demo_app.py` bringt etwas auf jeden Tab. Starten Sie es und richten Sie sgrud auf die ausgegebene pid.

Aus Python:

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.frames[:1])
```

Die [Referenz](../reference.md) behandelt jeden Befehl und jede Option, die Sampling-Modi und Ausgabeformate, die Tasten und Tabs der TUI, die Bibliothek, was jede Plattform meldet und wie Sie die Berechtigungen bekommen.

## Entwicklung

```
uv sync
uv run pytest
```

Die Tests starten `tests/target_app.py` und untersuchen es, sie durchlaufen also den echten Attach-Pfad. Die Textual-App wird headless über ihren Pilot getestet.
