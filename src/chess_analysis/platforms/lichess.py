"""Lichess public API client (PRD 4.1, 4.2, 7).

Two things differ from Chess.com and shape this module:

* There are no monthly archives. One endpoint streams a player's games newest
  first, narrowed by `max` for a first sync and by `since` afterwards, so a
  sync is one request rather than a walk.
* Authentication is optional. A personal API token raises the rate limits and
  is the user's own, generated from their Lichess account settings — no OAuth
  flow, since the app serves only its operator. It is sent as a bearer header
  set once on the client and is never logged or echoed back.

The export endpoint answers NDJSON: one JSON object per line, each carrying the
game's PGN alongside the metadata the list needs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from chess_analysis.models import Game, Platform, Result
from chess_analysis.platforms import PlatformError, eco_from_pgn

BASE_URL = "https://lichess.org"

DEFAULT_USER_AGENT = (
    "chess-analysis-engine/0.1 (self-hosted single-user game analysis tool)"
)

# Lichess asks for a full minute after a 429 rather than the usual doubling from
# a short delay, and answers a second early retry with another 429.
RATE_LIMIT_DELAY = 60.0

# Anything else is a variant and is filtered at sync rather than analysed (PRD 7).
STANDARD_VARIANT = "standard"

# Games that never started have no result to record; every other terminal
# status either has a winner or is a draw.
_NO_RESULT_STATUSES = frozenset({"aborted", "noStart", "unknownFinish"})

_SECONDS_PER_DAY = 86_400


class LichessError(PlatformError):
    """The Lichess API could not be used."""


class UnknownPlayer(LichessError):
    """No such username."""


class RateLimited(LichessError):
    """Still rate limited after backing off."""


class InvalidToken(LichessError):
    """The stored API token was rejected."""


class LichessClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 60.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._max_retries = max_retries
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/x-ndjson",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LichessClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def player_exists(self, username: str) -> bool:
        """One cheap request, so a bad username fails at settings-save time
        rather than silently at first sync (PRD 4.1)."""
        try:
            self._get(f"{BASE_URL}/api/user/{username.strip()}")
        except UnknownPlayer:
            return False
        return True

    def export_games(
        self,
        username: str,
        *,
        max_games: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """A player's games, newest first, as decoded NDJSON entries.

        Bounded by `max_games` or by `since`, so the response is a sync's worth
        of games rather than a whole career and is read whole rather than
        streamed. `opening` and `pgnInJson` are what make one request enough:
        the PGN and the ECO code arrive with the metadata instead of costing a
        second call per game.
        """
        params: dict[str, str] = {
            "pgnInJson": "true",
            "opening": "true",
            "sort": "dateDesc",
        }
        if max_games is not None:
            params["max"] = str(max_games)
        if since is not None:
            params["since"] = _millis(since)
        if until is not None:
            params["until"] = _millis(until)

        response = self._get(
            f"{BASE_URL}/api/games/user/{username.strip()}", params=params
        )
        return _decode_ndjson(response.text)

    def _get(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                raise LichessError(f"network error contacting Lichess: {exc}") from exc

            if response.status_code == 404:
                raise UnknownPlayer(f"Lichess returned 404 for {url}")
            if response.status_code == 401:
                raise InvalidToken("Lichess rejected the API token")
            if response.status_code == 429:
                if attempt == self._max_retries:
                    raise RateLimited(
                        "Lichess is rate limiting this account; try again shortly"
                    )
                self._sleep(RATE_LIMIT_DELAY)
                continue
            if response.status_code >= 400:
                raise LichessError(
                    f"Lichess returned {response.status_code} for {url}"
                )
            return response

        raise RateLimited("exhausted retries contacting Lichess")


def _decode_ndjson(body: str) -> list[dict[str, Any]]:
    """Parse the NDJSON body, ignoring the blank line it ends with."""
    entries = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise LichessError(f"could not parse the Lichess response: {exc}") from exc
    return entries


def parse_games(entries: list[dict[str, Any]], username: str) -> list[Game]:
    """Normalise export entries, dropping anything unusable.

    Variants and games without a PGN are filtered here so they never reach the
    analysis queue.
    """
    games = []
    for entry in entries:
        game = parse_game(entry, username)
        if game is not None:
            games.append(game)
    return games


def parse_game(entry: dict[str, Any], username: str) -> Game | None:
    if entry.get("variant") != STANDARD_VARIANT:
        return None

    pgn = entry.get("pgn")
    game_id = entry.get("id")
    if not pgn or not game_id:
        return None

    target = username.strip().lower()
    players = entry.get("players") or {}
    white = players.get("white") or {}
    black = players.get("black") or {}

    if _name(white) == target:
        opponent, color = black, "white"
    elif _name(black) == target:
        opponent, color = white, "black"
    else:
        # Not this player's game; the export belongs to someone else.
        return None

    # `lastMoveAt` is when the game finished, which is what Chess.com's
    # `end_time` means and what the list sorts on.
    played_at = entry.get("lastMoveAt") or entry.get("createdAt")
    if played_at is None:
        return None

    return Game(
        platform=Platform.LICHESS,
        platform_game_id=str(game_id),
        played_at=datetime.fromtimestamp(played_at / 1000, tz=UTC),
        pgn=pgn,
        time_control=_time_control(entry),
        player_color=color,
        opponent=_opponent(opponent),
        result=_result(entry, color),
        eco_code=(entry.get("opening") or {}).get("eco") or eco_from_pgn(pgn),
        url=f"{BASE_URL}/{game_id}",
    )


def _millis(value: datetime) -> str:
    """Lichess takes timestamps as milliseconds since the epoch."""
    return str(int(value.timestamp() * 1000))


def _name(side: dict[str, Any]) -> str | None:
    user = side.get("user") or {}
    name = user.get("name")
    return name.lower() if name else None


def _opponent(side: dict[str, Any]) -> str | None:
    user = side.get("user") or {}
    name = user.get("name")
    if name:
        return str(name)
    level = side.get("aiLevel")
    # Stockfish games have no user on that side, and "—" would read as missing
    # data rather than as who was actually played.
    return f"Stockfish level {level}" if level is not None else None


def _result(entry: dict[str, Any], color: str) -> Result | None:
    winner = entry.get("winner")
    if winner:
        return Result.WIN if winner == color else Result.LOSS
    if entry.get("status") in _NO_RESULT_STATUSES:
        return None
    return Result.DRAW


def _time_control(entry: dict[str, Any]) -> str | None:
    """Chess.com's notation, because the time-class filter reads both.

    Storing Lichess' own shape would mean a second set of rules in the SQL that
    sorts games into bullet, blitz, rapid and daily — one of which would drift.
    """
    clock = entry.get("clock") or {}
    initial = clock.get("initial")
    if initial is not None:
        increment = clock.get("increment") or 0
        return f"{initial}+{increment}" if increment else str(initial)

    days = entry.get("daysPerTurn")
    if days:
        return f"1/{int(days) * _SECONDS_PER_DAY}"
    return None
