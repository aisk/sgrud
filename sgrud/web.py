"""Serve the TUI to a browser with textual-serve.

textual-serve starts a fresh ``sgrud PID`` per browser connection, so
the terminal interface is reused as is. The server only decides which
pid that command attaches to and where the page is reachable.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

import aiohttp_jinja2
from aiohttp import web
from textual_serve.server import Server

from . import __name__ as _pkg
from .monitor import Monitor

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})

# Yama's default ptrace_scope only lets a process read the memory of its
# descendants. The browser sessions are siblings of a target started with
# ``run``, so the target opts in with PR_SET_PTRACER_ANY before exec'ing
# the real command. The flag survives exec and stays limited to this uid.
_ALLOW_PTRACE = (
    "import ctypes,os,sys;"
    "ctypes.CDLL(None).prctl(0x59616D61,ctypes.c_ulong(-1),0,0,0);"
    "os.execvp(sys.argv[1],sys.argv[1:])"
)


def allow_ptrace(argv: Sequence[str]) -> list[str]:
    """Wrap a ``run -- CMD`` command so sibling processes may inspect it.

    Only Linux has Yama, elsewhere the command is returned unchanged.
    """
    if sys.platform != "linux":
        return list(argv)
    return [sys.executable, "-S", "-c", _ALLOW_PTRACE, *argv]


def web_command(args: argparse.Namespace, pid: int) -> list[str]:
    """The ``sgrud PID`` invocation that serves one browser session.

    It attaches to ``pid`` with the terminal options carried over and
    never spawns anything itself.
    """
    argv = [sys.executable, "-m", _pkg, str(pid), "-n", str(args.interval)]
    argv += ["--rate", str(args.rate), "--mode", args.mode]
    for flag in ("stacks", "tasks", "gc", "children", "ipc", "native"):
        if getattr(args, f"no_{flag}"):
            argv.append(f"--no-{flag}")
    return argv


def _shell_join(argv: Sequence[str]) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


# The template decorator keeps the undecorated coroutine, which builds the
# page context from ``self.public_url``.
_index_context = Server.handle_index.__wrapped__  # ty: ignore[unresolved-attribute]


class _Server(Server):
    """textual-serve's server with page URLs taken from each request.

    The stock server bakes ``http://HOST:PORT`` into the page, which the
    browser cannot reach when HOST is a wildcard such as 0.0.0.0. Using
    the Host header instead makes the page work from whatever address
    the browser actually used, including behind a reverse proxy.
    """

    @aiohttp_jinja2.template("app_index.html")
    async def handle_index(self, request: web.Request) -> dict[str, Any]:
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        self.public_url = f"{scheme}://{request.host}"
        return await _index_context(self, request)


def serve(monitor: Monitor, args: argparse.Namespace) -> int:
    """Serve the TUI over HTTP until interrupted.

    ``monitor`` stays open for the whole run. With ``run -- CMD`` it owns
    the child, so every browser tab attaches to that one process instead
    of spawning another, and the child goes away when the server does.
    """
    if args.host not in LOOPBACK_HOSTS:
        print(
            f"sgrud: serving on {args.host} without authentication, anyone who can reach "
            "the port can inspect the target",
            file=sys.stderr,
        )
    shown = "localhost" if args.host in WILDCARD_HOSTS else args.host
    server = _Server(
        _shell_join(web_command(args, monitor.pid)),
        host=args.host,
        port=args.port,
        title=f"sgrud {monitor.pid}",
        public_url=f"http://{shown}:{args.port}",
    )
    try:
        with monitor:
            server.serve()
    except KeyboardInterrupt:
        pass
    return 0
