from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from chess_analysis import db, store
from chess_analysis.api import create_app
from chess_analysis.platforms.chesscom import ArchiveResponse, RateLimited
from chess_analysis.platforms.lichess import LichessError
from chess_analysis.platforms.lichess import RateLimited as LichessRateLimited
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


LICHESS_PGN = '[White "alice"]\n[Black "carol"]\n\n1. e4 d5 *'


def lichess_entry(game_id: str, played_at: datetime):
    return {
        "id": game_id,
        "variant": "standard",
        "speed": "blitz",
        "createdAt": int(played_at.timestamp() * 1000),
        "lastMoveAt": int(played_at.timestamp() * 1000),
        "status": "resign",
        "winner": "white",
        "clock": {"initial": 180, "increment": 0},
        "players": {
            "white": {"user": {"name": "alice"}},
            "black": {"user": {"name": "carol"}},
        },
        "pgn": LICHESS_PGN,
    }


class FakeLichessClient:
    known_players = {"alice"}
    entries: list[dict] = []
    error: Exception | None = None
    tokens: list[str | None] = []

    def __init__(self, token=None):
        type(self).tokens.append(token)

    def export_games(self, username, *, max_games=None, since=None, until=None):
        if type(self).error is not None:
            raise type(self).error
        return list(type(self).entries)

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
    FakeLichessClient.error = None
    FakeLichessClient.known_players = {"alice"}
    FakeLichessClient.tokens = []
    FakeLichessClient.entries = [
        lichess_entry("l1", datetime(2026, 8, 4, 12, tzinfo=UTC)),
        lichess_entry("l2", datetime(2026, 8, 5, 12, tzinfo=UTC)),
    ]
    worker = FakeWorker()
    app = create_app(
        db_path=tmp_path / "api.db",
        client_factory=FakeClient,
        lichess_client_factory=FakeLichessClient,
        worker=worker,
    )
    with TestClient(app) as test_client:
        test_client.db_path = tmp_path / "api.db"
        test_client.worker = worker
        yield test_client


def configure(client, username="alice", **extra):
    return client.put(
        "/api/settings",
        json={"chesscom_enabled": True, "chesscom_username": username} | extra,
    )


