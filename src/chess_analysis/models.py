"""Row shapes shared by storage, sync and the API (PRD 5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Platform(StrEnum):
    CHESSCOM = "chesscom"
    LICHESS = "lichess"


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"
    UNANALYSABLE = "unanalysable"
    """Aborted games and anything else with no moves to work with (PRD 7)."""


class Result(StrEnum):
    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"


@dataclass(frozen=True)
class Settings:
    """The single settings row. `last_synced_at` and `backfill_cursor` are kept
    rigorously separate: one says how fresh the data is, the other how deep the
    history goes (PRD 4.2)."""

    chesscom_enabled: bool = False
    chesscom_username: str | None = None
    chesscom_last_synced_at: datetime | None = None
    chesscom_backfill_cursor: datetime | None = None
    lichess_enabled: bool = False
    lichess_username: str | None = None
    lichess_token: str | None = None
    lichess_last_synced_at: datetime | None = None
    lichess_backfill_cursor: datetime | None = None
    reveal_lines_by_default: bool = False
    analysis_depth: int = 20
    background_analysis: bool = True
    """Analyse the whole synced archive, not only games you open."""

    @property
    def lichess_token_set(self) -> bool:
        """Whether a token is stored. The token itself never leaves this
        process — the UI needs to know it is there, not what it is (PRD 4.1)."""
        return bool(self.lichess_token)


@dataclass(frozen=True)
class GameFilter:
    """Which games the list is asking for. Every field None means all of them.

    Applied in SQL rather than in the browser: a filter that only narrowed the
    page already loaded would answer "no losses" when it means "no losses in the
    most recent fifty", which is worse than offering no filter at all.
    """

    player_color: str | None = None
    result: str | None = None
    time_class: str | None = None
    """bullet, blitz, rapid or daily — see `store` for the boundaries."""
    with_errors: bool = False
    """Only games the player made an inaccuracy or worse in."""

    def __bool__(self) -> bool:
        return bool(
            self.player_color or self.result or self.time_class or self.with_errors
        )


@dataclass(frozen=True)
class AnalysisSummary:
    """What a game's stored analysis adds up to.

    Aggregated over the moves severity was assigned to — the player's own (PRD
    4.4) — so accuracy answers "how well did I play" and not "how well was this
    game played". A game whose player colour is unknown was labelled on both
    sides, and summarises over both for the same reason.
    """

    moves: int
    inaccuracies: int
    mistakes: int
    blunders: int
    average_loss: float
    """Mean win percentage given up per move."""
    accuracy: float


@dataclass(frozen=True)
class Game:
    platform: Platform
    platform_game_id: str
    played_at: datetime
    pgn: str
    time_control: str | None = None
    player_color: str | None = None
    opponent: str | None = None
    result: Result | None = None
    eco_code: str | None = None
    url: str | None = None
    analysis_status: AnalysisStatus = AnalysisStatus.PENDING
    id: int | None = None
