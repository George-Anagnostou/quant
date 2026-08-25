from collections.abc import Iterable
from datetime import date, timedelta

import polars as pl

from quant.analysis import analyze_market_history
from quant.market_data import MARKET_DATA_SCHEMA, get_sp500_constituents
from quant.market_store import MarketDataRepository
from quant.quotes import resolve_market_history


def analyze_symbols(
    symbols: Iterable[str],
    windows: list[int],
    price_column: str,
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
) -> pl.DataFrame:
    symbols = [
        symbol.strip().upper()
        for symbol in dict.fromkeys(symbols)
        if symbol.strip()
    ]
    if not symbols:
        raise ValueError("At least one symbol is required")
    if not windows or any(window <= 0 for window in windows):
        raise ValueError("Analysis windows must be positive integers")
    if price_column not in {"Close", "Adjusted Close"}:
        raise ValueError("Price column must be Close or Adjusted Close")

    minimum_sessions = max(max(windows), 2)
    start = date.today() - timedelta(days=minimum_sessions * 2 + 30)
    repository = repository or MarketDataRepository()
    market_history = resolve_market_history(
        symbols,
        repository,
        list(MARKET_DATA_SCHEMA),
        minimum_sessions=minimum_sessions,
        refresh=refresh,
        start=start,
    )
    return analyze_market_history(market_history, windows, price_column)


def get_index_symbols(
    repository: MarketDataRepository | None = None,
) -> list[str]:
    repository = repository or MarketDataRepository()
    symbols = repository.list_universe_symbols("sp500")
    if symbols:
        return symbols
    return get_sp500_constituents().get_column("Symbol").to_list()
