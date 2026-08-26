import polars as pl

from quant.market_data import latest_market_snapshot


TRADING_DAYS_PER_YEAR = 252


def analyze_portfolio(
    positions: pl.DataFrame,
    market_history: pl.DataFrame,
) -> pl.DataFrame:
    _require_columns(positions, {"Symbol", "Quantity", "Average Cost"}, "positions")
    _require_columns(
        market_history,
        {"Date", "Symbol", "Last Price"},
        "market history",
    )
    if positions.filter(
        (pl.col("Quantity") <= 0)
        | (pl.col("Average Cost") < 0)
        | pl.col("Quantity").is_null()
        | pl.col("Average Cost").is_null()
        | ~pl.col("Quantity").is_finite()
        | ~pl.col("Average Cost").is_finite()
    ).height:
        raise ValueError("Portfolio quantities must be positive and costs non-negative")

    quotes = latest_market_snapshot(market_history).select(
        "Symbol",
        pl.col("Date").alias("As Of"),
        "Last Price",
    )
    analysis = positions.join(quotes, on="Symbol", how="left", validate="m:1")

    analysis = (
        analysis.with_columns(
            (pl.col("Quantity") * pl.col("Average Cost")).alias("Cost Basis"),
            (pl.col("Quantity") * pl.col("Last Price")).alias("Market Value"),
        )
        .with_columns(
            (pl.col("Market Value") - pl.col("Cost Basis")).alias("Gain/Loss"),
            pl.when(pl.col("Cost Basis") > 0)
            .then(
                (pl.col("Market Value") / pl.col("Cost Basis") - 1.0) * 100.0
            )
            .otherwise(None)
            .alias("Gain/Loss %"),
        )
    )
    total_value = analysis.get_column("Market Value").sum()
    weight = (
        pl.col("Market Value") / total_value * 100.0
        if total_value is not None and total_value > 0
        else pl.lit(None, dtype=pl.Float64)
    )
    return analysis.with_columns(weight.alias("Weight %")).sort(
        "Weight %", descending=True, nulls_last=True
    )


def summarize_portfolio(analysis: pl.DataFrame) -> pl.DataFrame:
    _require_columns(
        analysis,
        {"Cost Basis", "Market Value", "Gain/Loss"},
        "portfolio analysis",
    )
    return analysis.select(
        pl.col("Cost Basis").sum().alias("Cost Basis"),
        pl.when(pl.col("Market Value").is_not_null())
        .then(pl.col("Cost Basis"))
        .otherwise(None)
        .sum()
        .alias("Priced Cost Basis"),
        pl.col("Market Value").sum().alias("Market Value"),
        pl.col("Gain/Loss").sum().alias("Gain/Loss"),
        pl.col("Market Value").count().alias("Priced Positions"),
    ).with_columns(
        pl.when(pl.col("Priced Positions") == 0)
        .then(None)
        .otherwise(pl.col("Market Value"))
        .alias("Market Value"),
        pl.when(pl.col("Priced Positions") == 0)
        .then(None)
        .otherwise(pl.col("Gain/Loss"))
        .alias("Gain/Loss"),
    ).with_columns(
        pl.when(pl.col("Priced Cost Basis") > 0)
        .then(pl.col("Gain/Loss") / pl.col("Priced Cost Basis") * 100.0)
        .otherwise(None)
        .alias("Gain/Loss %")
    )


def summarize_allocation(
    analysis: pl.DataFrame,
    dimension: str,
) -> pl.DataFrame:
    _require_columns(
        analysis,
        {dimension, "Cost Basis", "Market Value", "Gain/Loss"},
        "portfolio analysis",
    )
    total_value = analysis.get_column("Market Value").sum()
    if total_value is None or total_value <= 0:
        raise ValueError("Portfolio market value must be positive")
    return (
        analysis.with_columns(
            pl.col(dimension).cast(pl.String).fill_null("Unspecified")
        )
        .group_by(dimension)
        .agg(
            pl.col("Cost Basis").sum(),
            pl.col("Market Value").sum(),
            pl.col("Gain/Loss").sum(),
        )
        .with_columns(
            (pl.col("Market Value") / total_value * 100.0).alias("Weight %")
        )
        .sort("Weight %", descending=True)
    )


