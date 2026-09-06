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
und `python -m asyncio ps`) und kombiniert das mit `/proc` für die Speicher-
und CPU-Abrechnung. Ein Schnappschuss der Stacks aller Threads kostet einige
Dutzend Mikrosekunden und auf der Zielseite gar nichts.

Erfordert Linux und CPython 3.15 oder neuer. Das Ziel muss dieselbe
major.minor-Version wie sgrud selbst verwenden.

## Verwendung

```
sgrud PID                           interaktive Terminaloberfläche
sgrud run -- python app.py          das Ziel als Kindprozess starten und untersuchen

sgrud dump PID                      ein Textschnappschuss
sgrud dump PID -n 0.5               alle 0,5 s weiter ausgeben, bis das Ziel beendet ist
sgrud dump PID --json               ein JSON-Objekt pro Zeile
sgrud dump run -- python app.py     `run -- CMD` funktioniert überall anstelle einer PID

sgrud profile PID                   5 s lang Stacks abtasten, die heißesten Funktionen ausgeben
sgrud profile PID -d 30 --mode gil  30 s lang abtasten, nur den Thread mit dem GIL zählen
sgrud profile PID --mode async      asyncio-Tasks statt Threads abtasten
sgrud profile PID --folded          zusammengefaltete Stacks für flamegraph.pl oder speedscope
```

`--no-stacks`, `--no-tasks` und `--no-gc` blenden Abschnitte, die Sie nicht
brauchen, aus der Oberfläche oder dem Dump aus.

### Sampling-Modi

- **wall**: jeder Thread mit einem Python-Stack zählt, ein schlafender Thread
  wiegt also genauso viel wie ein beschäftigter.
- **gil**: nur der Inhaber des GIL zählt. Das beantwortet die Frage „wohin
  geht die CPU“.
- **async**: tastet asyncio-Tasks statt Thread-Stacks ab, denn eine Coroutine,
  die in einem `await` wartet, liegt auf keinem Thread-Stack. Jeder Blatt-Task
  wird zu einem Stack: seine eigenen Frames, eine `<task NAME>`-Markierung und
  dann die Frames jedes Tasks, der auf ihn wartet, bis hinauf zur Wurzel. Jeder
  Task zählt, ob laufend oder angehalten, das beantwortet also die Frage
  „worauf warten meine Tasks“. Pro Sample ist das langsamer als das Lesen eines
  Stacks, rechnen Sie daher mit einer niedrigeren erreichten Rate.

### TUI

| Taste | Aktion |
| --- | --- |
| `1`-`6`, `tab`, `shift+tab` | Tabs wechseln |
| `p` / `r` | pausieren / aktualisieren |
| `+` / `-` | Aktualisierungsintervall ändern |
| `q` | beenden |
| `f` | Thread-Filter (Hotspots und Flame) |
| `m` | Modus wall/gil/async durchschalten (Hotspots und Flame) |
| `c` | Samples löschen (Hotspots und Flame) |
| `s` | Sortierung self/total umschalten (Hotspots) |
| `enter` / `backspace` / `esc` | hineinzoomen / herauszoomen / zurücksetzen (Flame) |

Die Pfeiltasten bewegen sich sofort durch den Inhalt des aktuellen Tabs.
Hotspots und Flame teilen sich einen Hintergrund-Sampler (`--rate`, Standard
100 Hz), der weiterläuft, während Sie sich andere Tabs ansehen. Der Flame
Graph wächst von unten nach oben und gibt jedem Thread in der ersten Zeile
einen eigenen Block, sodass ein untätiger Thread als hohe Säule erscheint,
statt mit den anderen vermischt zu werden.

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
    print(snap.gc[0].collections, snap.gc[0].history[:1])
    print(snap.to_dict())  # JSON friendly
```

`Monitor.stream(interval)` liefert Schnappschüsse, bis das Ziel beendet ist,
und wirft dann `ProcessExited`. CPU-Prozentwerte brauchen zwei Schnappschüsse,
der erste meldet daher `None`.

Zum Profilen führt `Sampler` `Monitor.sample_stacks()` in einem
Hintergrund-Thread aus und speist einen `Hotspots`-Aggregator:

```python
from sgrud.sampler import Sampler

with Sampler(monitor, rate=500, mode="gil") as sampler:
    time.sleep(5)
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()  # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## Berechtigungen

Speicher, CPU und Thread-Namen stammen aus `/proc` und funktionieren für jeden
Prozess, der Ihnen gehört. Alles andere liest den Speicher des Ziels, was
ptrace-Rechte erfordert, und das Standard-`kernel.yama.ptrace_scope=1` gewährt
diese nur für Kindprozesse. Ohne sie hängt sich sgrud im eingeschränkten Modus
an und zeigt ein Banner, das erklärt, was fehlt. Um alles zu bekommen, starten
Sie das Ziel über `sgrud run -- ...`, führen Sie sgrud mit `sudo` aus,
gewähren Sie `CAP_SYS_PTRACE` oder lockern Sie Yama für die Sitzung:

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

Übergeben Sie `require_full=True` an `Monitor.attach`, um fehlzuschlagen statt
in den eingeschränkten Modus zu wechseln. Ein mit `-X disable-remote-debug`
gestartetes Ziel lässt sich weiterhin untersuchen, da dieses Flag nur die
Code-Injektion abschaltet, die sgrud nicht verwendet.

## Entwicklung

```
uv sync
uv run pytest
```

Die Tests starten `tests/target_app.py` und untersuchen es, sie durchlaufen
also den echten Attach-Pfad. Die Textual-App wird headless über ihren Pilot
getestet.
