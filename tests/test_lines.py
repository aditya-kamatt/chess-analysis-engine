import chess
import pytest

from chess_analysis.lines import present_lines

START = chess.Board().fen()


def line(*uci: str, cp: int = 20) -> dict:
    return {"move": uci[0], "score": {"cp": cp}, "pv": list(uci)}


def test_uci_becomes_san():
    """The engine speaks g1f3; a player reads Nf3."""
    result = present_lines(START, [line("g1f3", "d7d5", "d2d4")])

    assert result[0]["san"] == "Nf3"
    assert result[0]["pv_san"] == ["Nf3", "d5", "d4"]
    assert result[0]["move"] == "g1f3"  # UCI kept for the board


def test_scores_and_order_are_preserved():
    result = present_lines(
        START,
        [line("e2e4", cp=40), line("d2d4", cp=30), line("c2c4", cp=20)],
    )

    assert [entry["san"] for entry in result] == ["e4", "d4", "c4"]
    assert [entry["score"]["cp"] for entry in result] == [40, 30, 20]


def test_transpositions_collapse_into_one_line():
    """1.Nf3 d5 2.d4 and 1.d4 d5 2.Nf3 are one idea, not two plans (PRD 4.5)."""
    result = present_lines(
        START,
        [
            line("g1f3", "d7d5", "d2d4", cp=40),
            line("d2d4", "d7d5", "g1f3", cp=38),
        ],
    )

    assert len(result) == 1
    assert result[0]["san"] == "Nf3"  # the better-scoring line keeps its place
    assert result[0]["alternatives"] == ["d4"]


def test_genuinely_different_plans_are_kept_apart():
    result = present_lines(
        START,
        [
            line("e2e4", "e7e5", "g1f3", cp=40),
            line("d2d4", "d7d5", "c2c4", cp=35),
            line("c2c4", "e7e5", "b1c3", cp=30),
        ],
    )

    assert [entry["san"] for entry in result] == ["e4", "d4", "c4"]
    assert all(entry["alternatives"] == [] for entry in result)


def test_convergence_beyond_the_horizon_is_left_alone():
    """Lines that only meet much later took different routes worth seeing."""
    early = present_lines(
        START,
        [line("g1f3", "d7d5", "d2d4"), line("d2d4", "d7d5", "g1f3")],
        horizon=4,
    )
    assert len(early) == 1

    late = present_lines(
        START,
        [line("g1f3", "d7d5", "d2d4"), line("d2d4", "d7d5", "g1f3")],
        horizon=1,
    )
    assert len(late) == 2


def test_three_way_convergence_merges_into_one():
    result = present_lines(
        START,
        [
            line("g1f3", "d7d5", "d2d4", "g8f6", "c2c4", cp=40),
            line("d2d4", "d7d5", "g1f3", "g8f6", "c2c4", cp=38),
        ],
    )

    assert len(result) == 1
    assert result[0]["alternatives"] == ["d4"]


def test_a_line_that_no_longer_replays_is_truncated_not_dropped():
    """The first move is still the engine's recommendation."""
    result = present_lines(START, [line("e2e4", "e7e5", "e2e4")])

    assert result[0]["san"] == "e4"
    assert result[0]["pv_san"] == ["e4", "e5"]
    assert result[0]["pv"] == ["e2e4", "e7e5"]


def test_a_line_whose_first_move_is_illegal_is_dropped():
    result = present_lines(START, [line("e2e5"), line("e2e4")])

    assert [entry["san"] for entry in result] == ["e4"]


def test_malformed_uci_does_not_raise():
    assert present_lines(START, [line("not-a-move")]) == []


def test_works_from_a_midgame_position():
    board = chess.Board()
    for san in ("e4", "e5", "Nf3", "Nc6"):
        board.push_san(san)

    result = present_lines(board.fen(), [line("f1b5", "a7a6", "b5a4")])

    assert result[0]["san"] == "Bb5"
    assert result[0]["pv_san"] == ["Bb5", "a6", "Ba4"]


def test_empty_input():
    assert present_lines(START, []) == []


@pytest.mark.parametrize("horizon", [1, 2, 4, 8])
def test_output_never_grows(horizon):
    given = [line("e2e4", "e7e5"), line("d2d4", "d7d5"), line("c2c4", "e7e5")]
    assert len(present_lines(START, given, horizon=horizon)) <= len(given)
