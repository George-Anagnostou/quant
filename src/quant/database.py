from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


DEFAULT_DATABASE_PATH = Path("data/quant.db")
LOCAL_ADMIN_USER_ID = "local-admin"
MAX_WATCHLIST_SYMBOLS = 20
SCHEMA_VERSION = 1
_INITIALIZATION_LOCK = threading.Lock()


def initialize_database(path: Path = DEFAULT_DATABASE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _INITIALIZATION_LOCK:
        with _open_connection(path) as connection:
            version = _schema_version(connection)
            if version == SCHEMA_VERSION:
                return
            if version != 0:
                raise RuntimeError(
                    f"Database schema {version} is not supported; delete {path} "
                    "and recreate it"
                )
            if _application_tables(connection):
                raise RuntimeError(
                    f"Existing database at {path} is not supported; delete it "
                    "and recreate it"
                )

            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = _schema_version(connection)
            if version == SCHEMA_VERSION:
                return
            if version != 0 or _application_tables(connection):
                raise RuntimeError(
                    f"Existing database at {path} is not supported; delete it "
                    "and recreate it"
                )
            _create_schema(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def is_database_initialized(path: Path = DEFAULT_DATABASE_PATH) -> bool:
    if not path.is_file():
        return False
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        version = _schema_version(connection)
        if version == SCHEMA_VERSION:
            return True
        if version == 0 and not _application_tables(connection):
            return False
        raise sqlite3.DatabaseError("Unsupported database schema")
    finally:
        connection.close()


@contextmanager
def database_connection(
    path: Path = DEFAULT_DATABASE_PATH,
    *,
    read_only: bool = False,
) -> Iterator[sqlite3.Connection]:
    with _open_connection(path, read_only=read_only) as connection:
        yield connection


@contextmanager
def _open_connection(
    path: Path,
    *,
    read_only: bool = False,
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro" if read_only else path,
        timeout=30,
        uri=read_only,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    statements = [
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE positions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 32),
            quantity REAL NOT NULL CHECK (quantity > 0),
            average_cost REAL NOT NULL CHECK (average_cost >= 0),
            account TEXT,
            asset_class TEXT,
            sector TEXT,
            acquired TEXT
        )
        """,
        "CREATE INDEX positions_user_id_idx ON positions(user_id)",
        """
        CREATE TABLE watchlist (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 32),
            sort_order INTEGER NOT NULL,
            PRIMARY KEY(user_id, symbol)
        ) WITHOUT ROWID
        """,
        """
        CREATE INDEX watchlist_user_order_idx
        ON watchlist(user_id, sort_order)
        """,
        f"""
        CREATE TRIGGER watchlist_size_limit
        BEFORE INSERT ON watchlist
        WHEN (
            SELECT COUNT(*) FROM watchlist WHERE user_id = NEW.user_id
        ) >= {MAX_WATCHLIST_SYMBOLS}
        BEGIN
            SELECT RAISE(ABORT, 'watchlist cannot exceed {MAX_WATCHLIST_SYMBOLS} symbols');
        END
        """,
        """
        CREATE TABLE securities (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL UNIQUE COLLATE NOCASE
                CHECK (length(symbol) BETWEEN 1 AND 32),
            company TEXT
        )
        """,
        """
        CREATE TABLE daily_bars (
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
        CREATE INDEX daily_bars_provider_date_idx
        ON daily_bars(provider, session_date)
        """,
        """
        CREATE TABLE universes (
            id TEXT PRIMARY KEY
        )
        """,
        """
        CREATE TABLE universe_memberships (
            universe_id TEXT NOT NULL REFERENCES universes(id) ON DELETE CASCADE,
            security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
            observed_on TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            PRIMARY KEY(universe_id, security_id, observed_on)
        ) WITHOUT ROWID
        """,
        """
        CREATE INDEX universe_memberships_observation_idx
        ON universe_memberships(universe_id, observed_on, sort_order)
        """,
    ]
    for statement in statements:
        connection.execute(statement)
    connection.execute(
        """
        INSERT INTO users(id)
        VALUES (?)
        """,
        (LOCAL_ADMIN_USER_ID,),
    )


def _schema_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def _application_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    ]