def analyze_market_history(
    market_history: pl.DataFrame,
    windows: list[int],
    price_column: str,
) -> pl.DataFrame:
    required = {"Date", "Symbol", "High", "Low", "Close", "Volume"}
    if price_column == "Adjusted Close":
        required.add("Adjusted Close")
    _require_columns(market_history, required, "market history")
    windows = list(dict.fromkeys(windows))
    if not windows or any(
        not isinstance(window, int) or window <= 0 for window in windows
    ):
        raise ValueError("Analysis windows must be positive integers")
    if price_column not in {"Close", "Adjusted Close"}:
        raise ValueError("Price column must be Close or Adjusted Close")

    analysis = market_history.sort(["Symbol", "Date"])
    if price_column == "Adjusted Close":
        adjustment = pl.when(pl.col("Close") != 0).then(
            pl.col("Adjusted Close") / pl.col("Close")
        )
        analysis = analysis.with_columns(
            pl.col("Adjusted Close").alias("_Analysis Price"),
            (pl.col("High") * adjustment).alias("_Analysis High"),
            (pl.col("Low") * adjustment).alias("_Analysis Low"),
        )
    else:
        analysis = analysis.with_columns(
            pl.col("Close").alias("_Analysis Price"),
            pl.col("High").alias("_Analysis High"),
            pl.col("Low").alias("_Analysis Low"),
        )

    previous_price = pl.col("_Analysis Price").shift(1).over("Symbol")
    rolling_expressions = [
        expression
        for window in windows
        for expression in (
            pl.col("_Analysis Price")
            .rolling_mean(window_size=window, min_samples=window)
            .over("Symbol")
            .alias(f"SMA {window}"),
            pl.col("_Analysis High")
            .rolling_max(window_size=window, min_samples=window)
            .over("Symbol")
            .alias(f"Rolling High {window}"),
            pl.col("_Analysis Low")
            .rolling_min(window_size=window, min_samples=window)
            .over("Symbol")
            .alias(f"Rolling Low {window}"),
            pl.col("Volume")
            .rolling_mean(window_size=window, min_samples=window)
            .over("Symbol")
            .alias(f"Volume SMA {window}"),
        )
    ]
    analysis = analysis.with_columns(
        [
            pl.col("_Analysis Price")
            .diff()
            .over("Symbol")
            .alias("Daily Change"),
            pl.when(previous_price != 0)
            .then((pl.col("_Analysis Price") / previous_price - 1.0) * 100.0)
            .otherwise(None)
            .alias("Daily Change %"),
            *rolling_expressions,
        ]
    ).with_columns(
        [
            pl.when(pl.col(f"Volume SMA {window}") != 0)
            .then(pl.col("Volume") / pl.col(f"Volume SMA {window}"))
            .otherwise(None)
            .alias(f"Relative Volume {window}")
            for window in windows
        ]
    )
    return analysis.drop("_Analysis Price", "_Analysis High", "_Analysis Low")


def calculate_daily_returns(
    price_history: pl.DataFrame,
    price_column: str = "Adjusted Close",
) -> pl.DataFrame:
    """Return Date/Symbol/simple Return rows, with returns stored as fractions."""
    _validate_price_history(price_history, price_column)
    return (
        price_history.sort(["Symbol", "Date"])
        .with_columns(
            (
                pl.col(price_column)
                / pl.col(price_column).shift(1).over("Symbol")
                - 1.0
            ).alias("Return")
        )
        .select("Date", "Symbol", "Return")
        .drop_nulls("Return")
    )


