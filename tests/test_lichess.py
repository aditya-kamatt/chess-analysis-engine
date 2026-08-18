import json
from datetime import UTC, datetime

import httpx
import pytest

from chess_analysis.models import Platform, Result
from chess_analysis.platforms.lichess import (
    InvalidToken,
    LichessClient,
    LichessError,
    RateLimited,
    UnknownPlayer,
    parse_game,
    parse_games,
)

PGN = '[Event "Rated blitz game"]\n[White "alice"]\n[Black "bob"]\n\n1. e4 d5 2. exd5 *'


def entry(**overrides):
    base = {
        "id": "abcd1234",
        "rated": True,
        "variant": "standard",
        "speed": "blitz",
        "createdAt": 1_754_000_000_000,
        "lastMoveAt": 1_754_000_600_000,
        "status": "resign",
        "winner": "white",
        "clock": {"initial": 180, "increment": 2, "totalTime": 260},
        "opening": {"eco": "B01", "name": "Scandinavian Defense"},
        "players": {
            "white": {"user": {"name": "Alice", "id": "alice"}, "rating": 1500},
            "black": {"user": {"name": "bob", "id": "bob"}, "rating": 1490},
        },
        "pgn": PGN,
    }
    return base | overrides


def ndjson(*entries) -> str:
    return "".join(json.dumps(one) + "\n" for one in entries)


def client_for(handler, **kwargs) -> LichessClient:
    return LichessClient(transport=httpx.MockTransport(handler), **kwargs)


def test_user_agent_is_always_sent():
    seen = []

    def handler(request):
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, text=ndjson())

    with client_for(handler) as client:
        client.player_exists("alice")
        client.export_games("alice", max_games=50)

    assert len(seen) == 2
    assert all(agent and "chess-analysis-engine" in agent for agent in seen)


def test_the_token_is_sent_as_a_bearer_header():
    """It raises the rate limits; without one the same calls still work."""
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, text=ndjson())

    with client_for(handler, token="tok-123") as client:
        client.export_games("alice", max_games=50)
    with client_for(handler) as client:
        client.export_games("alice", max_games=50)

    assert seen == ["Bearer tok-123", None]


def test_unknown_username_raises():
    with client_for(lambda request: httpx.Response(404)) as client:
        assert client.player_exists("nobody") is False
        with pytest.raises(UnknownPlayer):
            client.export_games("nobody", max_games=50)


def test_a_rejected_token_is_named_as_such():
    """"Not authorised" and "no such player" are different problems with
    different fixes, so they must not arrive as the same message."""
    with client_for(lambda request: httpx.Response(401), token="stale") as client:
        with pytest.raises(InvalidToken):
            client.export_games("alice", max_games=50)


def test_rate_limiting_is_retried_then_succeeds():
    responses = [httpx.Response(429), httpx.Response(200, text=ndjson(entry()))]
    delays = []

    with client_for(lambda request: responses.pop(0), sleep=delays.append) as client:
        assert len(client.export_games("alice", max_games=50)) == 1

    # Lichess asks for a full minute, not a doubling from a short delay.
    assert delays == [60.0]


def test_persistent_rate_limiting_surfaces_an_error():
    with client_for(
        lambda request: httpx.Response(429), max_retries=1, sleep=lambda _: None
    ) as client:
        with pytest.raises(RateLimited):
            client.export_games("alice", max_games=50)


def test_network_failure_is_wrapped():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with client_for(handler) as client:
        with pytest.raises(LichessError, match="network error"):
            client.export_games("alice", max_games=50)


def test_a_first_sync_asks_for_a_bounded_number_of_games():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, text=ndjson(entry()))

    with client_for(handler) as client:
        client.export_games("alice", max_games=50)

    assert seen["max"] == "50"
    assert seen["sort"] == "dateDesc"
    # One request has to carry the PGN and the opening, or every game would
    # cost a second call.
    assert seen["pgnInJson"] == "true"
    assert seen["opening"] == "true"
    assert "since" not in seen


def test_since_is_sent_as_milliseconds():
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, text=ndjson())

    with client_for(handler) as client:
        client.export_games("alice", since=datetime(2026, 8, 1, tzinfo=UTC))

    assert seen["since"] == "1785542400000"
    assert datetime.fromtimestamp(int(seen["since"]) / 1000, tz=UTC) == datetime(
        2026, 8, 1, tzinfo=UTC
    )


