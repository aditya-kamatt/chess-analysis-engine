from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from chess_analysis import db, store
from chess_analysis.api import create_app
from chess_analysis.platforms.chesscom import ArchiveResponse, RateLimited
from chess_analysis.worker import WorkerStatus

PGN = '[ECO "B01"]\n[White "alice"]\n[Black "bob"]\n\n1. e4 d5 *'


def entry(uuid: str, played_at: datetime):
    return {
        "uuid": uuid,
        "url": f"https://www.chess.com/game/live/{uuid}",
        "pgn": PGN,
        "time_control": "180",
        "end_time": int(played_at.timestamp()),
        "rules": "chess",
        "white": {"username": "alice", "result": "win"},
        "black": {"username": "bob", "result": "resigned"},
    }


class FakeClient:
    known_players = {"alice"}
    archive_url = "https://api.chess.com/pub/player/alice/games/2026/08"
    entries: list[dict] = []
    error: Exception | None = None

    def archive_urls(self, username):
        return [self.archive_url]

    def fetch_archive(self, url, *, etag=None, last_modified=None):
        if type(self).error is not None:
            raise type(self).error
        return ArchiveResponse(modified=True, entries=type(self).entries, etag='W/"1"')

    def player_exists(self, username):
        return username.lower() in type(self).known_players

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class FakeWorker:
    """The API's view of the worker; the real one is covered in test_worker."""

    def __init__(self):
        self.enqueued: list[int] = []
        self.priorities: list[int] = []
        self.started = False
        self.evaluation = None
        self.evaluation_error: Exception | None = None
        self.evaluated: list[str] = []

    def start(self):
        self.started = True

    def stop(self, timeout=10.0):
        self.started = False

    def enqueue(self, game_ids, *, priority=10):
        self.enqueued.extend(game_ids)
        self.priorities.extend([priority] * len(game_ids))
        return len(game_ids)

    def evaluate(self, fen, *, timeout=60.0):
        self.evaluated.append(fen)
        if self.evaluation_error is not None:
            raise self.evaluation_error
        return self.evaluation

    def status(self):
        return WorkerStatus(
            running=self.started,
            queued=len(self.enqueued),
            current_game_id=None,
            current_ply=0,
            current_total=0,
            completed=0,
            failed=0,
            error=None,
        )


@pytest.fixture
def client(tmp_path):
    FakeClient.error = None
    FakeClient.known_players = {"alice"}
    FakeClient.entries = [
        entry("g1", datetime(2026, 8, 1, 12, tzinfo=UTC)),
        entry("g2", datetime(2026, 8, 2, 12, tzinfo=UTC)),
        entry("g3", datetime(2026, 8, 3, 12, tzinfo=UTC)),
    ]
    worker = FakeWorker()
    app = create_app(
        db_path=tmp_path / "api.db", client_factory=FakeClient, worker=worker
    )
    with TestClient(app) as test_client:
        test_client.db_path = tmp_path / "api.db"
        test_client.worker = worker
        yield test_client


def configure(client, username="alice"):
    return client.put(
        "/api/settings", json={"chesscom_enabled": True, "chesscom_username": username}
    )


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_settings_start_empty(client):
    body = client.get("/api/settings").json()

    assert body["chesscom_enabled"] is False
    assert body["chesscom_username"] is None
    assert body["chesscom_last_synced_at"] is None


def test_saving_settings_validates_the_username(client):
    """Caught at save time, not silently at first sync (PRD 4.1)."""
    response = configure(client, "ghost")

    assert response.status_code == 422
    assert "ghost" in response.json()["detail"]
    assert client.get("/api/settings").json()["chesscom_enabled"] is False


def test_enabling_without_a_username_is_rejected(client):
    response = client.put(
        "/api/settings", json={"chesscom_enabled": True, "chesscom_username": "  "}
    )
    assert response.status_code == 422


def test_saving_valid_settings_persists_them(client):
    assert configure(client).status_code == 200

    body = client.get("/api/settings").json()
    assert body["chesscom_enabled"] is True
    assert body["chesscom_username"] == "alice"


def test_settings_never_expose_the_lichess_token(client):
    """The token is stored but must never be returned or logged (PRD 4.1)."""
    conn = db.connect(client.db_path)
    store.save_settings(conn, lichess_token="secret-token")
    conn.close()

    assert "secret" not in client.get("/api/settings").text


def test_changing_username_resets_the_cursors(client):
    configure(client)
    client.post("/api/sync")
    assert client.get("/api/settings").json()["chesscom_last_synced_at"] is not None

    FakeClient.known_players = {"alice", "bob"}
    configure(client, "bob")

    # A different account is a different archive; a stale cursor would make the
    # first sync of the new account fetch almost nothing.
    body = client.get("/api/settings").json()
    assert body["chesscom_last_synced_at"] is None
    assert body["chesscom_backfill_cursor"] is None


