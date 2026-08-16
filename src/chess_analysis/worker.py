"""Background analysis queue (PRD 4.3).

Analysis never runs inside a request handler. The game list must render before
newly synced games are analysed — someone who just played fifteen blitz games
should not wait on a full queue to see the list.

One thread, one Stockfish process configured with several threads. Several
engine processes would contend for the same cores and each start with a cold
hash table, which is slower than the single engine they are trying to beat.

Work is prioritised, not FIFO. Opening a game puts it at the front and
interrupts whatever background game is running, so nobody waits for a fifty-game
archive to finish before seeing the one game they are looking at. Interrupting
costs almost nothing: every position already evaluated is in the eval cache, so
the interrupted game resumes from where it stopped when it is picked up again.
"""

from __future__ import annotations

import io
import logging
import queue
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import chess
import chess.pgn

from chess_analysis import db, store
from chess_analysis.analyzer import UnanalysableGame, analyse_game
from chess_analysis.cache import SqliteEvalCache
from chess_analysis.engine import (
    DEFAULT_MULTIPV,
    EngineError,
    PositionEvaluator,
    StockfishEvaluator,
)
from chess_analysis.models import AnalysisStatus

LOGGER = logging.getLogger(__name__)

# Lower sorts first. The stop sentinel outranks everything so shutdown is not
# stuck behind a queue of games.
STOP_PRIORITY = -2
INTERACTIVE = -1
"""One position someone is waiting on — a sideline they just played."""
URGENT = 0
"""A game the user is looking at right now."""
BACKGROUND = 10
"""Bulk analysis of the synced archive."""


class AnalysisCancelled(Exception):
    """Raised inside the analysis loop when higher-priority work arrives."""


@dataclass
class EvaluationRequest:
    """One position to evaluate, and somewhere to put the answer.

    Carried through the same queue as games so a single Stockfish process stays
    the only one running, while jumping ahead of every game.
    """

    fen: str
    done: threading.Event = field(default_factory=threading.Event)
    analysis: Any = None
    error: str | None = None


class Evaluator(PositionEvaluator, Protocol):
    """A `PositionEvaluator` the worker also owns the lifetime of."""

    def close(self) -> None: ...


EvaluatorFactory = Callable[[sqlite3.Connection, int], Evaluator]


@dataclass(frozen=True)
class WorkerStatus:
    running: bool
    queued: int
    current_game_id: int | None
    current_ply: int
    current_total: int
    completed: int
    failed: int
    error: str | None
    """Set when the engine could not start; the UI surfaces it rather than
    letting every game silently fail."""


