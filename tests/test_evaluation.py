import chess
import pytest
from chess.engine import Cp, Mate, MateGiven

from chess_analysis.evaluation import (
    pov,
    score_from_dict,
    score_to_dict,
    terminal_score,
    win_percent,
    win_percent_loss,
)


def test_equal_position_is_fifty_percent():
    assert win_percent(Cp(0)) == pytest.approx(50.0)


def test_win_percent_is_symmetric():
    for centipawns in (25, 100, 350, 900):
        assert win_percent(Cp(centipawns)) + win_percent(Cp(-centipawns)) == pytest.approx(100.0)


def test_win_percent_is_monotonic():
    values = [win_percent(Cp(cp)) for cp in (-500, -100, 0, 100, 500)]
    assert values == sorted(values)


def test_a_pawn_matters_more_near_equality():
    """The reason classification uses win% and not raw centipawn loss."""
    near_equal = win_percent(Cp(0)) - win_percent(Cp(-100))
    already_winning = win_percent(Cp(900)) - win_percent(Cp(800))
    assert near_equal > already_winning


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (Mate(1), 100.0),
        (Mate(5), 100.0),
        (Mate(-1), 0.0),
        (Mate(-5), 0.0),
        (Mate(0), 0.0),  # side to move is checkmated
        (MateGiven, 100.0),  # side to move has just delivered mate
    ],
)
def test_mate_scores_pin_to_the_extremes(score, expected):
    assert win_percent(score) == expected


def test_pov_flips_for_black_only():
    score = Cp(120)
    assert pov(score, chess.WHITE) == score
    assert pov(score, chess.BLACK) == Cp(-120)


def test_loss_is_measured_from_the_mover():
    before, after = Cp(0), Cp(-200)  # white-relative
    white_loss = win_percent_loss(before, after, chess.WHITE)
    black_loss = win_percent_loss(before, after, chess.BLACK)
    assert white_loss > 0
    assert black_loss == 0.0  # black gained, so nothing to report


def test_loss_never_goes_negative():
    assert win_percent_loss(Cp(0), Cp(50), chess.WHITE) == 0.0


def test_terminal_score_for_checkmate():
    # Fool's mate: white is checkmated, so white-relative is "white is mated".
    board = chess.Board()
    for move in ("f3", "e5", "g4", "Qh4#"):
        board.push_san(move)

    score = terminal_score(board)
    assert win_percent(pov(score, chess.WHITE)) == 0.0
    assert win_percent(pov(score, chess.BLACK)) == 100.0


def test_terminal_score_for_stalemate():
    board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert board.is_stalemate()
    assert win_percent(terminal_score(board)) == pytest.approx(50.0)


@pytest.mark.parametrize(
    "score",
    [Cp(0), Cp(-350), Cp(42), Mate(3), Mate(-2), Mate(0), MateGiven],
)
def test_serialisation_round_trips(score):
    restored = score_from_dict(score_to_dict(score))
    assert win_percent(restored) == win_percent(score)
    assert restored.mate() == score.mate()
