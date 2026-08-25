from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


DEFAULT_DATABASE_PATH = Path("data/quant.db")
LOCAL_ADMIN_USER_ID = "local-admin"
SCHEMA_VERSION = 2
_INITIALIZATION_LOCK = threading.Lock()


def initialize_database(path: Path = DEFAULT_DATABASE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _INITIALIZATION_LOCK:
        with _open_connection(path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                return
            # Another process may migrate while this connection waits for the lock.
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                _migrate_user_data(connection)
                connection.execute("PRAGMA user_version = 1")
            if version < 2:
                _migrate_market_data(connection)
                connection.execute("PRAGMA user_version = 2")


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
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
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
    connection.execute(
        """
        INSERT OR IGNORE INTO metadata(key, value)
        SELECT ?, value FROM metadata WHERE key = ?
        """,
        (
            f"seeded:watchlist:{LOCAL_ADMIN_USER_ID}",
            "seeded:watchlist",
        ),
    )


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
        CREATE INDEX IF NOT EXISTS positions_user_id_idx ON positions(user_id);
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


def _migrate_market_data(connection: sqlite3.Connection) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS securities (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL UNIQUE COLLATE NOCASE,
            company TEXT,
            exchange TEXT,
            security_type TEXT NOT NULL DEFAULT 'equity',
            currency TEXT NOT NULL DEFAULT 'USD',
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS provider_symbols (
            provider TEXT NOT NULL,
            provider_symbol TEXT NOT NULL,
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            PRIMARY KEY(provider, provider_symbol)
        ) WITHOUT ROWID
        """,
        """
        CREATE TABLE IF NOT EXISTS daily_bars (
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            session_date TEXT NOT NULL,
            provider TEXT NOT NULL,
            open REAL CHECK (open IS NULL OR open > 0),
            high REAL CHECK (high IS NULL OR high > 0),
            low REAL CHECK (low IS NULL OR low > 0),
            close REAL NOT NULL CHECK (close > 0),
            adjusted_close REAL CHECK (adjusted_close IS NULL OR adjusted_close > 0),
            volume INTEGER CHECK (volume IS NULL OR volume >= 0),
            retrieved_at TEXT NOT NULL,
            CHECK (high IS NULL OR low IS NULL OR high >= low),
            CHECK (high IS NULL OR open IS NULL OR high >= open),
            CHECK (high IS NULL OR high >= close),
            CHECK (low IS NULL OR open IS NULL OR low <= open),
            CHECK (low IS NULL OR low <= close),
            PRIMARY KEY(security_id, session_date, provider)
        ) WITHOUT ROWID
        """,
        """
        CREATE INDEX IF NOT EXISTS daily_bars_provider_date_idx
        ON daily_bars(provider, session_date)
        """,
        """
        CREATE TABLE IF NOT EXISTS universes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS universe_memberships (
            universe_id TEXT NOT NULL REFERENCES universes(id) ON DELETE CASCADE,
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            observed_on TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            PRIMARY KEY(universe_id, security_id, observed_on)
        ) WITHOUT ROWID
        """,
        """
        CREATE INDEX IF NOT EXISTS universe_memberships_observation_idx
        ON universe_memberships(universe_id, observed_on, sort_order)
        """,
        """
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_symbols INTEGER NOT NULL DEFAULT 0,
            start_date TEXT,
            end_date TEXT,
            rows_received INTEGER NOT NULL DEFAULT 0,
            rows_written INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ingestion_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingestion_run_id TEXT REFERENCES ingestion_runs(id) ON DELETE CASCADE,
            security_id TEXT REFERENCES securities(id) ON DELETE CASCADE,
            session_date TEXT,
            severity TEXT NOT NULL,
            code TEXT NOT NULL,
            message TEXT NOT NULL,
            observed_value TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sync_requests (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            completed_at TEXT,
            error TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS sync_requests_status_idx
        ON sync_requests(status, requested_at)
        """,
    ]
    for statement in statements:
        connection.execute(statement)
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS watchlist_user_order_idx
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