def test_sync_requires_configuration(client):
    assert client.post("/api/sync").status_code == 400


def test_sync_inserts_games(client):
    configure(client)

    body = client.post("/api/sync").json()

    assert body["inserted"] == 3
    assert body["first_sync"] is True
    assert body["total_games"] == 3
    assert body["last_synced_at"] is not None


def test_second_sync_is_not_a_first_sync(client):
    configure(client)
    client.post("/api/sync")

    body = client.post("/api/sync").json()

    assert body["first_sync"] is False
    assert body["inserted"] == 0


def test_rate_limiting_surfaces_as_429(client):
    configure(client)
    FakeClient.error = RateLimited("Chess.com is rate limiting this account")

    response = client.post("/api/sync")

    assert response.status_code == 429
    assert "rate limiting" in response.json()["detail"]


def test_concurrent_sync_is_refused(client):
    configure(client)
    app = client.app
    app.state.sync_lock.acquire()
    try:
        response = client.post("/api/sync")
    finally:
        app.state.sync_lock.release()

    assert response.status_code == 409


def test_games_list_is_newest_first(client):
    configure(client)
    client.post("/api/sync")

    body = client.get("/api/games").json()

    assert body["total"] == 3
    assert [g["played_at"][:10] for g in body["games"]] == [
        "2026-08-03",
        "2026-08-02",
        "2026-08-01",
    ]


def test_game_summary_carries_what_the_list_shows(client):
    configure(client)
    client.post("/api/sync")

    game = client.get("/api/games").json()["games"][0]

    assert game["opponent"] == "bob"
    assert game["player_color"] == "white"
    assert game["result"] == "win"
    assert game["eco_code"] == "B01"
    assert game["time_control"] == "180"
    assert game["analysis_status"] == "pending"
    assert game["url"].startswith("https://www.chess.com/")


def test_game_detail_includes_the_pgn(client):
    """The board replays the PGN client-side, so detail carries it and the
    list does not."""
    configure(client)
    client.post("/api/sync")
    game_id = client.get("/api/games").json()["games"][0]["id"]

    detail = client.get(f"/api/games/{game_id}").json()

    assert detail["pgn"] == PGN
    assert detail["opponent"] == "bob"
    assert "pgn" not in client.get("/api/games").json()["games"][0]


def test_missing_game_is_404(client):
    assert client.get("/api/games/9999").status_code == 404


def test_games_list_reports_history_depth(client):
    configure(client)
    client.post("/api/sync")

    body = client.get("/api/games").json()

    assert body["history_back_to"].startswith("2026-08-01")


def test_games_list_paginates(client):
    configure(client)
    client.post("/api/sync")

    page = client.get("/api/games", params={"limit": 2, "offset": 2}).json()

    assert len(page["games"]) == 1
    assert page["total"] == 3


def test_empty_state_is_not_an_error(client):
    body = client.get("/api/games").json()

    assert body == {"games": [], "total": 0, "history_back_to": None}


def test_pagination_bounds_are_enforced(client):
    assert client.get("/api/games", params={"limit": 0}).status_code == 422
    assert client.get("/api/games", params={"limit": 500}).status_code == 422
    assert client.get("/api/games", params={"offset": -1}).status_code == 422


def test_sync_queues_new_games_for_analysis(client):
    """The list renders before analysis finishes, so sync only enqueues."""
    configure(client)

    client.post("/api/sync")

    assert sorted(client.worker.enqueued) == [1, 2, 3]


def test_resync_does_not_requeue_existing_games(client):
    configure(client)
    client.post("/api/sync")
    client.worker.enqueued.clear()

    client.post("/api/sync")

    assert client.worker.enqueued == []


def test_worker_starts_with_the_app(client):
    assert client.get("/api/analysis/status").json()["running"] is True


def test_analysis_status_is_reported(client):
    configure(client)
    client.post("/api/sync")

    body = client.get("/api/analysis/status").json()

    assert body["queued"] == 3
    assert body["error"] is None


def test_retry_requeues_a_game(client):
    configure(client)
    client.post("/api/sync")
    client.worker.enqueued.clear()
    client.worker.priorities.clear()

    conn = db.connect(client.db_path)
    store.set_analysis_status(conn, 1, "failed")
    conn.close()

    assert client.post("/api/games/1/analyse").status_code == 200
    assert client.worker.enqueued == [1]
    assert client.worker.priorities == [0]  # urgent, ahead of background work

    games = {g["id"]: g for g in client.get("/api/games").json()["games"]}
    assert games[1]["analysis_status"] == "pending"


def test_retry_of_a_missing_game_is_404(client):
    assert client.post("/api/games/9999/analyse").status_code == 404


