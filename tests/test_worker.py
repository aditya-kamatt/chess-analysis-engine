import time
from datetime import UTC, datetime

import chess
import pytest
from chess.engine import Cp, Mate

from chess_analysis import db, store
from chess_analysis.engine import EngineError
from chess_analysis.models import AnalysisStatus, Game, Platform
from chess_analysis.worker import BACKGROUND, URGENT, AnalysisWorker
from tests.fakes import FakeEvaluator

# 1. e4 d5 2. exd5 Qxd5 — four plies, no terminal position.
PGN = '[White "alice"]\n[Black "bob"]\n\n1. e4 d5 2. exd5 Qxd5 *'
EMPTY_PGN = '[White "alice"]\n[Black "bob"]\n\n*'


class ClosableEvaluator(FakeEvaluator):
    """A FakeEvaluator the worker can own the lifetime of."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = 0

    def close(self):
        self.closed += 1


class ExplodingEvaluator(ClosableEvaluator):
    def analyse(self, board):
        raise EngineError("Stockfish died")


def store_game(conn, pgn=PGN, color="white") -> int:
    return store.insert_games(
        conn,
        [
            Game(
                platform=Platform.CHESSCOM,
                platform_game_id=f"g{id(pgn)}{color}",
                played_at=datetime(2026, 8, 1, tzinfo=UTC),
                pgn=pgn,
                player_color=color,
            )
        ],
    )[0]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "worker.db"


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path)
    yield connection
    connection.close()


def run_worker(db_path, game_ids, factory) -> AnalysisWorker:
    """Start a worker, feed it, let it drain, then stop.

    Draining is explicit because `stop` interrupts the game in flight rather
    than waiting for it — the right behaviour for a container shutdown, since
    the game returns to pending and resumes on the next start.
    """
    worker = AnalysisWorker(db_path, evaluator_factory=factory)
    worker.start()
    worker.enqueue(game_ids)
    wait_until(idle(worker), message="queue never drained")
    worker.stop(timeout=15)
    return worker


def idle(worker):
    return lambda: worker.status().queued == 0 and worker.status().current_game_id is None


def status_of(conn, game_id) -> str:
    game = store.get_game(conn, game_id)
    assert game is not None
    return str(game.analysis_status)


def test_analyses_a_game_and_marks_it_complete(conn, db_path):
    game_id = store_game(conn)
    evaluator = ClosableEvaluator(default=Cp(20))

    run_worker(db_path, [game_id], lambda c, depth: evaluator)

    assert status_of(conn, game_id) == "complete"
    assert len(store.get_positions(conn, game_id)) == 4


def test_stored_positions_carry_the_analysis(conn, db_path):
    game_id = store_game(conn)
    evaluator = ClosableEvaluator(default=Cp(35))

    run_worker(db_path, [game_id], lambda c, depth: evaluator)

    first = store.get_positions(conn, game_id)[0]
    assert first["ply"] == 0
    assert first["side_to_move"] == "white"
    assert first["played_move"] == "e2e4"
    assert first["played_move_eval"] == {"cp": 35}
    assert first["depth"] == 20
    assert len(first["lines"]) == 3
    assert all("move" in line and "score" in line for line in first["lines"])


def test_severity_is_stored_for_the_players_blunder(conn, db_path):
    """White plays 2. exd5 into a position scored as lost."""
    game_id = store_game(conn, color="white")
    board = chess.Board()
    fens = [board.fen()]
    for san in ("e4", "d5", "exd5", "Qxd5"):
        board.push_san(san)
        fens.append(board.fen())

    evaluator = ClosableEvaluator({fens[2]: Cp(30), fens[3]: Mate(-2)})
    run_worker(db_path, [game_id], lambda c, depth: evaluator)

    positions = store.get_positions(conn, game_id)
    assert positions[2]["severity"] == "blunder"
    assert positions[2]["win_percent_loss"] > 30
    # Black's moves are never labelled when the player is white (PRD 4.4).
    assert positions[1]["severity"] is None
    assert positions[3]["severity"] is None


def test_a_game_with_no_moves_is_marked_unanalysable(conn, db_path):
    """Aborted games are stored but excluded from the queue (PRD 7)."""
    game_id = store_game(conn, pgn=EMPTY_PGN)

    run_worker(db_path, [game_id], lambda c, depth: ClosableEvaluator())

    assert status_of(conn, game_id) == "unanalysable"
    assert store.get_positions(conn, game_id) == []


def test_engine_crash_marks_the_game_failed_and_is_retriable(conn, db_path):
    game_id = store_game(conn)

    worker = run_worker(db_path, [game_id], lambda c, depth: ExplodingEvaluator())

    assert status_of(conn, game_id) == "failed"
    assert worker.status().failed == 1
    # Failure is per-game, not fatal: the worker is still able to take more.
    assert worker.status().error is None


def test_a_crashed_engine_is_replaced_for_the_next_game(conn, db_path):
    """A dead engine leaves a broken pipe; the next game needs a fresh one."""
    first = store_game(conn, color="white")
    second = store_game(conn, color="black")
    built = []

    def factory(c, depth):
        evaluator = ExplodingEvaluator() if not built else ClosableEvaluator()
        built.append(evaluator)
        return evaluator

    run_worker(db_path, [first, second], factory)

    assert len(built) == 2
    assert status_of(conn, first) == "failed"
    assert status_of(conn, second) == "complete"


def test_a_missing_engine_halts_without_failing_the_game(conn, db_path):
    """It is the setup that is broken, not the game, so it stays pending and
    the error is surfaced instead."""
    game_id = store_game(conn)

    def factory(c, depth):
        raise EngineError("no Stockfish binary found")

    worker = run_worker(db_path, [game_id], factory)

    assert status_of(conn, game_id) == "pending"
    assert worker.status().error is not None
    assert "Stockfish" in worker.status().error


def test_depth_change_restarts_the_engine(conn, db_path):
    first = store_game(conn, color="white")
    second = store_game(conn, color="black")
    depths = []

    def factory(c, depth):
        depths.append(depth)
        return ClosableEvaluator(depth=depth)

    worker = AnalysisWorker(db_path, evaluator_factory=factory)
    worker.start()
    worker.enqueue([first])
    deadline = time.monotonic() + 10
    while worker.status().completed < 1:
        assert time.monotonic() < deadline, "worker never finished the first game"
        time.sleep(0.01)
    store.save_settings(conn, analysis_depth=14)
    worker.enqueue([second])
    wait_until(idle(worker), message="second game never finished")
    worker.stop(timeout=15)

    # Results at different depths are not comparable, so the engine restarts.
    assert depths == [20, 14]
    assert store.get_positions(conn, second)[0]["depth"] == 14


def test_the_same_game_is_not_queued_twice(db_path):
    worker = AnalysisWorker(db_path, evaluator_factory=lambda c, d: ClosableEvaluator())

    assert worker.enqueue([1, 2, 3]) == 3
    assert worker.enqueue([2, 3, 4]) == 1
    assert worker.status().queued == 4


def test_interrupted_games_are_picked_up_again(conn, db_path):
    """`in_progress` means the process died mid-analysis; the row would sit in
    that state forever otherwise."""
    game_id = store_game(conn)
    store.set_analysis_status(conn, game_id, AnalysisStatus.IN_PROGRESS)

    assert store.unanalysed_game_ids(conn) == [game_id]

    run_worker(db_path, store.unanalysed_game_ids(conn), lambda c, d: ClosableEvaluator())
    assert status_of(conn, game_id) == "complete"


def test_reanalysis_replaces_rather_than_appends(conn, db_path):
    game_id = store_game(conn)
    run_worker(db_path, [game_id], lambda c, d: ClosableEvaluator())

    run_worker(db_path, [game_id], lambda c, d: ClosableEvaluator())

    assert len(store.get_positions(conn, game_id)) == 4


def test_the_engine_is_closed_when_the_worker_stops(conn, db_path):
    game_id = store_game(conn)
    evaluator = ClosableEvaluator()

    run_worker(db_path, [game_id], lambda c, depth: evaluator)

    assert evaluator.closed == 1


def make_long_pgn(plies: int = 80) -> str:
    """A long but legal game, so background work takes measurable time."""
    import random

    import chess.pgn

    board = chess.Board()
    rng = random.Random(7)
    game = chess.pgn.Game()
    node = game
    for _ in range(plies):
        if board.is_game_over():
            break
        move = rng.choice(list(board.legal_moves))
        board.push(move)
        node = node.add_variation(move)
    return str(game)


LONG_PGN = make_long_pgn()


class SlowEvaluator(ClosableEvaluator):
    """Spends real time per position, so preemption has something to interrupt."""

    def __init__(self, delay=0.01, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay

    def analyse(self, board):
        time.sleep(self.delay)
        return super().analyse(board)


def wait_until(predicate, timeout=15.0, message="condition never held"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.005)


def test_enqueue_bumps_priority_of_a_waiting_game(db_path):
    worker = AnalysisWorker(db_path, evaluator_factory=lambda c, d: ClosableEvaluator())

    assert worker.enqueue([1], priority=BACKGROUND) == 1
    assert worker.enqueue([1], priority=BACKGROUND) == 0  # already waiting
    assert worker.enqueue([1], priority=URGENT) == 1  # bumped to the front
    assert worker.enqueue([1], priority=BACKGROUND) == 0  # never demoted


def test_opening_a_game_preempts_background_work(conn, db_path):
    """The whole point: you should not wait for a fifty-game archive to finish
    before seeing the game you just opened."""
    background = store_game(conn, pgn=LONG_PGN, color="white")
    opened = store_game(conn, pgn=PGN, color="black")

    worker = AnalysisWorker(db_path, evaluator_factory=lambda c, d: SlowEvaluator())
    worker.start()
    try:
        worker.enqueue([background], priority=BACKGROUND)
        wait_until(
            lambda: worker.status().current_game_id == background
            and worker.status().current_ply > 3,
            message="background game never started",
        )

        worker.enqueue([opened], priority=URGENT)
        wait_until(
            lambda: status_of(conn, opened) == "complete",
            message="opened game never finished",
        )

        # The long game was interrupted, not finished ahead of the short one.
        assert status_of(conn, background) != "complete"

        # And it is not abandoned: it resumes once the urgent work is done.
        wait_until(
            lambda: status_of(conn, background) == "complete",
            message="preempted game never resumed",
        )
    finally:
        worker.stop()


def test_preemption_is_not_counted_as_a_failure(conn, db_path):
    background = store_game(conn, pgn=LONG_PGN, color="white")
    opened = store_game(conn, pgn=PGN, color="black")

    worker = AnalysisWorker(db_path, evaluator_factory=lambda c, d: SlowEvaluator())
    worker.start()
    try:
        worker.enqueue([background], priority=BACKGROUND)
        wait_until(lambda: worker.status().current_ply > 3)
        worker.enqueue([opened], priority=URGENT)
        wait_until(lambda: status_of(conn, background) == "complete")
    finally:
        worker.stop()

    assert worker.status().failed == 0


def test_evaluates_a_position_on_demand(db_path):
    """A sideline the user just played needs an answer now, not after the
    archive finishes."""
    worker = AnalysisWorker(
        db_path, evaluator_factory=lambda c, d: ClosableEvaluator(default=Cp(55))
    )
    worker.start()
    try:
        analysis = worker.evaluate(chess.Board().fen())
    finally:
        worker.stop()

    assert analysis is not None
    assert analysis.best.score == Cp(55)
    assert len(analysis.lines) == 3


def test_evaluation_of_a_finished_position_returns_nothing_to_analyse(db_path):
    board = chess.Board()
    for san in ("f3", "e5", "g4", "Qh4#"):
        board.push_san(san)

    worker = AnalysisWorker(db_path, evaluator_factory=lambda c, d: ClosableEvaluator())
    worker.start()
    try:
        assert worker.evaluate(board.fen()) is None
    finally:
        worker.stop()


def test_evaluation_jumps_ahead_of_a_running_game(conn, db_path):
    background = store_game(conn, pgn=LONG_PGN, color="white")

    worker = AnalysisWorker(db_path, evaluator_factory=lambda c, d: SlowEvaluator())
    worker.start()
    try:
        worker.enqueue([background], priority=BACKGROUND)
        wait_until(lambda: worker.status().current_ply > 3, message="game never started")

        started = time.monotonic()
        analysis = worker.evaluate(chess.Board().fen(), timeout=10)
        elapsed = time.monotonic() - started

        assert analysis is not None
        # The long game has ~80 positions at 10ms each; an answer that waited
        # for it would take far longer than this.
        assert elapsed < 2.0

        wait_until(lambda: status_of(conn, background) == "complete")
    finally:
        worker.stop()


def test_evaluation_reports_an_engine_failure_rather_than_hanging(db_path):
    worker = AnalysisWorker(db_path, evaluator_factory=lambda c, d: ExplodingEvaluator())
    worker.start()
    try:
        with pytest.raises(EngineError, match="Stockfish died"):
            worker.evaluate(chess.Board().fen(), timeout=10)
    finally:
        worker.stop()


def test_evaluation_reports_a_missing_engine(db_path):
    def factory(c, depth):
        raise EngineError("no Stockfish binary found")

    worker = AnalysisWorker(db_path, evaluator_factory=factory)
    worker.start()
    try:
        with pytest.raises(EngineError, match="no Stockfish binary"):
            worker.evaluate(chess.Board().fen(), timeout=10)
    finally:
        worker.stop()
