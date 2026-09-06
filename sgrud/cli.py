"""Command line interface.

sgrud PID                 one text snapshot
sgrud PID -n 0.5          keep printing snapshots every 0.5 s
sgrud PID --json          JSON lines instead of text
sgrud PID --profile 5     sample stacks for 5 s and print the hottest functions
sgrud run -- python app.py    spawn the target as a child, then inspect it
sgrud tui PID             interactive Textual interface
sgrud tui run -- python app.py
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections.abc import Sequence

from . import __name__ as _pkg
from .errors import SgrudError
from .format import format_snapshot
from .monitor import Monitor


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        help="a pid, or `run -- CMD [ARGS...]` to spawn the target as a child",
    )


def _add_sections(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-stacks", action="store_true", help="skip stack traces")
    parser.add_argument("--no-tasks", action="store_true", help="skip asyncio tasks")
    parser.add_argument("--no-gc", action="store_true", help="skip GC statistics")
    parser.add_argument("--no-native", action="store_true", help="hide <native> marker frames")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=_pkg, description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")

    dump = sub.add_parser("dump", help="print snapshots (default command)")
    _add_target(dump)
    _add_sections(dump)
    dump.add_argument(
        "-n",
        "--interval",
        type=float,
        default=None,
        help="repeat every N seconds until the target exits",
    )
    dump.add_argument("-c", "--count", type=int, default=None, help="stop after N snapshots")
    dump.add_argument("--json", action="store_true", help="emit one JSON object per line")
    dump.add_argument("--max-frames", type=int, default=None, help="frames per thread to show")
    dump.add_argument(
        "--profile",
        type=float,
        metavar="SECONDS",
        default=None,
        help="sample stacks for SECONDS and print a hotspot table instead",
    )
    dump.add_argument("--rate", type=float, default=200.0, help="samples per second for --profile")
    dump.add_argument(
        "--sort", choices=("self", "total"), default="self", help="hotspot ordering for --profile"
    )
    dump.add_argument(
        "--mode",
        choices=("wall", "gil"),
        default="wall",
        help="count every thread (wall) or only the GIL holder (gil)",
    )
    dump.add_argument(
        "--folded",
        action="store_true",
        help="with --profile, print collapsed stacks for flamegraph.pl or speedscope",
    )

    tui = sub.add_parser("tui", help="interactive terminal interface")
    _add_target(tui)
    _add_sections(tui)
    tui.add_argument("-n", "--interval", type=float, default=1.0, help="refresh interval")
    tui.add_argument(
        "--rate",
        type=float,
        default=100.0,
        help="background stack samples per second for the Hotspots tab, 0 to disable",
    )
    tui.add_argument(
        "--mode",
        choices=("wall", "gil"),
        default="wall",
        help="initial hotspot mode, toggle with `m` in the TUI",
    )
    return parser


def open_monitor(target: str, command: Sequence[str], **options) -> Monitor:
    if target == "run":
        if not command:
            raise SgrudError("`run` needs a command after `--`")
        return Monitor.spawn(list(command), **options)
    if command:
        raise SgrudError("a command after `--` only makes sense with `run`")
    if not target.isdigit():
        raise SgrudError(f"expected a pid or `run -- CMD`, got {target!r}")
    return Monitor.attach(int(target), **options)


def _dump(args: argparse.Namespace) -> int:
    sections = dict(stacks=not args.no_stacks, tasks=not args.no_tasks, gc=not args.no_gc)
    try:
        monitor = open_monitor(args.target, args.command_argv, native_frames=not args.no_native)
    except SgrudError as e:
        print(f"sgrud: {e}", file=sys.stderr)
        return 1
    produced = 0
    if monitor.limited is not None:
        print(
            f"sgrud: limited mode, only /proc data is available. {monitor.limited}", file=sys.stderr
        )
    try:
        with monitor:
            if args.profile is not None:
                if monitor.limited is not None:
                    print("sgrud: --profile needs access to the target's memory", file=sys.stderr)
                    return 1
                return _profile(monitor, args)
            if args.interval is None:
                # A second sample a moment later gives meaningful CPU percentages.
                monitor.snapshot(stacks=False, tasks=False, gc=False)
                time.sleep(0.1)
                snaps = iter([monitor.snapshot(**sections)])
            else:
                snaps = monitor.stream(args.interval, **sections)
            for snap in snaps:
                if args.json:
                    print(json.dumps(snap.to_dict(), default=str), flush=True)
                else:
                    if produced:
                        print()
                    print(
                        format_snapshot(
                            snap,
                            frames=sections["stacks"],
                            tasks=sections["tasks"],
                            gc=sections["gc"],
                            max_frames=args.max_frames,
                        ),
                        flush=True,
                    )
                produced += 1
                if args.count is not None and produced >= args.count:
                    break
    except SgrudError as e:
        print(f"sgrud: {e}", file=sys.stderr)
        return 1 if not produced else 0
    except KeyboardInterrupt:
        pass
    return 0


def _profile(monitor: Monitor, args: argparse.Namespace) -> int:
    from .format import format_hotspots
    from .sampler import Sampler

    sampler = Sampler(monitor, rate=args.rate, mode=args.mode)
    with sampler:
        deadline = time.monotonic() + args.profile
        while time.monotonic() < deadline and sampler.exited is None:
            time.sleep(0.05)
    hot = sampler.hotspots
    if args.folded:
        try:
            names = {t.tid: t.name for t in monitor.snapshot(tasks=False, gc=False).threads}
        except SgrudError:
            names = {}
        for line in hot.folded(names=names):
            print(line)
    elif args.json:
        rows = [dataclasses.asdict(r) for r in hot.rows(sort=args.sort)]
        print(
            json.dumps(
                {
                    "samples": hot.samples,
                    "rate": hot.rate(),
                    "mode": hot.mode,
                    "errors": sampler.errors,
                    "rows": rows,
                }
            )
        )
    else:
        print(
            format_hotspots(
                hot.rows(sort=args.sort), samples=hot.samples, rate=hot.rate(), mode=hot.mode
            )
        )
        if sampler.errors:
            print(f"({sampler.errors} samples failed, last: {sampler.last_error})")
    if sampler.exited is not None:
        print(f"sgrud: {sampler.exited}", file=sys.stderr)
    return 0


def _tui(args: argparse.Namespace) -> int:
    from .tui import run_tui

    try:
        monitor = open_monitor(args.target, args.command_argv, native_frames=not args.no_native)
    except SgrudError as e:
        print(f"sgrud: {e}", file=sys.stderr)
        return 1
    return run_tui(
        monitor,
        interval=args.interval,
        stacks=not args.no_stacks,
        tasks=not args.no_tasks,
        gc=not args.no_gc,
        sample_rate=args.rate,
        sample_mode=args.mode,
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command_argv: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, command_argv = argv[:cut], argv[cut + 1 :]
    if argv and argv[0] not in {"dump", "tui", "-h", "--help"}:
        argv.insert(0, "dump")
    args = build_parser().parse_args(argv)
    args.command_argv = command_argv
    if args.command == "tui":
        return _tui(args)
    if args.command == "dump":
        return _dump(args)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
