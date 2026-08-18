"""Queries over the SQLite schema. Everything SQL lives here."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from chess_analysis.classify import Severity
from chess_analysis.db import from_iso, now, to_iso, transaction
from chess_analysis.engine import line_to_dict
from chess_analysis.evaluation import move_accuracy, score_to_dict
from chess_analysis.models import (
    AnalysisStatus,
    AnalysisSummary,
    Game,
    GameFilter,
    Platform,
    Result,
    Settings,
)

_SETTINGS_COLUMNS = (
    "chesscom_enabled",
    "chesscom_username",
    "chesscom_last_synced_at",
    "chesscom_backfill_cursor",
    "lichess_enabled",
    "lichess_username",
    "lichess_token",
    "lichess_last_synced_at",
    "lichess_backfill_cursor",
    "reveal_lines_by_default",
    "analysis_depth",
    "background_analysis",
)

_TIMESTAMP_COLUMNS = frozenset(
    {
        "chesscom_last_synced_at",
        "chesscom_backfill_cursor",
        "lichess_last_synced_at",
        "lichess_backfill_cursor",
    }
)

_BOOLEAN_COLUMNS = frozenset(
    {
        "chesscom_enabled",
        "lichess_enabled",
        "reveal_lines_by_default",
        "background_analysis",
    }
)


def load_settings(conn: sqlite3.Connection) -> Settings:
    row = conn.execute(
        f"SELECT {', '.join(_SETTINGS_COLUMNS)} FROM settings WHERE id = 1"
    ).fetchone()

    values: dict[str, Any] = {}
    for column in _SETTINGS_COLUMNS:
        value = row[column]
        if column in _TIMESTAMP_COLUMNS:
            values[column] = from_iso(value)
        elif column in _BOOLEAN_COLUMNS:
            values[column] = bool(value)
        else:
            values[column] = value
    return Settings(**values)


def save_settings(conn: sqlite3.Connection, **fields: Any) -> None:
    """Update only the named columns, leaving the rest untouched."""
    unknown = set(fields) - set(_SETTINGS_COLUMNS)
    if unknown:
        raise ValueError(f"unknown settings columns: {sorted(unknown)}")
    if not fields:
        return

    values = []
    for column, value in fields.items():
        if column in _TIMESTAMP_COLUMNS and isinstance(value, datetime):
            value = to_iso(value)
        elif column in _BOOLEAN_COLUMNS:
            value = int(bool(value))
        values.append(value)

    assignments = ", ".join(f"{column} = ?" for column in fields)
    conn.execute(f"UPDATE settings SET {assignments} WHERE id = 1", values)


def insert_games(conn: sqlite3.Connection, games: list[Game]) -> list[int]:
    """Insert games, ignoring ones already stored. Returns the new row ids.

    Re-syncing overlaps by design — Chess.com's current-month archive is fetched
    whole every time — so collisions are the normal case, not an error. The ids
    are what sync hands to the analysis queue.
    """
    inserted: list[int] = []
    with transaction(conn):
        for game in games:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO games (
                    platform, platform_game_id, played_at, time_control,
                    player_color, opponent, result, eco_code, pgn, url,
                    analysis_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(game.platform),
                    game.platform_game_id,
                    to_iso(game.played_at),
                    game.time_control,
                    game.player_color,
                    game.opponent,
                    str(game.result) if game.result else None,
                    game.eco_code,
                    game.pgn,
                    game.url,
                    str(game.analysis_status),
                ),
            )
            if cursor.rowcount and cursor.lastrowid is not None:
                inserted.append(cursor.lastrowid)
    return inserted


# Chess.com's own boundaries, on base time alone. Their published rule adds
# forty times the increment before comparing; ignoring the increment moves only
# games within a few seconds of a boundary, which is the wrong precision to
# argue about in a filter that exists to say "show me my blitz".
_TIME_CLASS_SQL = """
    CASE
        WHEN time_control LIKE '1/%' THEN 'daily'
        WHEN time_control GLOB '[0-9]*'
             AND CAST(time_control AS INTEGER) < 180 THEN 'bullet'
        WHEN time_control GLOB '[0-9]*'
             AND CAST(time_control AS INTEGER) < 600 THEN 'blitz'
        WHEN time_control GLOB '[0-9]*' THEN 'rapid'
    END