def calculate_period_returns(
    price_history: pl.DataFrame,
    price_column: str = "Adjusted Close",
) -> pl.DataFrame:
    """Calculate calendar-cutoff period returns as fractions per symbol."""
    _validate_price_history(price_history, price_column)
    ordered = price_history.select("Date", "Symbol", price_column).sort(
        "Symbol", "Date"
    )
    period_values = ordered.group_by("Symbol", maintain_order=True).agg(
        pl.col("Date").max().alias("Latest Date"),
        pl.col(price_column).last().alias("_Latest Price"),
        pl.col(price_column)
        .filter(
            pl.col("Date")
            >= pl.col("Date").max() - pl.duration(days=30)
        )
        .first()
        .alias("_One Month Start Price"),
        pl.col(price_column)
        .filter(
            pl.col("Date").dt.year()
            == pl.col("Date").max().dt.year()
        )
        .first()
        .alias("_YTD Start Price"),
        pl.col("Date")
        .filter(
            pl.col("Date")
            >= pl.col("Date").max() - pl.duration(days=365)
        )
        .first()
        .alias("_Twelve-One Start Date"),
        pl.col(price_column)
        .filter(
            pl.col("Date")
            >= pl.col("Date").max() - pl.duration(days=365)
        )
        .first()
        .alias("_Twelve-One Start Price"),
        pl.col("Date")
        .filter(
            pl.col("Date")
            <= pl.col("Date").max() - pl.duration(days=30)
        )
        .last()
        .alias("_Twelve-One End Date"),
        pl.col(price_column)
        .filter(
            pl.col("Date")
            <= pl.col("Date").max() - pl.duration(days=30)
        )
        .last()
        .alias("_Twelve-One End Price"),
    )
    return (
        period_values.with_columns(
            (
                pl.col("_Latest Price")
                / pl.col("_One Month Start Price")
                - 1.0
            ).alias("One Month Return"),
            (
                pl.col("_Latest Price") / pl.col("_YTD Start Price") - 1.0
            ).alias("YTD Return"),
            pl.when(
                pl.col("_Twelve-One End Date")
                > pl.col("_Twelve-One Start Date")
            )
            .then(
                pl.col("_Twelve-One End Price")
                / pl.col("_Twelve-One Start Price")
                - 1.0
            )
            .otherwise(None)
            .alias("Twelve-One Momentum"),
        )
        .select(
            "Symbol",
            "Latest Date",
            "One Month Return",
            "YTD Return",
            "Twelve-One Momentum",
        )
        .sort("Symbol")
    )


def summarize_risk_metrics(returns: pl.DataFrame) -> pl.DataFrame:
    """Summarize returns with zero-rate Sharpe/Sortino and 252-day scaling."""
    returns = _validate_returns(returns, "returns")
    symbols = returns.select("Symbol").unique().sort("Symbol")
    clean = returns.drop_nulls("Return")

    path = (
        clean.with_columns(
            (pl.col("Return") + 1.0)
            .cum_prod()
            .over("Symbol")
            .alias("_Wealth")
        )
        .with_columns(
            pl.max_horizontal(
                pl.col("_Wealth").cum_max().over("Symbol"),
                pl.lit(1.0),
            ).alias("_Peak")
        )
        .with_columns(
            (pl.col("_Wealth") / pl.col("_Peak") - 1.0).alias(
                "_Drawdown"
            )
        )
    )
    summary = path.group_by("Symbol").agg(
        pl.len().alias("Observations"),
        ((pl.col("Return") + 1.0).product() - 1.0).alias(
            "Cumulative Return"
        ),
        pl.col("Return").mean().alias("_Mean Return"),
        pl.col("Return").std(ddof=1).alias("_Return Std"),
        (
            pl.when(pl.col("Return") < 0.0)
            .then(pl.col("Return"))
            .otherwise(0.0)
            .pow(2)
            .mean()
            .sqrt()
        ).alias("_Downside Deviation"),
        pl.col("_Drawdown").min().alias("Max Drawdown"),
    )
    return (
        symbols.join(summary, on="Symbol", how="left", validate="1:1")
        .with_columns(
            pl.col("Observations").fill_null(0),
            (pl.col("_Return Std") * TRADING_DAYS_PER_YEAR**0.5).alias(
                "Annualized Volatility"
            ),
            pl.when(pl.col("_Return Std") > 0.0)
            .then(
                pl.col("_Mean Return")
                / pl.col("_Return Std")
                * TRADING_DAYS_PER_YEAR**0.5
            )
            .otherwise(None)
            .alias("Sharpe Ratio"),
            pl.when(pl.col("_Downside Deviation") > 0.0)
            .then(
                pl.col("_Mean Return")
                / pl.col("_Downside Deviation")
                * TRADING_DAYS_PER_YEAR**0.5
            )
            .otherwise(None)
            .alias("Sortino Ratio"),
        )
        .select(
            "Symbol",
            "Cumulative Return",
            "Annualized Volatility",
            "Sharpe Ratio",
            "Sortino Ratio",
            "Max Drawdown",
            "Observations",
        )
        .sort("Symbol")
    )


