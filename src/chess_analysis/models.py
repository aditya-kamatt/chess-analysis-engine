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