"""

_WITH_ERRORS_SQL = """
    EXISTS (
        SELECT 1 FROM positions
        WHERE positions.game_id = games.id AND positions.severity IS NOT NULL
    )
"""


def _where(game_filter: GameFilter | None) -> tuple[str, list[Any]]:
    """The WHERE clause for a filter, shared by the list and its count.

    They must not drift: a count taken under different conditions from the rows
    turns "showing 20 of 12" into a visible lie.
    """
    if game_filter is None:
        return "", []

    clauses: list[str] = []
    values: list[Any] = []

    if game_filter.player_color:
        clauses.append("player_color = ?")
        values.append(game_filter.player_color)
    if game_filter.result:
        clauses.append("result = ?")
        values.append(game_filter.result)
    if game_filter.time_class:
        clauses.append(f"{_TIME_CLASS_SQL} = ?")
        values.append(game_filter.time_class)
    if game_filter.with_errors:
        clauses.append(_WITH_ERRORS_SQL)

    if not clauses:
        return "", []
    return "WHERE " + " AND ".join(clauses), values


def list_games(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    game_filter: GameFilter | None = None,
) -> list[Game]:
    where, values = _where(game_filter)
    rows = conn.execute(
        f"""
        SELECT * FROM games
        {where}
        ORDER BY played_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*values, limit, offset],
    ).fetchall()
    return [_game_from_row(row) for row in rows]


def get_game(conn: sqlite3.Connection, game_id: int) -> Game | None:
    row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    return _game_from_row(row) if row else None


def count_games(
    conn: sqlite3.Connection,
    game_filter: GameFilter | None = None,
) -> int:
    where, values = _where(game_filter)
    return conn.execute(f"SELECT COUNT(*) FROM games {where}", values).fetchone()[0]


def oldest_played_at(
    conn: sqlite3.Connection,
    platform: Platform | None = None,
) -> datetime | None:
    """How far back the stored history goes, for the load-more indicator.

    Across every platform by default: the list mixes them, so "history loaded
    back to" is a fact about the list and not about one account. Narrowed to a
    platform when a sync needs that account's own backfill cursor.
    """
    if platform is None:
        row = conn.execute("SELECT MIN(played_at) FROM games").fetchone()
    else:
        row = conn.execute(
            "SELECT MIN(played_at) FROM games WHERE platform = ?",
            (str(platform),),
        ).fetchone()
    return from_iso(row[0]) if row and row[0] else None


def set_analysis_status(
    conn: sqlite3.Connection,
    game_id: int,
    status: AnalysisStatus,
) -> None:
    conn.execute(
        "UPDATE games SET analysis_status = ? WHERE id = ?", (str(status), game_id)
    )


def unanalysed_game_ids(conn: sqlite3.Connection) -> list[int]:
    """Games the queue still owes work on, oldest game last.

    `in_progress` counts as unfinished: it means the process died mid-analysis,
    and the row would otherwise sit in that state forever.
    """
    rows = conn.execute(
        """
        SELECT id FROM games
        WHERE analysis_status IN ('pending', 'in_progress')
        ORDER BY played_at DESC
        """
    ).fetchall()
    return [row["id"] for row in rows]


