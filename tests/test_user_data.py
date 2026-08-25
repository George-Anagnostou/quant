import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from quant.database import LOCAL_ADMIN_USER_ID, database_connection
from quant.user_data import DEFAULT_WATCHLIST, UserDataRepository


class UserDataRepositoryTests(unittest.TestCase):
    def test_migrates_existing_rows_to_local_admin(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE positions (
                        id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        average_cost REAL NOT NULL,
                        account TEXT,
                        asset_class TEXT,
                        sector TEXT,
                        acquired TEXT
                    );
                    CREATE TABLE watchlist (
                        symbol TEXT PRIMARY KEY,
                        sort_order INTEGER NOT NULL
                    );
                    INSERT INTO positions(id, symbol, quantity, average_cost)
                    VALUES ('position-1', 'AAPL', 2, 100);
                    INSERT INTO watchlist(symbol, sort_order) VALUES ('MSFT', 0);
                    """
                )

            repository = UserDataRepository(path)

            self.assertEqual(repository.list_positions()[0]["symbol"], "AAPL")
            self.assertIn("MSFT", repository.list_watchlist())
            with database_connection(path) as connection:
                owner = connection.execute(
                    "SELECT user_id FROM positions WHERE id = 'position-1'"
                ).fetchone()[0]
            self.assertEqual(owner, LOCAL_ADMIN_USER_ID)

    def test_scopes_user_data(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "quant.db"
            local = UserDataRepository(path)
            local.initialize()
            with database_connection(path) as connection:
                connection.execute(
                    "INSERT INTO users(id, username) VALUES ('second-user', 'second-user')"
                )
            second = UserDataRepository(path, "second-user")
            second.initialize()
            second.add_position("MSFT", 1, 200)

            self.assertEqual(local.list_positions(), [])
            self.assertEqual(second.list_positions()[0]["symbol"], "MSFT")

    def test_initializes_database_and_does_not_reseed_after_deletion(self) -> None:
        with TemporaryDirectory() as directory:
            repository = UserDataRepository(Path(directory) / "quant.db")
            repository.initialize()

            self.assertEqual(repository.list_watchlist(), DEFAULT_WATCHLIST)

            position = repository.add_position("AAPL", 2, 100.0, account="roth")
            repository.remove_position(position["id"])
            repository.initialize()
            self.assertEqual(repository.list_positions(), [])

    def test_adds_and_removes_a_position(self) -> None:
        with TemporaryDirectory() as directory:
            repository = UserDataRepository(Path(directory) / "quant.db")
            repository.initialize()
            position = repository.add_position(
                " msft ", 3, 200.0, account="taxable", acquired="2026-02-01"
            )

            self.assertEqual(position["symbol"], "MSFT")
            self.assertTrue(repository.remove_position(position["id"]))
            self.assertFalse(repository.remove_position(position["id"]))

    def test_exposes_positions_as_the_shared_polars_shape(self) -> None:
        with TemporaryDirectory() as directory:
            repository = UserDataRepository(Path(directory) / "quant.db")
            repository.add_position(
                "aapl",
                2,
                100.0,
                account="roth",
                acquired="2026-01-15",
            )

            frame = repository.positions_frame()

        self.assertEqual(
            frame.select("Symbol", "Quantity", "Average Cost", "Account", "Acquired")
            .to_dicts(),
            [
                {
                    "Symbol": "AAPL",
                    "Quantity": 2.0,
                    "Average Cost": 100.0,
                    "Account": "roth",
                    "Acquired": "2026-01-15",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
