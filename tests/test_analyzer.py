import io

import chess
import chess.pgn
import pytest
from chess.engine import Cp, Mate

from chess_analysis.analyzer import (
    UnanalysableGame,
    analyse_game,
    player_color_for,
)
from chess_analysis.classify import Severity
from chess_analysis.evaluation import pov, win_percent
from tests.fakes import FakeEvaluator

FOOLS_MATE = '[White "alice"]\n[Black "bob"]\n\n1. f3 e5 2. g4 Qh4# 0-1'


def load(pgn: str) -> chess.pgn.Game:
    game = chess.pgn.read_game(io.StringIO(pgn))
    assert game is not None
    return game


def positions_of(game: chess.pgn.Game) -> list[str]:
    """FEN of every position in the mainline, starting position first."""
    board = game.board()
    fens = [board.fen()]
    for move in game.mainline_moves():
        board.push(move)
        fens.append(board.fen())
    return fens


def test_every_position_is_analysed_exactly_once():
    """The played move's evaluation is read from the next position, not
    computed separately — so a 4-ply game costs 4 analyses, not 8."""
    game = load(FOOLS_MATE)
    evaluator = FakeEvaluator()

    analyse_game(game, evaluator)

    # Five positions, but the final one is checkmate and never reaches Stockfish.
    assert len(evaluator.calls) == 4
    assert len(set(evaluator.calls)) == 4


def test_played_move_score_is_the_next_positions_evaluation():
    game = load(FOOLS_MATE)
    fens = positions_of(game)
    evaluator = FakeEvaluator({fens[1]: Cp(-40), fens[2]: Cp(15), fens[3]: Cp(-250)})

    result = analyse_game(game, evaluator)

    assert result.plies[0].played_move_score == Cp(-40)
    assert result.plies[1].played_move_score == Cp(15)
    assert result.plies[2].played_move_score == Cp(-250)


def test_blunder_is_labelled_for_the_player():
    game = load(FOOLS_MATE)
    fens = positions_of(game)
    # White stands slightly better, then plays g4 and is mated next move.
    evaluator = FakeEvaluator({fens[2]: Cp(50), fens[3]: Mate(-1)})

    result = analyse_game(game, evaluator, player_color=chess.WHITE)

    g4 = result.plies[2]
    assert g4.severity == Severity.BLUNDER
    assert g4.win_percent_loss > 50


def test_opponent_moves_are_never_labelled():
    """Severity applies to the player's own moves only (PRD 4.4)."""
    game = load(FOOLS_MATE)
    fens = positions_of(game)
    evaluator = FakeEvaluator({fens[1]: Cp(0), fens[2]: Mate(4)})

    result = analyse_game(game, evaluator, player_color=chess.WHITE)

    black_plies = [p for p in result.plies if p.side_to_move == chess.BLACK]
    assert all(p.severity is None for p in black_plies)
    assert any(p.win_percent_loss > 0 for p in black_plies)  # loss still measured


def test_both_sides_labelled_when_no_player_given():
    game = load(FOOLS_MATE)
    fens = positions_of(game)
    evaluator = FakeEvaluator({fens[1]: Cp(0), fens[2]: Mate(4)})

    result = analyse_game(game, evaluator)

    assert any(p.side_to_move == chess.BLACK and p.severity for p in result.plies)


def test_delivering_mate_is_not_an_error():
    game = load(FOOLS_MATE)
    evaluator = FakeEvaluator(default=Cp(0))

    result = analyse_game(game, evaluator)

    mating_move = result.plies[-1]
    assert mating_move.win_percent_loss == 0.0
    assert mating_move.severity is None


def test_final_score_of_a_checkmated_game():
    game = load(FOOLS_MATE)

    result = analyse_game(game, FakeEvaluator())

    assert win_percent(pov(result.final_score, chess.WHITE)) == 0.0


def test_progress_is_reported_per_position():
    game = load(FOOLS_MATE)
    seen: list[tuple[int, int]] = []

    analyse_game(game, FakeEvaluator(), progress=lambda d, t: seen.append((d, t)))

    assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


def test_ply_metadata_matches_the_game():
    game = load(FOOLS_MATE)

    result = analyse_game(game, FakeEvaluator(depth=18))

    assert [p.ply for p in result.plies] == [0, 1, 2, 3]
    assert [p.side_to_move for p in result.plies] == [
        chess.WHITE,
        chess.BLACK,
        chess.WHITE,
        chess.BLACK,
    ]
    assert all(p.depth == 18 for p in result.plies)
    assert result.depth == 18


def test_errors_returns_only_labelled_plies():
    game = load(FOOLS_MATE)
    fens = positions_of(game)
    evaluator = FakeEvaluator({fens[2]: Cp(50), fens[3]: Mate(-1)})

    result = analyse_game(game, evaluator, player_color=chess.WHITE)

    assert [p.ply for p in result.errors()] == [2]


def test_game_with_no_moves_is_unanalysable():
    """Aborted games are stored but excluded from the queue (PRD 7)."""
    with pytest.raises(UnanalysableGame):
        analyse_game(load('[White "alice"]\n\n*'), FakeEvaluator())


def test_variants_are_rejected():
    pgn = '[Variant "Chess960"]\n[FEN "bqnrkrnb/pppppppp/8/8/8/8/PPPPPPPP/BQNRKRNB w KQkq - 0 1"]\n\n1. e4 *'
    with pytest.raises(UnanalysableGame, match="Chess960"):
        analyse_game(load(pgn), FakeEvaluator())


@pytest.mark.parametrize(
    ("username", "expected"),
    [("alice", chess.WHITE), ("BOB", chess.BLACK), ("carol", None)],
)
def test_player_color_for(username, expected):
    assert player_color_for(load(FOOLS_MATE), username) == expected
