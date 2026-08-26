import math
import unittest
from datetime import date, timedelta

import polars as pl
from polars.testing import assert_frame_equal

from quant.analysis import (
    analyze_market_history,
    analyze_portfolio,
    calculate_benchmark_metrics,
    calculate_daily_returns,
    calculate_endpoint_return_contributions,
    calculate_pairwise_correlations,
    calculate_period_returns,
    calculate_portfolio_history,
    calculate_static_weight_variance_risk_contributions,
    score_eod_momentum_screen,
    summarize_allocation,
    summarize_portfolio,
    summarize_risk_metrics,
)


class PortfolioAnalysisTests(unittest.TestCase):
    def test_calculates_position_and_portfolio_values(self) -> None:
        positions = pl.DataFrame(
            {
                "Symbol": ["AAA", "BBB"],
                "Quantity": [10.0, 5.0],
                "Average Cost": [8.0, 20.0],
            }
        )
        market_history = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21), date(2026, 8, 21)],
                "Symbol": ["AAA", "BBB"],
                "Last Price": [10.0, 10.0],
                "Volume": [1_000, 2_000],
            }
        )

        result = analyze_portfolio(positions, market_history)
        summary = summarize_portfolio(result)

        self.assertEqual(result.get_column("Symbol").to_list(), ["AAA", "BBB"])
        self.assertAlmostEqual(result.item(0, "Weight %"), 100 / 1.5)
        self.assertAlmostEqual(result.item(1, "Weight %"), 100 / 3)
        self.assertEqual(result.get_column("Gain/Loss").to_list(), [20.0, -50.0])
        assert_frame_equal(
            summary,
            pl.DataFrame(
                {
                    "Cost Basis": [180.0],
                    "Priced Cost Basis": [180.0],
                    "Market Value": [150.0],
                    "Gain/Loss": [-30.0],
                    "Priced Positions": pl.Series([2], dtype=pl.UInt32),
                    "Gain/Loss %": [-100 / 6],
                }
            ),
        )

    def test_preserves_positions_with_missing_market_data(self) -> None:
        positions = pl.DataFrame(
            {
                "Symbol": ["AAA", "BBB"],
                "Quantity": [1.0, 2.0],
                "Average Cost": [10.0, 20.0],
            }
        )
        market_history = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAA"],
                "Last Price": [15.0],
            }
        )

        result = analyze_portfolio(positions, market_history)
        summary = summarize_portfolio(result).row(0, named=True)

        missing = result.filter(pl.col("Symbol") == "BBB").row(0, named=True)
        self.assertIsNone(missing["Last Price"])
        self.assertIsNone(missing["Market Value"])
        self.assertEqual(summary["Cost Basis"], 50.0)
        self.assertEqual(summary["Priced Cost Basis"], 10.0)
        self.assertEqual(summary["Market Value"], 15.0)
        self.assertEqual(summary["Gain/Loss %"], 50.0)

    def test_summarizes_allocation_by_portfolio_dimension(self) -> None:
        analysis = pl.DataFrame(
            {
                "Account": ["taxable", "roth", "taxable"],
                "Cost Basis": [80.0, 50.0, 20.0],
                "Market Value": [100.0, 60.0, 40.0],
                "Gain/Loss": [20.0, 10.0, 20.0],
            }
        )

        result = summarize_allocation(analysis, "Account")

        self.assertEqual(result.get_column("Account").to_list(), ["taxable", "roth"])
        self.assertEqual(result.get_column("Market Value").to_list(), [140.0, 60.0])
        self.assertEqual(result.get_column("Weight %").to_list(), [70.0, 30.0])

    def test_rejects_non_finite_portfolio_values(self) -> None:
        positions = pl.DataFrame(
            {
                "Symbol": ["AAA"],
                "Quantity": [math.inf],
                "Average Cost": [10.0],
            }
        )
        market_history = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAA"],
                "Last Price": [15.0],
            }
        )

        with self.assertRaisesRegex(ValueError, "quantities must be positive"):
            analyze_portfolio(positions, market_history)


