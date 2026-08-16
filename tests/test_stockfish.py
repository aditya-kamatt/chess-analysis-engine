"""Integration tests against a real Stockfish binary.

Skipped when no engine is on PATH, so the unit suite stays runnable anywhere.
Run at a shallow depth — this is checking the UCI plumbing, not playing strength.
"""

from __future__ import annotations

import chess
import chess.pgn
import pytest

from chess_analysis.analyzer import analyse_game
from chess_analysis.cache import InMemoryEvalCache
from chess_analysis.engine import EngineError, StockfishEvaluator, find_engine
from chess_analysis.evaluation import pov, win_percent

pytestmark = pytest.mark.skipif(
    find_engine() is None,
    reason="no stockfish binary — run scripts/fetch-stockfish.sh",
)

SHALLOW = 8


@pytest.fixture
def evaluator():
    with StockfishEvaluator(depth=SHALLOW, multipv=3, threads=1, hash_mb=64) as engine:
        yield engine


def test_returns_three_ranked_lines(evaluator):
    analysis = evaluator.analyse(chess.Board())

    assert len(analysis.lines) == 3
    assert analysis.depth == SHALLOW
    # Ranked best-first from the mover's point of view; white moves first here.
    scores = [win_percent(pov(line.score, chess.WHITE)) for line in analysis.lines]
    assert scores == sorted(scores, reverse=True)


def test_stores_the_full_principal_variation(evaluator):
    """Not just the first move (PRD 4.3) — the board plays the line out."""
    analysis = evaluator.analyse(chess.Board())

    for line in analysis.lines:
        assert len(line.pv) > 1
        assert line.pv[0] == line.move
        chess.Board().variation_san(line.pv)  # raises if the PV is not legal


def test_scores_are_white_relative(evaluator):
    """Black to move in a position where white is up a queen: still positive."""
    board = chess.Board("rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1")

    analysis = evaluator.analyse(board)

    assert win_percent(pov(analysis.best.score, chess.WHITE)) > 90


def test_fixed_depth_is_reproducible(evaluator):
    """The reason for fixed depth over fixed time (PRD 4.3)."""
    board = chess.Board()
    board.push_san("e4")

    first = evaluator.analyse(board)
    second = StockfishEvaluator(depth=SHALLOW, multipv=3, threads=1, hash_mb=64)
    try:
        assert second.analyse(board).lines[0].score == first.lines[0].score
    finally:
        second.close()


def test_finds_forced_mate(evaluator):
    # Back-rank mate in one: Rd8#.
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1")

    best = evaluator.analyse(board).best

    assert best.move == chess.Move.from_uci("d1d8")
    assert best.score.mate() == 1


def test_refuses_a_finished_position(evaluator):
    board = chess.Board()
    for move in ("f3", "e5", "g4", "Qh4#"):
        board.push_san(move)

    with pytest.raises(EngineError):
        evaluator.analyse(board)


def test_analyses_a_real_game_end_to_end():
    with open("games/opera-game.pgn") as handle:
        game = chess.pgn.read_game(handle)

    cache = InMemoryEvalCache()
    with StockfishEvaluator(depth=SHALLOW, multipv=3, threads=1, cache=cache) as engine:
        result = analyse_game(game, engine, player_color=chess.WHITE)

    assert len(result.plies) == 33
    # Morphy mates, so the final position is a win for white.
    assert win_percent(pov(result.final_score, chess.WHITE)) == 100.0
    # Black is losing badly by the end but is never labelled: white is the player.
    assert all(p.severity is None for p in result.plies if p.side_to_move == chess.BLACK)


def test_cache_short_circuits_repeat_positions():
    board = chess.Board()
    cache = InMemoryEvalCache()

    with StockfishEvaluator(depth=SHALLOW, cache=cache) as engine:
        engine.analyse(board)
        engine.analyse(board)

    assert (cache.hits, cache.misses) == (1, 1)
