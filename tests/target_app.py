"""A small program with threads and asyncio tasks for sgrud to inspect.

Prints ``READY`` on stdout once everything is running.
"""

import asyncio
import sys
import threading
import time


def busy_loop():
    x = 0
    while True:
        for i in range(50_000):
            x += i * i
        time.sleep(0.0005)


def idle_loop():
    while True:
        time.sleep(0.2)


def except_loop():
    # Sleeps inside an exception handler, so exception mode sampling has
    # something to find.
    try:
        raise ValueError("handled forever")
    except ValueError:
        while True:
            time.sleep(0.2)


async def leaf(n):
    await asyncio.sleep(3600)


async def branch(n):
    await asyncio.gather(*(leaf(i) for i in range(2)))


async def main():
    # Kept referenced so the tasks are not garbage collected mid-run.
    tasks = [asyncio.create_task(branch(i), name=f"branch-{i}") for i in range(3)]  # noqa: F841
    threading.Thread(target=busy_loop, name="busy", daemon=True).start()
    threading.Thread(target=idle_loop, name="idle", daemon=True).start()
    threading.Thread(target=except_loop, name="except", daemon=True).start()
    await asyncio.sleep(0.05)
    print("READY", flush=True)
    while True:
        await asyncio.sleep(0.5)
        list(range(10_000))  # churn some objects so gen0 collections happen


if __name__ == "__main__":
    if "--exit-after" in sys.argv:
        time.sleep(float(sys.argv[sys.argv.index("--exit-after") + 1]))
        sys.exit(3)
    if "--children" in sys.argv:
        # One Python grandchild through a Python child, and one non-Python
        # child, so child discovery has a tree to find.
        import subprocess

        napper = "import time; time.sleep(300)"
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {napper!r}]); {napper}",
            ]
        )
        if sys.platform == "win32":
            subprocess.Popen(["ping", "-n", "300", "127.0.0.1"], stdout=subprocess.DEVNULL)
        else:
            subprocess.Popen(["sleep", "300"])
    asyncio.run(main())