class MarketAnalysisTests(unittest.TestCase):
    def test_calculates_adjusted_price_and_volume_indicators(self) -> None:
        history = pl.DataFrame(
            {
                "Date": [
                    date(2026, 8, 19),
                    date(2026, 8, 20),
                    date(2026, 8, 21),
                ],
                "Symbol": ["AAA", "AAA", "AAA"],
                "Open": [10.0, 12.0, 11.0],
                "High": [11.0, 13.0, 12.0],
                "Low": [9.0, 11.0, 10.0],
                "Close": [10.0, 12.0, 11.0],
                "Adjusted Close": [5.0, 6.0, 11.0],
                "Last Price": [10.0, 12.0, 11.0],
                "Volume": [100, 200, 300],
            }
        )

        result = analyze_market_history(history, [2], "Adjusted Close")
        latest = result.row(-1, named=True)

        self.assertEqual(latest["Daily Change"], 5.0)
        self.assertAlmostEqual(latest["Daily Change %"], 100 * 5 / 6)
        self.assertEqual(latest["SMA 2"], 8.5)
        self.assertEqual(latest["Rolling High 2"], 12.0)
        self.assertEqual(latest["Rolling Low 2"], 5.5)
        self.assertEqual(latest["Volume SMA 2"], 250.0)
        self.assertEqual(latest["Relative Volume 2"], 1.2)


