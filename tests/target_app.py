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


async def leaf(n):
    await asyncio.sleep(3600)


async def branch(n):
    await asyncio.gather(*(leaf(i) for i in range(2)))


async def main():
    # Kept referenced so the tasks are not garbage collected mid-run.
    tasks = [asyncio.create_task(branch(i), name=f"branch-{i}") for i in range(3)]  # noqa: F841
    threading.Thread(target=busy_loop, name="busy", daemon=True).start()
    threading.Thread(target=idle_loop, name="idle", daemon=True).start()
    await asyncio.sleep(0.05)
    print("READY", flush=True)
    while True:
        await asyncio.sleep(0.5)
        list(range(10_000))  # churn some objects so gen0 collections happen


if __name__ == "__main__":
    if "--exit-after" in sys.argv:
        time.sleep(float(sys.argv[sys.argv.index("--exit-after") + 1]))
        sys.exit(3)
    asyncio.run(main())
