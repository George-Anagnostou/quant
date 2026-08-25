import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

import polars as pl

from quant.analysis import summarize_portfolio
from quant.database import DEFAULT_DATABASE_PATH
from quant.market_analysis import analyze_symbols, get_index_symbols
from quant.market_data import get_sp500_market_history, latest_market_snapshot
from quant.market_store import MarketDataRepository
from quant.portfolio import analyze_positions
from quant.user_data import UserDataRepository

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        parser.exit(2, f"quant: error: {error}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant",
        description="Cache-first portfolio and market analysis",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser("index", help="print the latest S&P 500 quotes")
    _add_refresh(index)
    _add_database(index)
    index.set_defaults(handler=_run_index)

    portfolio = commands.add_parser("portfolio", help="analyze the stored portfolio")
    _add_refresh(portfolio)
    portfolio.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path",
    )
    portfolio.set_defaults(handler=_run_portfolio)

    market = commands.add_parser("market", help="analyze stocks or the S&P 500")
    market.add_argument("symbols", nargs="*", metavar="SYMBOL")
    market.add_argument(
        "--index",
        action="store_true",
        help="analyze every symbol in the cached S&P 500",
    )
    market.add_argument(
        "--windows",
        nargs="+",
        type=int,
        required=True,
        metavar="N",
        help="rolling windows in trading sessions",
    )
    market.add_argument(
        "--price",
        choices=("close", "adjusted"),
        required=True,
        help="price basis for calculations",
    )
    _add_refresh(market)
    _add_database(market)
    market.set_defaults(handler=_run_market)
    return parser


def _add_refresh(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="replace cached values with fresh Yahoo data",
    )


def _add_database(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path",
    )


def _run_index(args: argparse.Namespace) -> None:
    repository = MarketDataRepository(args.database)
    symbols = repository.list_universe_symbols("sp500")
    if args.refresh or not symbols:
        history = get_sp500_market_history()
        repository.save(history, universe="sp500")
        print(f"Downloaded and stored {history.height:,} rows in {args.database}")
    else:
        history = repository.load(symbols)
        print(f"Loaded {history.height:,} rows from {args.database}")

    snapshot = latest_market_snapshot(history)
    display = snapshot.select(
        "Date",
        "Symbol",
        "Company",
        "Last Price",
        "Volume",
    ).with_columns(
        pl.col("Last Price")
        .map_elements(_decimal, return_dtype=pl.String)
        .fill_null("N/A"),
        pl.col("Volume")
        .map_elements(_integer, return_dtype=pl.String)
        .fill_null("N/A"),
    )
    print(f"Latest S&P 500 constituent data ({snapshot.height} listings)")
    _print_table(display, width=200, string_length=100)


def _run_portfolio(args: argparse.Namespace) -> None:
    repository = UserDataRepository(args.database)
    positions = repository.positions_frame()
    if positions.is_empty():
        print("Portfolio is empty")
        return
    analysis = analyze_positions(
        positions,
        MarketDataRepository(args.database),
        args.refresh,
    )
    summary = summarize_portfolio(analysis).row(0, named=True)
    total_return = summary["Gain/Loss %"]
    formatted_return = (
        _colored_percentage(total_return) if total_return is not None else "N/A"
    )
    display = analysis.select(
        "Symbol",
        "Quantity",
        "Average Cost",
        "Last Price",
        "Market Value",
        "Gain/Loss",
        "Gain/Loss %",
        "Weight %",
        "As Of",
    ).with_columns(
        pl.col("Quantity").map_elements(_quantity, return_dtype=pl.String),
        pl.col("Average Cost", "Last Price", "Market Value")
        .map_elements(_currency, return_dtype=pl.String),
        pl.col("Gain/Loss").map_elements(_colored_currency, return_dtype=pl.String),
        pl.col("Gain/Loss %").map_elements(
            _colored_percentage, return_dtype=pl.String
        ),
        pl.col("Weight %")
        .map_elements(_percentage, return_dtype=pl.String)
        .fill_null("N/A"),
    )

    print(f"Portfolio analysis ({analysis.height} positions)")
    print(
        f"Market value: {_currency(summary['Market Value'])} | "
        f"Cost basis: {_currency(summary['Cost Basis'])} | "
        f"Gain/loss: {_colored_currency(summary['Gain/Loss'])} ({formatted_return})"
    )
    _print_table(display, width=180)


def _run_market(args: argparse.Namespace) -> None:
    if args.index == bool(args.symbols):
        raise ValueError("provide symbols or --index, but not both")

    windows = list(dict.fromkeys(args.windows))
    repository = MarketDataRepository(args.database)
    symbols = get_index_symbols(repository) if args.index else args.symbols
    price_column = "Adjusted Close" if args.price == "adjusted" else "Close"
    analysis = analyze_symbols(
        symbols,
        windows,
        price_column,
        repository,
        args.refresh,
    )
    snapshot = latest_market_snapshot(analysis)
    price_metrics = [
        metric
        for window in windows
        for metric in (
            f"SMA {window}",
            f"Rolling High {window}",
            f"Rolling Low {window}",
        )
    ]
    volume_averages = [f"Volume SMA {window}" for window in windows]
    relative_volumes = [f"Relative Volume {window}" for window in windows]
    display = snapshot.select(
        "Symbol",
        "Date",
        pl.col(price_column).alias("Price"),
        "Daily Change",
        "Daily Change %",
        *price_metrics,
        *volume_averages,
        *relative_volumes,
    ).with_columns(
        pl.col("Price", *price_metrics)
        .map_elements(_decimal, return_dtype=pl.String)
        .fill_null("N/A"),
        pl.col("Daily Change")
        .map_elements(_colored_decimal, return_dtype=pl.String)
        .fill_null("N/A"),
        pl.col("Daily Change %").map_elements(
            _colored_percentage, return_dtype=pl.String
        )
        .fill_null("N/A"),
        pl.col(*volume_averages)
        .map_elements(_integer, return_dtype=pl.String)
        .fill_null("N/A"),
        pl.col(*relative_volumes)
        .map_elements(lambda value: f"{value:,.2f}x", return_dtype=pl.String)
        .fill_null("N/A"),
    )

    print(
        f"Market analysis ({snapshot.height} symbols, "
        f"{price_column.lower()} basis)"
    )
    _print_table(display, width=240)


def _print_table(
    frame: pl.DataFrame,
    width: int,
    string_length: int = 40,
) -> None:
    with pl.Config(
        tbl_rows=-1,
        tbl_cols=-1,
        tbl_width_chars=width,
        fmt_str_lengths=string_length,
        tbl_hide_dataframe_shape=True,
        tbl_hide_column_data_types=True,
    ):
        print(frame)


def _decimal(value: float) -> str:
    return f"{value:,.2f}"


def _integer(value: float | int) -> str:
    return f"{value:,.0f}"


def _quantity(value: float) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _currency(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _percentage(value: float) -> str:
    return f"{value:,.2f}%"


def _colored_currency(value: float | None) -> str | None:
    return _colored_change(value, _currency)


def _colored_decimal(value: float | None) -> str | None:
    return _colored_change(value, _decimal)


def _colored_percentage(value: float | None) -> str | None:
    return _colored_change(value, _percentage)


def _colored_change(
    value: float | None,
    formatter: Callable[[float], str],
) -> str | None:
    if value is None:
        return None
    formatted = formatter(value)
    if value > 0:
        return f"{GREEN}{formatted}{RESET}"
    if value < 0:
        return f"{RED}{formatted}{RESET}"
    return formatted
