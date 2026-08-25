from collections.abc import Iterable

import polars as pl

from quant.analysis import analyze_portfolio
from quant.market_store import MarketDataRepository
from quant.quotes import resolve_market_history


MARKET_DATA_COLUMNS = ["Date", "Symbol", "Last Price", "Volume"]


def load_portfolio_market_data(
    symbols: Iterable[str],
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
) -> pl.DataFrame:
    repository = repository or MarketDataRepository()
    return resolve_market_history(
        symbols,
        repository,
        MARKET_DATA_COLUMNS,
        refresh=refresh,
        allow_missing=True,
    )


def analyze_positions(
    positions: pl.DataFrame,
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
) -> pl.DataFrame:
    market_data = load_portfolio_market_data(
        positions.get_column("Symbol").unique(maintain_order=True).to_list(),
        repository,
        refresh,
    )
    return analyze_portfolio(positions, market_data)
