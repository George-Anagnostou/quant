from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import date
from urllib.parse import quote

from quant.api_client import (
    ApiClient,
    ApiHttpError,
    ApiProtocolError,
    ApiTimeoutError,
    ApiTransportError,
    ClientConfig,
)


DEFAULT_BASE_URL = "http://127.0.0.1:8001/api/alpha"
PERIODS = ("1mo", "3mo", "6mo", "1y", "2y", "5y")
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9.^=_-]{0,31}$")


class CliUsageError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise CliUsageError(message)


def main(argv: Sequence[str] | None = None) -> None:
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        _validate_args(args)
        config = ClientConfig(args.base_url, args.timeout)
        result = args.handler(ApiClient(config), args)
        _write_json(result, pretty=args.pretty, stream=sys.stdout)
    except CliUsageError as error:
        _exit_error(2, "invalid_usage", str(error))
    except ValueError as error:
        _exit_error(2, "invalid_configuration", str(error))
    except ApiTimeoutError as error:
        _exit_error(4, "timeout", str(error))
    except ApiTransportError as error:
        _exit_error(3, "transport_error", str(error))
    except ApiHttpError as error:
        _exit_error(
            5 if error.status < 500 else 6,
            "api_error",
            str(error),
            status=error.status,
            details=error.detail,
        )
    except ApiProtocolError as error:
        _exit_error(7, "protocol_error", str(error))
    except KeyboardInterrupt:
        _exit_error(130, "interrupted", "Request interrupted")
    except BrokenPipeError:
        # A downstream consumer (for example head) closed stdout normally.
        # Redirect the descriptor too, so interpreter shutdown cannot reflush it.
        try:
            with open(os.devnull, "w") as sink:
                os.dup2(sink.fileno(), sys.stdout.fileno())
        except (AttributeError, OSError, ValueError):
            pass
        raise SystemExit(0)