def calculate_benchmark_metrics(
    returns: pl.DataFrame,
    benchmark_returns: pl.DataFrame,
) -> pl.DataFrame:
    """Calculate beta and arithmetic annualized alpha on intersecting dates."""
    returns = _validate_returns(returns, "returns")
    _require_columns(benchmark_returns, {"Date", "Return"}, "benchmark returns")
    if benchmark_returns.filter(pl.col("Date").is_null()).height:
        raise ValueError("Benchmark return dates must not be null")
    if benchmark_returns.group_by("Date").len().filter(pl.col("len") > 1).height:
        raise ValueError("Benchmark returns contain duplicate dates")
    _validate_return_values(benchmark_returns, "Benchmark returns")

    symbols = returns.select("Symbol").unique().sort("Symbol")
    joined = returns.drop_nulls("Return").join(
        benchmark_returns.select(
            "Date", pl.col("Return").alias("_Benchmark Return")
        ).drop_nulls("_Benchmark Return"),
        on="Date",
        how="inner",
        validate="m:1",
    )
    moments = joined.group_by("Symbol").agg(
        pl.len().alias("Observations"),
        pl.col("Return").mean().alias("_Mean Asset Return"),
        pl.col("_Benchmark Return").mean().alias("_Mean Benchmark Return"),
        (
            (pl.col("Return") - pl.col("Return").mean())
            * (
                pl.col("_Benchmark Return")
                - pl.col("_Benchmark Return").mean()
            )
        )
        .sum()
        .alias("_Covariance Numerator"),
        (
            pl.col("_Benchmark Return")
            - pl.col("_Benchmark Return").mean()
        )
        .pow(2)
        .sum()
        .alias("_Benchmark SS"),
    )
    return (
        symbols.join(moments, on="Symbol", how="left", validate="1:1")
        .with_columns(pl.col("Observations").fill_null(0))
        .with_columns(
            pl.when(
                (pl.col("Observations") >= 2)
                & (pl.col("_Benchmark SS") > 0.0)
            )
            .then(pl.col("_Covariance Numerator") / pl.col("_Benchmark SS"))
            .otherwise(None)
            .alias("Beta")
        )
        .with_columns(
            pl.when(pl.col("Beta").is_not_null())
            .then(
                (
                    pl.col("_Mean Asset Return")
                    - pl.col("Beta")
                    * pl.col("_Mean Benchmark Return")
                )
                * TRADING_DAYS_PER_YEAR
            )
            .otherwise(None)
            .alias("Annualized Alpha")
        )
        .select("Symbol", "Beta", "Annualized Alpha", "Observations")
        .sort("Symbol")
    )


