"""Presenting candidate lines to a human (PRD 4.5).

Two things happen here, both display concerns rather than analysis ones, which
is why they run when a game is read rather than when it is analysed — retuning
them costs nothing, where changing what is stored would mean re-analysing.

1. UCI becomes SAN. The engine speaks `g1f3`; a player reads `Nf3`.
2. Converging variations collapse. At depth 18-20 the three principal
   variations frequently reach the same position by different move orders, and
   presenting one idea three times reads as noise rather than three plans.
"""

from __future__ import annotations

from typing import Any

import chess

# How deep to look for a transposition. Beyond a few plies, lines that meet
# again have usually taken genuinely different routes worth seeing separately.
DEFAULT_HORIZON = 4


def present_lines(
    fen: str,
    lines: list[dict[str, Any]],
    *,
    horizon: int = DEFAULT_HORIZON,
) -> list[dict[str, Any]]:
    """Annotate stored lines with SAN and merge move-order duplicates.

    Input lines are the stored shape — `move` and `pv` in UCI. Order is
    preserved, so the engine's ranking survives; a merged line is recorded as an
    alternative on the better-scoring line that absorbed it.
    """
    board = chess.Board(fen)
    prepared = [_prepare(board, line) for line in lines]

    kept: list[dict[str, Any]] = []
    for entry in prepared:
        if entry is None:
            continue
        absorbed = next(
            (k for k in kept if _converges(k["_positions"], entry["_positions"], horizon)),
            None,
        )
        if absorbed is None:
            kept.append(entry)
        else:
            absorbed["alternatives"].append(entry["san"])

    for entry in kept:
        del entry["_positions"]
    return kept


def _prepare(board: chess.Board, line: dict[str, Any]) -> dict[str, Any] | None:
    """Replay one line, collecting SAN and the position after each ply."""
    replay = board.copy()
    san: list[str] = []
    positions: list[str] = []

    for uci in line.get("pv", []):
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            break
        if move not in replay.legal_moves:
            # A stored line that no longer replays is truncated rather than
            # dropped: its first move is still the engine's recommendation.
            break
        san.append(replay.san(move))
        replay.push(move)
        positions.append(replay.epd())

    if not san:
        return None

    return {
        "move": line["move"],
        "san": san[0],
        "score": line["score"],
        "pv": list(line.get("pv", []))[: len(san)],
        "pv_san": san,
        "alternatives": [],
        "_positions": positions,
    }


def _converges(first: list[str], second: list[str], horizon: int) -> bool:
    """Do two lines stand in the same position at the same ply, early on?

    Ply 0 is skipped: two candidate moves are different by construction, so
    they cannot share a position after one move.
    """
    limit = min(len(first), len(second), horizon)
    return any(first[index] == second[index] for index in range(1, limit))
