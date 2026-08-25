from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


DEFAULT_DATABASE_PATH = Path("data/quant.db")
LOCAL_ADMIN_USER_ID = "local-admin"


def initialize_database(path: Path = DEFAULT_DATABASE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _open_connection(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            connection.execute("BEGIN IMMEDIATE")
            _migrate_user_data(connection)
            connection.execute("PRAGMA user_version = 1")


@contextmanager
def database_connection(
    path: Path = DEFAULT_DATABASE_PATH,
) -> Iterator[sqlite3.Connection]:
    with _open_connection(path) as connection:
        yield connection


@contextmanager
def _open_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _migrate_user_data(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            disabled_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            token_prefix TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL UNIQUE,
            scopes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            last_used_at TEXT,
            revoked_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO users(id, username, display_name)
        VALUES (?, ?, ?)
        """,
        (LOCAL_ADMIN_USER_ID, LOCAL_ADMIN_USER_ID, "Local Admin"),
    )
    _migrate_positions(connection)
    _migrate_watchlist(connection)


def _migrate_positions(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "positions"):
        _create_positions(connection)
        return
    if "user_id" in _table_columns(connection, "positions"):
        return

    connection.execute("ALTER TABLE positions RENAME TO positions_legacy")
    _create_positions(connection)
    connection.execute(
        """
        INSERT INTO positions(
            id, user_id, symbol, quantity, average_cost, account,
            asset_class, sector, acquired
        )
        SELECT id, ?, symbol, quantity, average_cost, account,
               asset_class, sector, acquired
        FROM positions_legacy
        """,
        (LOCAL_ADMIN_USER_ID,),
    )
    connection.execute("DROP TABLE positions_legacy")


def _create_positions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE positions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            quantity REAL NOT NULL CHECK (quantity > 0),
            average_cost REAL NOT NULL CHECK (average_cost >= 0),
            account TEXT,
            asset_class TEXT,
            sector TEXT,
            acquired TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX positions_user_id_idx ON positions(user_id);
        """
    )


def _migrate_watchlist(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "watchlist"):
        _create_watchlist(connection)
        return
    if "user_id" in _table_columns(connection, "watchlist"):
        return

    connection.execute("ALTER TABLE watchlist RENAME TO watchlist_legacy")
    _create_watchlist(connection)
    connection.execute(
        """
        INSERT INTO watchlist(user_id, symbol, sort_order)
        SELECT ?, symbol, sort_order
        FROM watchlist_legacy
        """,
        (LOCAL_ADMIN_USER_ID,),
    )
    connection.execute("DROP TABLE watchlist_legacy")


def _create_watchlist(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE watchlist (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            PRIMARY KEY(user_id, symbol)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        CREATE INDEX watchlist_user_order_idx
        ON watchlist(user_id, sort_order);
        """
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