class ReturnAnalyticsTests(unittest.TestCase):
    def test_calculates_calendar_period_returns_without_using_older_history(
        self,
    ) -> None:
        history = pl.DataFrame(
            {
                "Date": [
                    date(2024, 12, 1),
                    date(2025, 8, 1),
                    date(2025, 12, 31),
                    date(2026, 1, 1),
                    date(2026, 1, 20),
                    date(2026, 2, 15),
                ],
                "Symbol": ["AAA"] * 6,
                "Adjusted Close": [50.0, 80.0, 90.0, 100.0, 110.0, 132.0],
            }
        )

        result = calculate_period_returns(history).row(0, named=True)

        self.assertEqual(result["Latest Date"], date(2026, 2, 15))
        self.assertAlmostEqual(result["One Month Return"], 0.2)
        self.assertAlmostEqual(result["YTD Return"], 0.32)
        self.assertAlmostEqual(result["Twelve-One Momentum"], 0.25)

    def test_twelve_one_return_requires_distinct_start_and_end_dates(self) -> None:
        history = pl.DataFrame(
            {
                "Date": [date(2026, 1, 1), date(2026, 2, 1)],
                "Symbol": ["AAA", "AAA"],
                "Adjusted Close": [100.0, 110.0],
            }
        )

        result = calculate_period_returns(history).row(0, named=True)

        self.assertIsNone(result["Twelve-One Momentum"])

    def test_calculates_simple_returns_per_symbol_in_date_order(self) -> None:
        history = pl.DataFrame(
            {
                "Date": [
                    date(2026, 1, 3),
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 1),
                    date(2026, 1, 3),
                ],
                "Symbol": ["AAA", "AAA", "AAA", "BBB", "BBB"],
                "Adjusted Close": [99.0, 100.0, 110.0, 50.0, 55.0],
            }
        )

        result = calculate_daily_returns(history)

        self.assertEqual(
            result.select("Date", "Symbol").rows(),
            [
                (date(2026, 1, 2), "AAA"),
                (date(2026, 1, 3), "AAA"),
                (date(2026, 1, 3), "BBB"),
            ],
        )
        expected = [0.1, -0.1, 0.1]
        for actual, wanted in zip(
            result.get_column("Return"), expected, strict=True
        ):
            self.assertAlmostEqual(actual, wanted)

    def test_summarizes_known_risk_path_and_ignores_null_returns(self) -> None:
        returns = pl.DataFrame(
            {
                "Date": [
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 3),
                    date(2026, 1, 4),
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 1),
                ],
                "Symbol": ["AAA", "AAA", "AAA", "AAA", "CONST", "CONST", "NULL"],
                "Return": pl.Series(
                    [0.1, None, -0.1, 0.05, 0.01, 0.01, None],
                    dtype=pl.Float64,
                ),
            }
        )

        result = summarize_risk_metrics(returns)
        aaa = result.filter(pl.col("Symbol") == "AAA").row(0, named=True)
        constant = result.filter(pl.col("Symbol") == "CONST").row(
            0, named=True
        )
        null = result.filter(pl.col("Symbol") == "NULL").row(0, named=True)

        known_returns = [0.1, -0.1, 0.05]
        mean_return = sum(known_returns) / len(known_returns)
        sample_std = math.sqrt(
            sum((value - mean_return) ** 2 for value in known_returns) / 2
        )
        downside_deviation = math.sqrt(0.1**2 / 3)
        self.assertEqual(aaa["Observations"], 3)
        self.assertAlmostEqual(aaa["Cumulative Return"], 0.0395)
        self.assertAlmostEqual(
            aaa["Annualized Volatility"], sample_std * math.sqrt(252)
        )
        self.assertAlmostEqual(
            aaa["Sharpe Ratio"], mean_return / sample_std * math.sqrt(252)
        )
        self.assertAlmostEqual(
            aaa["Sortino Ratio"],
            mean_return / downside_deviation * math.sqrt(252),
        )
        self.assertAlmostEqual(aaa["Max Drawdown"], -0.1)
        self.assertAlmostEqual(constant["Cumulative Return"], 0.0201)
        self.assertEqual(constant["Annualized Volatility"], 0.0)
        self.assertIsNone(constant["Sharpe Ratio"])
        self.assertEqual(constant["Max Drawdown"], 0.0)
        self.assertEqual(null["Observations"], 0)
        self.assertIsNone(null["Cumulative Return"])

    def test_calculates_beta_and_alpha_only_on_intersecting_dates(self) -> None:
        returns = pl.DataFrame(
            {
                "Date": [
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 3),
                    date(2026, 1, 1),
                ],
                "Symbol": ["AAA", "AAA", "AAA", "BBB"],
                "Return": [0.50, 0.04, 0.03, 0.02],
            }
        )
        benchmark = pl.DataFrame(
            {
                "Date": [date(2026, 1, 2), date(2026, 1, 3)],
                "Return": [0.02, 0.01],
            }
        )

        result = calculate_benchmark_metrics(returns, benchmark)
        aaa = result.filter(pl.col("Symbol") == "AAA").row(0, named=True)
        bbb = result.filter(pl.col("Symbol") == "BBB").row(0, named=True)

        self.assertEqual(aaa["Observations"], 2)
        self.assertAlmostEqual(aaa["Beta"], 1.0)
        self.assertAlmostEqual(aaa["Annualized Alpha"], 0.02 * 252)
        self.assertEqual(bbb["Observations"], 0)
        self.assertIsNone(bbb["Beta"])
        self.assertIsNone(bbb["Annualized Alpha"])

    def test_returns_full_long_form_correlation_matrix(self) -> None:
        returns = pl.DataFrame(
            {
                "Date": [
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 3),
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 3),
                    date(2026, 1, 2),
                    date(2026, 1, 4),
                ],
                "Symbol": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB", "CONST", "CONST"],
                "Return": [0.01, 0.02, 0.03, 0.03, 0.02, 0.01, 0.02, 0.02],
            }
        )

        result = calculate_pairwise_correlations(returns)

        self.assertEqual(result.height, 9)
        aaa_bbb = result.filter(
            (pl.col("Symbol") == "AAA")
            & (pl.col("Other Symbol") == "BBB")
        ).row(0, named=True)
        bbb_aaa = result.filter(
            (pl.col("Symbol") == "BBB")
            & (pl.col("Other Symbol") == "AAA")
        ).row(0, named=True)
        aaa_const = result.filter(
            (pl.col("Symbol") == "AAA")
            & (pl.col("Other Symbol") == "CONST")
        ).row(0, named=True)
        const_const = result.filter(
            (pl.col("Symbol") == "CONST")
            & (pl.col("Other Symbol") == "CONST")
        ).row(0, named=True)
        self.assertAlmostEqual(aaa_bbb["Correlation"], -1.0)
        self.assertAlmostEqual(bbb_aaa["Correlation"], -1.0)
        self.assertEqual(aaa_bbb["Observations"], 3)
        self.assertEqual(aaa_const["Observations"], 1)
        self.assertIsNone(aaa_const["Correlation"])
        self.assertEqual(const_const["Observations"], 2)
        self.assertIsNone(const_const["Correlation"])

    def test_constant_benchmark_has_undefined_beta_without_roundoff(self) -> None:
        dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(100)]
        returns = pl.DataFrame(
            {"Date": dates, "Symbol": ["AAA"] * 100, "Return": [0.1] * 100}
        )
        benchmark = pl.DataFrame({"Date": dates, "Return": [0.2] * 100})

        result = calculate_benchmark_metrics(returns, benchmark).row(
            0, named=True
        )

        self.assertEqual(result["Observations"], 100)
        self.assertIsNone(result["Beta"])
        self.assertIsNone(result["Annualized Alpha"])

    def test_rejects_invalid_prices_returns_and_duplicate_rows(self) -> None:
        invalid_prices = pl.DataFrame(
            {
                "Date": [date(2026, 1, 1)],
                "Symbol": ["AAA"],
                "Adjusted Close": [0.0],
            }
        )
        invalid_returns = pl.DataFrame(
            {
                "Date": [date(2026, 1, 1)],
                "Symbol": ["AAA"],
                "Return": [math.inf],
            }
        )
        duplicate_returns = pl.DataFrame(
            {
                "Date": [date(2026, 1, 1), date(2026, 1, 1)],
                "Symbol": ["AAA", "AAA"],
                "Return": [0.01, 0.02],
            }
        )

        with self.assertRaisesRegex(ValueError, "finite and positive"):
            calculate_daily_returns(invalid_prices)
        with self.assertRaisesRegex(ValueError, "finite and at least -1"):
            summarize_risk_metrics(invalid_returns)
        with self.assertRaisesRegex(ValueError, "duplicate symbol-date"):
            summarize_risk_metrics(duplicate_returns)