def score_eod_momentum_screen(
    price_history: pl.DataFrame,
    benchmark_symbol: str = "SPY",
) -> pl.DataFrame:
    """Score an adjusted-close EOD screen with deterministic linear buckets.

    Momentum maps each period return from -20%..40%; return maps Sharpe
    -1..2 and alpha -10%..20%; risk reverses volatility 60%..10% and absolute
    drawdown 50%..5%. Trend averages available 0/100 above-SMA flags (equal
    to 50 points each when both exist). Composite is the mean available bucket
    score times sqrt(available raw inputs / 9).
    """
    _validate_price_history(price_history, "Adjusted Close")
    if not isinstance(benchmark_symbol, str) or not benchmark_symbol.strip():
        raise ValueError("Benchmark symbol must not be blank")
    if not price_history.filter(
        pl.col("Symbol") == benchmark_symbol
    ).height:
        raise ValueError(f"Benchmark symbol {benchmark_symbol} is not in history")

    ordered = price_history.select(
        "Date", "Symbol", "Adjusted Close"
    ).sort("Symbol", "Date")
    technicals = (
        ordered.with_columns(
            pl.col("Adjusted Close")
            .rolling_mean(window_size=50, min_samples=50)
            .over("Symbol")
            .alias("SMA 50"),
            pl.col("Adjusted Close")
            .rolling_mean(window_size=200, min_samples=200)
            .over("Symbol")
            .alias("SMA 200"),
        )
        .group_by("Symbol", maintain_order=True)
        .agg(
            pl.col("Date").last().alias("Latest Date"),
            pl.col("Adjusted Close").last().alias("Latest Price"),
            pl.col("SMA 50").last(),
            pl.col("SMA 200").last(),
        )
        .filter(pl.col("Symbol") != benchmark_symbol)
    )
    returns = calculate_daily_returns(ordered)
    asset_returns = returns.filter(pl.col("Symbol") != benchmark_symbol)
    benchmark_returns = returns.filter(
        pl.col("Symbol") == benchmark_symbol
    ).select("Date", "Return")
    risk = summarize_risk_metrics(asset_returns)
    benchmark = calculate_benchmark_metrics(
        asset_returns, benchmark_returns
    ).rename({"Observations": "Benchmark Observations"})
    periods = calculate_period_returns(ordered).drop("Latest Date")

    screen = (
        technicals.join(periods, on="Symbol", how="left", validate="1:1")
        .join(risk, on="Symbol", how="left", validate="1:1")
        .join(benchmark, on="Symbol", how="left", validate="1:1")
        .with_columns(
            (
                (pl.col("One Month Return") + 0.20) / 0.60 * 100.0
            )
            .clip(0.0, 100.0)
            .alias("_One Month Score"),
            ((pl.col("YTD Return") + 0.20) / 0.60 * 100.0)
            .clip(0.0, 100.0)
            .alias("_YTD Score"),
            (
                (pl.col("Twelve-One Momentum") + 0.20) / 0.60 * 100.0
            )
            .clip(0.0, 100.0)
            .alias("_Twelve-One Score"),
            ((pl.col("Sharpe Ratio") + 1.0) / 3.0 * 100.0)
            .clip(0.0, 100.0)
            .alias("_Sharpe Score"),
            ((pl.col("Annualized Alpha") + 0.10) / 0.30 * 100.0)
            .clip(0.0, 100.0)
            .alias("_Alpha Score"),
            ((0.60 - pl.col("Annualized Volatility")) / 0.50 * 100.0)
            .clip(0.0, 100.0)
            .alias("_Volatility Score"),
            ((0.50 - pl.col("Max Drawdown").abs()) / 0.45 * 100.0)
            .clip(0.0, 100.0)
            .alias("_Drawdown Score"),
            pl.when(pl.col("SMA 50").is_not_null())
            .then(
                pl.when(pl.col("Latest Price") > pl.col("SMA 50"))
                .then(100.0)
                .otherwise(0.0)
            )
            .otherwise(None)
            .alias("_SMA 50 Score"),
            pl.when(pl.col("SMA 200").is_not_null())
            .then(
                pl.when(pl.col("Latest Price") > pl.col("SMA 200"))
                .then(100.0)
                .otherwise(0.0)
            )
            .otherwise(None)
            .alias("_SMA 200 Score"),
        )
        .with_columns(
            pl.mean_horizontal(
                "_One Month Score", "_YTD Score", "_Twelve-One Score"
            ).alias("Momentum Score"),
            pl.mean_horizontal("_Sharpe Score", "_Alpha Score").alias(
                "Return Score"
            ),
            pl.mean_horizontal(
                "_Volatility Score", "_Drawdown Score"
            ).alias("Risk Score"),
            pl.mean_horizontal("_SMA 50 Score", "_SMA 200 Score").alias(
                "Trend Score"
            ),
            (
                pl.sum_horizontal(
                    pl.col("One Month Return").is_not_null(),
                    pl.col("YTD Return").is_not_null(),
                    pl.col("Twelve-One Momentum").is_not_null(),
                    pl.col("Sharpe Ratio").is_not_null(),
                    pl.col("Annualized Alpha").is_not_null(),
                    pl.col("Annualized Volatility").is_not_null(),
                    pl.col("Max Drawdown").is_not_null(),
                    pl.col("SMA 50").is_not_null(),
                    pl.col("SMA 200").is_not_null(),
                ).cast(pl.Float64)
                / 9.0
            ).alias("Coverage"),
        )
        .with_columns(
            (
                pl.mean_horizontal(
                    "Momentum Score",
                    "Return Score",
                    "Risk Score",
                    "Trend Score",
                )
                * pl.col("Coverage").sqrt()
            ).alias("Composite Score"),
            pl.concat_list(
                pl.when(pl.col("One Month Return") > 0.0)
                .then(pl.lit("Positive 1M"))
                .otherwise(pl.lit(None, dtype=pl.String)),
                pl.when(pl.col("YTD Return") > 0.0)
                .then(pl.lit("Positive YTD"))
                .otherwise(pl.lit(None, dtype=pl.String)),
                pl.when(pl.col("Twelve-One Momentum") > 0.0)
                .then(pl.lit("Positive 12-1"))
                .otherwise(pl.lit(None, dtype=pl.String)),
                pl.when(pl.col("Annualized Alpha") > 0.0)
                .then(pl.lit("Positive Alpha"))
                .otherwise(pl.lit(None, dtype=pl.String)),
                pl.when(pl.col("Latest Price") > pl.col("SMA 50"))
                .then(pl.lit("Above SMA 50"))
                .otherwise(pl.lit(None, dtype=pl.String)),
                pl.when(pl.col("Latest Price") > pl.col("SMA 200"))
                .then(pl.lit("Above SMA 200"))
                .otherwise(pl.lit(None, dtype=pl.String)),
            )
            .list.drop_nulls()
            .alias("Signals"),
        )
    )
    return screen.select(
        "Symbol",
        "Latest Date",
        "Latest Price",
        "SMA 50",
        "SMA 200",
        "One Month Return",
        "YTD Return",
        "Twelve-One Momentum",
        "Cumulative Return",
        "Annualized Volatility",
        "Sharpe Ratio",
        "Sortino Ratio",
        "Max Drawdown",
        "Observations",
        "Beta",
        "Annualized Alpha",
        "Benchmark Observations",
        "Momentum Score",
        "Return Score",
        "Risk Score",
        "Trend Score",
        "Coverage",
        "Composite Score",
        "Signals",
    ).sort("Composite Score", "Symbol", descending=[True, False])


