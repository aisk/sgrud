# sgrud

[English](../../README.md) | [简体中文](README.zh-CN.md) | **日本語** | [한국어](README.ko.md) | [Tiếng Việt](README.vi.md) | [Français](README.fr.md) | [Deutsch](README.de.md)

sgrud (スコットランド・ゲール語の *sgrùd* に由来し、「検査」や「調査」を意味します) は、実行中の Python プロセスを調べるための診断ツールです。CPython プロセスにアタッチして、そのメモリ、CPU、スレッド、asyncio タスク、スタック、ガベージコレクタを、対象を遅くすることなく観察できます。

sgrud は対象を停止させることも、計装することもありません。CPython 3.15 の `_remote_debugging` モジュール (Tachyon プロファイラや `python -m asyncio ps` を支える仕組み) を通じてインタープリタの状態をプロセスメモリから直接読み取り、メモリ、CPU、スレッド、開いているファイルについては psutil を通じて OS が報告する情報を組み合わせます。全スレッドのスタックのスナップショットは数十マイクロ秒で取得でき、対象側のコストはゼロです。

![Threads タブ。各スレッドの状態、CPU 使用率、現在の Python スタックを表示](https://github.com/user-attachments/assets/5a6bf1a3-fddb-48fe-9aa6-18c76f751ef7)

*Threads タブ：各スレッドの状態、CPU 占有率、ライブの Python スタック。*

## 機能

- **Process**: プラットフォームが許す限り分解したメモリ、CPU、ページフォルト、上限、cgroup のクォータとスロットル、そして子プロセスの一覧 (Python インタープリタであるものには印が付きます)。
- **Threads**: すべてのスレッドの状態、CPU 占有率、ライブの Python スタック、Linux ではブロックしているシステムコール。
- **Tasks**: asyncio のタスクツリー。各タスクが停止しているコルーチンフレーム付き。
- **GC**: 回収に費やした時間、回収レート、追跡中のオブジェクト数、回収の履歴、そして回収のきっかけになった関数。
- **Hotspots** と **Flame**: wall、GIL、CPU、exception、asyncio タスクの各モードを持つバックグラウンドサンプリングプロファイラ。表またはフレームグラフで表示します。
- **IPC**: 開いているディスクリプタ、パイプとその反対側の端を持つプロセス、ソケット、共有メモリ、ファイルロック。ハングしたプロセスのために。
- `sgrud dump` は同じ内容をテキストか JSON で出力し、`sgrud profile` は一定時間サンプリングして Tachyon の任意の形式で書き出し、`sgrud probe` はメモリだけでは分からない情報を対象に問い合わせます。`--web` は同じ画面をブラウザに表示します。
- 単純な dataclass を返す `Monitor` クラス。すべての機能をライブラリとしても利用できます。

## インストール

```
pip install sgrud
pip install "sgrud[web]"    # --web を追加
```

`uv tool install sgrud` と `pipx install sgrud` でも構いません。sgrud には Linux、macOS、Windows 上の CPython 3.15 以降が必要で、対象は sgrud 自身と同じ major.minor バージョンで動作している必要があります。

メモリ、CPU、スレッド名は自分が所有する任意のプロセスで動作します。インタープリタの状態を読み取るには、Linux では ptrace の権限、macOS では root、Windows では同じユーザーが必要です。権限がない場合、sgrud は制限モードでアタッチし、何が不足しているかを表示します。すべての機能を使う最も簡単な方法は対象を sgrud から起動することです。他の方法は [Permissions](../reference.md#permissions) を参照してください。

## 使い方

```
sgrud PID                           対話的なターミナルインターフェース
sgrud run -- python app.py          対象を子プロセスとして起動して調べる
sgrud PID --web                     同じ画面をブラウザで表示する

sgrud dump PID                      テキストのスナップショットを 1 回出力、--json で JSON
sgrud profile PID -d 30 --mode gil  30 秒間 GIL を保持するスレッドをサンプリングし、最もホットな関数を表示
sgrud profile PID -o out.html       代わりにフレームグラフを書き出す
sgrud probe PID                     対象内でスクリプトを実行: gc の閾値、アロケータ、スレッド
```

`examples/demo_app.py` はどのタブにも何かを表示させる対象プログラムです。起動して、表示された pid に sgrud を向けてください。

Python から:

```python
from sgrud import Monitor

with Monitor.attach(pid) as m:  # or Monitor.spawn(["python", "app.py"])
    snap = m.snapshot()
    print(snap.process.memory.rss, snap.process.cpu_percent)
    for t in snap.threads:
        print(t.tid, t.name, t.status.describe(), t.frames[:1])
```

[リファレンス](../reference.md) では、すべてのコマンドとオプション、サンプリングモードと出力形式、TUI のキーとタブ、ライブラリ、各プラットフォームが報告する内容、権限の取得方法を説明しています。

## 開発

```
uv sync
uv run pytest
```

テストは `tests/target_app.py` を起動して調べるため、実際のアタッチ経路を検証します。Textual アプリはその pilot を通じてヘッドレスでテストされます。
