"""Configured game-source accounts and their independent sync outcomes.

Chess.com and Lichess remain separate adapters: their collection
implementation differs.  This module owns the account-level rules shared by
the application: validation, cursor reset, client lifetime and partial sync
failure.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from chess_analysis import store
from chess_analysis.models import Platform, Settings
from chess_analysis.platforms import PlatformError
from chess_analysis.platforms.chesscom import ChessComClient
from chess_analysis.platforms.lichess import LichessClient
from chess_analysis.sync import SyncError, SyncResult, sync_chesscom, sync_lichess


class SourceConfigurationError(Exception):
    """A configured source account cannot be used."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class AccountUpdate:
    chesscom_enabled: bool
    chesscom_username: str | None
    lichess_enabled: bool
    lichess_username: str | None
    lichess_token: str | None


@dataclass(frozen=True)
class SyncOutcome:
    results: list[SyncResult]
    failures: list[tuple[Platform, Exception]]


class SourceAccounts:
    """Deep module for configured Chess.com and Lichess accounts."""

    def __init__(
        self,
        chesscom_client_factory: Callable[[], ChessComClient] = ChessComClient,
        lichess_client_factory: Callable[[str | None], LichessClient] = LichessClient,
    ) -> None:
        self._chesscom_client_factory = chesscom_client_factory
        self._lichess_client_factory = lichess_client_factory

    def validated_fields(
        self, current: Settings, update: AccountUpdate
    ) -> dict[str, object]:
        chesscom_username = (update.chesscom_username or "").strip() or None
        lichess_username = (update.lichess_username or "").strip() or None
        lichess_token = current.lichess_token
        if update.lichess_token is not None:
            lichess_token = update.lichess_token.strip() or None

        self._validate_chesscom(update.chesscom_enabled, chesscom_username)
        self._validate_lichess(update.lichess_enabled, lichess_username, lichess_token)

        fields: dict[str, object] = {
            "chesscom_enabled": update.chesscom_enabled,
            "chesscom_username": chesscom_username,
            "lichess_enabled": update.lichess_enabled,
            "lichess_username": lichess_username,
            "lichess_token": lichess_token,
        }
        if current.chesscom_username != chesscom_username:
            fields.update(_cleared_cursors(Platform.CHESSCOM))
        if current.lichess_username != lichess_username:
            fields.update(_cleared_cursors(Platform.LICHESS))
        return fields

    def sync(self, conn: sqlite3.Connection) -> SyncOutcome:
        settings = store.load_settings(conn)
        runners: list[tuple[Platform, Callable[[], SyncResult]]] = []
        if settings.chesscom_enabled:
            runners.append((Platform.CHESSCOM, lambda: self._sync_chesscom(conn)))
        if settings.lichess_enabled:
            runners.append((Platform.LICHESS, lambda: self._sync_lichess(conn)))
        if not runners:
            raise SourceConfigurationError("No platform is configured")

        results: list[SyncResult] = []
        failures: list[tuple[Platform, Exception]] = []
        for platform, run in runners:
            try:
                results.append(run())
            except (PlatformError, SyncError) as exc:
                failures.append((platform, exc))
        return SyncOutcome(results=results, failures=failures)

    def _validate_chesscom(self, enabled: bool, username: str | None) -> None:
        if not enabled:
            return
        if not username:
            raise SourceConfigurationError("Enter a Chess.com username")
        try:
            with self._chesscom_client_factory() as client:
                exists = client.player_exists(username)
        except PlatformError as exc:
            raise SourceConfigurationError(str(exc), cause=exc) from exc
        if not exists:
            raise SourceConfigurationError(f"No Chess.com user named {username}")

    def _validate_lichess(
        self, enabled: bool, username: str | None, token: str | None
    ) -> None:
        if not enabled:
            return
        if not username:
            raise SourceConfigurationError("Enter a Lichess username")
        try:
            with self._lichess_client_factory(token) as client:
                exists = client.player_exists(username)
        except PlatformError as exc:
            raise SourceConfigurationError(str(exc), cause=exc) from exc
        if not exists:
            raise SourceConfigurationError(f"No Lichess user named {username}")

    def _sync_chesscom(self, conn: sqlite3.Connection) -> SyncResult:
        with self._chesscom_client_factory() as client:
            return sync_chesscom(conn, client)

    def _sync_lichess(self, conn: sqlite3.Connection) -> SyncResult:
        token = store.load_settings(conn).lichess_token
        with self._lichess_client_factory(token) as client:
            return sync_lichess(conn, client)


def _cleared_cursors(platform: Platform) -> dict[str, object]:
    return {
        f"{platform}_last_synced_at": None,
        f"{platform}_backfill_cursor": None,
    }
