"""Walk a game, evaluate every position, label the player's errors.

The central trick is that each position is analysed exactly once. The evaluation
of a *played* move at ply N is simply the evaluation of position N+1 — both are
white-relative, so no sign juggling is needed — which halves the engine work
versus evaluating the played move separately.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import chess
import chess.pgn
from chess.engine import Score

from chess_analysis.classify import DEFAULT_THRESHOLDS, Severity, Thresholds
from chess_analysis.engine import Line, PositionAnalysis, PositionEvaluator
from chess_analysis.evaluation import terminal_score, win_percent_loss

# Everything else is filtered at sync (PRD 7); this is a backstop.
_STANDARD_VARIANTS = {"", "standard", "chess"}


class UnanalysableGame(Exception):
    """The game cannot be analysed: no moves, or a variant we do not support."""


@dataclass(frozen=True)
class AnalysedPly:
    """One played move and the analysis of the position it was played from."""

    ply: int
    fen: str
    side_to_move: chess.Color
    played_move: chess.Move
    played_move_score: Score
    """White-relative evaluation *after* the played move."""
    lines: tuple[Line, ...]
    """Candidate lines for the position before the move, best first."""
    depth: int
    win_percent_loss: float
    severity: Severity | None
    """Set only for the analysed player's own moves (PRD 4.4)."""

    @property
    def best_score(self) -> Score:
        return self.lines[0].score


@dataclass(frozen=True)
class AnalysedGame:
    plies: tuple[AnalysedPly, ...]
    final_score: Score
    """White-relative evaluation of the final position."""
    depth: int

    def errors(self) -> tuple[AnalysedPly, ...]:
        return tuple(p for p in self.plies if p.severity is not None)


def analyse_game(
    game: chess.pgn.Game,
    evaluator: PositionEvaluator,
    *,
    player_color: chess.Color | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    progress: Callable[[int, int], None] | None = None,
) -> AnalysedGame:
    """Analyse every position in `game`'s mainline.

    `player_color` limits severity labels to that side's moves; None labels
    both. `progress` is called with (positions done, total) so long games can
    show a progress indicator (PRD 7).
    """
    variant = game.headers.get("Variant", "").strip().lower()
    if variant not in _STANDARD_VARIANTS:
        raise UnanalysableGame(f"unsupported variant: {game.headers['Variant']}")

    moves = list(game.mainline_moves())
    if not moves:
        raise UnanalysableGame("game has no moves")

    boards = _replay(game, moves)

    # A position the game ended in has no engine evaluation; its value is known
    # outright, so we score it directly and never hand it to Stockfish.
    analyses: list[PositionAnalysis | None] = []
    for index, board in enumerate(boards):
        analyses.append(None if board.is_game_over() else evaluator.analyse(board))
        if progress is not None:
            progress(index + 1, len(boards))

    def score_at(index: int) -> Score:
        analysis = analyses[index]
        return terminal_score(boards[index]) if analysis is None else analysis.best.score

    plies: list[AnalysedPly] = []
    for index, move in enumerate(moves):
        board = boards[index]
        mover = board.turn
        # Non-terminal by construction: a move was played from this position.
        analysis = analyses[index]
        assert analysis is not None

        after = score_at(index + 1)
        loss = win_percent_loss(analysis.best.score, after, mover)
        labelled = player_color is None or mover == player_color

        plies.append(
            AnalysedPly(
                ply=index,
                fen=board.fen(),
                side_to_move=mover,
                played_move=move,
                played_move_score=after,
                lines=analysis.lines,
                depth=analysis.depth,
                win_percent_loss=loss,
                severity=thresholds.classify(loss) if labelled else None,
            )
        )

    return AnalysedGame(
        plies=tuple(plies),
        final_score=score_at(len(boards) - 1),
        depth=evaluator.depth,
    )


def player_color_for(game: chess.pgn.Game, username: str) -> chess.Color | None:
    """Which side `username` played, matched case-insensitively on PGN headers."""
    target = username.strip().lower()
    if game.headers.get("White", "").strip().lower() == target:
        return chess.WHITE
    if game.headers.get("Black", "").strip().lower() == target:
        return chess.BLACK
    return None


def _replay(game: chess.pgn.Game, moves: list[chess.Move]) -> list[chess.Board]:
    """Every position in the mainline, starting position through final."""
    board = game.board()
    boards = [board.copy()]
    for move in moves:
        board.push(move)
        boards.append(board.copy())
    return boards
