import polars as pl

from quant.market_data import latest_market_snapshot


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


def _require_columns(
    frame: pl.DataFrame,
    required: set[str],
    label: str,
) -> None:
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing {label} columns: {', '.join(sorted(missing))}")