class AnalysisWorker:
    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        engine_path: str | None = None,
        multipv: int = DEFAULT_MULTIPV,
        threads: int | None = None,
        hash_mb: int | None = None,
        evaluator_factory: EvaluatorFactory | None = None,
    ) -> None:
        self._db_path = db_path
        self._engine_path = engine_path
        self._multipv = multipv
        self._threads = threads
        self._hash_mb = hash_mb
        self._evaluator_factory = evaluator_factory or self._start_engine

        self._queue: queue.PriorityQueue[
            tuple[int, int, int | EvaluationRequest | None]
        ] = queue.PriorityQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._sequence = 0

        # game id -> best priority queued at, so a bump can overtake an entry
        # already sitting in the queue.
        self._queued: dict[int, int] = {}
        self._current: int | None = None
        self._current_priority = BACKGROUND
        self._progress = (0, 0)
        self._completed = 0
        self._failed = 0
        self._error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="analysis-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._cancel.set()
        with self._lock:
            self._sequence += 1
            self._queue.put((STOP_PRIORITY, self._sequence, None))
        self._thread.join(timeout)
        self._thread = None

    def enqueue(self, game_ids: list[int], *, priority: int = BACKGROUND) -> int:
        """Queue games, or raise the priority of ones already waiting.

        Enqueuing urgent work interrupts a background game already running.
        """
        added = 0
        with self._lock:
            for game_id in game_ids:
                queued_at = self._queued.get(game_id)
                if queued_at is not None and queued_at <= priority:
                    continue
                self._queued[game_id] = priority
                self._sequence += 1
                self._queue.put((priority, self._sequence, game_id))
                added += 1

            preempt = (
                self._current is not None
                and self._current not in game_ids
                and priority < self._current_priority
            )
        if preempt:
            self._cancel.set()
        return added

    def evaluate(self, fen: str, *, timeout: float = 60.0):
        """Evaluate one position ahead of everything queued, and wait for it.

        The caller blocks, but the work still happens on the worker thread with
        the one engine and the shared cache — the rule against analysing inside
        a request handler is about not making the app wait on *batch* work, and
        this is a single position someone asked for and is watching.
        """
        request = EvaluationRequest(fen=fen)
        with self._lock:
            self._sequence += 1
            self._queue.put((INTERACTIVE, self._sequence, request))
            preempt = self._current is not None
        if preempt:
            self._cancel.set()

        if not request.done.wait(timeout):
            raise EngineError("the engine did not answer in time")
        if request.error is not None:
            raise EngineError(request.error)
        return request.analysis

    def status(self) -> WorkerStatus:
        with self._lock:
            ply, total = self._progress
            return WorkerStatus(
                running=self._thread is not None and self._thread.is_alive(),
                queued=len(self._queued),
                current_game_id=self._current,
                current_ply=ply,
                current_total=total,
                completed=self._completed,
                failed=self._failed,
                error=self._error,
            )

    # -- worker thread ----------------------------------------------------

    def _run(self) -> None:
        try:
            conn = db.connect(self._db_path)
        except Exception as exc:
            # Dying here would leave the queue silently frozen; record it so
            # the UI can say why nothing is being analysed.
            LOGGER.exception("analysis worker could not open the database")
            with self._lock:
                self._error = f"could not open the database: {exc}"
            return

        evaluator: Evaluator | None = None
        depth: int | None = None

        try:
            while True:
                priority, _sequence, payload = self._queue.get()
                if payload is None:
                    break

                self._cancel.clear()
                wanted = store.load_settings(conn).analysis_depth
                if evaluator is not None and depth != wanted:
                    evaluator.close()
                    evaluator = None
                if evaluator is None:
                    try:
                        evaluator = self._evaluator_factory(conn, wanted)
                        depth = wanted
                    except EngineError as exc:
                        self._fail_to_start(conn, payload, str(exc))
                        break

                if isinstance(payload, EvaluationRequest):
                    if not self._evaluate_one(evaluator, payload):
                        evaluator.close()
                        evaluator = None
                    continue

                game_id = payload
                with self._lock:
                    # A priority bump leaves a stale duplicate behind; the
                    # entry that matches the recorded priority is the live one.
                    if self._queued.get(game_id) != priority:
                        continue
                    del self._queued[game_id]
                    self._current = game_id
                    self._current_priority = priority
                    self._progress = (0, 0)

                if not self._analyse(conn, evaluator, game_id):
                    # The engine died mid-game; drop it so the next job starts
                    # a fresh process rather than inheriting a broken pipe.
                    evaluator.close()
                    evaluator = None

                with self._lock:
                    self._current = None
                    self._current_priority = BACKGROUND
                    self._progress = (0, 0)
        finally:
            if evaluator is not None:
                evaluator.close()
            conn.close()

    def _start_engine(self, conn: sqlite3.Connection, depth: int) -> Evaluator:
        return StockfishEvaluator(
            self._engine_path,
            depth=depth,
            multipv=self._multipv,
            threads=self._threads,
            hash_mb=self._hash_mb,
            cache=SqliteEvalCache(conn),
        )

    def _fail_to_start(
        self,
        conn: sqlite3.Connection,
        payload: int | EvaluationRequest,
        message: str,
    ) -> None:
        """No engine, so nothing can run. A game is left pending rather than
        marked failed — it is the setup that is broken, not the game — and a
        waiting caller is told rather than left to time out."""
        LOGGER.error("analysis worker stopping: %s", message)

        if isinstance(payload, EvaluationRequest):
            payload.error = message
            payload.done.set()
        else:
            store.set_analysis_status(conn, payload, AnalysisStatus.PENDING)
            with self._lock:
                self._queued.pop(payload, None)

        with self._lock:
            self._error = message
            self._current = None

    def _evaluate_one(self, evaluator: Evaluator, request: EvaluationRequest) -> bool:
        """Answer one interactive request. Returns False if the engine died."""
        healthy = True
        try:
            board = chess.Board(request.fen)
            request.analysis = (
                None if board.is_game_over() else evaluator.analyse(board)
            )
        except EngineError as exc:
            request.error = str(exc)
            healthy = False
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            LOGGER.exception("evaluating %s failed", request.fen)
            request.error = str(exc)
        finally:
            request.done.set()
        return healthy

    def _analyse(
        self,
        conn: sqlite3.Connection,
        evaluator: Evaluator,
        game_id: int,
    ) -> bool:
        """Analyse one game. Returns False if the engine needs replacing."""
        game = store.get_game(conn, game_id)
        if game is None:
            return True

        store.set_analysis_status(conn, game_id, AnalysisStatus.IN_PROGRESS)

        def progress(done: int, total: int) -> None:
            if self._cancel.is_set():
                raise AnalysisCancelled
            with self._lock:
                self._progress = (done, total)

        try:
            parsed = chess.pgn.read_game(io.StringIO(game.pgn))
            if parsed is None:
                raise UnanalysableGame("PGN could not be read")

            result = analyse_game(
                parsed,
                evaluator,
                player_color=_color(game.player_color),
                progress=progress,
            )
        except AnalysisCancelled:
            # Not a failure: the work already done is in the eval cache, so
            # resuming later re-reads it rather than recomputing.
            LOGGER.info("analysis of game %s preempted", game_id)
            store.set_analysis_status(conn, game_id, AnalysisStatus.PENDING)
            self.enqueue([game_id], priority=self._current_priority)
            return True
        except UnanalysableGame as exc:
            LOGGER.info("game %s is unanalysable: %s", game_id, exc)
            store.set_analysis_status(conn, game_id, AnalysisStatus.UNANALYSABLE)
            return True
        except EngineError as exc:
            LOGGER.warning("engine failed on game %s: %s", game_id, exc)
            store.set_analysis_status(conn, game_id, AnalysisStatus.FAILED)
            with self._lock:
                self._failed += 1
            return False
        except Exception:
            LOGGER.exception("analysis of game %s failed", game_id)
            store.set_analysis_status(conn, game_id, AnalysisStatus.FAILED)
            with self._lock:
                self._failed += 1
            return True

        store.save_analysis(conn, game_id, list(result.plies))
        store.set_analysis_status(conn, game_id, AnalysisStatus.COMPLETE)
        with self._lock:
            self._completed += 1
        return True


def _color(player_color: str | None) -> chess.Color | None:
    if player_color == "white":
        return chess.WHITE
    if player_color == "black":
        return chess.BLACK
    return None
