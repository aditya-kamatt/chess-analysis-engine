"""Score conventions and the win-percentage model (PRD 4.4, 4.5).

Two conventions hold everywhere below this module:

1. Stored scores are *white-relative*. Stockfish reports relative to the side to
   move, so we flip once at the engine boundary. Comparing the evaluation of two
   consecutive positions then needs no sign fix, which is what makes deriving a
   played move's evaluation from the next position cheap and safe.
2. Win percentage is always asked from a named side's point of view:
   ``win_percent(pov(score, color))``.

The same sigmoid backs both move classification and the evaluation bar fill, so
the two can never drift apart.
"""

from __future__ import annotations

import math
from typing import Any

import chess
from chess.engine import Cp, Mate, MateGiven, Score

# Lichess' win-percentage model. A pawn is worth much more near equality than it
# is at +9, which is exactly why raw centipawn loss is a poor severity signal.
WIN_PERCENT_SCALE = 0.00368208

# Lichess' accuracy curve, fitted so that giving up nothing scores 100 and the
# figure decays fast through the range where errors actually live. It runs off
# the same win-percentage loss that assigns severity, so a game cannot report a
# high accuracy and a page full of blunders.
_ACCURACY_SCALE = 103.1668
_ACCURACY_DECAY = 0.04354
_ACCURACY_OFFSET = 3.1669

# Large enough that a forced mate at any realistic distance still outranks every
# centipawn score, used only to recover the sign of a mate score.
_MATE_SCORE = 100_000


def pov(score: Score, color: chess.Color) -> Score:
    """Convert a white-relative score to `color`'s point of view."""
    return score if color == chess.WHITE else -score


def win_percent(score: Score) -> float:
    """Win probability (0-100) for the side `score` is relative to.

    Mate scores pin to the extremes rather than running through the sigmoid, so
    the evaluation bar fills completely on a forced mate (PRD 4.5).
    """
    if score.is_mate():
        # Mate(0) is "side to move is mated", MateGiven its negation. Both report
        # mate() == 0, so use the signed score to tell them apart.
        return 100.0 if score.score(mate_score=_MATE_SCORE) > 0 else 0.0

    centipawns = score.score()
    assert centipawns is not None  # not a mate score, so always present
    return 50 + 50 * (2 / (1 + math.exp(-WIN_PERCENT_SCALE * centipawns)) - 1)


def win_percent_loss(before: Score, after: Score, mover: chess.Color) -> float:
    """Win percentage the `mover` gave up, from white-relative scores.

    `before` is the position's best available evaluation, `after` the evaluation
    once the played move is on the board. Clamped at zero: a played move that
    beats the engine's own first line is noise from search instability, not a
    gain worth reporting.
    """
    return max(
        0.0,
        win_percent(pov(before, mover)) - win_percent(pov(after, mover)),
    )


def move_accuracy(loss: float) -> float:
    """Accuracy (0-100) for one move that gave up `loss` win percent.

    Giving up nothing scores 99.9999 rather than a clean 100 — the curve is
    fitted, not constructed — which rounds to 100 everywhere it is displayed.
    The clamp matters at the other end: past about 80 win percent given up the
    curve goes negative, and a move can only be as bad as zero.
    """
    raw = _ACCURACY_SCALE * math.exp(-_ACCURACY_DECAY * loss) - _ACCURACY_OFFSET
    return max(0.0, min(100.0, raw))


def terminal_score(board: chess.Board) -> Score:
    """White-relative score for a position the game has already ended in."""
    if board.is_checkmate():
        # The side to move is mated; PovScore flips that to white-relative.
        return chess.engine.PovScore(Mate(0), board.turn).white()
    return Cp(0)


def score_to_dict(score: Score) -> dict[str, Any]:
    """Serialise a score for the `lines` JSON columns (PRD 5)."""
    if score.is_mate():
        if score.score(mate_score=_MATE_SCORE) > 0 and score.mate() == 0:
            # Mate already delivered: the side to move has been checkmated.
            return {"mate": 0, "mate_given": True}
        return {"mate": score.mate()}
    return {"cp": score.score()}


def score_from_dict(data: dict[str, Any]) -> Score:
    """Inverse of `score_to_dict`."""
    if "mate" in data and data["mate"] is not None:
        if data.get("mate_given"):
            return MateGiven
        return Mate(data["mate"])
    return Cp(data["cp"])