def configure_lichess(client, username="alice", **extra):
    return client.put(
        "/api/settings",
        json={"lichess_enabled": True, "lichess_username": username} | extra,
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
    assert body["failures"] == []
    assert [p["platform"] for p in body["platforms"]] == ["chesscom"]
    assert body["platforms"][0]["last_synced_at"] is not None


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


def test_lichess_settings_validate_the_username(client):
    """Same contract as Chess.com: a bad username fails at save time."""
    response = configure_lichess(client, "ghost")

    assert response.status_code == 422
    assert "ghost" in response.json()["detail"]
    assert client.get("/api/settings").json()["lichess_enabled"] is False


def test_enabling_lichess_without_a_username_is_rejected(client):
    response = client.put(
        "/api/settings", json={"lichess_enabled": True, "lichess_username": " "}
    )
    assert response.status_code == 422


def test_the_token_is_stored_and_reported_only_as_present(client):
    configure_lichess(client, lichess_token="tok-123")

    body = client.get("/api/settings").json()
    assert body["lichess_enabled"] is True
    assert body["lichess_token_set"] is True
    assert "tok-123" not in client.get("/api/settings").text
    # Stored means used: validation and sync both go out authenticated.
    assert FakeLichessClient.tokens == ["tok-123"]


def test_the_token_survives_a_save_that_omits_it(client):
    """The form cannot show the token, so a blank field is "unchanged" — not
    "delete it", which would silently drop the user back to anonymous limits."""
    configure_lichess(client, lichess_token="tok-123")

    configure_lichess(client)

    conn = db.connect(client.db_path)
    assert store.load_settings(conn).lichess_token == "tok-123"
    conn.close()
    assert client.get("/api/settings").json()["lichess_token_set"] is True


def test_an_empty_token_forgets_the_stored_one(client):
    configure_lichess(client, lichess_token="tok-123")

    configure_lichess(client, lichess_token="")

    assert client.get("/api/settings").json()["lichess_token_set"] is False


def test_changing_the_lichess_username_resets_its_cursors(client):
    configure_lichess(client)
    client.post("/api/sync")
    assert client.get("/api/settings").json()["lichess_last_synced_at"] is not None

    FakeLichessClient.known_players = {"alice", "dave"}
    configure_lichess(client, "dave")

    body = client.get("/api/settings").json()
    assert body["lichess_last_synced_at"] is None
    assert body["lichess_backfill_cursor"] is None


def test_the_platforms_are_independently_toggleable(client):
    """Neither, either or both (PRD 4.1). Saving one must not disable the
    other, so the form always sends both sections."""
    client.put(
        "/api/settings",
        json={
            "chesscom_enabled": True,
            "chesscom_username": "alice",
            "lichess_enabled": True,
            "lichess_username": "alice",
        },
    )

    body = client.get("/api/settings").json()
    assert body["chesscom_enabled"] is True
    assert body["lichess_enabled"] is True


def test_a_rejected_lichess_username_saves_nothing_at_all(client):
    """Both platforms are validated before either is written."""
    client.put(
        "/api/settings",
        json={
            "chesscom_enabled": True,
            "chesscom_username": "alice",
            "lichess_enabled": True,
            "lichess_username": "ghost",
        },
    )

    body = client.get("/api/settings").json()
    assert body["chesscom_enabled"] is False
    assert body["lichess_enabled"] is False


def test_lichess_sync_inserts_games(client):
    configure_lichess(client)

    body = client.post("/api/sync").json()

    assert body["inserted"] == 2
    assert [p["platform"] for p in body["platforms"]] == ["lichess"]
    assert body["total_games"] == 2


def test_syncing_both_platforms_reports_each_one(client):
    configure(client, lichess_enabled=True, lichess_username="alice")

    body = client.post("/api/sync").json()

    assert [p["platform"] for p in body["platforms"]] == ["chesscom", "lichess"]
    assert [p["inserted"] for p in body["platforms"]] == [3, 2]
    assert body["inserted"] == 5
    assert body["total_games"] == 5
    # Everything new is queued, whichever platform it came from.
    assert len(client.worker.enqueued) == 5


def test_one_platform_failing_does_not_discard_the_others_games(client):
    """A rate-limited account must not cost the user the games the other one
    just returned (PRD 4.2)."""
    configure(client, lichess_enabled=True, lichess_username="alice")
    FakeLichessClient.error = LichessRateLimited("Lichess is rate limiting")

    response = client.post("/api/sync")

    assert response.status_code == 200
    body = response.json()
    assert [p["platform"] for p in body["platforms"]] == ["chesscom"]
    assert body["failures"] == [
        {"platform": "lichess", "message": "Lichess is rate limiting"}
    ]
    assert body["total_games"] == 3


def test_every_platform_failing_is_an_error_response(client):
    configure_lichess(client)
    FakeLichessClient.error = LichessError("lichess is down")

    response = client.post("/api/sync")

    assert response.status_code == 502
    assert "lichess is down" in response.json()["detail"]


def test_the_lichess_game_reaches_the_list(client):
    configure_lichess(client)
    client.post("/api/sync")

    game = client.get("/api/games").json()["games"][0]

    assert game["platform"] == "lichess"
    assert game["opponent"] == "carol"
    assert game["player_color"] == "white"
    assert game["result"] == "win"
    assert game["time_control"] == "180"
    assert game["url"] == "https://lichess.org/l2"


def test_history_depth_spans_both_platforms(client):
    """The indicator describes the list, which mixes them."""
    configure(client, lichess_enabled=True, lichess_username="alice")
    client.post("/api/sync")

    body = client.get("/api/games").json()

    assert body["total"] == 5
    assert body["history_back_to"].startswith("2026-08-01")


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
    assert client.get("/api/games", params={"limit": 501}).status_code == 422
    assert client.get("/api/games", params={"offset": -1}).status_code == 422
    # The list re-requests its whole open window, so the ceiling is a screenful.
    assert client.get("/api/games", params={"limit": 500}).status_code == 200


def games_played_as(client, colors):
    """Store one game per colour, so the colour filter has something to sort."""
    conn = db.connect(client.db_path)
    for index, color in enumerate(colors):
        conn.execute(
            "INSERT INTO games (platform, platform_game_id, played_at, pgn,"
            " player_color, result, time_control) VALUES"
            " ('chesscom', ?, ?, '*', ?, ?, ?)",
            (
                f"g{index}",
                f"2026-08-{index + 1:02d}T00:00:00+00:00",
                color,
                "win" if index % 2 else "loss",
                "180" if index % 2 else "600",
            ),
        )
    conn.close()


def test_filtering_by_colour_and_result(client):
    games_played_as(client, ["white", "black", "white", "black"])

    white = client.get("/api/games", params={"color": "white"}).json()
    losses = client.get("/api/games", params={"result": "loss"}).json()

    assert {g["player_color"] for g in white["games"]} == {"white"}
    assert {g["result"] for g in losses["games"]} == {"loss"}


def test_filtered_total_matches_the_filtered_rows(client):
    """The count drives "showing 20 of N"; taken unfiltered it would read as a
    list that is hiding rows it is not actually hiding."""
    games_played_as(client, ["white", "black", "white", "black"])

    body = client.get("/api/games", params={"color": "white"}).json()

    assert body["total"] == len(body["games"]) == 2


def test_filtering_by_time_class(client):
    games_played_as(client, ["white", "black", "white", "black"])

    blitz = client.get("/api/games", params={"time_class": "blitz"}).json()
    rapid = client.get("/api/games", params={"time_class": "rapid"}).json()

    assert {g["time_control"] for g in blitz["games"]} == {"180"}
    assert {g["time_control"] for g in rapid["games"]} == {"600"}


def test_an_unparseable_time_control_falls_in_no_class(client):
    """Chess.com sends "-" for some games; it must not silently count as
    bullet just because CAST says zero."""
    conn = db.connect(client.db_path)
    conn.execute(
        "INSERT INTO games (platform, platform_game_id, played_at, pgn,"
        " time_control) VALUES ('chesscom', 'odd', '2026-08-01T00:00:00+00:00',"
        " '*', '-')"
    )
    conn.close()

    for time_class in ("bullet", "blitz", "rapid", "daily"):
        body = client.get("/api/games", params={"time_class": time_class}).json()
        assert body["games"] == [], time_class


def test_filtering_to_games_with_errors(client):
    configure(client)
    client.post("/api/sync")
    store_analysis(client, 1, [analysed_ply(severity="blunder", win_percent_loss=40.0)])
    store_analysis(client, 2, [analysed_ply()])  # analysed, but played clean

    body = client.get("/api/games", params={"with_errors": "true"}).json()

    assert [g["id"] for g in body["games"]] == [1]
    assert body["total"] == 1


def test_unknown_filter_values_are_rejected(client):
    assert client.get("/api/games", params={"color": "purple"}).status_code == 422
    assert client.get("/api/games", params={"time_class": "hyper"}).status_code == 422


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

    assert client.get("/api/games/1/analysis").json() == {
        "positions": [],
        "summary": None,
    }


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


def test_summary_counts_the_players_errors(client):
    """The list and the game header are built from this, so it must answer
    "which game is worth reviewing" without shipping every ply."""
    from chess.engine import Cp

    configure(client)
    client.post("/api/sync")
    store_analysis(
        client,
        1,
        [
            analysed_ply(ply=0, win_percent_loss=1.0),
            analysed_ply(ply=1, win_percent_loss=12.0, severity="inaccuracy"),
            analysed_ply(ply=2, win_percent_loss=24.0, severity="mistake"),
            analysed_ply(
                ply=3, played_move_score=Cp(-400), win_percent_loss=41.0,
                severity="blunder",
            ),
        ],
    )

    summary = client.get("/api/games/1/analysis").json()["summary"]

    assert summary["moves"] == 4
    assert summary["inaccuracies"] == 1
    assert summary["mistakes"] == 1
    assert summary["blunders"] == 1
    assert summary["average_loss"] == pytest.approx(19.5)
    assert 0 < summary["accuracy"] < 100


def test_summary_ignores_the_opponents_moves(client):
    """Accuracy answers "how well did I play", so the opponent's half of the
    game cannot count towards it (PRD 4.4)."""
    import chess

    configure(client)  # alice, who played white in the fixture PGN
    client.post("/api/sync")
    store_analysis(
        client,
        1,
        [
            analysed_ply(ply=0, side_to_move=chess.WHITE, win_percent_loss=2.0),
            analysed_ply(ply=1, side_to_move=chess.BLACK, win_percent_loss=60.0),
        ],
    )

    summary = client.get("/api/games/1/analysis").json()["summary"]

    assert summary["moves"] == 1
    assert summary["average_loss"] == pytest.approx(2.0)


def test_game_list_carries_summaries(client):
    """One batched query behind the list, not one per row."""
    configure(client)
    client.post("/api/sync")
    store_analysis(client, 1, [analysed_ply(win_percent_loss=30.0, severity="blunder")])

    games = {g["id"]: g for g in client.get("/api/games").json()["games"]}

    assert games[1]["analysis"]["blunders"] == 1
    # Nothing measured yet is not the same as a game played without error.
    assert games[2]["analysis"] is None


def test_a_clean_game_still_reports_a_summary(client):
    """Zero errors and no analysis must not look the same in the list."""
    configure(client)
    client.post("/api/sync")
    store_analysis(client, 1, [analysed_ply(win_percent_loss=0.0)])

    summary = client.get("/api/games/1/analysis").json()["summary"]

    assert summary["blunders"] == 0
    # The fitted curve tops out a hair under 100; it rounds to 100 on screen.
    assert summary["accuracy"] == pytest.approx(100.0, abs=0.001)


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
