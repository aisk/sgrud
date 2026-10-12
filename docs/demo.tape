# Records docs/demo.gif. Run from the repository root: vhs docs/demo.tape
Output docs/demo.gif

Set Shell bash
Set FontSize 14
Set Width 1400
Set Height 800
Set Padding 10
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Framerate 12

Hide
Type "export PATH=$PWD/.venv/bin:$PATH PS1='$ ' && clear"
Enter
Show

Type "sgrud run -- python examples/demo_app.py"
Sleep 500ms
Enter
Sleep 4s

# Threads: walk the table, the stack panel follows
Down@600ms 6
Sleep 1.5s

# Tasks: the asyncio task tree
Type "2"
Sleep 1.5s
Down@600ms 6
Sleep 1.5s

# GC: probe the target
Type "3"
Sleep 2s
Type "x"
Sleep 3.5s

# Process: step into a Python child and come back
Type "4"
Sleep 2.5s
Down@500ms 4
Sleep 500ms
Enter
Sleep 2.5s
Type "1"
Sleep 2.5s
Type "b"
Sleep 2s

# Hotspots: sort, sampling mode, window
Type "5"
Sleep 2.5s
Type "s"
Sleep 2s
Type "m"
Sleep 3s
Type "w"
Sleep 3s

# Flame: move around, zoom, filter by thread
Type "6"
Sleep 2.5s
Up@500ms 1
Left@30ms 11
Right@250ms 8
Up@400ms 6
Sleep 500ms
Enter
Sleep 2s
Escape
Sleep 1.5s
Type "f"
Sleep 1.5s
Down@500ms 2
Enter
Sleep 2.5s

# IPC
Type "7"
Sleep 5s

Hide
Type "q"
Sleep 2s
