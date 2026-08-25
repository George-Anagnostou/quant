import math
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl
from polars.testing import assert_frame_equal

from quant.database import database_connection
from quant.market_store import MarketDataRepository


class MarketDataStorageTests(unittest.TestCase):
    def test_round_trips_market_data_through_sqlite(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Company": ["Apple Inc."],
                "Open": [223.0],
                "High": [226.0],
                "Low": [222.0],
                "Close": [225.50],
                "Adjusted Close": [225.50],
                "Volume": [2_000_000],
            }
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "quant.db"
            repository = MarketDataRepository(path)
            repository.save(market_data)

            self.assertTrue(path.exists())
            assert_frame_equal(
                repository.load(columns=market_data.columns),
                market_data,
                check_dtypes=False,
            )

    def test_keeps_provider_observations_separate(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Close": [225.50],
            }
        )

        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(market_data, provider="yahoo")
            repository.save(
                market_data.with_columns(pl.lit(225.75).alias("Close")),
                provider="replacement",
            )

            self.assertEqual(repository.load(provider="yahoo")["Close"][0], 225.50)
            self.assertEqual(
                repository.load(provider="replacement")["Close"][0], 225.75
            )

    def test_normalizes_symbols_and_uses_last_price_when_close_is_null(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": [" aapl "],
                "Close": [None],
                "Last Price": [225.50],
            }
        )

        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(market_data)

            self.assertEqual(
                repository.load([" aapl "]).select("Symbol", "Close").to_dicts(),
                [{"Symbol": "AAPL", "Close": 225.50}],
            )

    def test_rejects_invalid_or_duplicate_market_rows(self) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            invalid = pl.DataFrame(
                {
                    "Date": [date(2026, 8, 21)],
                    "Symbol": ["AAPL"],
                    "Close": [math.inf],
                }
            )
            duplicate = pl.DataFrame(
                {
                    "Date": [date(2026, 8, 21), date(2026, 8, 21)],
                    "Symbol": [" aapl", "AAPL"],
                    "Close": [225.0, 226.0],
                }
            )

            with self.assertRaisesRegex(ValueError, "finite and positive"):
                repository.save(invalid)
            with self.assertRaisesRegex(ValueError, "duplicate symbol-date"):
                repository.save(duplicate)

    def test_records_ordered_universe_observations(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21), date(2026, 8, 21)],
                "Symbol": ["MSFT", "AAPL"],
                "Close": [500.0, 225.50],
            }
        )

        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(market_data)
            repository.save_universe("sp500", market_data)

            self.assertEqual(
                repository.list_universe_symbols("sp500"),
                ["MSFT", "AAPL"],
            )

            repository.save_universe(
                "sp500",
                market_data.filter(pl.col("Symbol") == "AAPL"),
            )
            self.assertEqual(repository.list_universe_symbols("sp500"), ["AAPL"])

    def test_concurrent_first_save_uses_the_persisted_security_id(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)],
                "Symbol": ["AAPL"],
                "Close": [225.50],
            }
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"

            def save() -> None:
                MarketDataRepository(path).save(market_data)

            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = [executor.submit(save) for _ in range(24)]
                for future in futures:
                    future.result()

            self.assertEqual(MarketDataRepository(path).load().height, 1)


if __name__ == "__main__":
    unittest.main()