def calculate_pairwise_correlations(returns: pl.DataFrame) -> pl.DataFrame:
    """Return the full long-form symbol correlation matrix by common dates."""
    returns = _validate_returns(returns, "returns")
    symbols = returns.select("Symbol").unique().sort("Symbol")
    all_pairs = symbols.join(
        symbols.rename({"Symbol": "Other Symbol"}), how="cross"
    )
    clean = returns.drop_nulls("Return")
    paired = clean.select(
        "Date", "Symbol", pl.col("Return").alias("_Left Return")
    ).join(
        clean.select(
            "Date",
            pl.col("Symbol").alias("Other Symbol"),
            pl.col("Return").alias("_Right Return"),
        ),
        on="Date",
        how="inner",
        validate="m:m",
    )
    moments = paired.group_by("Symbol", "Other Symbol").agg(
        pl.len().alias("Observations"),
        (
            (pl.col("_Left Return") - pl.col("_Left Return").mean())
            * (pl.col("_Right Return") - pl.col("_Right Return").mean())
        )
        .sum()
        .alias("_Cross Deviation"),
        (pl.col("_Left Return") - pl.col("_Left Return").mean())
        .pow(2)
        .sum()
        .alias("_Left SS"),
        (pl.col("_Right Return") - pl.col("_Right Return").mean())
        .pow(2)
        .sum()
        .alias("_Right SS"),
    )
    return (
        all_pairs.join(
            moments,
            on=["Symbol", "Other Symbol"],
            how="left",
            validate="1:1",
        )
        .with_columns(pl.col("Observations").fill_null(0))
        .with_columns(
            pl.when(
                (pl.col("Observations") >= 2)
                & (pl.col("_Left SS") > 0.0)
                & (pl.col("_Right SS") > 0.0)
            )
            .then(
                (
                    pl.col("_Cross Deviation")
                    / (pl.col("_Left SS") * pl.col("_Right SS")).sqrt()
                ).clip(-1.0, 1.0)
            )
            .otherwise(None)
            .alias("Correlation")
        )
        .select("Symbol", "Other Symbol", "Correlation", "Observations")
        .sort("Symbol", "Other Symbol")
    )


def calculate_portfolio_history(
    positions: pl.DataFrame,
    price_history: pl.DataFrame,
    price_column: str = "Adjusted Close",
) -> pl.DataFrame:
    """Value fixed current shares on dates with prices for every holding."""
    values = _complete_position_value_history(
        positions, price_history, price_column
    )
    history = (
        values.group_by("Date")
        .agg(pl.col("Position Value").sum().alias("Portfolio Value"))
        .sort("Date")
    )
    return history.with_columns(
        (
            pl.col("Portfolio Value")
            / pl.col("Portfolio Value").shift(1)
            - 1.0
        ).alias("Portfolio Return")
    )


