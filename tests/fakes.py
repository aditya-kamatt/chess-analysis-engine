"""A scripted evaluator, so the analyzer can be tested without Stockfish."""

from __future__ import annotations

import chess
from chess.engine import Cp, Score

from chess_analysis.engine import Line, PositionAnalysis


class FakeEvaluator:
    """Returns pre-set white-relative scores, keyed by FEN.

    Candidate moves are taken from the position's legal moves in whatever order
    python-chess yields them; only `lines[0].score` matters to the analyzer.
    """

    def __init__(
        self,
        scores: dict[str, Score] | None = None,
        *,
        default: Score = Cp(0),
        depth: int = 20,
        multipv: int = 3,
    ) -> None:
        self.scores = scores or {}
        self.default = default
        self.depth = depth
        self.multipv = multipv
        self.calls: list[str] = []

    def analyse(self, board: chess.Board) -> PositionAnalysis:
        fen = board.fen()
        self.calls.append(fen)
        score = self.scores.get(fen, self.default)

        moves = list(board.legal_moves)[: self.multipv]
        lines = tuple(Line(move=m, score=score, pv=(m,)) for m in moves)
        return PositionAnalysis(
            fen=fen,
            depth=self.depth,
            multipv=self.multipv,
            lines=lines,
        )