def test_analysis_of_an_unanalysed_game_is_empty(client):
    configure(client)
    client.post("/api/sync")

    assert client.get("/api/games/1/analysis").json() == {"positions": []}


def test_analysis_of_a_missing_game_is_404(client):
    assert client.get("/api/games/9999/analysis").status_code == 404


def analysed_ply(**overrides):
    import chess
    from chess.engine import Cp

    from chess_analysis.analyzer import AnalysedPly
    from chess_analysis.engine import Line

    move = chess.Move.from_uci("e2e4")
    defaults = dict(
        ply=0,
        fen=chess.Board().fen(),
        side_to_move=chess.WHITE,
        played_move=move,
        played_move_score=Cp(30),
        lines=(Line(move=move, score=Cp(40), pv=(move,)),),
        depth=20,
        win_percent_loss=0.7,
        severity=None,
    )
    return AnalysedPly(**(defaults | overrides))


def store_analysis(client, game_id, plies):
    conn = db.connect(client.db_path)
    store.save_analysis(conn, game_id, plies)
    conn.close()


def test_analysis_carries_win_percentages(client):
    """The evaluation bar fills from these rather than reimplementing the
    sigmoid in the browser, so it cannot disagree with the severity labels."""
    from chess.engine import Cp

    from chess_analysis.evaluation import win_percent

    configure(client)
    client.post("/api/sync")
    store_analysis(client, 1, [analysed_ply()])

    position = client.get("/api/games/1/analysis").json()["positions"][0]

    assert position["eval"] == {"cp": 40}
    assert position["eval_win_percent"] == pytest.approx(win_percent(Cp(40)))
    assert position["played_win_percent"] == pytest.approx(win_percent(Cp(30)))


def test_mate_pins_the_evaluation_bar(client):
    """Mate scores pin the bar to full rather than running through the
    sigmoid (PRD 4.5)."""
    from chess.engine import Mate

    configure(client)
    client.post("/api/sync")
    store_analysis(client, 1, [analysed_ply(played_move_score=Mate(3))])

    position = client.get("/api/games/1/analysis").json()["positions"][0]

    assert position["played_win_percent"] == 100.0
    assert position["played_move_eval"] == {"mate": 3}


def test_severity_reaches_the_move_list(client):
    from chess.engine import Cp

    configure(client)
    client.post("/api/sync")
    store_analysis(
        client,
        1,
        [analysed_ply(played_move_score=Cp(-400), win_percent_loss=35.9, severity="blunder")],
    )

    position = client.get("/api/games/1/analysis").json()["positions"][0]

    assert position["severity"] == "blunder"
    assert position["win_percent_loss"] == pytest.approx(35.9)


def test_opening_a_game_analyses_it_next(client):
    """Opening a game must not wait behind a fifty-game archive."""
    configure(client)
    client.post("/api/sync")
    client.worker.enqueued.clear()
    client.worker.priorities.clear()

    client.post("/api/games/2/analyse")

    assert client.worker.enqueued == [2]
    assert client.worker.priorities == [0]


def test_opening_an_analysed_game_does_not_reanalyse_it(client):
    configure(client)
    client.post("/api/sync")
    store_analysis(client, 1, [analysed_ply()])
    conn = db.connect(client.db_path)
    store.set_analysis_status(conn, 1, "complete")
    conn.close()
    client.worker.enqueued.clear()

    client.post("/api/games/1/analyse")

    assert client.worker.enqueued == []


def test_force_reanalyses_a_finished_game(client):
    configure(client)
    client.post("/api/sync")
    conn = db.connect(client.db_path)
    store.set_analysis_status(conn, 1, "complete")
    conn.close()
    client.worker.enqueued.clear()

    client.post("/api/games/1/analyse", params={"force": True})

    assert client.worker.enqueued == [1]


def test_background_analysis_off_leaves_sync_quiet(client):
    """Only games you open get analysed (PRD 10, question 1)."""
    client.put(
        "/api/settings",
        json={
            "chesscom_enabled": True,
            "chesscom_username": "alice",
            "background_analysis": False,
        },
    )
    client.worker.enqueued.clear()

    client.post("/api/sync")

    assert client.worker.enqueued == []
    # Opening one still works.
    client.post("/api/games/1/analyse")
    assert client.worker.enqueued == [1]


def test_background_analysis_defaults_on(client):
    assert client.get("/api/settings").json()["background_analysis"] is True


def uci_line(*moves, cp=20):
    import chess
    from chess.engine import Cp

    from chess_analysis.engine import Line

    parsed = [chess.Move.from_uci(m) for m in moves]
    return Line(move=parsed[0], score=Cp(cp), pv=tuple(parsed))


