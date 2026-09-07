"""A program with something on every sgrud tab.

Run it, then point sgrud at the pid it prints from another terminal:

    python examples/demo_app.py
    sgrud PID

What it starts, and where sgrud shows it:

- Threads. ``sim-0`` and ``sim-1`` run a pure Python particle simulation and
  fight each other for the GIL. ``compress`` runs zlib, which releases the
  GIL, so it counts in cpu sampling mode but not in gil mode. ``lock-waiter``
  sits on a ``threading.Lock`` nobody releases, ``pipe-reader`` in ``read()``
  on a pipe nobody writes to, ``socket-reader`` in ``recv()`` on a connection
  to the echo server, ``file-locker`` in ``flock()`` on a file a child
  process holds. ``handler`` sleeps inside an ``except`` block for exception
  mode. ``churn`` builds reference cycles so the collector has work.
- Tasks. An asyncio echo server with chatty and quiet clients, a supervisor
  over a producer and consumers on an ``asyncio.Queue``, and tasks waiting
  for an ``asyncio.Lock`` and an ``asyncio.Event`` that never come.
- Process. ``multiprocessing`` workers on a ``Queue``, the child holding the
  file lock, and a child that is not Python at all.
- IPC. The pipe, the sockets, the shared memory segment the workers update,
  the semaphores behind the ``Queue``, and the file lock with who holds it.
- GC and Hotspots and Flame. The churn thread and the simulation.

Everything runs until Ctrl-C, or for ``--seconds N``.
"""

from __future__ import annotations

import asyncio
import collections
import multiprocessing
import os
import random
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from multiprocessing import shared_memory

FOREVER = 3600.0


# --- CPU work for the Hotspots and Flame tabs ------------------------------


class Particle:
    __slots__ = ("x", "y", "vx", "vy")

    def __init__(self, rng: random.Random) -> None:
        self.x = rng.random()
        self.y = rng.random()
        self.vx = rng.uniform(-0.1, 0.1)
        self.vy = rng.uniform(-0.1, 0.1)


def integrate(particles: list[Particle], dt: float) -> None:
    for p in particles:
        p.x += p.vx * dt
        p.y += p.vy * dt
        if not 0.0 <= p.x <= 1.0:
            p.vx = -p.vx
        if not 0.0 <= p.y <= 1.0:
            p.vy = -p.vy


def collide(particles: list[Particle]) -> int:
    hits = 0
    for i, a in enumerate(particles):
        for b in particles[i + 1 :]:
            dx = a.x - b.x
            dy = a.y - b.y
            if dx * dx + dy * dy < 1e-4:
                a.vx, b.vx = b.vx, a.vx
                a.vy, b.vy = b.vy, a.vy
                hits += 1
    return hits


def energy(particles: list[Particle]) -> float:
    return sum(p.vx * p.vx + p.vy * p.vy for p in particles)


def step(particles: list[Particle], dt: float) -> float:
    integrate(particles, dt)
    collide(particles)
    return energy(particles)


def simulate(seed: int) -> None:
    rng = random.Random(seed)
    particles = [Particle(rng) for _ in range(60)]
    while True:
        for _ in range(20):
            step(particles, 0.01)
        time.sleep(0.005)


def compress_loop() -> None:
    # zlib drops the GIL while it works, so this thread is on the CPU
    # without holding the GIL, which is what cpu mode counts and gil
    # mode does not.
    data = random.randbytes(1 << 20)
    while True:
        zlib.compress(data, 9)
        time.sleep(0.2)


# --- Threads that block for the Threads and IPC tabs -----------------------


def wait_for_lock(lock: threading.Lock) -> None:
    # A futex wait, and the Python stack next to it says it is this lock.
    with lock:
        pass


def read_pipe(fd: int) -> None:
    os.read(fd, 1)


def read_socket(sock: socket.socket) -> None:
    sock.recv(1024)


def lock_file(path: str) -> None:
    # Blocks on flock(2) because a child process already holds the lock,
    # and the IPC tab names that child as the holder.
    import fcntl

    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)


def handle_forever() -> None:
    try:
        raise ValueError("handled forever")
    except ValueError:
        while True:
            time.sleep(0.5)


# --- Allocation churn for the GC tab -----------------------------------------


class Node:
    def __init__(self, value: int) -> None:
        self.value = value
        self.other: Node | None = None


def churn() -> None:
    recent: collections.deque[list[Node]] = collections.deque(maxlen=50)
    while True:
        nodes = [Node(i) for i in range(400)]
        for a, b in zip(nodes, nodes[1:], strict=False):
            a.other, b.other = b, a  # cycles, so only the collector frees them
        recent.append(nodes)
        time.sleep(0.05)


# --- Child processes for the Process tab ------------------------------------


def exit_with_parent() -> None:
    # The demo may be killed outright, by `sgrud run` for one, so every
    # child watches for that itself instead of relying on a clean shutdown.
    parent = multiprocessing.parent_process()
    if parent is not None:
        parent.join()
        os._exit(0)


def worker(queue: multiprocessing.Queue, segment_name: str, slot: int) -> None:
    # Blocks in Queue.get(), which is a semaphore and a pipe shared with the
    # parent. Each item it takes bumps its counter in the shared segment.
    threading.Thread(target=exit_with_parent, daemon=True).start()
    segment = shared_memory.SharedMemory(segment_name)
    buf = segment.buf
    assert buf is not None
    try:
        while True:
            queue.get()
            count = struct.unpack_from("Q", buf, slot * 8)[0]
            struct.pack_into("Q", buf, slot * 8, count + 1)
    finally:
        segment.close()


