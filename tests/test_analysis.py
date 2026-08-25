import unittest
from datetime import date

import polars as pl
from polars.testing import assert_frame_equal

from quant.analysis import (
    analyze_market_history,
    analyze_portfolio,
    summarize_allocation,
    summarize_portfolio,
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