def _build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="quant query",
        description="Machine-readable client for the Quant alpha API",
    )
    _add_global_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, **kwargs):
        child = commands.add_parser(name, **kwargs)
        _add_global_options(child, suppress_defaults=True)
        return child

    health = command("health", help="check API liveness")
    health.set_defaults(handler=lambda client, _: client.request("GET", "health"))

    status = command("status", help="inspect stored market-data coverage")
    status.add_argument("symbols", nargs="*", metavar="SYMBOL", type=_symbol)
    status.set_defaults(handler=_status)

    search = command("search", help="search stored securities")
    search.add_argument("query", type=_search_query)
    search.add_argument("--limit", type=_bounded_int(1, 50, "limit"), default=10)
    search.set_defaults(handler=_search)

    quotes = command("quotes", help="read latest stored quotes")
    quotes.add_argument("symbols", nargs="+", metavar="SYMBOL", type=_symbol)
    quotes.set_defaults(handler=_quotes)

    bars = command("bars", help="read stored daily price bars")
    bars.add_argument("symbol", type=_symbol)
    bars.add_argument("--start", type=_date)
    bars.add_argument("--end", type=_date)
    bars.add_argument("--limit", type=_bounded_int(1, 5_000, "limit"), default=500)
    bars.set_defaults(handler=_bars)

    technical = command("technical", help="calculate stored technicals")
    technical.add_argument("symbol", type=_symbol)
    technical.add_argument(
        "--windows", nargs="+", type=_bounded_int(1, 2_520, "window"), required=True
    )
    technical.add_argument(
        "--price-basis", choices=("close", "adjustedClose"), default="adjustedClose"
    )
    technical.set_defaults(handler=_technical)

    risk = command("risk", help="calculate stored symbol risk")
    risk.add_argument("symbols", nargs="+", metavar="SYMBOL", type=_symbol)
    _add_risk_options(risk)
    risk.set_defaults(handler=_risk)

    screener = command("screener", help="run the stored EOD screener")
    screener.add_argument("symbols", nargs="*", metavar="SYMBOL", type=_symbol)
    _add_risk_options(screener)
    screener.set_defaults(handler=_screener)

    portfolio = command("portfolio", help="read stored portfolio valuation")
    portfolio.set_defaults(
        handler=lambda client, _: client.request("GET", "portfolio")
    )

    holdings = command("holdings", help="agent portfolio commands (also quant portfolio)")
    actions = holdings.add_subparsers(dest="portfolio_command", required=True)
    for name, endpoint in (("show", "holdings"), ("schema", "schema"), ("schemas", "schemas"), ("history", "history")):
        child = actions.add_parser(name)
        _add_global_options(child, suppress_defaults=True)
        if name == "history":
            child.add_argument('--limit', type=_bounded_int(1, 1000, 'limit'), default=100)
            child.add_argument('--offset', type=_bounded_int(0, 1000000, 'offset'), default=0)
        child.set_defaults(handler=lambda client, args, endpoint=endpoint: client.request(
            'GET', f'portfolio/{endpoint}', query={'limit': args.limit, 'offset': args.offset} if endpoint == 'history' else None))
    for name in ('preview', 'apply', 'capture'):
        child = actions.add_parser(name)
        _add_global_options(child, suppress_defaults=True)
        child.add_argument('--json-file', required=True, help='JSON command file, or - for stdin')
        child.set_defaults(handler=_request, method='POST', path=f'portfolio/{name}')
    for name, endpoint in (('analysis','analysis'),('transactions','transactions'),('tax-lots','tax-lots'),('nav-list','nav')):
        child = actions.add_parser(name)
        _add_global_options(child,suppress_defaults=True)
        child.add_argument('--account')
        if name in {'analysis','tax-lots'}:
            child.add_argument('--date',type=_date)
        if name in {'transactions','nav-list'}:
            child.add_argument('--limit',type=_bounded_int(1,1000,'limit'),default=100)
            child.add_argument('--offset',type=_bounded_int(0,1000000,'offset'),default=0)
        if name == 'transactions':
            child.add_argument('--start',type=_date)
            child.add_argument('--end',type=_date)
        child.set_defaults(handler=lambda client,args,endpoint=endpoint:client.request('GET',f'portfolio/{endpoint}',
            query={key:getattr(args,key) for key in ('account','date','start','end','limit','offset') if hasattr(args,key)}))
    for name,endpoint in (('transactions-preview','transactions/preview'),('transactions-import','transactions/import'),
                          ('nav-capture','nav'),('nav-report','nav/observations'),('scenario','holdings-scenario'),('simulate-sale','sale-simulation')):
        child=actions.add_parser(name)
        _add_global_options(child,suppress_defaults=True)
        child.add_argument('--json-file',required=True)
        child.set_defaults(handler=_request,method='POST',path=f'portfolio/{endpoint}')
    for name in ('nav','performance'):
        child=actions.add_parser(name)
        _add_global_options(child,suppress_defaults=True)
        child.add_argument('identifier')
        child.set_defaults(handler=lambda client,args,name=name:client.request('GET',
            f"portfolio/nav/{quote(args.identifier,safe='')}" + ('/performance' if name=='performance' else '')))
    portfolio_analysis = command('portfolio-analysis',help='ticker valuation, gain and allocation through the portfolio API')
    portfolio_analysis.add_argument('--account')
    portfolio_analysis.add_argument('--date',type=_date)
    portfolio_analysis.set_defaults(handler=lambda client,args:client.request('GET','portfolio/analysis',query={'account':args.account,'date':args.date}))

    portfolio_risk = command("portfolio-risk", help="calculate stored portfolio risk")
    _add_risk_options(portfolio_risk)
    portfolio_risk.set_defaults(handler=_portfolio_risk)
    readiness = command("readiness", help="check stored prerequisites for a dated snapshot review")
    readiness.add_argument("snapshot_id")
    readiness.add_argument("--period", choices=PERIODS, default="1y")
    readiness.add_argument("--benchmark", type=_symbol, default="SPY")
    readiness.set_defaults(handler=lambda client, args: client.request(
        "GET", f"portfolio/snapshots/{quote(args.snapshot_id, safe='')}/readiness",
        query={"period": args.period, "benchmark": args.benchmark}))
    for name, endpoint in (("capabilities", "capabilities"), ("quality", "data/quality"),
                           ("gaps", "data/gaps"), ("snapshots", "portfolio/snapshots"), ("universes", "universes")):
        child = command(name, help=f"read {endpoint}")
        child.set_defaults(handler=lambda client, _, endpoint=endpoint: client.request("GET", endpoint))
    request = command("request", help="call a documented alpha resource; JSON input is forwarded without financial computation")
    request.add_argument("path")
    request.add_argument("--method", choices=("GET", "POST"), default="GET")
    request.add_argument("--json-file", help="JSON request file, or - for stdin")
    request.set_defaults(handler=_request)
    return parser


def _request(client, args):
    from quant.api_client import _load_json
    if args.path.startswith(("/", "http:" , "https:")) or ".." in args.path or "#" in args.path:
        raise CliUsageError("Use a relative alpha resource path")
    body = None
    if args.json_file:
        if args.method != "POST":
            raise CliUsageError("JSON input requires POST")
        try:
            if args.json_file == "-":
                raw = sys.stdin.buffer.read(8*1024*1024+1)
            else:
                with open(args.json_file, "rb") as stream:
                    raw = stream.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:
                raise ValueError("Request exceeds 8 MiB")
            body = _load_json(raw)
        except (OSError, ValueError) as error:
            raise CliUsageError(str(error)) from error
    return client.request(args.method, args.path, body=body)


