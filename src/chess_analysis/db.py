"""SQLite storage (PRD 5, 6).

One user, one file, no concurrency pressure — but the analysis worker writes
from its own thread while requests read, so WAL mode and a busy timeout are not
optional.

Migrations are a plain list keyed off `PRAGMA user_version`. Alembic is the
usual answer, but it is built around SQLAlchemy models and there are none here;
twenty lines of DDL list is the smaller thing that actually fits.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB_PATH = Path("data/chess.db")

_INIT_LOCK = threading.Lock()

_MIGRATIONS: list[str] = [
    # v0 -> v1
    """
    CREATE TABLE settings (
        id                       INTEGER PRIMARY KEY CHECK (id = 1),
        chesscom_enabled         INTEGER NOT NULL DEFAULT 0,
        chesscom_username        TEXT,
        chesscom_last_synced_at  TEXT,
        chesscom_backfill_cursor TEXT,
        lichess_enabled          INTEGER NOT NULL DEFAULT 0,
        lichess_username         TEXT,
        lichess_token            TEXT,
        lichess_last_synced_at   TEXT,
        lichess_backfill_cursor  TEXT,
        reveal_lines_by_default  INTEGER NOT NULL DEFAULT 0,
        analysis_depth           INTEGER NOT NULL DEFAULT 20
    );
    INSERT OR IGNORE INTO settings (id) VALUES (1);

    CREATE TABLE games (
        id               INTEGER PRIMARY KEY,
        platform         TEXT NOT NULL,
        platform_game_id TEXT NOT NULL,
        played_at        TEXT NOT NULL,
        time_control     TEXT,
        player_color     TEXT,
        opponent         TEXT,
        result           TEXT,
        eco_code         TEXT,
        pgn              TEXT NOT NULL,
        url              TEXT,
        analysis_status  TEXT NOT NULL DEFAULT 'pending',
        UNIQUE (platform, platform_game_id)
    );
    CREATE INDEX games_played_at ON games (played_at DESC);
    CREATE INDEX games_status ON games (analysis_status);

    CREATE TABLE positions (
        id               INTEGER PRIMARY KEY,
        game_id          INTEGER NOT NULL REFERENCES games (id) ON DELETE CASCADE,
        ply              INTEGER NOT NULL,
        fen              TEXT NOT NULL,
        side_to_move     TEXT NOT NULL,
        played_move      TEXT NOT NULL,
        played_move_eval TEXT NOT NULL,
        lines            TEXT NOT NULL,
        depth            INTEGER NOT NULL,
        win_percent_loss REAL NOT NULL,
        severity         TEXT,
        UNIQUE (game_id, ply)
    );

    CREATE TABLE eval_cache (
        fen        TEXT NOT NULL,
        depth      INTEGER NOT NULL,
        multipv    INTEGER NOT NULL,
        lines      TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (fen, depth, multipv)
    );

    -- Conditional-request state, so a re-sync of an unchanged Chess.com archive
    -- costs one 304 rather than a full download (PRD 4.2).
    CREATE TABLE http_cache (
        url           TEXT PRIMARY KEY,
        etag          TEXT,
        last_modified TEXT,
        fetched_at    TEXT NOT NULL
    );
    """,
    # v1 -> v2: opening a game analyses it on demand, so analysing the whole
    # archive up front becomes optional (PRD 10, question 1).
    """
    ALTER TABLE settings
        ADD COLUMN background_analysis INTEGER NOT NULL DEFAULT 1;
    """,
]


def default_path() -> Path:
    return Path(os.environ.get("CHESS_ANALYSIS_DB", DEFAULT_DB_PATH))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, creating and migrating it if needed."""
    resolved = Path(path) if path is not None else default_path()
    if resolved != Path(":memory:"):
        resolved.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(resolved, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # busy_timeout first, so it governs every statement after it.
    conn.execute("PRAGMA busy_timeout = 5000")

    # Switching to WAL takes an exclusive lock and does not always defer to the
    # busy handler, so a timeout alone is not enough: the analysis worker and a
    # request opening a fresh database at once collide on "database is locked".
    # One process owns this file, so serialising setup here is sufficient.
    with _INIT_LOCK:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index in range(version, len(_MIGRATIONS)):
        conn.executescript(_MIGRATIONS[index])
        # PRAGMA does not take parameters; the value is a list index.
        conn.execute(f"PRAGMA user_version = {index + 1}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def to_iso(value: datetime | None) -> str | None:
    """Timestamps are stored as UTC ISO-8601 so they sort lexicographically."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def now() -> datetime:
    return datetime.now(UTC)
