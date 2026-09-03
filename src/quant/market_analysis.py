from collections.abc import Iterable
from datetime import datetime, timedelta

import polars as pl

from quant.analysis import (
    analyze_market_history,
    calculate_benchmark_metrics,
    calculate_daily_returns,
    calculate_pairwise_correlations,
    calculate_period_returns,
    score_eod_momentum_screen,
    summarize_risk_metrics,
)
from quant.market_data import EASTERN_TIME, get_sp500_constituents
from quant.market_store import MarketDataRepository
from quant.quotes import resolve_market_history


MAX_ANALYSIS_WINDOWS = 10
MAX_ANALYSIS_WINDOW = 2520
# Periods contain this many benchmark-to-benchmark return intervals.
PERIOD_TARGET_RETURNS = {
    "1mo": 21,
    "3mo": 63,
    "6mo": 126,
    "1y": 252,
    "2y": 504,
    "5y": 1260,
}
PERIOD_CALENDAR_LOOKBACK_DAYS = {
    "1mo": 45,
    "3mo": 120,
    "6mo": 240,
    "1y": 400,
    "2y": 800,
    "5y": 2000,
}
EOD_HISTORY_COLUMNS = ["Date", "Symbol", "Adjusted Close"]


def analyze_symbols(
    symbols: Iterable[str],
    windows: list[int],
    price_column: str,
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
    allow_missing: bool = False,
    fetch_missing: bool = True,
) -> pl.DataFrame:
    symbols = list(
        dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
    )
    if not symbols:
        raise ValueError("At least one symbol is required")
    if not windows or any(
        not isinstance(window, int)
        or isinstance(window, bool)
        or window <= 0
        for window in windows
    ):
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
        fetch_missing=fetch_missing,
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


def analyze_symbol_risk(
    symbols: Iterable[str],
    period: str = "1y",
    benchmark_symbol: str = "SPY",
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
    fetch_missing: bool = True,
) -> dict[str, pl.DataFrame]:
    asset_symbols, benchmark_symbol, history, period_history = (
        load_eod_analysis_history(
            symbols,
            period,
            benchmark_symbol,
            repository,
            refresh,
            fetch_missing,
        )
    )
    if benchmark_symbol not in _accepted_symbols([benchmark_symbol], history):
        raise RuntimeError(
            f"Market data unavailable for benchmark: {benchmark_symbol}"
        )
    analysis_symbols = [
        symbol for symbol in asset_symbols if symbol != benchmark_symbol
    ]
    if not analysis_symbols:
        raise ValueError("At least one non-benchmark symbol is required")
    accepted = _accepted_symbols(analysis_symbols, history)
    if not accepted:
        raise RuntimeError("Market data unavailable for requested symbols")

    returns = calculate_daily_returns(history)
    asset_returns = returns.filter(pl.col("Symbol").is_in(accepted))
    benchmark_returns = returns.filter(
        pl.col("Symbol") == benchmark_symbol
    ).select("Date", "Return")
    periods = calculate_period_returns(
        period_history.filter(pl.col("Symbol").is_in(accepted)),
        ytd_base_date=_ytd_base_date(period_history, benchmark_symbol),
        require_exact_ytd_base=True,
    )
    risk = summarize_risk_metrics(asset_returns)
    benchmark = calculate_benchmark_metrics(
        asset_returns,
        benchmark_returns,
    ).rename({"Observations": "Benchmark Observations"})
    metrics = (
        pl.DataFrame({"Symbol": accepted})
        .join(periods, on="Symbol", how="left", validate="1:1")
        .join(risk, on="Symbol", how="left", validate="1:1")
        .join(benchmark, on="Symbol", how="left", validate="1:1")
        .sort("Symbol")
    )
    correlations = (
        calculate_pairwise_correlations(asset_returns)
        if len(accepted) > 1
        else _empty_correlations()
    )
    return {"metrics": metrics, "correlations": correlations}


