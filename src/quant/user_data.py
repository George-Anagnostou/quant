from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path

import polars as pl

from quant.database import (
    DEFAULT_DATABASE_PATH,
    LOCAL_ADMIN_USER_ID,
    database_connection,
    initialize_database,
)

DEFAULT_USER_DATA_PATH = DEFAULT_DATABASE_PATH
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"]


class UserDataRepository:
    def __init__(
        self,
        path: Path = DEFAULT_USER_DATA_PATH,
        user_id: str = LOCAL_ADMIN_USER_ID,
    ) -> None:
        self.path = path
        self.user_id = user_id

    def initialize(self) -> None:
        initialize_database(self.path)
        with self._connect() as connection:
            user = connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (self.user_id,)
            ).fetchone()
            if user is None:
                raise ValueError(f"Unknown user: {self.user_id}")
            if not self._is_seeded(connection, f"watchlist:{self.user_id}"):
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO watchlist(user_id, symbol, sort_order)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (self.user_id, symbol, index)
                        for index, symbol in enumerate(DEFAULT_WATCHLIST)
                    ],
                )
                self._mark_seeded(connection, f"watchlist:{self.user_id}")

    def list_positions(self) -> list[dict]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, symbol, quantity, average_cost, account,
                       asset_class, sector, acquired
                FROM positions
                WHERE user_id = ?
                ORDER BY rowid
                """,
                (self.user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def positions_frame(self) -> pl.DataFrame:
        positions = self.list_positions()
        return pl.DataFrame(
            {
                "ID": [position["id"] for position in positions],
                "Symbol": [position["symbol"] for position in positions],
                "Quantity": [position["quantity"] for position in positions],
                "Average Cost": [position["average_cost"] for position in positions],
                "Account": [position["account"] for position in positions],
                "Asset Class": [position["asset_class"] for position in positions],
                "Sector": [position["sector"] for position in positions],
                "Acquired": [position["acquired"] for position in positions],
            },
            schema={
                "ID": pl.String,
                "Symbol": pl.String,
                "Quantity": pl.Float64,
                "Average Cost": pl.Float64,
                "Account": pl.String,
                "Asset Class": pl.String,
                "Sector": pl.String,
                "Acquired": pl.String,
            },
        )

    def add_position(
        self,
        symbol: str,
        quantity: float,
        average_cost: float,
        account: str | None = None,
        asset_class: str | None = None,
        sector: str | None = None,
        acquired: str | None = None,
    ) -> dict:
        self.initialize()
        position = {
            "id": str(uuid.uuid4()),
            "user_id": self.user_id,
            "symbol": symbol.strip().upper(),
            "quantity": float(quantity),
            "average_cost": float(average_cost),
            "account": _clean_optional(account),
            "asset_class": _clean_optional(asset_class),
            "sector": _clean_optional(sector),
            "acquired": _clean_optional(acquired),
        }
        if not position["symbol"]:
            raise ValueError("Symbol is required")
        if position["quantity"] <= 0 or position["average_cost"] < 0:
            raise ValueError("Quantity must be positive and cost non-negative")
        if position["acquired"]:
            date.fromisoformat(position["acquired"])
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO positions(
                    id, user_id, symbol, quantity, average_cost, account,
                    asset_class, sector, acquired
                ) VALUES (
                    :id, :user_id, :symbol, :quantity, :average_cost, :account,
                    :asset_class, :sector, :acquired
                )
                """,
                position,
            )
        return position

    def remove_position(self, position_id: str) -> bool:
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM positions WHERE id = ? AND user_id = ?",
                (position_id, self.user_id),
            )
        return cursor.rowcount > 0

    def list_watchlist(self) -> list[str]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol
                FROM watchlist
                WHERE user_id = ?
                ORDER BY sort_order, symbol
                """,
                (self.user_id,),
            ).fetchall()
        return [row["symbol"] for row in rows]

    def add_watchlist(self, symbol: str) -> list[str]:
        self.initialize()
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("Symbol is required")
        with self._connect() as connection:
            next_order = connection.execute(
                """
                SELECT COALESCE(MAX(sort_order), -1) + 1
                FROM watchlist
                WHERE user_id = ?
                """,
                (self.user_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT OR IGNORE INTO watchlist(user_id, symbol, sort_order)
                VALUES (?, ?, ?)
                """,
                (self.user_id, symbol, next_order),
            )
        return self.list_watchlist()

    def remove_watchlist(self, symbol: str) -> list[str]:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND symbol = ?",
                (self.user_id, symbol.strip().upper()),
            )
        return self.list_watchlist()

    def _connect(self):
        return database_connection(self.path)

    @staticmethod
    def _is_seeded(connection: sqlite3.Connection, key: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM metadata WHERE key = ?", (f"seeded:{key}",)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _mark_seeded(connection: sqlite3.Connection, key: str) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, '1')",
            (f"seeded:{key}",),
        )


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