def calculate_endpoint_return_contributions(
    positions: pl.DataFrame,
    price_history: pl.DataFrame,
    price_column: str = "Adjusted Close",
) -> pl.DataFrame:
    """Attribute the exact fixed-share return between common first/last dates."""
    values = _complete_position_value_history(
        positions, price_history, price_column
    )
    dates = values.get_column("Date").unique().sort()
    if len(dates) < 2:
        raise ValueError("Endpoint attribution requires two common price dates")
    start_date = dates[0]
    end_date = dates[-1]
    start = values.filter(pl.col("Date") == start_date).select(
        "Symbol",
        "Quantity",
        pl.col("Position Value").alias("Start Value"),
    )
    end = values.filter(pl.col("Date") == end_date).select(
        "Symbol", pl.col("Position Value").alias("End Value")
    )
    total_start_value = start.get_column("Start Value").sum()
    return (
        start.join(end, on="Symbol", how="inner", validate="1:1")
        .with_columns(
            pl.lit(start_date).alias("Start Date"),
            pl.lit(end_date).alias("End Date"),
            (
                (pl.col("End Value") - pl.col("Start Value"))
                / total_start_value
            ).alias("Return Contribution"),
        )
        .select(
            "Symbol",
            "Quantity",
            "Start Date",
            "End Date",
            "Start Value",
            "End Value",
            "Return Contribution",
        )
        .sort("Symbol")
    )


def calculate_static_weight_variance_risk_contributions(
    returns: pl.DataFrame,
    weights: pl.DataFrame,
) -> pl.DataFrame:
    """Approximate variance risk shares using fixed weights and sample covariance.

    This intentionally ignores weight drift and rebalancing. Contributions are
    fractions of portfolio variance and sum to one when portfolio variance is
    positive and at least two complete common return dates exist.
    """
    returns = _validate_returns(returns, "returns")
    _require_columns(weights, {"Symbol", "Weight"}, "weights")
    if weights.height == 0:
        raise ValueError("Weights must contain at least one symbol")
    if weights.filter(
        pl.col("Symbol").is_null()
        | (pl.col("Symbol").cast(pl.String).str.strip_chars() == "")
    ).height:
        raise ValueError("Weight symbols must not be null or blank")
    if weights.group_by("Symbol").len().filter(pl.col("len") > 1).height:
        raise ValueError("Weights contain duplicate symbols")
    if weights.filter(
        pl.col("Weight").is_null()
        | ~pl.col("Weight").is_finite()
        | (pl.col("Weight") <= 0.0)
    ).height:
        raise ValueError("Weights must be finite and positive")
    total_weight = weights.get_column("Weight").sum()
    if total_weight is None or abs(total_weight - 1.0) > 1e-6:
        raise ValueError("Weights must sum to 1")

    weights = weights.select("Symbol", pl.col("Weight").cast(pl.Float64)).sort(
        "Symbol"
    )
    clean = returns.drop_nulls("Return").join(
        weights.select("Symbol"), on="Symbol", how="inner", validate="m:1"
    )
    complete_dates = (
        clean.group_by("Date")
        .agg(pl.col("Symbol").n_unique().alias("_Symbols"))
        .filter(pl.col("_Symbols") == weights.height)
        .select("Date")
    )
    complete = clean.join(complete_dates, on="Date", how="inner")
    if complete.get_column("Date").n_unique() < 2:
        return weights.with_columns(
            pl.lit(None, dtype=pl.Float64).alias(
                "Variance Risk Contribution"
            )
        )

    centered = complete.join(
        complete.group_by("Symbol").agg(
            pl.col("Return").mean().alias("_Mean Return")
        ),
        on="Symbol",
        how="left",
        validate="m:1",
    ).with_columns(
        (pl.col("Return") - pl.col("_Mean Return")).alias("_Deviation")
    )
    observations = complete.get_column("Date").n_unique()
    covariance = centered.select(
        "Date", "Symbol", pl.col("_Deviation").alias("_Left Deviation")
    ).join(
        centered.select(
            "Date",
            pl.col("Symbol").alias("Other Symbol"),
            pl.col("_Deviation").alias("_Right Deviation"),
        ),
        on="Date",
        how="inner",
        validate="m:m",
    ).group_by("Symbol", "Other Symbol").agg(
        (
            (pl.col("_Left Deviation") * pl.col("_Right Deviation")).sum()
            / (observations - 1)
        ).alias("_Covariance")
    )
    marginal = (
        covariance.join(
            weights.rename(
                {"Symbol": "Other Symbol", "Weight": "_Other Weight"}
            ),
            on="Other Symbol",
            how="inner",
            validate="m:1",
        )
        .group_by("Symbol")
        .agg(
            (pl.col("_Covariance") * pl.col("_Other Weight"))
            .sum()
            .alias("_Marginal Variance")
        )
    )
    contributions = weights.join(
        marginal, on="Symbol", how="left", validate="1:1"
    ).with_columns(
        (pl.col("Weight") * pl.col("_Marginal Variance")).alias(
            "_Variance Contribution"
        )
    )
    portfolio_variance = contributions.get_column(
        "_Variance Contribution"
    ).sum()
    if portfolio_variance is None or portfolio_variance <= 0.0:
        return weights.with_columns(
            pl.lit(None, dtype=pl.Float64).alias(
                "Variance Risk Contribution"
            )
        )
    return contributions.select(
        "Symbol",
        "Weight",
        (
            pl.col("_Variance Contribution") / portfolio_variance
        ).alias("Variance Risk Contribution"),
    ).sort("Symbol")