def screen_symbols_eod(
    symbols: Iterable[str],
    period: str = "1y",
    benchmark_symbol: str = "SPY",
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
    fetch_missing: bool = True,
) -> pl.DataFrame:
    asset_symbols, benchmark_symbol, history, period_history = (
        load_eod_analysis_history(
            symbols,
            period,
            benchmark_symbol,
            repository,
            refresh,
            fetch_missing,
        )
    )
    if benchmark_symbol not in _accepted_symbols([benchmark_symbol], history):
        raise RuntimeError(
            f"Market data unavailable for benchmark: {benchmark_symbol}"
        )
    analysis_symbols = [
        symbol for symbol in asset_symbols if symbol != benchmark_symbol
    ]
    if not analysis_symbols:
        raise ValueError("At least one non-benchmark symbol is required")
    accepted = _accepted_symbols(analysis_symbols, history)
    if not accepted:
        raise RuntimeError("Market data unavailable for requested symbols")
    return score_eod_momentum_screen(
        history.filter(pl.col("Symbol").is_in([*accepted, benchmark_symbol])),
        benchmark_symbol,
        period_history.filter(
            pl.col("Symbol").is_in([*accepted, benchmark_symbol])
        ),
        ytd_base_date=_ytd_base_date(period_history, benchmark_symbol),
        require_exact_ytd_base=True,
    )


def load_eod_analysis_history(
    symbols: Iterable[str],
    period: str,
    benchmark_symbol: str,
    repository: MarketDataRepository | None,
    refresh: bool,
    fetch_missing: bool = True,
) -> tuple[list[str], str, pl.DataFrame, pl.DataFrame]:
    asset_symbols = _normalize_symbols(symbols)
    benchmark_symbol = _normalize_benchmark(benchmark_symbol)
    period = _normalize_period(period)
    requested = list(dict.fromkeys([*asset_symbols, benchmark_symbol]))
    required_prices = PERIOD_TARGET_RETURNS[period] + 1
    repository = repository or MarketDataRepository()
    current_date = datetime.now(EASTERN_TIME).date()
    start = min(
        current_date - timedelta(days=PERIOD_CALENDAR_LOOKBACK_DAYS[period]),
        current_date.replace(year=current_date.year - 1, month=12, day=20),
    )
    history = resolve_market_history(
        requested,
        repository,
        EOD_HISTORY_COLUMNS,
        minimum_sessions=required_prices,
        refresh=refresh,
        start=start,
        allow_missing=True,
        fetch_missing=fetch_missing,
    )
    history = history.drop_nulls(EOD_HISTORY_COLUMNS).filter(
        pl.col("Symbol").is_in(requested)
    )
    benchmark_dates = _latest_symbol_dates(
        history, benchmark_symbol, required_prices
    )
    if benchmark_dates.height == required_prices:
        row_counts = history.group_by("Symbol").len()
        covered_counts = (
            history.join(benchmark_dates, on="Date", how="inner")
            .group_by("Symbol")
            .len()
            .rename({"len": "_Covered"})
        )
        gap_symbols = (
            row_counts.join(
                covered_counts, on="Symbol", how="left", validate="1:1"
            )
            .filter(
                (pl.col("len") >= required_prices)
                & (pl.col("_Covered").fill_null(0) < required_prices)
            )
            .get_column("Symbol")
            .to_list()
        )
        ytd_base_date = _ytd_base_date(history, benchmark_symbol)
        benchmark_latest = benchmark_dates.get_column("Date").max()
        if (
            ytd_base_date is None
            and benchmark_latest is not None
            and benchmark_latest.year == current_date.year
            and benchmark_latest.timetuple().tm_yday > 10
        ):
            gap_symbols.append(benchmark_symbol)
        gap_symbols = list(dict.fromkeys(gap_symbols))
        if gap_symbols and fetch_missing:
            resolve_market_history(
                gap_symbols,
                repository,
                EOD_HISTORY_COLUMNS,
                minimum_sessions=required_prices,
                refresh=True,
                start=start,
                allow_missing=True,
            )
            history = repository.load(
                requested, start=start, columns=EOD_HISTORY_COLUMNS
            ).drop_nulls(EOD_HISTORY_COLUMNS)

    benchmark_dates = _latest_symbol_dates(
        history, benchmark_symbol, required_prices
    )
    complete_symbols = _symbols_covering_dates(
        history, benchmark_dates, required_prices
    )
    ytd_base_date = _ytd_base_date(history, benchmark_symbol)
    if ytd_base_date is not None:
        context_missing = (
            complete_symbols.join(
                history.filter(pl.col("Date") == ytd_base_date).select("Symbol"),
                on="Symbol",
                how="anti",
            )
            .get_column("Symbol")
            .to_list()
        )
        if context_missing and fetch_missing:
            resolve_market_history(
                context_missing,
                repository,
                EOD_HISTORY_COLUMNS,
                minimum_sessions=required_prices,
                refresh=True,
                start=start,
                allow_missing=True,
            )
            history = repository.load(
                requested, start=start, columns=EOD_HISTORY_COLUMNS
            ).drop_nulls(EOD_HISTORY_COLUMNS)
            benchmark_dates = _latest_symbol_dates(
                history, benchmark_symbol, required_prices
            )
            complete_symbols = _symbols_covering_dates(
                history, benchmark_dates, required_prices
            )

    analysis_history = history.join(benchmark_dates, on="Date", how="inner").join(
        complete_symbols, on="Symbol", how="inner", validate="m:1"
    ).sort("Symbol", "Date")
    accepted = analysis_history.get_column("Symbol").unique().to_list()
    context_dates = history.filter(pl.col("Symbol") == benchmark_symbol).select(
        "Date"
    )
    period_history = (
        history.join(context_dates, on="Date", how="inner")
        .filter(pl.col("Symbol").is_in(accepted))
        .sort("Symbol", "Date")
    )
    return asset_symbols, benchmark_symbol, analysis_history, period_history


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    if isinstance(symbols, str):
        raise ValueError("Symbols must be an iterable of strings")
    try:
        values = list(symbols)
    except TypeError as error:
        raise ValueError("Symbols must be an iterable of strings") from error
    if any(not isinstance(symbol, str) for symbol in values):
        raise ValueError("Symbols must be an iterable of strings")
    normalized = [
        symbol.strip().upper() for symbol in values if symbol.strip()
    ]
    normalized = list(dict.fromkeys(normalized))
    if not normalized:
        raise ValueError("At least one symbol is required")
    return normalized