def save_analysis(conn: sqlite3.Connection, game_id: int, plies: list[Any]) -> None:
    """Replace a game's stored analysis with `plies` (a list of AnalysedPly).

    Delete-then-insert so re-analysing at a new depth cannot leave a mix of old
    and new rows behind.
    """
    with transaction(conn):
        conn.execute("DELETE FROM positions WHERE game_id = ?", (game_id,))
        conn.executemany(
            """
            INSERT INTO positions (
                game_id, ply, fen, side_to_move, played_move, played_move_eval,
                lines, depth, win_percent_loss, severity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    game_id,
                    ply.ply,
                    ply.fen,
                    "white" if ply.side_to_move else "black",
                    ply.played_move.uci(),
                    json.dumps(score_to_dict(ply.played_move_score)),
                    json.dumps([line_to_dict(line) for line in ply.lines]),
                    ply.depth,
                    ply.win_percent_loss,
                    str(ply.severity) if ply.severity else None,
                )
                for ply in plies
            ],
        )


def get_positions(conn: sqlite3.Connection, game_id: int) -> list[dict[str, Any]]:
    """Stored analysis for a game, ply order, decoded from JSON."""
    rows = conn.execute(
        "SELECT * FROM positions WHERE game_id = ? ORDER BY ply", (game_id,)
    ).fetchall()
    return [
        {
            "ply": row["ply"],
            "fen": row["fen"],
            "side_to_move": row["side_to_move"],
            "played_move": row["played_move"],
            "played_move_eval": json.loads(row["played_move_eval"]),
            "lines": json.loads(row["lines"]),
            "depth": row["depth"],
            "win_percent_loss": row["win_percent_loss"],
            "severity": row["severity"],
        }
        for row in rows
    ]


def analysis_summaries(
    conn: sqlite3.Connection,
    game_ids: Sequence[int],
) -> dict[int, AnalysisSummary]:
    """Summarise several games' analysis at once, keyed by game id.

    Batched because the game list needs one of these per row: fifty separate
    aggregate queries to decide which game is worth reviewing would be fifty
    round trips to answer one screen.

    Games with no stored analysis are absent from the result rather than present
    with zeroes — nothing has been measured yet, which is not the same as a game
    played without error.
    """
    ids = list(game_ids)
    if not ids:
        return {}

    placeholders = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT positions.game_id, positions.severity, positions.win_percent_loss
        FROM positions
        JOIN games ON games.id = positions.game_id
        WHERE positions.game_id IN ({placeholders})
          AND (games.player_color IS NULL
               OR positions.side_to_move = games.player_color)
        """,
        ids,
    ).fetchall()

    by_game: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_game[row["game_id"]].append(row)

    return {game_id: _summarise(moves) for game_id, moves in by_game.items()}


def _summarise(moves: list[sqlite3.Row]) -> AnalysisSummary:
    """Aggregate one game's player moves. `moves` is never empty."""
    counts = Counter(row["severity"] for row in moves)
    losses = [row["win_percent_loss"] for row in moves]

    return AnalysisSummary(
        moves=len(moves),
        inaccuracies=counts[Severity.INACCURACY],
        mistakes=counts[Severity.MISTAKE],
        blunders=counts[Severity.BLUNDER],
        average_loss=sum(losses) / len(losses),
        # The mean of the per-move curve, not the curve of the mean loss: one
        # catastrophic move and a game of quiet drift average the same win
        # percentage away but are not equally accurate.
        accuracy=sum(move_accuracy(loss) for loss in losses) / len(losses),
    )


def get_http_cache(
    conn: sqlite3.Connection,
    url: str,
) -> tuple[str | None, str | None]:
    """Stored ETag and Last-Modified for a URL, for conditional requests."""
    row = conn.execute(
        "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
    ).fetchone()
    return (row["etag"], row["last_modified"]) if row else (None, None)


def put_http_cache(
    conn: sqlite3.Connection,
    url: str,
    etag: str | None,
    last_modified: str | None,
) -> None:
    if etag is None and last_modified is None:
        return
    conn.execute(
        """
        INSERT INTO http_cache (url, etag, last_modified, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (url) DO UPDATE SET
            etag = excluded.etag,
            last_modified = excluded.last_modified,
            fetched_at = excluded.fetched_at
        """,
        (url, etag, last_modified, to_iso(now())),
    )


def _game_from_row(row: sqlite3.Row) -> Game:
    played_at = from_iso(row["played_at"])
    assert played_at is not None  # NOT NULL in the schema
    return Game(
        id=row["id"],
        platform=Platform(row["platform"]),
        platform_game_id=row["platform_game_id"],
        played_at=played_at,
        time_control=row["time_control"],
        player_color=row["player_color"],
        opponent=row["opponent"],
        result=Result(row["result"]) if row["result"] else None,
        eco_code=row["eco_code"],
        pgn=row["pgn"],
        url=row["url"],
        analysis_status=AnalysisStatus(row["analysis_status"]),
    )
