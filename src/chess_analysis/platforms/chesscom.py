"""Chess.com public API client (PRD 4.1, 4.2, 7).

Two constraints shape this whole module:

* Every request must carry a descriptive `User-Agent` — Cloudflare rejects
  requests without one. It is set on the client, not per call, so there is no
  code path that can omit it.
* Requests must be issued serially. Parallel calls to the same endpoint return
  429, so this is a plain synchronous client and callers loop over archives.

No authentication: reading a player's archives is public.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from chess_analysis.models import Game, Platform, Result
from chess_analysis.platforms import PlatformError, eco_from_pgn

BASE_URL = "https://api.chess.com/pub"

DEFAULT_USER_AGENT = (
    "chess-analysis-engine/0.1 (self-hosted single-user game analysis tool)"
)

# Chess.com reports the outcome from each player's side with a fine-grained
# reason; we only need the three-way result.
_DRAW_RESULTS = frozenset(
    {
        "agreed",
        "repetition",
        "stalemate",
        "insufficient",
        "50move",
        "timevsinsufficient",
    }
)
_WIN_RESULTS = frozenset({"win"})

# Anything else is a variant and is filtered at sync rather than analysed (PRD 7).
STANDARD_RULES = "chess"


class ChessComError(PlatformError):
    """The Chess.com API could not be used."""


class UnknownPlayer(ChessComError):
    """No such username."""


class RateLimited(ChessComError):
    """Still rate limited after backing off."""


@dataclass
class ArchiveResponse:
    """The result of fetching one monthly archive.

    `modified` is False when the server answered 304, meaning the stored copy is
    still current and nothing needs re-parsing.
    """

    modified: bool
    entries: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None


class ChessComClient:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._max_retries = max_retries
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ChessComClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def player_exists(self, username: str) -> bool:
        """One cheap request, so a bad username fails at settings-save time
        rather than silently at first sync (PRD 4.1)."""
        try:
            self._get(f"{BASE_URL}/player/{username.strip().lower()}")
        except UnknownPlayer:
            return False
        return True

    def archive_urls(self, username: str) -> list[str]:
        """Monthly archive URLs, oldest first — the order Chess.com returns."""
        response = self._get(
            f"{BASE_URL}/player/{username.strip().lower()}/games/archives"
        )
        return list(response.json().get("archives", []))

    def fetch_archive(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> ArchiveResponse:
        """Fetch one month, conditionally when we have validators for it."""
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        response = self._get(url, headers=headers)
        if response.status_code == 304:
            return ArchiveResponse(modified=False, etag=etag, last_modified=last_modified)

        return ArchiveResponse(
            modified=True,
            entries=list(response.json().get("games", [])),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )

    def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        delay = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(url, headers=headers)
            except httpx.RequestError as exc:
                raise ChessComError(f"network error contacting Chess.com: {exc}") from exc

            if response.status_code == 404:
                raise UnknownPlayer(f"Chess.com returned 404 for {url}")
            if response.status_code in (429, 503):
                if attempt == self._max_retries:
                    raise RateLimited(
                        "Chess.com is rate limiting this account; try again shortly"
                    )
                self._sleep(delay)
                delay *= 2
                continue
            if response.status_code >= 400 and response.status_code != 304:
                raise ChessComError(
                    f"Chess.com returned {response.status_code} for {url}"
                )
            return response

        raise RateLimited("exhausted retries contacting Chess.com")


def parse_archive(entries: list[dict[str, Any]], username: str) -> list[Game]:
    """Normalise archive entries, dropping anything unusable.

    Variants and games without a PGN (aborted before any move was recorded) are
    filtered here so they never reach the analysis queue.
    """
    games = []
    for entry in entries:
        game = parse_entry(entry, username)
        if game is not None:
            games.append(game)
    return games


def parse_entry(entry: dict[str, Any], username: str) -> Game | None:
    if entry.get("rules") != STANDARD_RULES:
        return None

    pgn = entry.get("pgn")
    if not pgn:
        return None

    target = username.strip().lower()
    white = entry.get("white", {})
    black = entry.get("black", {})

    if white.get("username", "").lower() == target:
        player, opponent, color = white, black, "white"
    elif black.get("username", "").lower() == target:
        player, opponent, color = black, white, "black"
    else:
        # Not this player's game; the archive belongs to someone else.
        return None

    end_time = entry.get("end_time")
    if end_time is None:
        return None

    return Game(
        platform=Platform.CHESSCOM,
        platform_game_id=_game_id(entry),
        played_at=datetime.fromtimestamp(end_time, tz=UTC),
        pgn=pgn,
        time_control=entry.get("time_control"),
        player_color=color,
        opponent=opponent.get("username"),
        result=_result(player.get("result")),
        # The archive JSON's `eco` field is a URL; the code lives in the PGN.
        eco_code=eco_from_pgn(pgn),
        url=entry.get("url"),
    )


def _game_id(entry: dict[str, Any]) -> str:
    uuid = entry.get("uuid")
    if uuid:
        return str(uuid)
    return str(entry.get("url", "")).rstrip("/").rsplit("/", 1)[-1]


def _result(code: str | None) -> Result | None:
    if code is None:
        return None
    if code in _WIN_RESULTS:
        return Result.WIN
    if code in _DRAW_RESULTS:
        return Result.DRAW
    return Result.LOSS