def _normalize_benchmark(benchmark_symbol: str) -> str:
    if not isinstance(benchmark_symbol, str) or not benchmark_symbol.strip():
        raise ValueError("Benchmark symbol must not be blank")
    return benchmark_symbol.strip().upper()


def _normalize_period(period: str) -> str:
    if not isinstance(period, str):
        raise ValueError("Period must be one of: 1mo, 3mo, 6mo, 1y, 2y, 5y")
    normalized = period.strip().lower()
    if normalized not in PERIOD_TARGET_RETURNS:
        raise ValueError("Period must be one of: 1mo, 3mo, 6mo, 1y, 2y, 5y")
    return normalized


def _accepted_symbols(
    requested: list[str],
    history: pl.DataFrame,
) -> list[str]:
    available = set(history.get_column("Symbol").unique().to_list())
    return [symbol for symbol in requested if symbol in available]


def _latest_symbol_dates(
    history: pl.DataFrame,
    symbol: str,
    limit: int,
) -> pl.DataFrame:
    return (
        history.filter(pl.col("Symbol") == symbol)
        .select("Date")
        .unique()
        .sort("Date")
        .tail(limit)
    )


def _symbols_covering_dates(
    history: pl.DataFrame,
    dates: pl.DataFrame,
    required_prices: int,
) -> pl.DataFrame:
    return (
        history.join(dates, on="Date", how="inner")
        .group_by("Symbol")
        .agg(pl.col("Date").n_unique().alias("_Sessions"))
        .filter(pl.col("_Sessions") == required_prices)
        .select("Symbol")
    )


def _ytd_base_date(history: pl.DataFrame, symbol: str):
    dates = (
        history.filter(pl.col("Symbol") == symbol)
        .get_column("Date")
        .unique()
        .sort()
        .to_list()
    )
    if not dates:
        return None
    latest_year = dates[-1].year
    current_year_dates = [value for value in dates if value.year == latest_year]
    prior_dates = [value for value in dates if value.year < latest_year]
    if not current_year_dates or not prior_dates:
        return None
    first_current = current_year_dates[0]
    prior_close = prior_dates[-1]
    if first_current.timetuple().tm_yday > 10:
        return None
    return prior_close if (first_current - prior_close).days <= 10 else None


def _empty_correlations() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "Symbol": pl.String,
            "Other Symbol": pl.String,
            "Correlation": pl.Float64,
            "Observations": pl.UInt32,
        }
    )
