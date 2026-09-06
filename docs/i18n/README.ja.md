# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | **日本語** | [한국어](README.ko.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

sgrud (スコットランド・ゲール語の *sgrùd* に由来し、「検査」や「調査」を意味します)
は、実行中の Python プロセスを調べるための診断ツールです。CPython プロセスに
アタッチして、そのメモリ、CPU、スレッド、asyncio タスク、スタック、ガベージ
コレクタを、対象を遅くすることなく観察できます。

sgrud は対象を停止させることも、計装することもありません。CPython 3.15 の
`_remote_debugging` モジュール (Tachyon プロファイラや `python -m asyncio ps`
を支える仕組み) を通じてインタープリタの状態をプロセスメモリから直接読み取り、
メモリと CPU の計測には OS が報告する情報を組み合わせます。全スレッドの
スタックのスナップショットは数十マイクロ秒で取得でき、対象側のコストはゼロです。

CPython 3.15 以降が必要で、Linux、macOS、Windows で動作します。対象は sgrud
自身と同じ major.minor バージョンで動作している必要があります。Linux では
すべての情報が得られます。他のプラットフォームとの違いは
[プラットフォーム](#プラットフォーム) を参照してください。

## 使い方

```
sgrud PID                           対話的なターミナルインターフェース
sgrud run -- python app.py          対象を子プロセスとして起動して調べる
sgrud PID --web                     同じ画面をブラウザで表示する

sgrud dump PID                      テキストのスナップショットを 1 回出力
sgrud dump PID -n 0.5               対象が終了するまで 0.5 秒ごとに出力し続ける
sgrud dump PID --json               1 行につき 1 つの JSON オブジェクト
sgrud dump run -- python app.py     `run -- CMD` はどこでも pid の代わりに使える

sgrud profile PID                   5 秒間スタックをサンプリングし、最もホットな関数を表示
sgrud profile PID -d 30 --mode gil  30 秒間サンプリングし、GIL を保持するスレッドだけを数える
sgrud profile PID --mode async      スレッドの代わりに asyncio タスクをサンプリング
sgrud profile PID --folded          flamegraph.pl や speedscope 向けの折りたたみスタック
```

`--no-stacks`、`--no-tasks`、`--no-gc` を指定すると、不要なセクションを
インターフェースやダンプから省けます。

### サンプリングモード

- **wall**: Python スタックを持つすべてのスレッドを数えるため、スリープ中の
  スレッドも忙しいスレッドと同じ重みになります。
- **gil**: GIL を保持しているスレッドだけを数えます。「CPU はどこで使われて
  いるのか」に答えるモードです。
- **async**: スレッドスタックの代わりに asyncio タスクをサンプリングします。
  `await` で待機中のコルーチンはどのスレッドのスタックにも存在しないためです。
  各リーフタスクが 1 つのスタックになります。自身のフレーム、`<task NAME>`
  マーカー、そしてルートまでそのタスクを await している各タスクのフレームが
  続きます。実行中か中断中かにかかわらずすべてのタスクを数えるため、「タスクは
  何を待っているのか」に答えるモードです。スタックの読み取りより 1 サンプル
  あたりの処理が遅いため、実際のレートは低くなります。

### TUI

| キー | 動作 |
| --- | --- |
| `1`-`6`、`tab`、`shift+tab` | タブの切り替え |
| `p` / `r` | 一時停止 / 更新 |
| `+` / `-` | 更新間隔の変更 |
| `q` | 終了 |
| `f` | スレッドフィルタ (Hotspots と Flame) |
| `m` | wall/gil/async モードの切り替え (Hotspots と Flame) |
| `c` | サンプルのクリア (Hotspots と Flame) |
| `s` | self/total の並び順の切り替え (Hotspots) |
| `enter` / `backspace` / `esc` | ズームイン / ズームアウト / リセット (Flame) |

矢印キーは現在のタブの内容をすぐに移動します。Hotspots と Flame は 1 つの
バックグラウンドサンプラー (`--rate`、デフォルト 100 Hz) を共有しており、
他のタブを見ている間も動作し続けます。フレームグラフは下から上に伸び、最初の
行で各スレッドに独自のブロックを割り当てるため、アイドル状態のスレッドは他の
スレッドに混ざることなく、高い柱として表示されます。

### Web

`--web` を付けると、同じ画面を [textual-serve](https://github.com/Textualize/textual-serve)
経由でブラウザに表示します。これはオプション依存なので `sgrud[web]` をインストール
してください。既定では `http://127.0.0.1:8000` で待ち受け、`--host` と `--port`
で変更できます。ブラウザのタブごとに独立した画面が同じ対象プロセスに接続します。
認証はないので、localhost に限定するか、認証を提供するものの背後に置いてください。
Linux で `run -- CMD` を使うと、ブラウザセッションは対象の親ではないため、
対象は同じユーザーの任意のプロセスから読めるように起動されます。

## ライブラリ

TUI はフロントエンドにすぎません。すべては `Monitor` から得られ、
単純な frozen dataclass を返します。

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

`Monitor.stream(interval)` は対象が終了するまでスナップショットを yield し、
その後 `ProcessExited` を送出します。CPU 使用率の計算には 2 つの
スナップショットが必要なため、最初のスナップショットでは `None` になります。

プロファイリングには `Sampler` を使います。バックグラウンドスレッドで
`Monitor.sample_stacks()` を実行し、`Hotspots` アグリゲータに結果を渡します。

```python
from sgrud.sampler import Sampler

with Sampler(monitor, rate=500, mode="gil") as sampler:
    time.sleep(5)
for row in sampler.hotspots.rows(sort="self", limit=10):
    print(row.self_percent, row.funcname, row.filename)
tree = sampler.hotspots.call_tree()  # merged call tree, one child per thread
print("\n".join(sampler.hotspots.folded()))  # flamegraph.pl input
```

## プラットフォーム

スタック、asyncio タスク、GC、プロファイラは `_remote_debugging` から取得
するため、どこでも同じように動作します。プロセスとスレッドの計測は psutil を
通じて OS から取得しており、プラットフォームごとの違いはここにあります。

- **Linux** はスレッドごとのスケジューラ状態を含むすべてを報告します。wall
  モードでスレッドが CPU 上にあるかどうかを示すのはこの状態です。
- **Windows** にはスレッド名とスレッドごとの CPU 時間がありますが、スケジューラ
  状態はありません。そのため wall モードではスレッドが `cpu` / `idle` ではなく
  `?` と表示されます。swap と共有メモリは報告されません。
- **macOS** では OS のスレッドをインタープリタのスレッド ID と対応付けられない
  ため、スレッドは名前と CPU の数値なしで表示され、メモリは `rss` / `vms` のみ
  です。他のプロセスのメモリを読むには root が必要なので、sgrud は `sudo` で
  実行してください。

## 権限

メモリ、CPU、スレッド名は OS から取得するため、自分が所有する任意の
プロセスで動作します。それ以外はすべて対象のメモリを読み取ります。Linux では
ptrace の権限が必要ですが、デフォルトの `kernel.yama.ptrace_scope=1` では
子プロセスに対してしか許可されません。権限がない場合、sgrud は制限モードでアタッチし、
何が不足しているかを説明するバナーを表示します。すべての機能を使うには、
対象を `sgrud run -- ...` で起動する、sgrud を `sudo` で実行する、
`CAP_SYS_PTRACE` を付与する、またはセッション中だけ Yama を緩和してください。

```
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

macOS では root だけが他のプロセスのメモリを読めるため、`sudo` を使って
ください。Windows では同じユーザーのプロセスならそのまま動作し、他のユーザーの
プロセスには管理者権限が必要です。

`Monitor.attach` に `require_full=True` を渡すと、機能を落とす代わりに
失敗するようになります。`-X disable-remote-debug` で起動した対象も調べられます。
このフラグはコードインジェクションを無効にするだけであり、sgrud はそれを
使わないためです。

## 開発

```
uv sync
uv run pytest
```

テストは `tests/target_app.py` を起動して調べるため、実際のアタッチ経路を
検証します。Textual アプリはその pilot を通じてヘッドレスでテストされます。
