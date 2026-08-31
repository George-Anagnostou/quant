import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import polars as pl

from quant.market_data import (
    _is_completed_daily_bar,
    get_market_history,
    get_latest_market_data,
    get_sp500_market_history,
)


class FakeSeries:
    def __init__(self, value, date) -> None:
        self.value = value
        self.index = [date]
        self.empty = False

    def dropna(self):
        return self

    def items(self):
        return [(self.index[0], self.value)]


class FakeColumns:
    def __init__(self, values, date) -> None:
        self.values = values
        self.date = date
        self.columns = values.keys()
        self.at = self

    def __getitem__(self, key):
        if isinstance(key, tuple):
            _, symbol = key
            return self.values[symbol]
        return FakeSeries(self.values[key], self.date)


class FakeHistory:
    empty = False

    def __init__(self) -> None:
        trading_date = date(2026, 8, 21)
        self.columns = {
            "Open": FakeColumns(
                {"BRK-B": 498.0, "AAPL": 223.0}, trading_date
            ),
            "High": FakeColumns(
                {"BRK-B": 501.0, "AAPL": 226.0}, trading_date
            ),
            "Low": FakeColumns(
                {"BRK-B": 497.0, "AAPL": 222.0}, trading_date
            ),
            "Close": FakeColumns(
                {"BRK-B": 500.25, "AAPL": 225.50}, trading_date
            ),
            "Adj Close": FakeColumns(
                {"BRK-B": 500.25, "AAPL": 225.50}, trading_date
            ),
            "Volume": FakeColumns(
                {"BRK-B": 1_000_000, "AAPL": 2_000_000}, trading_date
            ),
        }

    def __getitem__(self, column):
        return self.columns[column]


class LatestMarketDataTests(unittest.TestCase):
    @patch("quant.market_data.get_market_history")
    def test_sp500_history_omits_constituents_without_bars(
        self,
        get_market_history,
    ) -> None:
        constituents = pl.DataFrame(
            {
                "Symbol": ["AAPL", "MISSING"],
                "Company": ["Apple Inc.", "Missing Inc."],
            }
        )
        get_market_history.return_value = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Open": [223.0],
                "High": [226.0],
                "Low": [222.0],
                "Close": [225.50],
                "Adjusted Close": [225.50],
                "Last Price": [225.50],
                "Volume": [2_000_000],
            }
        )

        result = get_sp500_market_history(constituents)

        self.assertEqual(result.get_column("Symbol").to_list(), ["AAPL"])
    def test_accepts_current_session_only_after_settlement_cutoff(self) -> None:
        eastern = ZoneInfo("America/New_York")
        session = date(2026, 8, 25)

        self.assertFalse(
            _is_completed_daily_bar(
                session,
                datetime(2026, 8, 25, 19, 59, tzinfo=eastern),
            )
        )
        self.assertTrue(
            _is_completed_daily_bar(
                session,
                datetime(2026, 8, 25, 20, 0, tzinfo=eastern),
            )
        )

    @patch("quant.market_data.yf.download", return_value=FakeHistory())
    def test_downloads_all_symbols_in_one_batch(self, download) -> None:
        result = get_latest_market_data(["BRK.B", "AAPL"])

        download.assert_called_once()
        self.assertEqual(download.call_args.kwargs["tickers"], ["BRK-B", "AAPL"])
        self.assertEqual(
            result.select("Symbol", "Last Price", "Volume").to_dicts(),
            [
                {"Symbol": "BRK.B", "Last Price": 500.25, "Volume": 1_000_000},
                {"Symbol": "AAPL", "Last Price": 225.50, "Volume": 2_000_000},
            ],
        )

    @patch("quant.market_data.yf.download", side_effect=OSError("offline"))
    def test_reports_provider_failures_consistently(self, download) -> None:
        with self.assertRaisesRegex(RuntimeError, "Yahoo market data request failed"):
            get_market_history(["AAPL"])


if __name__ == "__main__":
    unittest.main()
