from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.database import DEFAULT_DATABASE_PATH
from quant.market_data import EASTERN_TIME, get_market_history, get_sp500_constituents
from quant.market_store import MarketDataRepository
from quant.user_data import UserDataRepository


logger = logging.getLogger(__name__)
DEFAULT_HORIZON = date(2025, 1, 1)
CORRECTION_OVERLAP_DAYS = 14
DEFAULT_MARKET_SYMBOLS = (
    "SPY", "^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX", "^TNX",
    "BTC-USD", "ETH-USD",
)


@dataclass
class SyncResult:
    requestedSymbols: int
    downloadedRows: int = 0
    savedRows: int = 0
    failedSymbols: list[str] = field(default_factory=list)
    universeSource: str = "stored"


def synchronize_on_startup(
    path: Path = DEFAULT_DATABASE_PATH,
    *,
    horizon: date = DEFAULT_HORIZON,
    batch_size: int = 50,
    today: date | None = None,
) -> SyncResult:
    current_session_date = today or datetime.now(EASTERN_TIME).date()
    if horizon > current_session_date:
        raise ValueError("Synchronization horizon cannot be in the future")
    if not 1 <= batch_size <= 100:
        raise ValueError("Synchronization batch size must be between 1 and 100")

    market = MarketDataRepository(path)
    users = UserDataRepository(path)
    universe_source = "remote"
    try:
        constituents = get_sp500_constituents()
        market.save_universe("sp500", constituents)
        universe_symbols = constituents.get_column("Symbol").to_list()
    except RuntimeError:
        universe_source = "stored"
        universe_symbols = market.list_universe_symbols("sp500")
        if not universe_symbols:
            raise
        logger.warning("Unable to refresh S&P 500 membership; using stored universe")

    positions = users.positions_frame()
    position_symbols = (
        positions.get_column("Symbol").to_list() if not positions.is_empty() else []
    )
    symbols = list(dict.fromkeys([
        *universe_symbols,
        *users.list_watchlist(),
        *position_symbols,
        *DEFAULT_MARKET_SYMBOLS,
    ]))
    result = SyncResult(
        requestedSymbols=len(symbols), universeSource=universe_source
    )
    coverage = {row["Symbol"]: row for row in market.coverage(symbols).to_dicts()}
    groups: dict[date, list[str]] = {}
    for symbol in symbols:
        row = coverage.get(symbol)
        start = (
            max(
                horizon,
                row["Last Session"] - timedelta(days=CORRECTION_OVERLAP_DAYS),
            )
            if row is not None
            else horizon
        )
        groups.setdefault(start, []).append(symbol)

    for start, group in sorted(groups.items()):
        for offset in range(0, len(group), batch_size):
            _download_with_retries(
                market, group[offset : offset + batch_size], start, result
            )
    result.failedSymbols = list(dict.fromkeys(result.failedSymbols))
    return result


def _download_with_retries(
    repository: MarketDataRepository,
    symbols: list[str],
    start: date,
    result: SyncResult,
) -> None:
    try:
        history = get_market_history(symbols, start)
    except RuntimeError:
        if len(symbols) == 1:
            logger.warning("Startup synchronization failed for %s", symbols[0])
            result.failedSymbols.append(symbols[0])
            return
        midpoint = len(symbols) // 2
        _download_with_retries(repository, symbols[:midpoint], start, result)
        _download_with_retries(repository, symbols[midpoint:], start, result)
        return

    result.downloadedRows += history.height
    try:
        result.savedRows += repository.save(history)
    except ValueError:
        if len(symbols) == 1:
            logger.warning("Startup validation failed for %s", symbols[0])
            result.failedSymbols.append(symbols[0])
            return
        midpoint = len(symbols) // 2
        _download_with_retries(repository, symbols[:midpoint], start, result)
        _download_with_retries(repository, symbols[midpoint:], start, result)
        return
    returned = set(history.get_column("Symbol").unique().to_list())
    missing = [symbol for symbol in symbols if symbol not in returned]
    if not missing:
        return
    if len(symbols) == 1:
        result.failedSymbols.extend(missing)
        return
    for symbol in missing:
        _download_with_retries(repository, [symbol], start, result)
