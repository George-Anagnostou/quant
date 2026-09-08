from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from quant.database import DEFAULT_DATABASE_PATH
from quant.query_cli import main as query_main


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
DEFAULT_HORIZON = None


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["query"]:
        query_main(arguments[1:])
        return
    if arguments[:2] == ["data", "status"]:
        query_main(["status", *arguments[2:]])
        return

    parser = _build_parser()
    args = parser.parse_args(arguments)
    if args.command == "data" and args.data_command == "backup":
        import json
        from quant.database import backup_database
        backup_database(args.database, args.destination)
        print(json.dumps({"backup": str(args.destination), "verified": True}))
        return
    if args.command == "serve":
        run_server(
            host=args.host,
            port=args.port,
            database=args.database,
            sync_enabled=not args.no_sync,
            horizon=args.horizon,
            batch_size=args.batch_size,
        )
        return
    parser.error("a data subcommand is required")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant",
        description="Quant web server, financial API, and API client",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser(
        "serve", help="start the webpage, API, and background data synchronization"
    )
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=_port, default=DEFAULT_PORT)
    serve.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    serve.add_argument(
        "--no-sync",
        action="store_true",
        help="disable scheduled and explicitly queued provider synchronization",
    )
    serve.add_argument(
        "--horizon",
        type=_date,
        default=DEFAULT_HORIZON,
        help="override history horizon (default: 10 years personal, 5 years universe)",
    )
    serve.add_argument(
        "--batch-size",
        type=_batch_size,
        default=50,
        help="maximum symbols in a provider request (default: 50)",
    )

    query = commands.add_parser("query", help="query the running API")
    query.add_argument("arguments", nargs=argparse.REMAINDER)

    data = commands.add_parser("data", help="inspect the server data pipeline")
    data_commands = data.add_subparsers(dest="data_command")
    status = data_commands.add_parser("status", help="inspect stored data coverage")
    status.add_argument("arguments", nargs=argparse.REMAINDER)
    backup = data_commands.add_parser("backup", help="create an exclusive verified SQLite backup")
    backup.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    backup.add_argument("--destination", type=Path, required=True)
    return parser


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _batch_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("batch size must be an integer") from error
    if not 1 <= size <= 100:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 100")
    return size


def run_server(**options) -> None:
    from quant.dashboard.server import run

    run(**options)
