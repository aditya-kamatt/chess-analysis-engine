from datetime import UTC, datetime

import httpx
import pytest

from chess_analysis.models import Platform, Result
from chess_analysis.platforms.chesscom import (
    ChessComClient,
    ChessComError,
    RateLimited,
    UnknownPlayer,
    parse_archive,
    parse_entry,
)

PGN = '[Event "Live Chess"]\n[ECO "B01"]\n[White "alice"]\n[Black "bob"]\n\n1. e4 d5 2. exd5 *'


def entry(**overrides):
    base = {
        "url": "https://www.chess.com/game/live/1234",
        "uuid": "abc-123",
        "pgn": PGN,
        "time_control": "180",
        "end_time": 1_754_000_000,
        "rules": "chess",
        "white": {"username": "Alice", "result": "win", "rating": 1500},
        "black": {"username": "bob", "result": "resigned", "rating": 1490},
    }
    return base | overrides


def client_for(handler, **kwargs) -> ChessComClient:
    return ChessComClient(transport=httpx.MockTransport(handler), **kwargs)


def test_user_agent_is_always_sent():
    """Cloudflare rejects requests without one, so it lives on the client."""
    seen = []

    def handler(request):
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, json={"archives": []})

    with client_for(handler) as client:
        client.archive_urls("alice")
        client.player_exists("alice")

    assert len(seen) == 2
    assert all(agent and "chess-analysis-engine" in agent for agent in seen)


def test_unknown_username_raises():
    with client_for(lambda request: httpx.Response(404)) as client:
        assert client.player_exists("nobody") is False
        with pytest.raises(UnknownPlayer):
            client.archive_urls("nobody")


def test_rate_limiting_is_retried_then_succeeds():
    responses = [httpx.Response(429), httpx.Response(429), httpx.Response(200, json={"archives": ["u"]})]
    delays = []

    with client_for(lambda request: responses.pop(0), sleep=delays.append) as client:
        assert client.archive_urls("alice") == ["u"]

    assert delays == [1.0, 2.0]  # backs off rather than hammering


def test_persistent_rate_limiting_surfaces_an_error():
    with client_for(
        lambda request: httpx.Response(429), max_retries=2, sleep=lambda _: None
    ) as client:
        with pytest.raises(RateLimited):
            client.archive_urls("alice")


def test_network_failure_is_wrapped():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with client_for(handler) as client:
        with pytest.raises(ChessComError, match="network error"):
            client.archive_urls("alice")


def test_archive_is_fetched_conditionally():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(304)

    with client_for(handler) as client:
        response = client.fetch_archive("https://x/games/2026/08", etag='W/"v1"')

    assert seen["if-none-match"] == 'W/"v1"'
    assert response.modified is False
    assert response.entries == []


def test_modified_archive_returns_entries_and_validators():
    def handler(request):
        return httpx.Response(
            200,
            json={"games": [entry()]},
            headers={"ETag": 'W/"v2"', "Last-Modified": "Fri, 15 Aug 2026 00:00:00 GMT"},
        )

    with client_for(handler) as client:
        response = client.fetch_archive("https://x/games/2026/08")

    assert response.modified is True
    assert len(response.entries) == 1
    assert response.etag == 'W/"v2"'
    assert response.last_modified == "Fri, 15 Aug 2026 00:00:00 GMT"


def test_parses_a_game_from_the_players_side():
    game = parse_entry(entry(), "alice")

    assert game is not None
    assert game.platform == Platform.CHESSCOM
    assert game.platform_game_id == "abc-123"
    assert game.player_color == "white"
    assert game.opponent == "bob"
    assert game.result == Result.WIN
    assert game.time_control == "180"
    assert game.url == "https://www.chess.com/game/live/1234"


def test_username_matching_ignores_case():
    """Chess.com echoes the display casing, not what the user typed."""
    game = parse_entry(entry(), "ALICE")
    assert game is not None and game.player_color == "white"


def test_parses_from_the_black_side():
    game = parse_entry(entry(), "bob")
    assert game is not None
    assert game.player_color == "black"
    assert game.opponent == "Alice"
    assert game.result == Result.LOSS


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("win", Result.WIN),
        ("checkmated", Result.LOSS),
        ("timeout", Result.LOSS),
        ("resigned", Result.LOSS),
        ("abandoned", Result.LOSS),
        ("agreed", Result.DRAW),
        ("stalemate", Result.DRAW),
        ("repetition", Result.DRAW),
        ("insufficient", Result.DRAW),
        ("50move", Result.DRAW),
    ],
)
def test_result_codes_collapse_to_three_outcomes(code, expected):
    game = parse_entry(entry(white={"username": "alice", "result": code}), "alice")
    assert game is not None and game.result == expected


def test_eco_comes_from_the_pgn_not_the_json():
    """The archive's `eco` field is a URL to the opening page."""
    game = parse_entry(entry(eco="https://www.chess.com/openings/Scandinavian"), "alice")
    assert game is not None and game.eco_code == "B01"


def test_end_time_becomes_an_aware_timestamp():
    """Epoch seconds in, UTC-aware datetime out — naive timestamps would sort
    wrongly against the ISO strings the database stores."""
    game = parse_entry(entry(), "alice")
    assert game is not None
    assert game.played_at == datetime(2025, 7, 31, 22, 13, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    "bad",
    [
        {"rules": "chess960"},
        {"rules": "bughouse"},
        {"pgn": None},
        {"pgn": ""},
        {"end_time": None},
    ],
)
def test_unusable_entries_are_filtered(bad):
    """Variants and abandoned games never reach the analysis queue (PRD 7)."""
    assert parse_entry(entry(**bad), "alice") is None


def test_games_belonging_to_another_player_are_ignored():
    assert parse_entry(entry(), "carol") is None


def test_parse_archive_filters_in_bulk():
    entries = [entry(), entry(uuid="d-2", rules="chess960"), entry(uuid="d-3")]
    assert len(parse_archive(entries, "alice")) == 2
