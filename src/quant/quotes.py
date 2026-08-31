from collections.abc import Iterable
from datetime import date

import polars as pl

from quant.market_data import get_market_history
from quant.market_store import MarketDataRepository


def resolve_market_history(
    symbols: Iterable[str],
    repository: MarketDataRepository,
    required_columns: list[str],
    minimum_sessions: int = 1,
    refresh: bool = False,
    start: date | None = None,
    allow_missing: bool = False,
) -> pl.DataFrame:
    symbols = list(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    market_data = repository.load(
        symbols,
        start=start,
        columns=required_columns,
    )
    valid_data = market_data.drop_nulls(required_columns)
    available = set(
        valid_data.group_by("Symbol")
        .len()
        .filter(pl.col("len") >= minimum_sessions)
        .get_column("Symbol")
        .to_list()
    )
    symbols_to_download = (
        symbols if refresh else [symbol for symbol in symbols if symbol not in available]
    )
    if symbols_to_download:
        try:
            downloaded = (
                get_market_history(symbols_to_download, start)
                if start is not None
                else get_market_history(symbols_to_download)
            )
        except RuntimeError:
            if not allow_missing:
                raise
        else:
            try:
                repository.save(downloaded)
            except ValueError as error:
                raise RuntimeError(
                    f"Downloaded market data is invalid: {error}"
                ) from error
        market_data = repository.load(
            symbols,
            start=start,
            columns=required_columns,
        )

    market_data = market_data.filter(pl.col("Symbol").is_in(symbols))
    if not allow_missing:
        valid_data = market_data.drop_nulls(required_columns)
        available = set(
            valid_data.group_by("Symbol")
            .len()
            .filter(pl.col("len") >= minimum_sessions)
            .get_column("Symbol")
            .to_list()
        )
        unavailable = [symbol for symbol in symbols if symbol not in available]
        if unavailable:
            raise RuntimeError(
                f"Market data unavailable for: {', '.join(unavailable)}"
            )
    return market_data
