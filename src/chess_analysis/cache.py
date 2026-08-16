"""Evaluation cache (PRD 4.3).

Openings repeat heavily across one player's archive, so caching by position —
not by game — is where most of the engine time is saved. The cache is global:
there is exactly one user, so there is nothing to scope it to.

The SQLite-backed implementation lands with the persistence layer; everything
here is written against the `EvalCache` protocol so it drops in unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Protocol

import chess

from chess_analysis.db import now, to_iso
from chess_analysis.engine import PositionAnalysis, line_from_dict, line_to_dict


# The fifty-move rule fires at 100 halfmoves. Well below that it cannot affect
# the search, so the clock is normalised away; near it the clock genuinely
# changes the evaluation and stays in the key.
_FIFTY_MOVE_HORIZON = 60


def cache_key(board: chess.Board, depth: int, multipv: int) -> tuple[str, int, int]:
    """Key an analysis by position, depth and MultiPV.

    The fullmove number is dropped, and the halfmove clock with it away from the
    fifty-move rule, so that a position reached by different move orders shares
    one entry. Those transposition hits are the whole point of the cache.
    """
    placement, turn, castling, ep_square, halfmove, _fullmove = board.fen().split(" ")
    clock = halfmove if int(halfmove) >= _FIFTY_MOVE_HORIZON else "0"
    position = " ".join([placement, turn, castling, ep_square, clock])
    return (position, depth, multipv)


class EvalCache(Protocol):
    def get(self, key: tuple[str, int, int]) -> PositionAnalysis | None: ...

    def put(self, key: tuple[str, int, int], analysis: PositionAnalysis) -> None: ...


class InMemoryEvalCache:
    """Process-local cache, useful for tests and single-game runs."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, int], PositionAnalysis] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[str, int, int]) -> PositionAnalysis | None:
        analysis = self._entries.get(key)
        if analysis is None:
            self.misses += 1
        else:
            self.hits += 1
        return analysis

    def put(self, key: tuple[str, int, int], analysis: PositionAnalysis) -> None:
        self._entries[key] = analysis

    def __len__(self) -> int:
        return len(self._entries)


class NullEvalCache:
    """Caches nothing. Use when measuring raw engine throughput."""

    def get(self, key: tuple[str, int, int]) -> PositionAnalysis | None:
        return None

    def put(self, key: tuple[str, int, int], analysis: PositionAnalysis) -> None:
        pass


class SqliteEvalCache:
    """The persistent cache. Global, single-user, survives restarts (PRD 4.3).

    Owns no connection of its own — it is handed the analysis worker's, since
    that is the only thread writing engine results.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[str, int, int]) -> PositionAnalysis | None:
        fen, depth, multipv = key
        row = self._conn.execute(
            "SELECT lines FROM eval_cache WHERE fen = ? AND depth = ? AND multipv = ?",
            (fen, depth, multipv),
        ).fetchone()

        if row is None:
            self.misses += 1
            return None

        self.hits += 1
        return PositionAnalysis(
            fen=fen,
            depth=depth,
            multipv=multipv,
            lines=tuple(line_from_dict(item) for item in json.loads(row["lines"])),
        )

    def put(self, key: tuple[str, int, int], analysis: PositionAnalysis) -> None:
        fen, depth, multipv = key
        self._conn.execute(
            """
            INSERT INTO eval_cache (fen, depth, multipv, lines, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (fen, depth, multipv) DO UPDATE SET
                lines = excluded.lines,
                created_at = excluded.created_at
            """,
            (
                fen,
                depth,
                multipv,
                json.dumps([line_to_dict(line) for line in analysis.lines]),
                to_iso(now()),
            ),
        )

    def __len__(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM eval_cache").fetchone()[0]
