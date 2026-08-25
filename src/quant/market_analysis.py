from collections.abc import Iterable
from datetime import datetime, timedelta

import polars as pl

from quant.analysis import analyze_market_history
from quant.market_data import EASTERN_TIME, get_sp500_constituents
from quant.market_store import MarketDataRepository
from quant.quotes import resolve_market_history


MAX_ANALYSIS_WINDOWS = 10
MAX_ANALYSIS_WINDOW = 2520


def analyze_symbols(
    symbols: Iterable[str],
    windows: list[int],
    price_column: str,
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
    allow_missing: bool = False,
) -> pl.DataFrame:
    symbols = list(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    if not symbols:
        raise ValueError("At least one symbol is required")
    if not windows or any(window <= 0 for window in windows):
        raise ValueError("Analysis windows must be positive integers")
    if len(windows) > MAX_ANALYSIS_WINDOWS:
        raise ValueError(f"At most {MAX_ANALYSIS_WINDOWS} windows are allowed")
    if max(windows) > MAX_ANALYSIS_WINDOW:
        raise ValueError(
            f"Analysis windows cannot exceed {MAX_ANALYSIS_WINDOW} sessions"
        )
    if price_column not in {"Close", "Adjusted Close"}:
        raise ValueError("Price column must be Close or Adjusted Close")

    minimum_sessions = max(max(windows), 2)
    start = datetime.now(EASTERN_TIME).date() - timedelta(
        days=minimum_sessions * 2 + 30
    )
    required_columns = ["Date", "Symbol", "High", "Low", "Close", "Volume"]
    if price_column == "Adjusted Close":
        required_columns.append("Adjusted Close")
    repository = repository or MarketDataRepository()
    market_history = resolve_market_history(
        symbols,
        repository,
        required_columns,
        minimum_sessions=minimum_sessions,
        refresh=refresh,
        start=start,
        allow_missing=allow_missing,
    )
    if market_history.is_empty():
        raise RuntimeError("Market data unavailable for requested symbols")
    return analyze_market_history(market_history, windows, price_column)


def get_index_symbols(
    repository: MarketDataRepository | None = None,
) -> list[str]:
    repository = repository or MarketDataRepository()
    symbols = repository.list_universe_symbols("sp500")
    if symbols:
        return symbols
    return get_sp500_constituents().get_column("Symbol").to_list()
