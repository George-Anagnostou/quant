from collections.abc import Iterable

import polars as pl

from quant.analysis import (
    analyze_portfolio,
    calculate_benchmark_metrics,
    calculate_daily_returns,
    calculate_endpoint_return_contributions,
    calculate_pairwise_correlations,
    calculate_portfolio_history,
    calculate_static_weight_variance_risk_contributions,
    summarize_risk_metrics,
)
from quant.market_analysis import load_eod_analysis_history
from quant.market_store import MarketDataRepository
from quant.quotes import resolve_market_history


MARKET_DATA_COLUMNS = ["Date", "Symbol", "Last Price", "Volume"]


def load_portfolio_market_data(
    symbols: Iterable[str],
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
    fetch_missing: bool = True,
) -> pl.DataFrame:
    repository = repository or MarketDataRepository()
    return resolve_market_history(
        symbols,
        repository,
        MARKET_DATA_COLUMNS,
        refresh=refresh,
        allow_missing=True,
        fetch_missing=fetch_missing,
    )


def analyze_positions(
    positions: pl.DataFrame,
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
    fetch_missing: bool = True,
) -> pl.DataFrame:
    market_data = load_portfolio_market_data(
        positions.get_column("Symbol").unique(maintain_order=True).to_list(),
        repository,
        refresh,
        fetch_missing,
    )
    return analyze_portfolio(positions, market_data)


def analyze_portfolio_risk(
    positions: pl.DataFrame,
    period: str = "1y",
    benchmark_symbol: str = "SPY",
    repository: MarketDataRepository | None = None,
    refresh: bool = False,
    fetch_missing: bool = True,
) -> dict[str, pl.DataFrame | list[str]]:
    """Analyze fixed current shares over common stored EOD sessions."""
    if positions.is_empty():
        raise ValueError("Portfolio has no positions")
    if "Symbol" not in positions.columns or any(
        not isinstance(symbol, str)
        for symbol in positions.get_column("Symbol").to_list()
    ):
        raise ValueError("Portfolio symbols must be strings")
    positions = positions.with_columns(
        pl.col("Symbol").str.strip_chars().str.to_uppercase()
    )
    symbols = positions.get_column("Symbol").unique(maintain_order=True).to_list()
    requested, benchmark_symbol, history, _ = load_eod_analysis_history(
        symbols,
        period,
        benchmark_symbol,
        repository,
        refresh,
        fetch_missing,
    )
    available = set(
        history.group_by("Symbol")
        .len()
        .filter(pl.col("len") >= 2)
        .get_column("Symbol")
        .to_list()
    )
    if benchmark_symbol not in available:
        raise RuntimeError(
            f"Market data unavailable for benchmark: {benchmark_symbol}"
        )
    unavailable = [symbol for symbol in requested if symbol not in available]
    if unavailable:
        raise RuntimeError(
            f"Portfolio risk data unavailable for: {', '.join(unavailable)}"
        )

    asset_history = history.filter(pl.col("Symbol").is_in(requested))
    portfolio_history = calculate_portfolio_history(positions, asset_history)
    if portfolio_history.height < 2:
        raise RuntimeError("Portfolio risk analysis requires two common price dates")
    portfolio_returns = portfolio_history.drop_nulls("Portfolio Return").select(
        "Date",
        pl.lit("Portfolio").alias("Symbol"),
        pl.col("Portfolio Return").alias("Return"),
    )
    common_dates = portfolio_history.select("Date")
    asset_history = asset_history.join(common_dates, on="Date", how="inner")
    benchmark_returns = calculate_daily_returns(
        history.filter(pl.col("Symbol") == benchmark_symbol).join(
            common_dates, on="Date", how="inner"
        )
    ).select("Date", "Return")
    summary = summarize_risk_metrics(portfolio_returns)
    benchmark = calculate_benchmark_metrics(
        portfolio_returns, benchmark_returns
    ).rename({"Observations": "Benchmark Observations"})
    metrics = summary.join(
        benchmark, on="Symbol", how="left", validate="1:1"
    )

    contributions = calculate_endpoint_return_contributions(
        positions, asset_history
    )
    latest_date = portfolio_history.get_column("Date").max()
    quantities = positions.group_by("Symbol").agg(pl.col("Quantity").sum())
    weights = (
        asset_history.filter(pl.col("Date") == latest_date)
        .join(quantities, on="Symbol", how="inner", validate="1:1")
        .with_columns(
            (pl.col("Adjusted Close") * pl.col("Quantity")).alias("_Value")
        )
        .select(
            "Symbol",
            (pl.col("_Value") / pl.col("_Value").sum()).alias("Weight"),
        )
    )
    asset_returns = calculate_daily_returns(asset_history)
    risk_contributions = calculate_static_weight_variance_risk_contributions(
        asset_returns, weights
    )
    correlations = calculate_pairwise_correlations(asset_returns)
    return {
        "metrics": metrics,
        "history": portfolio_history,
        "return_contributions": contributions,
        "risk_contributions": risk_contributions,
        "correlations": correlations,
        "unavailable_symbols": [],
    }
