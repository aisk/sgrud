# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Tiếng Việt](README.vi.md) | **Français** | [Deutsch](README.de.md)

sgrud (du gaélique écossais *sgrùd*, qui signifie « inspection » ou « examen ») est un outil de diagnostic pour inspecter des processus Python en cours d'exécution. Attachez-vous à un processus CPython et observez sa mémoire, son CPU, ses threads, ses tâches asyncio, ses piles et son ramasse-miettes sans le ralentir.

sgrud n'arrête ni n'instrumente jamais la cible. Il lit l'état de l'interpréteur directement dans la mémoire du processus grâce au module `_remote_debugging` de CPython 3.15 (la mécanique derrière le profileur Tachyon et `python -m asyncio ps`) et le combine avec ce que le système rapporte via psutil sur la mémoire, le CPU, les threads et les fichiers ouverts. Un instantané de la pile de chaque thread coûte quelques dizaines de microsecondes, et rien du côté de la cible.

![L'onglet Threads, montrant l'état, l'utilisation CPU et la pile Python courante de chaque thread](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*L'onglet Threads : chaque thread avec son état, sa part de CPU et sa pile Python en direct.*

## Fonctionnalités

- **Process** : la mémoire détaillée autant que la plateforme le permet, le CPU, les défauts de page, les limites, le quota et le bridage du cgroup, et les processus enfants avec les interpréteurs Python parmi eux marqués.
- **Threads** : chaque thread avec son état, sa part de CPU, sa pile Python en direct et, sous Linux, l'appel système dans lequel il est bloqué.
- **Tasks** : l'arbre des tâches asyncio, chaque tâche avec les frames de coroutine où elle est arrêtée.
- **GC** : le temps passé à collecter, le rythme des collectes, les objets suivis, un historique des collectes et les fonctions qui les ont déclenchées.
- **Hotspots** et **Flame** : un profileur par échantillonnage en arrière-plan avec les modes wall, GIL, CPU, exception et tâches asyncio, affiché en tableau ou en graphe en flammes.
- **IPC** : les descripteurs ouverts, les tubes et qui tient leur autre extrémité, les sockets, la mémoire partagée et les verrous de fichier, pour le processus qui se bloque.
- `sgrud dump` affiche la même chose en texte ou en JSON, `sgrud profile` échantillonne pendant un moment et écrit n'importe quel format Tachyon, et `sgrud probe` demande à la cible ce que la mémoire seule ne peut pas montrer. `--web` sert l'interface dans un navigateur.
- Une classe `Monitor` qui renvoie de simples dataclasses, de sorte que tout cela est disponible en bibliothèque.

## Installation

```
pip install sgrud
pip install "sgrud[web]"    # ajoute --web
```

`uv tool install sgrud` et `pipx install sgrud` fonctionnent aussi. sgrud nécessite CPython 3.15 ou plus récent sur Linux, macOS ou Windows, et la cible doit utiliser la même version majeure.mineure que sgrud lui-même.

La mémoire, le CPU et les noms de threads fonctionnent pour tout processus qui vous appartient. Lire l'état de l'interpréteur exige des droits ptrace sur Linux, root sur macOS et le même utilisateur sur Windows. Sans ces droits, sgrud s'attache en mode limité et indique ce qui manque. Le moyen le plus simple de tout obtenir est de lancer la cible via sgrud, voir [Permissions](../reference.md#permissions) pour les autres façons.

## Utilisation

```
sgrud PID                           interface interactive dans le terminal
sgrud run -- python app.py          lance la cible comme processus enfant et l'inspecte
sgrud PID --web                     la même interface servie dans un navigateur

sgrud dump PID                      un instantané texte, --json pour du JSON
sgrud profile PID -d 30 --mode gil  échantillonne le détenteur du GIL pendant 30 s, affiche les fonctions les plus chaudes
sgrud profile PID -o out.html       écrit un flame graph à la place
sgrud probe PID                     exécute un script dans la cible : seuils du gc, allocateur, threads
```

`examples/demo_app.py` met quelque chose sur chaque onglet. Lancez-le et pointez sgrud sur le pid qu'il affiche.

Depuis Python :

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.frames[:1])
```

La [référence](../reference.md) couvre chaque commande et option, les modes d'échantillonnage et les formats de sortie, les touches et onglets de la TUI, la bibliothèque, ce que chaque plateforme rapporte et comment obtenir les permissions.

## Développement

```
uv sync
uv run pytest
```

Les tests lancent `tests/target_app.py` et l'inspectent, ils exercent donc le vrai chemin d'attachement. L'application Textual est testée sans affichage via son pilote.