def _add_global_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    parser.add_argument(
        "--base-url",
        default=(
            argparse.SUPPRESS
            if suppress_defaults
            else os.environ.get("QUANT_API_BASE_URL", DEFAULT_BASE_URL)
        ),
        help="Quant alpha API base URL",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=(
            argparse.SUPPRESS
            if suppress_defaults
            else os.environ.get("QUANT_API_TIMEOUT", "30")
        ),
        help="request timeout in seconds",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent JSON output",
        default=argparse.SUPPRESS if suppress_defaults else False,
    )


def _add_risk_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--period", choices=PERIODS, default="1y")
    parser.add_argument("--benchmark", type=_symbol, default="SPY")


def _symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise argparse.ArgumentTypeError("invalid symbol")
    return symbol


def _search_query(value: str) -> str:
    query = " ".join(value.split())
    if not query or len(query) > 100:
        raise argparse.ArgumentTypeError("query must contain 1-100 characters")
    return query


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _bounded_int(minimum: int, maximum: int, label: str):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{label} must be an integer") from error
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {minimum} and {maximum}"
            )
        return number

    return parse


def _validate_args(args: argparse.Namespace) -> None:
    symbols = getattr(args, "symbols", [])
    maximum = {"status": 100, "quotes": 20, "risk": 20, "screener": 100}.get(
        args.command
    )
    if maximum is not None and len(dict.fromkeys(symbols)) > maximum:
        raise CliUsageError(f"at most {maximum} symbols are allowed")
    if args.command == "bars" and args.start and args.end and args.start > args.end:
        raise CliUsageError("start date cannot be after end date")
    if args.command == "technical":
        args.windows = list(dict.fromkeys(args.windows))
        if len(args.windows) > 10:
            raise CliUsageError("at most 10 windows are allowed")
    if args.command in {"risk", "screener"} and symbols and all(
        symbol == args.benchmark for symbol in symbols
    ):
        raise CliUsageError(f"{args.command} requires a non-benchmark symbol")


def _status(client: ApiClient, args: argparse.Namespace):
    return client.request(
        "GET",
        "data/status",
        query={"symbols": _symbols(args.symbols) if args.symbols else None},
    )


def _search(client: ApiClient, args: argparse.Namespace):
    return client.request(
        "GET", "securities", query={"query": args.query, "limit": args.limit}
    )


def _quotes(client: ApiClient, args: argparse.Namespace):
    return client.request(
        "GET", "market/quotes", query={"symbols": _symbols(args.symbols)}
    )


def _bars(client: ApiClient, args: argparse.Namespace):
    return client.request(
        "GET",
        f"securities/{quote(args.symbol, safe='')}/bars",
        query={"start": args.start, "end": args.end, "limit": args.limit},
    )


def _technical(client: ApiClient, args: argparse.Namespace):
    return client.request(
        "GET",
        f"securities/{quote(args.symbol, safe='')}/technicals",
        query={
            "windows": ",".join(str(window) for window in args.windows),
            "priceBasis": args.price_basis,
        },
    )


def _risk(client: ApiClient, args: argparse.Namespace):
    return client.request(
        "GET",
        "market/risk",
        query={
            "symbols": _symbols(args.symbols),
            "period": args.period,
            "benchmark": args.benchmark,
        },
    )


def _screener(client: ApiClient, args: argparse.Namespace):
    return client.request(
        "GET",
        "market/screener",
        query={
            "symbols": _symbols(args.symbols) if args.symbols else None,
            "period": args.period,
            "benchmark": args.benchmark,
        },
    )


def _portfolio_risk(client: ApiClient, args: argparse.Namespace):
    return client.request(
        "GET",
        "portfolio/risk",
        query={"period": args.period, "benchmark": args.benchmark},
    )


def _symbols(values: list[str]) -> str:
    return ",".join(dict.fromkeys(values))


def _write_json(value: object, *, pretty: bool, stream) -> None:
    options = {
        "sort_keys": True,
        "ensure_ascii": True,
        "allow_nan": False,
    }
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    stream.write(json.dumps(value, **options))
    stream.write("\n")
    stream.flush()


def _exit_error(
    code: int,
    error_code: str,
    message: str,
    *,
    status: int | None = None,
    details: object | None = None,
) -> None:
    error = {"code": error_code, "message": message}
    if status is not None:
        error["status"] = status
    if details is not None:
        error["details"] = details
    _write_json({"ok": False, "error": error}, pretty=False, stream=sys.stderr)
    raise SystemExit(code)
