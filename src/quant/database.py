from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, closing
from pathlib import Path


DEFAULT_DATABASE_PATH = Path("data/quant.db")
LOCAL_ADMIN_USER_ID = "local-admin"
MAX_WATCHLIST_SYMBOLS = 20
SCHEMA_VERSION = 2
_INITIALIZATION_LOCK = threading.Lock()


def initialize_database(path: Path = DEFAULT_DATABASE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _INITIALIZATION_LOCK:
        with _open_connection(path) as connection:
            version = _schema_version(connection)
            if version == SCHEMA_VERSION:
                return
            if version not in {0, 1}:
                raise RuntimeError(
                    f"Database schema {version} is not supported by this version of Quant"
                )
            if version == 0 and _application_tables(connection):
                raise RuntimeError(
                    f"Existing unversioned database at {path} is not supported; preserve it and use an explicit migration"
                )

            if version == 1:
                backup_database(path, path.with_name(f"{path.name}.v1-{uuid.uuid4().hex}.backup"))
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = _schema_version(connection)
            if version == SCHEMA_VERSION:
                return
            if version not in {0, 1} or (version == 0 and _application_tables(connection)):
                raise RuntimeError(
                    f"Database at {path} changed during initialization; preserve it and inspect its schema"
                )
            if version == 0:
                _create_schema(connection)
            _migrate_v2(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def backup_database(source: Path, destination: Path) -> Path:
    """Create an exclusive, verified online backup; never replace an existing file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb"):
        pass
    try:
        with _open_connection(source, read_only=True) as original:
            with closing(sqlite3.connect(destination)) as backup:
                original.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("Backup integrity check failed")
                if backup.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.DatabaseError("Backup foreign key check failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _migrate_v2(connection: sqlite3.Connection) -> None:
    for statement in (
        "ALTER TABLE securities ADD COLUMN currency TEXT",
        "ALTER TABLE securities ADD COLUMN instrument_type TEXT",
        "ALTER TABLE securities ADD COLUMN calendar TEXT",
        """CREATE TABLE provider_symbols (
            security_id TEXT NOT NULL REFERENCES securities(id), provider TEXT NOT NULL,
            provider_symbol TEXT NOT NULL, PRIMARY KEY(security_id, provider),
            UNIQUE(provider, provider_symbol))""",
        """CREATE TABLE ingestion_runs (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, universe_source TEXT NOT NULL)""",
        """CREATE TABLE ingestion_jobs (
            run_id TEXT NOT NULL REFERENCES ingestion_runs(id), symbol TEXT NOT NULL,
            start_date TEXT NOT NULL, reasons TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, saved_rows INTEGER NOT NULL DEFAULT 0,
            error TEXT, PRIMARY KEY(run_id, symbol))""",
        """CREATE TABLE data_issues (
            symbol TEXT NOT NULL, code TEXT NOT NULL, session_date TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, resolved_at TEXT,
            detail TEXT NOT NULL, PRIMARY KEY(symbol, code, session_date))""",
        """CREATE TABLE sync_coverage (
            symbol TEXT PRIMARY KEY, requested_start TEXT NOT NULL,
            checked_at TEXT NOT NULL)""",
        """CREATE TABLE records (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL, import_key TEXT NOT NULL, digest TEXT NOT NULL,
            created_at TEXT NOT NULL, payload TEXT NOT NULL,
            UNIQUE(user_id, kind, import_key))""",
        "CREATE INDEX records_kind_user ON records(user_id, kind, created_at, id)",
        """CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
            operation TEXT NOT NULL, record_id TEXT NOT NULL, created_at TEXT NOT NULL)""",
    ):
        connection.execute(statement)


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