def _validate_price_history(
    price_history: pl.DataFrame,
    price_column: str,
) -> None:
    _require_columns(
        price_history, {"Date", "Symbol", price_column}, "price history"
    )
    if price_history.filter(
        pl.col("Date").is_null()
        | pl.col("Symbol").is_null()
        | (pl.col("Symbol").cast(pl.String).str.strip_chars() == "")
    ).height:
        raise ValueError("Price dates and symbols must not be null or blank")
    if price_history.group_by("Date", "Symbol").len().filter(
        pl.col("len") > 1
    ).height:
        raise ValueError("Price history contains duplicate symbol-date rows")
    if price_history.filter(
        pl.col(price_column).is_null()
        | ~pl.col(price_column).is_finite()
        | (pl.col(price_column) <= 0.0)
    ).height:
        raise ValueError(f"{price_column} values must be finite and positive")


def _validate_returns(returns: pl.DataFrame, label: str) -> pl.DataFrame:
    _require_columns(returns, {"Date", "Symbol", "Return"}, label)
    if returns.filter(
        pl.col("Date").is_null()
        | pl.col("Symbol").is_null()
        | (pl.col("Symbol").cast(pl.String).str.strip_chars() == "")
    ).height:
        raise ValueError(f"{label.capitalize()} dates and symbols must be valid")
    if returns.group_by("Date", "Symbol").len().filter(
        pl.col("len") > 1
    ).height:
        raise ValueError(f"{label.capitalize()} contain duplicate symbol-date rows")
    _validate_return_values(returns, label.capitalize())
    return returns.select("Date", "Symbol", "Return").sort(["Symbol", "Date"])


def _validate_return_values(returns: pl.DataFrame, label: str) -> None:
    if returns.filter(
        pl.col("Return").is_not_null()
        & (~pl.col("Return").is_finite() | (pl.col("Return") < -1.0))
    ).height:
        raise ValueError(f"{label} must be finite and at least -1")


def _complete_position_value_history(
    positions: pl.DataFrame,
    price_history: pl.DataFrame,
    price_column: str,
) -> pl.DataFrame:
    _require_columns(positions, {"Symbol", "Quantity"}, "positions")
    if positions.height == 0:
        raise ValueError("Positions must contain at least one lot")
    if positions.filter(
        pl.col("Symbol").is_null()
        | (pl.col("Symbol").cast(pl.String).str.strip_chars() == "")
    ).height:
        raise ValueError("Position symbols must not be null or blank")
    if positions.filter(
        pl.col("Quantity").is_null()
        | ~pl.col("Quantity").is_finite()
        | (pl.col("Quantity") <= 0.0)
    ).height:
        raise ValueError("Position quantities must be finite and positive")
    _validate_price_history(price_history, price_column)

    holdings = positions.group_by("Symbol").agg(pl.col("Quantity").sum())
    values = (
        price_history.select(
            "Date", "Symbol", pl.col(price_column).alias("Price")
        )
        .join(holdings, on="Symbol", how="inner", validate="m:1")
        .with_columns(
            (pl.col("Quantity") * pl.col("Price")).alias("Position Value")
        )
    )
    complete_dates = (
        values.group_by("Date")
        .agg(pl.col("Symbol").n_unique().alias("_Symbols"))
        .filter(pl.col("_Symbols") == holdings.height)
        .select("Date")
    )
    return (
        values.join(complete_dates, on="Date", how="inner", validate="m:1")
        .select("Date", "Symbol", "Quantity", "Price", "Position Value")
        .sort("Date", "Symbol")
    )


def _require_columns(
    frame: pl.DataFrame,
    required: set[str],
    label: str,
) -> None:
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing {label} columns: {', '.join(sorted(missing))}")
