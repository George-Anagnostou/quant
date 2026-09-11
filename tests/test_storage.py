import math
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import polars as pl
from polars.testing import assert_frame_equal

from quant.database import database_connection, is_database_initialized
from quant.market_store import MarketDataRepository
from quant.user_data import UserDataRepository


class MarketDataStorageTests(unittest.TestCase):
    def test_read_only_repositories_read_but_cannot_write(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant ?# data.db"
            frame = pl.DataFrame({"Date": [date(2026, 8, 21)], "Symbol": ["AAPL"], "Close": [100.0]})
            MarketDataRepository(path).save(frame)
            position = UserDataRepository(path).add_position("AAPL", 1, 10)
            market = MarketDataRepository(path, read_only=True)
            user = UserDataRepository(path, read_only=True)
            self.assertTrue(is_database_initialized(path))
            self.assertEqual(market.load()["Close"][0], 100.0)
            self.assertEqual(user.list_positions()[0]["symbol"], "AAPL")
            with self.assertRaises(sqlite3.OperationalError):
                market.save(frame)
            with self.assertRaises(sqlite3.OperationalError):
                user.add_position("AAPL", 1, 10)
            with self.assertRaises(sqlite3.OperationalError):
                user.remove_position(position["id"])

    def test_read_only_connection_never_creates_missing_database(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            with self.assertRaises(sqlite3.OperationalError):
                with database_connection(path, read_only=True):
                    pass
            self.assertFalse(path.exists())

    def test_corrupt_or_incompatible_database_is_not_reported_as_empty(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            path.write_bytes(b"not a sqlite database")
            with self.assertRaises(sqlite3.DatabaseError):
                is_database_initialized(path)
            other = Path(directory) / "future.db"
            with database_connection(other) as connection:
                connection.execute("PRAGMA user_version = 999")
            with self.assertRaises(sqlite3.DatabaseError):
                is_database_initialized(other)

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

    def test_exposes_storage_provenance_and_coverage(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 20), date(2026, 8, 21)],
                "Symbol": ["AAPL", "AAPL"],
                "Close": [224.0, 225.5],
            }
        )
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(market_data)

            stored = repository.load(
                ["AAPL"], columns=["Symbol", "Provider", "Retrieved At"]
            )
            coverage = repository.coverage(["AAPL"]).row(0, named=True)

        self.assertEqual(stored.get_column("Provider").to_list(), ["yahoo"] * 2)
        self.assertTrue(all(stored.get_column("Retrieved At")))
        self.assertEqual(coverage["First Session"], date(2026, 8, 20))
        self.assertEqual(coverage["Last Session"], date(2026, 8, 21))
        self.assertEqual(coverage["Session Count"], 2)
        self.assertEqual(coverage["Complete OHLCV Count"], 0)
        self.assertEqual(coverage["Adjusted Close Count"], 0)

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

    def test_searches_local_securities_with_exact_and_prefix_priority(self) -> None:
        market_data = pl.DataFrame(
            {
                "Date": [date(2026, 8, 21)] * 4,
                "Symbol": ["MAPP", "AAPL", "ZZZ", "APP"],
                "Company": [
                    "Mapping Corp.",
                    "Apple Inc.",
                    "Pineapple Holdings",
                    "App Corp.",
                ],
                "Close": [10.0, 20.0, 30.0, 40.0],
            }
        )

        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")
            repository.save(market_data)

            results = repository.search_securities(" aPp ")

        self.assertEqual(
            results.get_column("Symbol").to_list(),
            ["APP", "AAPL", "MAPP", "ZZZ"],
        )

    def test_bounds_local_security_search(self) -> None:
        with TemporaryDirectory() as directory:
            repository = MarketDataRepository(Path(directory) / "quant.db")

            with self.assertRaisesRegex(ValueError, "must not be blank"):
                repository.search_securities("  ")
            with self.assertRaisesRegex(ValueError, "between 1 and 50"):
                repository.search_securities("app", 51)

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