class MomentumScreenTests(unittest.TestCase):
    @staticmethod
    def _history(days: int) -> pl.DataFrame:
        dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(days)]
        return pl.DataFrame(
            {
                "Date": dates + dates,
                "Symbol": ["AAA"] * days + ["SPY"] * days,
                "Adjusted Close": (
                    [100.0 + index for index in range(days)]
                    + [200.0 + index * 0.5 for index in range(days)]
                ),
            }
        )

    def test_scores_symbols_deterministically_and_excludes_benchmark(self) -> None:
        history = self._history(220)

        first = score_eod_momentum_screen(history)
        second = score_eod_momentum_screen(history)

        assert_frame_equal(first, second)
        self.assertEqual(first.get_column("Symbol").to_list(), ["AAA"])
        row = first.row(0, named=True)
        self.assertEqual(row["Coverage"], 1.0)
        self.assertGreater(row["Composite Score"], 0.0)
        self.assertIn("Above SMA 200", row["Signals"])

    def test_scores_available_trend_inputs_for_short_history(self) -> None:
        result = score_eod_momentum_screen(self._history(60)).row(0, named=True)

        self.assertIsNotNone(result["SMA 50"])
        self.assertIsNone(result["SMA 200"])
        self.assertEqual(result["Trend Score"], 100.0)
        self.assertLess(result["Coverage"], 1.0)

    def test_requires_benchmark_history(self) -> None:
        history = self._history(60).filter(pl.col("Symbol") == "AAA")

        with self.assertRaisesRegex(ValueError, "SPY is not in history"):
            score_eod_momentum_screen(history)


class PortfolioRiskAnalyticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.positions = pl.DataFrame(
            {
                "Symbol": ["AAA", "AAA", "BBB"],
                "Quantity": [1.0, 2.0, 2.0],
            }
        )
        self.history = pl.DataFrame(
            {
                "Date": [
                    date(2026, 1, 1),
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 3),
                    date(2026, 1, 3),
                ],
                "Symbol": ["AAA", "BBB", "AAA", "AAA", "BBB"],
                "Adjusted Close": [10.0, 20.0, 11.0, 12.0, 18.0],
            }
        )

    def test_values_duplicate_lots_on_complete_common_dates(self) -> None:
        result = calculate_portfolio_history(self.positions, self.history)

        self.assertEqual(
            result.get_column("Date").to_list(),
            [date(2026, 1, 1), date(2026, 1, 3)],
        )
        self.assertEqual(result.get_column("Portfolio Value").to_list(), [70.0, 72.0])
        self.assertIsNone(result.item(0, "Portfolio Return"))
        self.assertAlmostEqual(result.item(1, "Portfolio Return"), 72 / 70 - 1)

    def test_endpoint_contributions_sum_to_exact_portfolio_return(self) -> None:
        result = calculate_endpoint_return_contributions(
            self.positions, self.history
        )

        aaa = result.filter(pl.col("Symbol") == "AAA").row(0, named=True)
        bbb = result.filter(pl.col("Symbol") == "BBB").row(0, named=True)
        self.assertEqual(aaa["Quantity"], 3.0)
        self.assertEqual(aaa["Start Value"], 30.0)
        self.assertEqual(aaa["End Value"], 36.0)
        self.assertAlmostEqual(aaa["Return Contribution"], 6 / 70)
        self.assertAlmostEqual(bbb["Return Contribution"], -4 / 70)
        self.assertAlmostEqual(
            result.get_column("Return Contribution").sum(), 72 / 70 - 1
        )

    def test_static_weight_variance_contributions_sum_to_one(self) -> None:
        returns = pl.DataFrame(
            {
                "Date": [
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 3),
                    date(2026, 1, 4),
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 3),
                ],
                "Symbol": ["AAA", "AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
                "Return": [0.1, -0.1, 0.1, 0.8, 0.05, -0.05, 0.05],
            }
        )
        weights = pl.DataFrame(
            {"Symbol": ["AAA", "BBB"], "Weight": [0.5, 0.5]}
        )

        result = calculate_static_weight_variance_risk_contributions(
            returns, weights
        )

        aaa = result.filter(pl.col("Symbol") == "AAA").item(
            0, "Variance Risk Contribution"
        )
        bbb = result.filter(pl.col("Symbol") == "BBB").item(
            0, "Variance Risk Contribution"
        )
        self.assertAlmostEqual(aaa, 2 / 3)
        self.assertAlmostEqual(bbb, 1 / 3)
        self.assertAlmostEqual(
            result.get_column("Variance Risk Contribution").sum(), 1.0
        )

    def test_constant_portfolio_returns_have_undefined_risk_contribution(self) -> None:
        returns = pl.DataFrame(
            {
                "Date": [
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                    date(2026, 1, 1),
                    date(2026, 1, 2),
                ],
                "Symbol": ["AAA", "AAA", "BBB", "BBB"],
                "Return": [0.01, 0.01, 0.02, 0.02],
            }
        )
        weights = pl.DataFrame(
            {"Symbol": ["AAA", "BBB"], "Weight": [0.5, 0.5]}
        )

        result = calculate_static_weight_variance_risk_contributions(
            returns, weights
        )

        self.assertEqual(
            result.get_column("Variance Risk Contribution").to_list(),
            [None, None],
        )

    def test_rejects_non_positive_position_quantity(self) -> None:
        invalid_positions = pl.DataFrame(
            {"Symbol": ["AAA"], "Quantity": [0.0]}
        )

        with self.assertRaisesRegex(ValueError, "finite and positive"):
            calculate_portfolio_history(invalid_positions, self.history)