def test_ndjson_lines_are_decoded_individually():
    body = ndjson(entry(), entry(id="second"))

    with client_for(lambda request: httpx.Response(200, text=body)) as client:
        entries = client.export_games("alice", max_games=50)

    assert [one["id"] for one in entries] == ["abcd1234", "second"]


def test_a_malformed_line_is_reported_not_swallowed():
    with client_for(lambda request: httpx.Response(200, text="{oops\n")) as client:
        with pytest.raises(LichessError, match="could not parse"):
            client.export_games("alice", max_games=50)


def test_parses_a_game_from_the_players_side():
    game = parse_game(entry(), "alice")

    assert game is not None
    assert game.platform == Platform.LICHESS
    assert game.platform_game_id == "abcd1234"
    assert game.player_color == "white"
    assert game.opponent == "bob"
    assert game.result == Result.WIN
    assert game.eco_code == "B01"
    assert game.url == "https://lichess.org/abcd1234"


def test_username_matching_ignores_case():
    """Lichess echoes the display casing, not what the user typed."""
    game = parse_game(entry(), "ALICE")
    assert game is not None and game.player_color == "white"


def test_parses_from_the_black_side():
    game = parse_game(entry(), "bob")
    assert game is not None
    assert game.player_color == "black"
    assert game.opponent == "Alice"
    assert game.result == Result.LOSS


def test_the_finish_time_is_what_the_list_sorts_on():
    """`lastMoveAt`, not `createdAt`: the list is "my recent games", and a long
    game started yesterday belongs where it ended."""
    game = parse_game(entry(), "alice")
    assert game is not None
    assert game.played_at == datetime(2025, 7, 31, 22, 23, 20, tzinfo=UTC)


def test_a_game_still_in_its_first_move_falls_back_to_its_start():
    game = parse_game(entry(lastMoveAt=None), "alice")
    assert game is not None
    assert game.played_at == datetime(2025, 7, 31, 22, 13, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"winner": "white"}, Result.WIN),
        ({"winner": "black"}, Result.LOSS),
        ({"winner": None, "status": "draw"}, Result.DRAW),
        ({"winner": None, "status": "stalemate"}, Result.DRAW),
        # Nobody won and it was not a draw either; there is no result to show.
        ({"winner": None, "status": "aborted"}, None),
        ({"winner": None, "status": "noStart"}, None),
    ],
)
def test_outcomes_collapse_to_three_results_or_none(overrides, expected):
    game = parse_game(entry(**overrides), "alice")
    assert game is not None and game.result == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "180+2"),
        ({"clock": {"initial": 300, "increment": 0}}, "300"),
        ({"clock": {"initial": 60, "increment": 1}}, "60+1"),
        # Correspondence, in the "seconds per move" shape Chess.com uses, so
        # one SQL rule sorts both platforms into time classes.
        ({"clock": None, "daysPerTurn": 3}, "1/259200"),
        ({"clock": None}, None),
    ],
)
def test_time_control_is_stored_in_chesscoms_notation(overrides, expected):
    game = parse_game(entry(**overrides), "alice")
    assert game is not None and game.time_control == expected


def test_the_eco_code_falls_back_to_the_pgn():
    """`opening` is requested but absent from unfinished or very short games."""
    pgn = '[ECO "C20"]\n[White "alice"]\n[Black "bob"]\n\n1. e4 e5 *'
    game = parse_game(entry(opening=None, pgn=pgn), "alice")
    assert game is not None and game.eco_code == "C20"


def test_a_game_against_the_engine_names_its_opponent():
    against_ai = entry(
        players={
            "white": {"user": {"name": "alice"}},
            "black": {"aiLevel": 5},
        }
    )
    game = parse_game(against_ai, "alice")
    assert game is not None and game.opponent == "Stockfish level 5"


@pytest.mark.parametrize(
    "bad",
    [
        {"variant": "chess960"},
        {"variant": "atomic"},
        {"variant": "fromPosition"},
        {"pgn": None},
        {"pgn": ""},
        {"id": None},
    ],
)
def test_unusable_entries_are_filtered(bad):
    """Variants and games with nothing to replay never reach the queue (PRD 7)."""
    assert parse_game(entry(**bad), "alice") is None


def test_games_belonging_to_another_player_are_ignored():
    assert parse_game(entry(), "carol") is None


def test_parse_games_filters_in_bulk():
    entries = [entry(), entry(id="g2", variant="crazyhouse"), entry(id="g3")]

    games = parse_games(entries, "alice")

    assert [game.platform_game_id for game in games] == ["abcd1234", "g3"]
