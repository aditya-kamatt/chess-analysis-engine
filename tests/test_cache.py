import chess
from chess.engine import Cp

from chess_analysis.cache import InMemoryEvalCache, cache_key
from chess_analysis.engine import Line, PositionAnalysis


def board_after(*moves: str) -> chess.Board:
    board = chess.Board()
    for move in moves:
        board.push_san(move)
    return board


def test_transpositions_share_one_entry():
    """The opening repeats constantly across one player's games, and it repeats
    by transposition as often as by move order."""
    a = board_after("Nf3", "d5", "d4")
    b = board_after("d4", "d5", "Nf3")
    assert a.fen() != b.fen()  # differing halfmove clock and move number
    assert cache_key(a, 20, 3) == cache_key(b, 20, 3)


def test_depth_and_multipv_are_part_of_the_key():
    board = board_after("e4")
    assert cache_key(board, 20, 3) != cache_key(board, 18, 3)
    assert cache_key(board, 20, 3) != cache_key(board, 20, 1)


def test_distinct_positions_do_not_collide():
    assert cache_key(board_after("e4"), 20, 3) != cache_key(board_after("d4"), 20, 3)


def test_side_to_move_is_part_of_the_key():
    same_pieces_white_to_move = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    same_pieces_black_to_move = chess.Board("4k3/8/8/8/8/8/8/4K3 b - - 0 1")
    assert cache_key(same_pieces_white_to_move, 20, 3) != cache_key(
        same_pieces_black_to_move, 20, 3
    )


def test_clock_is_kept_near_the_fifty_move_rule():
    """Close to the rule the clock changes the evaluation, so entries must not
    be shared."""
    approaching = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 80 60")
    at_the_edge = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 98 60")
    assert cache_key(approaching, 20, 3) != cache_key(at_the_edge, 20, 3)


def test_in_memory_cache_round_trips_and_counts():
    cache = InMemoryEvalCache()
    board = board_after("e4")
    key = cache_key(board, 20, 3)
    analysis = PositionAnalysis(
        fen=board.fen(),
        depth=20,
        multipv=3,
        lines=(Line(move=chess.Move.from_uci("e7e5"), score=Cp(20)),),
    )

    assert cache.get(key) is None
    cache.put(key, analysis)
    assert cache.get(key) == analysis

    assert (cache.hits, cache.misses) == (1, 1)
    assert len(cache) == 1


def test_sqlite_cache_persists_across_connections(tmp_path):
    """Openings repeat across games and across restarts, so the cache is a
    table rather than a dict (PRD 4.3)."""
    from chess_analysis import db
    from chess_analysis.cache import SqliteEvalCache

    board = board_after("e4")
    key = cache_key(board, 20, 3)
    analysis = PositionAnalysis(
        fen=board.fen(),
        depth=20,
        multipv=3,
        lines=(
            Line(
                move=chess.Move.from_uci("e7e5"),
                score=Cp(20),
                pv=(chess.Move.from_uci("e7e5"), chess.Move.from_uci("g1f3")),
            ),
        ),
    )

    first = db.connect(tmp_path / "cache.db")
    SqliteEvalCache(first).put(key, analysis)
    first.close()

    second = db.connect(tmp_path / "cache.db")
    cache = SqliteEvalCache(second)
    restored = cache.get(key)
    second.close()

    assert restored is not None
    assert restored.lines[0].move == chess.Move.from_uci("e7e5")
    assert restored.lines[0].score == Cp(20)
    assert len(restored.lines[0].pv) == 2
    assert (cache.hits, cache.misses) == (1, 0)


def test_cache_hit_reports_the_current_fen(tmp_path):
    """The key normalises move counters away, so a transposition would
    otherwise inherit whichever FEN got stored first."""
    from chess_analysis import db
    from chess_analysis.cache import SqliteEvalCache
    from chess_analysis.engine import StockfishEvaluator, find_engine

    if find_engine() is None:
        import pytest

        pytest.skip("no stockfish binary")

    conn = db.connect(tmp_path / "fen.db")
    try:
        with StockfishEvaluator(depth=6, cache=SqliteEvalCache(conn)) as engine:
            first = engine.analyse(board_after("Nf3", "d5", "d4"))
            second = engine.analyse(board_after("d4", "d5", "Nf3"))
    finally:
        conn.close()

    assert first.fen != second.fen
    assert second.fen == board_after("d4", "d5", "Nf3").fen()
    assert second.lines[0].score == first.lines[0].score
