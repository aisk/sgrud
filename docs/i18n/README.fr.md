# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Tiếng Việt](README.vi.md) | **Français** | [Deutsch](README.de.md)

sgrud (du gaélique écossais *sgrùd*, qui signifie « inspection » ou
« examen ») est un outil de diagnostic pour inspecter des processus Python
en cours d'exécution. Attachez-vous à un processus CPython et observez sa
mémoire, son CPU, ses threads, ses tâches asyncio, ses piles et son ramasse-miettes
sans le ralentir.

sgrud n'arrête ni n'instrumente jamais la cible. Il lit l'état de
l'interpréteur directement dans la mémoire du processus grâce au module
`_remote_debugging` de CPython 3.15 (la mécanique derrière le profileur
Tachyon et `python -m asyncio ps`) et le combine avec ce que le système
rapporte pour la comptabilité mémoire et CPU. Un instantané de la pile de
chaque thread coûte quelques dizaines de microsecondes, et rien du côté de
la cible.

Nécessite CPython 3.15 ou plus récent sur Linux, macOS ou Windows. La cible
doit utiliser la même version majeure.mineure que sgrud lui-même. Linux
donne le tableau complet, voir [Plateformes](#plateformes) pour ce qui
manque aux autres.

![L'onglet Threads, montrant l'état, l'utilisation CPU et la pile Python courante de chaque thread](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*L'onglet Threads : chaque thread avec son état, sa part de CPU et sa pile Python en direct.*

## Utilisation

```
sgrud PID                           interface interactive dans le terminal
sgrud run -- python app.py          lance la cible comme processus enfant et l'inspecte
sgrud PID --web                     la même interface servie dans un navigateur

sgrud dump PID                      un instantané texte
sgrud dump PID -n 0.5               continue d'afficher toutes les 0,5 s jusqu'à la fin de la cible
sgrud dump PID --json               un objet JSON par ligne
sgrud dump run -- python app.py     `run -- CMD` remplace un pid partout

sgrud profile PID                   échantillonne les piles pendant 5 s, affiche les fonctions les plus chaudes
sgrud profile PID -d 30 --mode gil  échantillonne pendant 30 s, ne compte que le thread détenant le GIL
sgrud profile PID --mode async      échantillonne les tâches asyncio au lieu des threads
sgrud profile PID --folded          piles repliées pour flamegraph.pl ou speedscope
sgrud profile PID -o out.html       écrit un flame graph, ou .json / .pstats / .txt / .jsonl / un dossier
sgrud profile PID -o out.bin        enregistre pour `python -m profiling.sampling replay`
sgrud PID --record out.bin          l'interface, en enregistrant chaque échantillon pris
```

`--no-stacks`, `--no-tasks` et `--no-gc` retirent de l'interface ou du
dump les sections dont vous n'avez pas besoin.

### Modes d'échantillonnage

- **wall** : chaque thread possédant une pile Python compte, donc un thread
  endormi pèse autant qu'un thread occupé.
- **gil** : seul le détenteur du GIL compte. Cela répond à la question
  « où passe le CPU ».
- **cpu** : seuls les threads que l'OS exécute sur un cœur comptent, donc du
  code C ayant relâché le GIL compte encore et un thread attendant le GIL
  ne compte pas.
- **exception** : seuls les threads en train de traiter une exception
  comptent, ce qui montre où les exceptions sont levées et jusqu'où elles
  remontent avant d'être attrapées.
- **async** : échantillonne les tâches asyncio au lieu des piles de threads,
  puisqu'une coroutine en attente dans un `await` ne figure sur la pile
  d'aucun thread. Chaque tâche feuille devient une pile : ses propres
  frames, un marqueur `<task NAME>`, puis les frames de chaque tâche qui
  l'attend jusqu'à la racine. Chaque tâche compte, en cours ou suspendue,
  ce qui répond à la question « qu'attendent mes tâches ». C'est plus lent
  par échantillon que la lecture d'une pile, attendez-vous donc à une
  fréquence effective plus basse.

### Formats de sortie

`profile -o PATH` écrit les échantillons dans un format de
`profiling.sampling` de la bibliothèque standard (le profileur Tachyon) au
lieu d'afficher un tableau. L'extension choisit le format : `.html` est un
flame graph, `.json` un document Firefox Profiler, `.pstats` se charge avec
`pstats.Stats`, `.txt` contient des piles repliées, `.jsonl` un échantillon
par ligne et un dossier reçoit une carte de chaleur du code source. `.bin`
est le format binaire de Tachyon, que `python -m profiling.sampling replay`
convertit plus tard vers n'importe lequel des autres. `--baseline old.bin`
rend le flame graph différentiel par rapport à un enregistrement antérieur,
et `--opcodes` enregistre l'instruction bytecode de chaque frame pour les
formats qui l'affichent. `sgrud PID --record out.bin` fait le même
enregistrement sous l'interface, à travers les changements de mode.

### TUI

| Touche | Action |
| --- | --- |
| `1`-`6`, `tab`, `shift+tab` | changer d'onglet |
| `p` / `r` | pause / rafraîchir |
| `+` / `-` | modifier l'intervalle de rafraîchissement |
| `q` | quitter |
| `f` | filtre de threads (Hotspots et Flame) |
| `m` | alterner entre les modes d'échantillonnage (Hotspots et Flame) |
| `c` | effacer les échantillons (Hotspots et Flame) |
| `s` | basculer le tri self/total (Hotspots) |
| `enter` / `backspace` / `esc` | zoom avant / arrière / réinitialiser (Flame) |

Les flèches du clavier parcourent immédiatement le contenu de l'onglet
courant. Hotspots et Flame partagent un même échantillonneur en arrière-plan
(`--rate`, 100 Hz par défaut) qui continue de tourner pendant que vous
consultez d'autres onglets. Le graphe en flammes pousse depuis le bas et
attribue à chaque thread son propre bloc sur la première ligne, si bien
qu'un thread inactif apparaît comme une grande colonne au lieu d'être
mélangé aux autres.

L'onglet GC montre la part du temps passée à collecter, le nombre de collectes
par seconde, le nombre d'objets suivis et un historique des collectes. La
cible ne garde que ses 11 dernières collectes de la jeune génération et les 3
dernières des anciennes, donc le monitor accumule chaque enregistrement qu'il
a vu. Pendant que l'échantillonneur tourne, l'onglet nomme aussi les fonctions
qui ont déclenché les collectes, c'est-à-dire là où les allocations se
concentrent. L'onglet Process détaille la mémoire autant que la plateforme le
permet, voir [Plateformes](#plateformes).

![L'onglet Tasks, montrant l'arbre des tâches asyncio et ce que chaque tâche attend](https://github.com/user-attachments/assets/e8f1e9b0-2d8c-4b39-b67a-b9ba3ae2fa1d)

*L'onglet Tasks : les tâches asyncio en arbre, chacune avec les frames de coroutine où elle est arrêtée.*

![L'onglet Hotspots, listant les fonctions avec le plus d'échantillons CPU](https://github.com/user-attachments/assets/32c75ac9-e279-41e8-83a2-b7e2970275e4)

*L'onglet Hotspots : les fonctions classées par échantillons self et total de l'échantillonneur en arrière-plan.*

![L'onglet Flame, montrant un graphe en flammes des piles échantillonnées](https://github.com/user-attachments/assets/b78cfbec-3619-45a6-a2a9-5bc57f2e45f3)

*L'onglet Flame : les mêmes échantillons en graphe en flammes, un bloc par thread sur la première ligne.*

### Web

`--web` sert la même interface dans un navigateur via
[textual-serve](https://github.com/Textualize/textual-serve), une dépendance
optionnelle, installez donc `sgrud[web]`. Elle écoute sur
`http://127.0.0.1:8000` sauf indication contraire par `--host` et `--port`.
Chaque onglet du navigateur reçoit sa propre copie de l'interface attachée à la
même cible. Il n'y a aucune authentification, gardez-la donc sur localhost ou
derrière quelque chose qui en fournit une.
Sous Linux avec `run -- CMD`, la cible est
lancée de façon à ce que tout processus du même utilisateur puisse la lire,
car les sessions du navigateur ne sont pas son parent.

## Bibliothèque

La TUI n'est qu'une interface. Tout provient de `Monitor`, qui renvoie de
simples dataclasses figées :

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
    print(snap.to_dict())  # JSON friendly
```

`Monitor.stream(interval)` produit des instantanés jusqu'à la fin de la
cible, puis lève `ProcessExited`. Les pourcentages CPU nécessitent deux
instantanés, le premier renvoie donc `None`.

Pour le profilage, `Sampler` exécute `Monitor.sample_stacks()` dans un
thread en arrière-plan et alimente un agrégateur `Hotspots` :

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

## Plateformes

Les piles, les tâches asyncio, le GC et le profileur proviennent de
`_remote_debugging` et se comportent de la même façon partout. La
comptabilité des processus et des threads vient du système via psutil, et
c'est là que les plateformes diffèrent.

- **Linux** rapporte tout, y compris l'état d'ordonnancement de chaque thread,
  qui est ce qui marque un thread comme étant sur le CPU en mode wall, et la
  mémoire complète : la part anonyme et la part fichier du rss, USS et PSS, le
  tas brk et les mappages anonymes, les huge pages transparentes, le taux de
  défauts de page, la limite mémoire du cgroup et le score OOM.
- **Windows** a les noms de threads et le temps CPU par thread mais pas d'état
  d'ordonnancement, donc les threads affichent `?` au lieu de `cpu` / `idle`
  en mode wall. La mémoire comprend `rss`, `vms`, le pic du working set, les
  octets privés, USS et le taux de défauts de page.
- **macOS** ne peut pas faire correspondre les threads du système aux id de
  threads de l'interpréteur, donc les threads apparaissent sans nom ni
  chiffres CPU. La mémoire comprend `rss`, `vms`, USS et le taux de défauts de
  page. Lire la mémoire d'un autre processus demande root, lancez donc sgrud
  avec `sudo`.

## Permissions

La mémoire, le CPU et les noms de threads proviennent du système et
fonctionnent pour tout processus qui vous appartient. Tout le reste lit la
mémoire de la cible. Sur Linux cela exige des droits ptrace, et la valeur
par défaut `kernel.yama.ptrace_scope=1` ne les accorde que pour les
processus enfants.
Sans ces droits, sgrud s'attache en mode limité et affiche un bandeau
expliquant ce qui manque. Pour tout obtenir, lancez la cible via
`sgrud run -- ...`, exécutez sgrud avec `sudo`, accordez `CAP_SYS_PTRACE`,
ou assouplissez Yama pour la session :

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

Sur macOS seul root peut lire la mémoire d'un autre processus, utilisez
donc `sudo`. Sur Windows tout processus du même utilisateur fonctionne, les
autres exigent un administrateur.

Passez `require_full=True` à `Monitor.attach` pour échouer au lieu de se
dégrader. Une cible lancée avec `-X disable-remote-debug` reste inspectable,
puisque ce drapeau ne désactive que l'injection de code, que sgrud
n'utilise pas.

## Développement

```
uv sync
uv run pytest
```

Les tests lancent `tests/target_app.py` et l'inspectent, ils exercent donc
le vrai chemin d'attachement. L'application Textual est testée sans
affichage via son pilote.
