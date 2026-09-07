# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | **Deutsch**

sgrud (vom schottisch-gälischen *sgrùd*, was „Prüfung“ oder „Untersuchung“
bedeutet) ist ein Diagnosewerkzeug zur Untersuchung laufender Python-Prozesse.
Hängen Sie sich an einen CPython-Prozess an und beobachten Sie seinen Speicher,
seine CPU, Threads, asyncio-Tasks, Stacks und den Garbage Collector, ohne ihn
auszubremsen.

sgrud hält das Ziel nie an und instrumentiert es nicht. Es liest den
Interpreterzustand direkt aus dem Prozessspeicher über das Modul
`_remote_debugging` von CPython 3.15 (die Mechanik hinter dem Tachyon-Profiler
und `python -m asyncio ps`) und kombiniert das mit dem, was das Betriebssystem
für die Speicher- und CPU-Abrechnung meldet. Ein Schnappschuss der Stacks aller
Threads kostet einige Dutzend Mikrosekunden und auf der Zielseite gar nichts.

Erfordert CPython 3.15 oder neuer unter Linux, macOS oder Windows. Das Ziel
muss dieselbe major.minor-Version wie sgrud selbst verwenden. Linux liefert
das vollständige Bild, siehe [Plattformen](#plattformen) für das, was den
anderen fehlt.

![Der Threads-Tab mit Zustand, CPU-Auslastung und aktuellem Python-Stack jedes Threads](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*Der Threads-Tab: jeder Thread mit Zustand, CPU-Anteil und aktuellem Python-Stack.*

## Verwendung

```
sgrud PID                           interaktive Terminaloberfläche
sgrud run -- python app.py          das Ziel als Kindprozess starten und untersuchen
sgrud PID --web                     dieselbe Oberfläche im Browser

sgrud dump PID                      ein Textschnappschuss
sgrud dump PID -n 0.5               alle 0,5 s weiter ausgeben, bis das Ziel beendet ist
sgrud dump PID --json               ein JSON-Objekt pro Zeile
sgrud dump run -- python app.py     `run -- CMD` funktioniert überall anstelle einer PID

sgrud profile PID                   5 s lang Stacks abtasten, die heißesten Funktionen ausgeben
sgrud profile PID -d 30 --mode gil  30 s lang abtasten, nur den Thread mit dem GIL zählen
sgrud profile PID --mode async      asyncio-Tasks statt Threads abtasten
sgrud profile PID --folded          zusammengefaltete Stacks für flamegraph.pl oder speedscope
sgrud profile PID -o out.html       Flame-Graph schreiben, oder .json / .pstats / .txt / .jsonl / ein Verzeichnis
sgrud profile PID -o out.bin        Aufzeichnung für `python -m profiling.sampling replay`
sgrud PID --record out.bin          die Oberfläche, wobei jedes Sample aufgezeichnet wird

sgrud probe PID                     ein Skript im Ziel ausführen: gc-Schwellen, Allokator, Threads
sgrud probe PID -t 10               zusätzlich verfolgte Objekte nach Typ zählen, die zehn häufigsten
```

`--no-stacks`, `--no-tasks`, `--no-gc`, `--no-children` und `--no-ipc` blenden
Abschnitte, die Sie nicht brauchen, aus der Oberfläche oder dem Dump aus.

`examples/demo_app.py` bringt etwas auf jeden Tab. Es lässt beschäftigte und
blockierte Threads laufen, einen asyncio-Taskbaum, `multiprocessing`-Worker,
Pipes, Sockets, Shared Memory und eine Dateisperre, die ein Kindprozess hält.
Starten Sie es und richten Sie sgrud auf die ausgegebene pid.

### Sampling-Modi

- **wall**: jeder Thread mit einem Python-Stack zählt, ein schlafender Thread
  wiegt also genauso viel wie ein beschäftigter.
- **gil**: nur der Inhaber des GIL zählt. Das beantwortet die Frage „wohin
  geht die CPU“.
- **cpu**: nur Threads zählen, die das Betriebssystem gerade auf einem Kern
  ausführt. C-Code, der den GIL freigegeben hat, zählt also weiterhin, ein
  Thread, der auf den GIL wartet, nicht.
- **exception**: nur Threads zählen, die gerade eine Exception behandeln.
  Das zeigt, wo Exceptions ausgelöst werden und wie weit sie wandern, bevor
  sie gefangen werden.
- **async**: tastet asyncio-Tasks statt Thread-Stacks ab, denn eine Coroutine,
  die in einem `await` wartet, liegt auf keinem Thread-Stack. Jeder Blatt-Task
  wird zu einem Stack: seine eigenen Frames, eine `<task NAME>`-Markierung und
  dann die Frames jedes Tasks, der auf ihn wartet, bis hinauf zur Wurzel. Jeder
  Task zählt, ob laufend oder angehalten, das beantwortet also die Frage
  „worauf warten meine Tasks“. Pro Sample ist das langsamer als das Lesen eines
  Stacks, rechnen Sie daher mit einer niedrigeren erreichten Rate.

### Ausgabeformate

`profile -o PATH` schreibt die Samples in einem Format von
`profiling.sampling` aus der Standardbibliothek (dem Tachyon-Profiler),
statt eine Tabelle auszugeben. Die Endung bestimmt das Format: `.html` ist
ein Flame-Graph, `.json` ein Firefox-Profiler-Dokument, `.pstats` lässt sich
mit `pstats.Stats` laden, `.txt` sind zusammengefaltete Stacks, `.jsonl` ein
Sample pro Zeile und ein Verzeichnis bekommt eine Heatmap des Quelltexts.
`.bin` ist das Binärformat von Tachyon, das
`python -m profiling.sampling replay` später in jedes der anderen umwandelt.
`--baseline old.bin` macht den Flame-Graph zu einem Differenzgraphen
gegenüber einer früheren Aufzeichnung, und `--opcodes` zeichnet für die
Formate, die sie anzeigen, die Bytecode-Instruktion jedes Frames auf.
`sgrud PID --record out.bin` macht dieselbe Aufzeichnung unter der
Oberfläche, über Moduswechsel hinweg.

### TUI

| Taste | Aktion |
| --- | --- |
| `1`-`7`, `tab`, `shift+tab` | Tabs wechseln |
| `p` / `r` | pausieren / aktualisieren |
| `+` / `-` | Aktualisierungsintervall ändern |
| `q` | beenden |
| `f` | Thread-Filter (Hotspots und Flame) |
| `m` | Sampling-Modus durchschalten (Hotspots und Flame) |
| `x` | Ziel sondieren (GC) |
| `c` | Samples löschen (Hotspots und Flame) |
| `s` | Sortierung self/total umschalten (Hotspots) |
| `enter` / `backspace` / `esc` | hineinzoomen / herauszoomen / zurücksetzen (Flame) |

Die Pfeiltasten bewegen sich sofort durch den Inhalt des aktuellen Tabs.
Hotspots und Flame teilen sich einen Hintergrund-Sampler (`--rate`, Standard
100 Hz), der weiterläuft, während Sie sich andere Tabs ansehen. Der Flame
Graph wächst von unten nach oben und gibt jedem Thread in der ersten Zeile
einen eigenen Block, sodass ein untätiger Thread als hohe Säule erscheint,
statt mit den anderen vermischt zu werden.

Der GC-Tab zeigt den Anteil der Zeit, der auf Collections entfällt,
Collections pro Sekunde, die Zahl der verfolgten Objekte und eine Historie der
Collections. Das Ziel behält nur seine letzten 11 Collections der jungen und 3
der alten Generationen, deshalb sammelt der Monitor jeden Eintrag, den er
gesehen hat. Läuft der Sampler, nennt der Tab auch die Funktionen, aus denen
Collections ausgelöst wurden, also die Stellen mit dem meisten
Allokationsaufkommen. Der Process-Tab schlüsselt den Speicher so weit auf, wie
es die Plattform erlaubt, siehe [Plattformen](#plattformen), und listet die
Kindprozesse des Ziels mit CPU und Speicher auf, wobei die Python-Interpreter
darunter markiert werden. Ein `multiprocessing`-Pool oder ein von einem
Supervisor gestarteter Worker ist so auf einen Blick zu sehen, und jeder davon
lässt sich mit einem zweiten `sgrud PID` untersuchen. Unter Linux zeigt der
Tab auch die cgroup, in der das Ziel läuft, etwa die des Containers: ihr
CPU-Kontingent und den Anteil der Perioden, in denen sie gedrosselt wurde,
OOM-Kills und das pid-Limit, auf das auch Threads zählen, daneben die Zahl
der CPUs, auf denen der Prozess laufen darf. Jede cgroup-Zahl gilt für die
ganze cgroup, nicht nur für das Ziel.

Der IPC-Tab ist für den Prozess, der hängt: Er listet jeden Deskriptor, den
das Ziel offen hat, Pipes, Sockets mit Adressen und Zustand, Shared Memory,
Dateien, und zu jeder Pipe, welche von Eltern- und Kindprozessen des Ziels
das andere Ende halten. Über der Tabelle stehen die Zahl der Deskriptoren
gegenüber ihrem Limit, die gemappten Shared-Memory-Segmente und
`multiprocessing`-Semaphoren sowie die Dateisperren, die das Ziel hält oder,
in Rot, auf die es wartet, samt der pid, die sie hält. Unter Linux zeigt der
Threads-Tab zusätzlich den Systemaufruf, in dem ein schlafender Thread
steckt, und den betroffenen Deskriptor, etwa `read(fd 4)`, und die
IPC-Tabelle nennt den Thread neben diesem Deskriptor. Gezeigt wird nur, was
der Kernel meldet: Ein `futex`-Warten ist ein Lock oder das GIL, und der
Python-Stack daneben sagt, welches von beiden.

![Der Tasks-Tab mit dem Baum der asyncio-Tasks und dem, worauf jeder Task wartet](https://github.com/user-attachments/assets/e8f1e9b0-2d8c-4b39-b67a-b9ba3ae2fa1d)

*Der Tasks-Tab: asyncio-Tasks als Baum, jeder mit den Coroutine-Frames, in denen er wartet.*

![Der Hotspots-Tab mit den Funktionen mit den meisten CPU-Samples](https://github.com/user-attachments/assets/32c75ac9-e279-41e8-83a2-b7e2970275e4)

*Der Hotspots-Tab: Funktionen sortiert nach Self- und Total-Samples des Hintergrund-Samplers.*

![Der Flame-Tab mit einem Flame Graph der gesampelten Stacks](https://github.com/user-attachments/assets/b78cfbec-3619-45a6-a2a9-5bc57f2e45f3)

*Der Flame-Tab: dieselben Samples als Flame Graph, ein Block pro Thread in der ersten Zeile.*

### Web

`--web` stellt dieselbe Oberfläche über
[textual-serve](https://github.com/Textualize/textual-serve) im Browser bereit.
Das ist eine optionale Abhängigkeit, installieren Sie dafür `sgrud[web]`.
Gelauscht wird auf `http://127.0.0.1:8000`, sofern `--host` und `--port` nichts
anderes sagen. Jeder Browser-Tab bekommt eine eigene Kopie der Oberfläche, die
an dasselbe Ziel angehängt ist. Es gibt keine Authentifizierung, also lassen Sie
es auf localhost oder hinter etwas, das eine bereitstellt.
Unter Linux wird das Ziel bei
`run -- CMD` so gestartet, dass jeder Prozess desselben Benutzers es lesen darf,
da die Browser-Sitzungen nicht sein Elternprozess sind.

### Sondieren

Alles oben liest den Speicher des Ziels von außen. `sgrud probe` ist die
einzige Ausnahme: Es lässt über `sys.remote_exec` den Hauptthread des Ziels
an seinem nächsten sicheren Punkt ein kurzes Skript ausführen, das meldet,
was der Interpreter nicht im Speicher preisgibt. Das sind die gc-Schwellen
und die Zähler, mit denen sie verglichen werden, ob der Collector aktiv ist,
wie viele Objekte eingefroren sind oder in `gc.garbage` liegen, die Zahl der
Blöcke des Allokators, die Zahl der Module und Threads, mit `-t N` ein
Histogramm der verfolgten Objekte nach Typ und ein tracemalloc-Schnappschuss,
falls das Ziel das Tracing bereits eingeschaltet hat. `x` im GC-Tab führt
dieselbe Sonde aus. Sie kostet das Ziel einige Millisekunden auf seinem
Hauptthread, mit Typ-Histogramm mehr, und wartet darauf, dass dieser Thread
einen sicheren Punkt erreicht, ein in C-Code festhängender Hauptthread lässt
sie also in den Timeout laufen. sgrud führt sie nie von sich aus aus.

## Bibliothek

Die TUI ist nur ein Frontend. Alles stammt aus `Monitor`, das einfache
eingefrorene Dataclasses zurückgibt:

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
    print(snap.process.cgroup.cpu_quota, snap.process.cgroup.throttled_percent, snap.process.cgroup.oom_kills)
    print([(c.pid, c.python, c.rss) for c in snap.children])
    print(snap.to_dict())  # JSON friendly
    result = m.probe(types=5)  # runs code in the target, see Probing
    print(result.gc_threshold, result.gc_count, result.types)
```

`Monitor.stream(interval)` liefert Schnappschüsse, bis das Ziel beendet ist,
und wirft dann `ProcessExited`. CPU-Prozentwerte brauchen zwei Schnappschüsse,
der erste meldet daher `None`.

Zum Profilen führt `Sampler` `Monitor.sample_stacks()` in einem
Hintergrund-Thread aus und speist einen `Hotspots`-Aggregator:

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

## Plattformen

Stacks, asyncio-Tasks, GC und der Profiler stammen aus `_remote_debugging` und
verhalten sich überall gleich. Die Prozess- und Thread-Abrechnung kommt über
psutil vom Betriebssystem, und dort unterscheiden sich die Plattformen.

- **Linux** meldet alles, einschließlich des Scheduler-Zustands jedes Threads,
  der im wall-Modus einen Thread als auf der CPU laufend markiert, sowie das
  vollständige Speicherbild: anonymer und dateigestützter Anteil des rss, USS
  und PSS, brk-Heap und anonyme Mappings, Transparent Huge Pages,
  Page-Fault-Rate, das Speicherlimit der cgroup und den OOM-Score, mit cgroup
  v2 auch CPU-Kontingent, Drosselung, OOM-Kills und pid-Limit der cgroup. Nur hier
  gibt es auch das vollständige IPC-Bild: Pipes und ihre anderen Enden, Shared
  Memory, Dateisperren und den Systemaufruf, in dem jeder Thread blockiert
  (das braucht denselben Zugriff wie das Lesen des Speichers, und eine
  Aufruftabelle hat sgrud für x86_64, aarch64, riscv64 und loongarch64; sonst
  erscheinen Aufrufe als Nummer).
- **Windows** hat Thread-Namen und CPU-Zeit pro Thread, aber keinen
  Scheduler-Zustand, daher zeigen Threads im wall-Modus `?` statt `cpu` /
  `idle`. Der Speicher umfasst `rss`, `vms`, das Working-Set-Maximum, private
  Bytes, USS und die Page-Fault-Rate. IPC beschränkt sich auf die Zahl der
  Handles, offene Dateien und Sockets, ohne Deskriptornummern.
- **macOS** kann Betriebssystem-Threads nicht den Thread-IDs des Interpreters
  zuordnen, daher erscheinen Threads ohne Namen und CPU-Werte. Der Speicher
  umfasst `rss`, `vms`, USS und die Page-Fault-Rate. IPC beschränkt sich auf
  die Zahl der Deskriptoren, offene Dateien und Sockets. Den Speicher eines
  anderen Prozesses zu lesen erfordert root, also sgrud mit `sudo` starten.

## Berechtigungen

Speicher, CPU und Thread-Namen stammen vom Betriebssystem und funktionieren für
jeden Prozess, der Ihnen gehört. Alles andere liest den Speicher des Ziels.
Unter Linux erfordert das ptrace-Rechte, und das Standard-`kernel.yama.ptrace_scope=1`
gewährt diese nur für Kindprozesse. Ohne sie hängt sich sgrud im eingeschränkten Modus
an und zeigt ein Banner, das erklärt, was fehlt. Um alles zu bekommen, starten
Sie das Ziel über `sgrud run -- ...`, führen Sie sgrud mit `sudo` aus,
gewähren Sie `CAP_SYS_PTRACE` oder lockern Sie Yama für die Sitzung:

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

Unter macOS kann nur root den Speicher eines anderen Prozesses lesen, verwenden
Sie also `sudo`. Unter Windows funktioniert jeder Prozess desselben Benutzers,
andere erfordern einen Administrator.

Übergeben Sie `require_full=True` an `Monitor.attach`, um fehlzuschlagen statt
in den eingeschränkten Modus zu wechseln. Ein mit `-X disable-remote-debug`
gestartetes Ziel lässt sich weiterhin untersuchen, da dieses Flag nur die
Code-Injektion abschaltet. Das Einzige, was es blockiert, ist `sgrud probe`.

## Entwicklung

```
uv sync
uv run pytest
```

Die Tests starten `tests/target_app.py` und untersuchen es, sie durchlaufen
also den echten Attach-Pfad. Die Textual-App wird headless über ihren Pilot
getestet.
