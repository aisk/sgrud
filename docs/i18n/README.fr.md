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
```

`--no-stacks`, `--no-tasks` et `--no-gc` retirent de l'interface ou du
dump les sections dont vous n'avez pas besoin.

### Modes d'échantillonnage

- **wall** : chaque thread possédant une pile Python compte, donc un thread
  endormi pèse autant qu'un thread occupé.
- **gil** : seul le détenteur du GIL compte. Cela répond à la question
  « où passe le CPU ».
- **async** : échantillonne les tâches asyncio au lieu des piles de threads,
  puisqu'une coroutine en attente dans un `await` ne figure sur la pile
  d'aucun thread. Chaque tâche feuille devient une pile : ses propres
  frames, un marqueur `<task NAME>`, puis les frames de chaque tâche qui
  l'attend jusqu'à la racine. Chaque tâche compte, en cours ou suspendue,
  ce qui répond à la question « qu'attendent mes tâches ». C'est plus lent
  par échantillon que la lecture d'une pile, attendez-vous donc à une
  fréquence effective plus basse.

### TUI

| Touche | Action |
| --- | --- |
| `1`-`6`, `tab`, `shift+tab` | changer d'onglet |
| `p` / `r` | pause / rafraîchir |
| `+` / `-` | modifier l'intervalle de rafraîchissement |
| `q` | quitter |
| `f` | filtre de threads (Hotspots et Flame) |
| `m` | alterner entre les modes wall/gil/async (Hotspots et Flame) |
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
    print(snap.gc[0].collections, snap.gc[0].history[:1])
    print(snap.to_dict())  # JSON friendly
```

`Monitor.stream(interval)` produit des instantanés jusqu'à la fin de la
cible, puis lève `ProcessExited`. Les pourcentages CPU nécessitent deux
instantanés, le premier renvoie donc `None`.

Pour le profilage, `Sampler` exécute `Monitor.sample_stacks()` dans un
thread en arrière-plan et alimente un agrégateur `Hotspots` :

```python
from sgrud.sampler import Sampler

with Sampler(monitor, rate=500, mode="gil") as sampler:
    time.sleep(5)
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

- **Linux** rapporte tout, y compris l'état d'ordonnancement de chaque
  thread, qui sert à marquer un thread comme sur CPU en mode wall.
- **Windows** a les noms de threads et le temps CPU par thread mais pas
  d'état d'ordonnancement, donc en mode wall les threads affichent `?` au
  lieu de `cpu` / `idle`. Le swap et la mémoire partagée ne sont pas
  rapportés.
- **macOS** ne peut pas faire correspondre les threads du système aux
  identifiants de threads de l'interpréteur, donc les threads apparaissent
  sans nom ni chiffres CPU et seuls `rss` / `vms` sont rapportés pour la
  mémoire. Lire la mémoire d'un autre processus exige root, lancez donc
  sgrud avec `sudo`.

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
