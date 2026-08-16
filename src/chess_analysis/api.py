"""HTTP API and static hosting for the web UI.

Analysis never runs in a request handler (PRD 4.3) — that arrives with the
background worker. Sync does run inline: it is a handful of serial HTTP calls
the user explicitly asked for and watches a spinner through.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import chess
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from chess_analysis import db, store
from chess_analysis.engine import EngineError, line_to_dict
from chess_analysis.evaluation import (
    score_from_dict,
    score_to_dict,
    terminal_score,
    win_percent,
)
from chess_analysis.lines import present_lines
from chess_analysis.models import AnalysisStatus, Game, Platform
from chess_analysis.platforms.chesscom import (
    ChessComClient,
    ChessComError,
    RateLimited,
    UnknownPlayer,
)
from chess_analysis.sync import SyncError, sync_chesscom
from chess_analysis.worker import URGENT, AnalysisWorker

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """A connection per request. SQLite connections are not shareable across
    threads, and FastAPI runs these synchronous handlers in a thread pool."""
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


# Module scope on purpose: `from __future__ import annotations` makes these
# strings, and FastAPI resolves them against module globals.
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


class SettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chesscom_enabled: bool
    chesscom_username: str | None
    chesscom_last_synced_at: datetime | None
    chesscom_backfill_cursor: datetime | None
    reveal_lines_by_default: bool
    analysis_depth: int
    background_analysis: bool


class SettingsUpdate(BaseModel):
    """Chess.com and preferences only; Lichess lands with its own milestone."""

    chesscom_enabled: bool = False
    chesscom_username: str | None = None
    reveal_lines_by_default: bool = False
    analysis_depth: int = Field(default=20, ge=6, le=30)
    background_analysis: bool = True


class PreferencesUpdate(BaseModel):
    """Partial update for preferences only.

    Separate from `SettingsUpdate` because that one carries account fields with
    defaults: sending it to flip one checkbox would quietly disable Chess.com.
    """

    reveal_lines_by_default: bool | None = None
    analysis_depth: int | None = Field(default=None, ge=6, le=30)
    background_analysis: bool | None = None


class EvaluateRequest(BaseModel):
    fen: str


class GameSummary(BaseModel):
    id: int
    platform: str
    played_at: datetime
    time_control: str | None
    player_color: str | None
    opponent: str | None
    result: str | None
    eco_code: str | None
    url: str | None
    analysis_status: str


class GameDetail(GameSummary):
    """A summary plus the PGN, which the board replays client-side."""

    pgn: str


class GameList(BaseModel):
    games: list[GameSummary]
    total: int
    history_back_to: datetime | None
    """Oldest stored game, for the "history loaded back to" indicator."""


class AnalysisStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    running: bool
    queued: int
    current_game_id: int | None
    current_ply: int
    current_total: int
    completed: int
    failed: int
    error: str | None


class SyncResponse(BaseModel):
    inserted: int
    entries_seen: int
    archives_read: int
    archives_unchanged: int
    first_sync: bool
    last_synced_at: datetime
    backfill_cursor: datetime | None
    total_games: int


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    worker = app.state.worker
    worker.start()

    # Anything left queued or interrupted by a restart resumes here.
    conn = db.connect(app.state.db_path)
    try:
        if store.load_settings(conn).background_analysis:
            worker.enqueue(store.unanalysed_game_ids(conn))
    finally:
        conn.close()

    yield
    worker.stop()


def create_app(
    *,
    db_path: Path | str | None = None,
    client_factory: Callable[[], ChessComClient] = ChessComClient,
    worker: AnalysisWorker | None = None,
) -> FastAPI:
    app = FastAPI(title="Chess Analysis", lifespan=_lifespan)
    app.state.db_path = db_path
    app.state.client_factory = client_factory
    app.state.worker = worker if worker is not None else AnalysisWorker(db_path)
    # In-memory guard: one sync at a time, and the button disables while it runs.
    app.state.sync_lock = threading.Lock()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/settings", response_model=SettingsResponse)
    def read_settings(conn: Conn) -> Any:
        return store.load_settings(conn)

    @app.put("/api/settings", response_model=SettingsResponse)
    def write_settings(conn: Conn, update: SettingsUpdate) -> Any:
        username = (update.chesscom_username or "").strip() or None

        if update.chesscom_enabled:
            if not username:
                raise HTTPException(422, "Enter a Chess.com username")
            with _wrapped_errors():
                with client_factory() as client:
                    if not client.player_exists(username):
                        raise HTTPException(422, f"No Chess.com user named {username}")

        fields: dict[str, Any] = {
            "chesscom_enabled": update.chesscom_enabled,
            "chesscom_username": username,
            "reveal_lines_by_default": update.reveal_lines_by_default,
            "analysis_depth": update.analysis_depth,
            "background_analysis": update.background_analysis,
        }

        # A different account is a different archive: the cursors describe the
        # old one and must not be carried over, or the first sync of the new
        # account would fetch only "since" a time that never applied to it.
        current = store.load_settings(conn)
        if current.chesscom_username != username:
            fields["chesscom_last_synced_at"] = None
            fields["chesscom_backfill_cursor"] = None

        store.save_settings(conn, **fields)
        return store.load_settings(conn)

    @app.patch("/api/settings", response_model=SettingsResponse)
    def patch_settings(conn: Conn, update: PreferencesUpdate) -> Any:
        fields = {
            name: value
            for name, value in update.model_dump().items()
            if value is not None
        }
        if fields:
            store.save_settings(conn, **fields)
        return store.load_settings(conn)

    @app.post("/api/sync", response_model=SyncResponse)
    def run_sync(conn: Conn) -> Any:
        if not app.state.sync_lock.acquire(blocking=False):
            raise HTTPException(409, "A sync is already running")
        try:
            with _wrapped_errors():
                with client_factory() as client:
                    result = sync_chesscom(conn, client)
        finally:
            app.state.sync_lock.release()

        # The list renders immediately; analysis catches up behind it (PRD 4.3).
        # With background analysis off, only games the user opens are analysed.
        if store.load_settings(conn).background_analysis:
            app.state.worker.enqueue(result.inserted_ids)

        return SyncResponse(
            inserted=result.inserted,
            entries_seen=result.entries_seen,
            archives_read=result.archives_read,
            archives_unchanged=result.archives_unchanged,
            first_sync=result.first_sync,
            last_synced_at=result.last_synced_at,
            backfill_cursor=result.backfill_cursor,
            total_games=store.count_games(conn),
        )

    @app.get("/api/games", response_model=GameList)
    def read_games(
        conn: Conn,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Any:
        games = store.list_games(conn, limit=limit, offset=offset)
        return GameList(
            games=[GameSummary(**_summary_fields(game)) for game in games],
            total=store.count_games(conn),
            history_back_to=store.oldest_played_at(conn, Platform.CHESSCOM),
        )

    @app.get("/api/games/{game_id}", response_model=GameDetail)
    def read_game(conn: Conn, game_id: int) -> Any:
        game = store.get_game(conn, game_id)
        if game is None:
            raise HTTPException(404, "No such game")
        return GameDetail(**_summary_fields(game), pgn=game.pgn)

    @app.get("/api/games/{game_id}/analysis")
    def read_analysis(conn: Conn, game_id: int) -> Any:
        if store.get_game(conn, game_id) is None:
            raise HTTPException(404, "No such game")
        return {"positions": _with_win_percents(store.get_positions(conn, game_id))}

    @app.post("/api/games/{game_id}/analyse", response_model=AnalysisStatusResponse)
    def queue_analysis(
        conn: Conn,
        game_id: int,
        force: Annotated[bool, Query()] = False,
    ) -> Any:
        """Analyse this game next, interrupting background work.

        Called when a game is opened, so it is idempotent for work already
        done: without `force` a finished game is not analysed again.
        """
        game = store.get_game(conn, game_id)
        if game is None:
            raise HTTPException(404, "No such game")

        settled = {AnalysisStatus.COMPLETE, AnalysisStatus.UNANALYSABLE}
        if game.analysis_status in settled and not force:
            return app.state.worker.status()

        store.set_analysis_status(conn, game_id, AnalysisStatus.PENDING)
        app.state.worker.enqueue([game_id], priority=URGENT)
        return app.state.worker.status()

    @app.post("/api/evaluate")
    def evaluate(body: EvaluateRequest) -> Any:
        """Evaluate an arbitrary position — a sideline the user just played.

        Jumps ahead of every queued game, and shares the engine and the
        evaluation cache with them, so a position already seen comes back
        without touching Stockfish at all.
        """
        try:
            board = chess.Board(body.fen)
        except ValueError as exc:
            raise HTTPException(422, "That is not a valid position") from exc

        if board.is_game_over():
            score = terminal_score(board)
            return {
                "fen": body.fen,
                "over": _outcome(board),
                "eval": score_to_dict(score),
                "win_percent": win_percent(score),
                "lines": [],
                "depth": None,
            }

        try:
            analysis = app.state.worker.evaluate(body.fen)
        except EngineError as exc:
            raise HTTPException(503, str(exc)) from exc

        best = analysis.best.score
        return {
            "fen": body.fen,
            "over": None,
            "eval": score_to_dict(best),
            "win_percent": win_percent(best),
            "lines": present_lines(
                body.fen, [line_to_dict(line) for line in analysis.lines]
            ),
            "depth": analysis.depth,
        }

    @app.get("/api/analysis/status", response_model=AnalysisStatusResponse)
    def analysis_status() -> Any:
        return app.state.worker.status()

    # Registered after every real API route and before the static mount. An
    # unknown /api path would otherwise fall through to StaticFiles, which
    # answers anything but GET with a bare "Method Not Allowed" — the symptom of
    # a server running older code than the UI it is serving, which is easy to
    # hit because rebuilding the frontend changes what a running server serves
    # without reloading its Python.
    @app.api_route(
        "/api/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def unknown_api(rest: str) -> Any:
        raise HTTPException(
            404,
            f"No API endpoint /api/{rest}. If the page was rebuilt recently, "
            "restart the server — it may be running older code than the UI.",
        )

    # Built frontend, when there is one. In development Vite serves the UI and
    # proxies /api here instead.
    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="ui")

    return app


def _with_win_percents(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach win percentages to each ply.

    Computed here with the same model the classifier uses, rather than
    reimplementing the sigmoid in TypeScript: if the evaluation bar and the
    severity labels ever drifted apart the UI would contradict itself.

    `eval` is the position before the move, `played` the position after it —
    together they cover every board state the user can step to.
    """
    enriched = []
    for position in positions:
        best = position["lines"][0]["score"] if position["lines"] else None
        played = position["played_move_eval"]
        enriched.append(
            {
                **position,
                "lines": present_lines(position["fen"], position["lines"]),
                "eval": best,
                "eval_win_percent": (
                    win_percent(score_from_dict(best)) if best is not None else None
                ),
                "played_win_percent": win_percent(score_from_dict(played)),
            }
        )
    return enriched


def _outcome(board: chess.Board) -> str:
    """Why a position is final, for the readout on a sideline that ends."""
    if board.is_checkmate():
        return "checkmate"
    if board.is_stalemate():
        return "stalemate"
    if board.is_insufficient_material():
        return "insufficient material"
    return "draw"


def _summary_fields(game: Game) -> dict[str, Any]:
    return {
        "id": game.id,
        "platform": str(game.platform),
        "played_at": game.played_at,
        "time_control": game.time_control,
        "player_color": game.player_color,
        "opponent": game.opponent,
        "result": str(game.result) if game.result else None,
        "eco_code": game.eco_code,
        "url": game.url,
        "analysis_status": str(game.analysis_status),
    }


@contextmanager
def _wrapped_errors() -> Iterator[None]:
    """Turn platform failures into responses naming the cause (PRD 4.2)."""
    try:
        yield
    except UnknownPlayer as exc:
        raise HTTPException(422, "Chess.com does not recognise that username") from exc
    except RateLimited as exc:
        raise HTTPException(429, str(exc)) from exc
    except ChessComError as exc:
        raise HTTPException(502, str(exc)) from exc
    except SyncError as exc:
        raise HTTPException(400, str(exc)) from exc


app = create_app()