# Holds the file lock, then reads stdin, which hits EOF when the demo dies.
LOCK_HOLDER = """
import fcntl, os, sys
f = open(sys.argv[1], "w")
fcntl.flock(f, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
try:
    os.unlink(sys.argv[1])
except OSError:
    pass
"""


def start_lock_holder(path: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"held"
    return proc


def start_non_python() -> subprocess.Popen[bytes]:
    # Reads stdin, so it exits on its own when the demo dies.
    if sys.platform == "win32":
        return subprocess.Popen(["more"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    return subprocess.Popen(["cat"], stdin=subprocess.PIPE)


# --- asyncio for the Tasks tab ----------------------------------------------


async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def chatty_client(port: int, n: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    while True:
        writer.write(b"ping %d\n" % n)
        await writer.drain()
        await reader.readline()
        await asyncio.sleep(0.5 + n * 0.3)


async def quiet_client(port: int) -> None:
    # Connects and then waits for a reply that never comes.
    reader, _writer = await asyncio.open_connection("127.0.0.1", port)
    await reader.read(1024)


async def produce(queue: asyncio.Queue[int]) -> None:
    n = 0
    while True:
        await queue.put(n)
        n += 1
        await asyncio.sleep(0.4)


async def consume(queue: asyncio.Queue[int], n: int) -> None:
    while True:
        item = await queue.get()
        await asyncio.sleep(0.1 * (item % 3 + n))


async def supervise() -> None:
    queue: asyncio.Queue[int] = asyncio.Queue()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(produce(queue), name="producer")
        for n in range(3):
            tg.create_task(consume(queue, n), name=f"consumer-{n}")


async def hold_lock(lock: asyncio.Lock) -> None:
    async with lock:
        await asyncio.sleep(FOREVER)


async def wait_lock(lock: asyncio.Lock) -> None:
    async with lock:
        pass


async def wait_event(event: asyncio.Event) -> None:
    await event.wait()


async def feed_workers(queue: multiprocessing.Queue) -> None:
    n = 0
    while True:
        queue.put(n)
        n += 1
        await asyncio.sleep(1)


async def serve(seconds: float | None, mp_queue: multiprocessing.Queue) -> None:
    # The server accepts connections from here on, no serve_forever() task
    # needed. That task's cancellation would wait for every connection to
    # close, and the thread parked in recv() never closes its own.
    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    # A plain socket for a thread to block on, connected to the same server.
    plain = socket.create_connection(("127.0.0.1", port))
    threading.Thread(target=read_socket, args=(plain,), name="socket-reader", daemon=True).start()

    lock = asyncio.Lock()
    event = asyncio.Event()
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(supervise(), name="supervisor"),
                tg.create_task(hold_lock(lock), name="lock-holder"),
                tg.create_task(wait_lock(lock), name="lock-waiter"),
                tg.create_task(wait_event(event), name="event-waiter"),
                tg.create_task(feed_workers(mp_queue), name="feeder"),
            ]
            for n in range(2):
                tasks.append(tg.create_task(chatty_client(port, n), name=f"chatty-{n}"))
                tasks.append(tg.create_task(quiet_client(port), name=f"quiet-{n}"))
            await asyncio.sleep(seconds if seconds is not None else FOREVER * 24 * 365)
            for task in tasks:
                task.cancel()
    finally:
        server.close()


# --- Wiring ------------------------------------------------------------------


def main(seconds: float | None) -> None:
    lock = threading.Lock()
    lock.acquire()  # held for the rest of the run, so lock-waiter waits
    pipe_r, pipe_w = os.pipe()

    segment = shared_memory.SharedMemory(create=True, size=64)
    mp_queue: multiprocessing.Queue = multiprocessing.Queue()
    workers = [
        multiprocessing.Process(
            target=worker, args=(mp_queue, segment.name, n), name=f"worker-{n}", daemon=True
        )
        for n in range(2)
    ]
    for w in workers:
        w.start()

    threads = {
        "sim-0": (simulate, (0,)),
        "sim-1": (simulate, (1,)),
        "compress": (compress_loop, ()),
        "lock-waiter": (wait_for_lock, (lock,)),
        "pipe-reader": (read_pipe, (pipe_r,)),
        "handler": (handle_forever, ()),
        "churn": (churn, ()),
    }
    children: list[subprocess.Popen[bytes]] = [start_non_python()]
    lock_path = os.path.join(tempfile.gettempdir(), f"sgrud-demo-{os.getpid()}.lock")
    if sys.platform != "win32":
        children.append(start_lock_holder(lock_path))
        threads["file-locker"] = (lock_file, (lock_path,))
    for name, (target, args) in threads.items():
        threading.Thread(target=target, args=args, name=name, daemon=True).start()

    pid = os.getpid()
    print(f"sgrud demo running as pid {pid}", flush=True)
    print(
        f"\nIn another terminal:\n\n    sgrud {pid}\n    sgrud dump {pid}\n"
        f"    sgrud profile {pid} --mode cpu\n\nCtrl-C stops it.\n",
        flush=True,
    )
    try:
        asyncio.run(serve(seconds, mp_queue))
    except KeyboardInterrupt:
        pass
    finally:
        for proc in children:
            proc.kill()
        for w in workers:
            w.terminate()
        segment.close()
        segment.unlink()
        os.close(pipe_w)
        if os.path.exists(lock_path):
            os.unlink(lock_path)


if __name__ == "__main__":
    # The guard matters: multiprocessing imports this file again in the
    # workers, and without it they would start the whole demo over.
    seconds = None
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal.default_int_handler)  # `kill PID` cleans up too
    main(seconds)
