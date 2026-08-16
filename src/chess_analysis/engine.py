"""Stockfish over UCI (PRD 4.3).

Fixed depth, never fixed time: time-based analysis returns different results on
each run, which makes evaluations and severity labels non-reproducible and reads
to the user as a bug.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

import chess
import chess.engine

from chess_analysis.evaluation import score_from_dict, score_to_dict

if TYPE_CHECKING:
    from chess_analysis.cache import EvalCache

DEFAULT_DEPTH = 20
DEFAULT_MULTIPV = 3


class EngineError(Exception):
    """Stockfish failed or returned something unusable."""


def find_engine() -> str | None:
    """Locate Stockfish: explicit override, then the vendored build, then PATH.

    Stockfish is not packaged for every distribution, so `scripts/fetch-stockfish.sh`
    drops a pinned build in `vendor/`. That path does not exist once the package
    is installed elsewhere (the container), where PATH or the override applies.
    """
    override = os.environ.get("STOCKFISH_PATH")
    if override:
        return override

    vendored = Path(__file__).resolve().parents[2] / "vendor" / "stockfish"
    if vendored.is_file() and os.access(vendored, os.X_OK):
        return str(vendored)

    return shutil.which("stockfish")


@dataclass(frozen=True)
class Line:
    """One candidate line. `score` is white-relative (see `evaluation`)."""

    move: chess.Move
    score: chess.engine.Score
    pv: tuple[chess.Move, ...] = field(default=())


@dataclass(frozen=True)
class PositionAnalysis:
    """MultiPV analysis of a single position, `lines` ranked best-first."""

    fen: str
    depth: int
    multipv: int
    lines: tuple[Line, ...]

    @property
    def best(self) -> Line:
        return self.lines[0]


def line_to_dict(line: Line) -> dict[str, Any]:
    """Serialise for the `lines` JSON columns. Moves are UCI: unlike SAN it
    needs no board to interpret."""
    return {
        "move": line.move.uci(),
        "score": score_to_dict(line.score),
        "pv": [move.uci() for move in line.pv],
    }


def line_from_dict(data: dict[str, Any]) -> Line:
    return Line(
        move=chess.Move.from_uci(data["move"]),
        score=score_from_dict(data["score"]),
        pv=tuple(chess.Move.from_uci(uci) for uci in data.get("pv", [])),
    )


class PositionEvaluator(Protocol):
    """What the analyzer needs from an engine, so tests can substitute one."""

    depth: int
    multipv: int

    def analyse(self, board: chess.Board) -> PositionAnalysis: ...


class StockfishEvaluator:
    """A single long-lived Stockfish process.

    One process configured with several threads, rather than several processes:
    parallel engines contend for the same cores and each gets a cold hash table.
    """

    def __init__(
        self,
        engine_path: str | None = None,
        *,
        depth: int = DEFAULT_DEPTH,
        multipv: int = DEFAULT_MULTIPV,
        threads: int | None = None,
        hash_mb: int | None = None,
        cache: EvalCache | None = None,
    ) -> None:
        resolved = engine_path or find_engine()
        if resolved is None:
            raise EngineError(
                "no Stockfish binary found — run scripts/fetch-stockfish.sh, "
                "or set STOCKFISH_PATH"
            )

        self.depth = depth
        self.multipv = multipv
        self._cache = cache
        self._engine = chess.engine.SimpleEngine.popen_uci(resolved)

        options: dict[str, int] = {}
        if threads is not None:
            options["Threads"] = threads
        if hash_mb is not None:
            options["Hash"] = hash_mb
        if options:
            self._engine.configure(options)

    def analyse(self, board: chess.Board) -> PositionAnalysis:
        if board.is_game_over():
            raise EngineError("cannot analyse a finished position")

        from chess_analysis.cache import cache_key

        key = cache_key(board, self.depth, self.multipv)
        if self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                # The key normalises move counters away, so the stored FEN is
                # whichever transposition got there first. Report this one's.
                return replace(cached, fen=board.fen())

        try:
            infos = self._engine.analyse(
                board,
                chess.engine.Limit(depth=self.depth),
                multipv=self.multipv,
            )
        except chess.engine.EngineError as exc:  # crashed or refused the position
            raise EngineError(str(exc)) from exc

        analysis = self._to_analysis(board, infos)
        if self._cache is not None:
            self._cache.put(key, analysis)
        return analysis

    def _to_analysis(
        self,
        board: chess.Board,
        infos: list[chess.engine.InfoDict],
    ) -> PositionAnalysis:
        lines: list[Line] = []
        # Stockfish returns fewer lines than requested when the position has
        # fewer legal moves, and orders them by the 1-based `multipv` field.
        for info in sorted(infos, key=lambda i: i.get("multipv", 1)):
            pv = info.get("pv")
            score = info.get("score")
            if not pv or score is None:
                continue
            lines.append(
                Line(move=pv[0], score=score.white(), pv=tuple(pv)),
            )

        if not lines:
            raise EngineError(f"no usable lines returned for {board.fen()}")

        return PositionAnalysis(
            fen=board.fen(),
            depth=self.depth,
            multipv=self.multipv,
            lines=tuple(lines),
        )

    def close(self) -> None:
        self._engine.quit()

    def __enter__(self) -> StockfishEvaluator:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
