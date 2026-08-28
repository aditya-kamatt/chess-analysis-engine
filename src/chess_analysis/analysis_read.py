"""The read model for one analysed game.

The stored Position sequence carries meaning across adjacent rows: the next
row contains the refutation of this move and the previous row identifies a
recapture.  Keep that implementation behind this module's interface so HTTP
code only requests ready-to-render positions.
"""

from __future__ import annotations

from typing import Any

from chess_analysis.evaluation import score_from_dict, win_percent
from chess_analysis.explain import explain_error
from chess_analysis.lines import present_lines


def present_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach display values and explanations to a stored Position sequence."""
    return [_present_position(positions, index) for index in range(len(positions))]


def _present_position(positions: list[dict[str, Any]], index: int) -> dict[str, Any]:
    position = positions[index]
    following = positions[index + 1] if index + 1 < len(positions) else None
    best = position["lines"][0]["score"] if position["lines"] else None

    return {
        **position,
        "lines": present_lines(position["fen"], position["lines"]),
        "eval": best,
        "eval_win_percent": (
            win_percent(score_from_dict(best)) if best is not None else None
        ),
        "played_win_percent": win_percent(
            score_from_dict(position["played_move_eval"])
        ),
        "explanation": (
            explain_error(
                position["fen"],
                position["played_move"],
                position["lines"],
                refutation=following["lines"] if following else None,
                win_percent_loss=position["win_percent_loss"],
                last_move=positions[index - 1]["played_move"] if index else None,
            )
            if position["severity"]
            else None
        ),
    }