def test_analysis_lines_are_presented_in_san(client):
    """The engine speaks g1f3; the panel shows Nf3."""
    configure(client)
    client.post("/api/sync")
    store_analysis(client, 1, [analysed_ply(lines=(uci_line("g1f3", "d7d5", "d2d4"),))])

    line = client.get("/api/games/1/analysis").json()["positions"][0]["lines"][0]

    assert line["san"] == "Nf3"
    assert line["pv_san"] == ["Nf3", "d5", "d4"]
    assert line["move"] == "g1f3"  # UCI survives, for the board


def test_analysis_collapses_transposing_lines(client):
    """Three PVs that are one idea by different move orders read as noise."""
    configure(client)
    client.post("/api/sync")
    store_analysis(
        client,
        1,
        [
            analysed_ply(
                lines=(
                    uci_line("g1f3", "d7d5", "d2d4", cp=40),
                    uci_line("d2d4", "d7d5", "g1f3", cp=38),
                )
            )
        ],
    )

    lines = client.get("/api/games/1/analysis").json()["positions"][0]["lines"]

    assert len(lines) == 1
    assert lines[0]["san"] == "Nf3"
    assert lines[0]["alternatives"] == ["d4"]


def test_preferences_patch_leaves_the_account_alone(client):
    """Flipping one checkbox must not disable Chess.com — which is exactly what
    sending the full settings body with defaults would do."""
    configure(client)
    before = client.get("/api/settings").json()

    body = client.patch(
        "/api/settings", json={"reveal_lines_by_default": True}
    ).json()

    assert body["reveal_lines_by_default"] is True
    assert body["chesscom_enabled"] is True
    assert body["chesscom_username"] == "alice"
    assert body["analysis_depth"] == before["analysis_depth"]
    assert body["background_analysis"] == before["background_analysis"]


def test_preferences_patch_ignores_omitted_fields(client):
    client.patch("/api/settings", json={"analysis_depth": 14})

    body = client.patch("/api/settings", json={"reveal_lines_by_default": True}).json()

    assert body["analysis_depth"] == 14


def test_preferences_patch_validates_depth(client):
    assert client.patch("/api/settings", json={"analysis_depth": 99}).status_code == 422


def test_evaluate_returns_lines_for_an_arbitrary_position(client):
    """A sideline has no stored analysis, so its position is evaluated live."""
    import chess
    from chess.engine import Cp

    from chess_analysis.engine import Line, PositionAnalysis

    board = chess.Board()
    board.push_san("e4")
    move = chess.Move.from_uci("e7e5")
    client.worker.evaluation = PositionAnalysis(
        fen=board.fen(),
        depth=18,
        multipv=3,
        lines=(Line(move=move, score=Cp(-25), pv=(move,)),),
    )

    body = client.post("/api/evaluate", json={"fen": board.fen()}).json()

    assert body["eval"] == {"cp": -25}
    assert 0 < body["win_percent"] < 50  # white-relative, black slightly better
    assert body["lines"][0]["san"] == "e5"
    assert body["depth"] == 18
    assert body["over"] is None


def test_evaluate_rejects_a_bad_fen(client):
    assert client.post("/api/evaluate", json={"fen": "not a fen"}).status_code == 422


def test_evaluate_scores_a_finished_position_without_the_engine(client):
    """Sidelines often end in mate; that is an answer, not an error."""
    import chess

    board = chess.Board()
    for san in ("f3", "e5", "g4", "Qh4#"):
        board.push_san(san)
    client.worker.evaluation = "should not be consulted"

    body = client.post("/api/evaluate", json={"fen": board.fen()}).json()

    assert body["over"] == "checkmate"
    assert body["win_percent"] == 0.0  # white is mated
    assert body["lines"] == []


def test_evaluate_surfaces_an_engine_failure(client):
    import chess

    from chess_analysis.engine import EngineError

    client.worker.evaluation_error = EngineError("no Stockfish binary found")

    response = client.post("/api/evaluate", json={"fen": chess.Board().fen()})

    assert response.status_code == 503
    assert "Stockfish" in response.json()["detail"]


def test_unknown_api_path_is_a_clear_404(client):
    """Without this, an unknown /api path falls through to the static mount and
    any non-GET comes back as a bare "Method Not Allowed" — which reads as a
    broken UI rather than a server running older code than the page."""
    response = client.post("/api/does-not-exist", json={})

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "/api/does-not-exist" in detail
    assert "restart the server" in detail


def test_unknown_api_path_404s_for_every_method(client):
    for call in (
        client.get("/api/nope"),
        client.put("/api/nope", json={}),
        client.patch("/api/nope", json={}),
        client.delete("/api/nope"),
    ):
        assert call.status_code == 404


def test_the_catch_all_does_not_shadow_real_routes(client):
    """It is registered last, so every real endpoint still wins."""
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/games").status_code == 200
    assert client.post("/api/sync").status_code == 400  # not configured, not 404
