"""Command line.

    python3 -m crumenae                 start the app and open the browser
    python3 -m crumenae serve --port 8765 --no-browser
    python3 -m crumenae sync [--wallet 3] [--full]
    python3 -m crumenae snapshot        live balances now (for launchd/cron at 23:59 on 31/12)
    python3 -m crumenae report 2025     write data/reports/crypto_report_TY2025/
    python3 -m crumenae export          rewrite data/exports/ (wallets, balances, transactions)
    python3 -m crumenae demo            fictional data, offline, in demo-data/
"""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .paths import ROOT_DIR


def _ctx(args, offline: bool = False):
    from .context import Context
    return Context(Path(args.data_dir) if args.data_dir else None, offline=offline)


def _print(line: str) -> None:
    print(line, flush=True)


def cmd_serve(args, ctx=None) -> None:
    from .server import serve
    ctx = ctx or _ctx(args)

    def ready(httpd) -> None:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        print(f"Crumenae {__version__} running at {url}  (data: {ctx.data_dir}; Ctrl+C to stop)", flush=True)
        if not args.no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: listening beyond this computer exposes your balances to the network.", file=sys.stderr)
    try:
        serve(ctx, host=args.host, port=args.port, ready=ready)
    except KeyboardInterrupt:
        print("\nstopped")
    except OSError as e:
        sys.exit(f"Cannot listen on port {args.port}: {e}. Try --port 8766")


def cmd_sync(args) -> None:
    from .sync import run_sync
    ctx = _ctx(args)
    run_sync(ctx, _print, args.wallet or None, args.full)


def cmd_snapshot(args) -> None:
    from .sync import take_snapshot
    ctx = _ctx(args)
    n = take_snapshot(ctx, _print, source="scheduled")
    _print(f"{n} wallet snapshot(s) stored")


def cmd_report(args) -> None:
    from .report import write_folder
    ctx = _ctx(args)
    folder = write_folder(ctx, args.year, args.portfolio)
    _print(f"Report written to {folder}")


def cmd_export(args) -> None:
    from .exports import exports_dir, write_all
    ctx = _ctx(args)
    changed = write_all(ctx)
    _print(f"{exports_dir(ctx)}: {', '.join(changed) if changed else 'already up to date'}")


def cmd_demo(args) -> None:
    from .demo import seed
    if not args.data_dir:
        args.data_dir = str(ROOT_DIR / "demo-data")
    ctx = _ctx(args, offline=True)
    seed(ctx)
    cmd_serve(args, ctx)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="crumenae", description="Local crypto ledger with EUR year-end values.")
    parser.add_argument("--data-dir", help="where the database and reports live (default: ./data)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    def add_server_opts(p):
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8765)
        p.add_argument("--no-browser", action="store_true")

    add_server_opts(sub.add_parser("serve", help="start the web app"))
    p = sub.add_parser("sync", help="sync wallets from the command line")
    p.add_argument("--wallet", type=int, action="append", help="wallet id (repeatable)")
    p.add_argument("--full", action="store_true", help="re-read the whole history")
    sub.add_parser("snapshot", help="store live balances now")
    p = sub.add_parser("report", help="write the year-end report folder")
    p.add_argument("year", type=int)
    p.add_argument("--portfolio")
    sub.add_parser("export", help="rewrite the CSV exports in data/exports/")
    add_server_opts(sub.add_parser("demo", help="explore with fictional data, offline"))

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args((["--data-dir", args.data_dir] if args.data_dir else []) + ["serve"])
    {"serve": cmd_serve, "sync": cmd_sync, "snapshot": cmd_snapshot, "report": cmd_report,
     "export": cmd_export, "demo": cmd_demo}[args.command](args)


if __name__ == "__main__":
    main()
