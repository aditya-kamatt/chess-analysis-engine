"""Pulling games from a platform into the local database (PRD 4.2).

One function per platform, because the shapes genuinely differ — Chess.com is a
walk backward through monthly archives, Lichess is a single narrowed request —
and both end at `_finish`, which is what keeps the cursor bookkeeping identical
between them.

`last_synced_at` and `backfill_cursor` are moved by different code paths on
purpose. Sync moves the first one forward; backfill moves the second one
backward. Conflating them either loses games or misreports how fresh the data
is, so nothing in this module writes both.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from chess_analysis.db import now
from chess_analysis.models import Platform, Settings
from chess_analysis.platforms.chesscom import ChessComClient, parse_archive
from chess_analysis.platforms.lichess import LichessClient, parse_games
from chess_analysis.store import (
    get_http_cache,
    insert_games,
    load_settings,
    oldest_played_at,
    put_http_cache,
    save_settings,
)

FIRST_SYNC_TARGET = 50

# Lichess narrows by when a game *started*, so asking only for games since the
# last sync would miss one that began just before it and finished just after —
# most often a correspondence game. Re-asking for a day either side costs a
# handful of already-stored games, which `insert_games` ignores.
LICHESS_SYNC_OVERLAP = timedelta(days=1)

_ARCHIVE_MONTH = re.compile(r"/games/(\d{4})/(\d{2})/?$")


class SyncError(Exception):
    """Sync could not complete. `last_synced_at` is left where it was."""


@dataclass(frozen=True)
class SyncResult:
    platform: Platform
    entries_seen: int
    """Raw archive entries examined, including variants and duplicates."""
    inserted: int
    inserted_ids: list[int]
    """New rows, in the order stored — what sync hands to the analysis queue."""
    archives_read: int
    """Archives actually downloaded; a 304 does not count. Lichess has no
    archives to speak of, so its single games request counts as one."""
    archives_unchanged: int
    first_sync: bool
    last_synced_at: datetime
    backfill_cursor: datetime | None


def sync_chesscom(
    conn: sqlite3.Connection,
    client: ChessComClient,
    *,
    target: int = FIRST_SYNC_TARGET,
) -> SyncResult:
    settings = load_settings(conn)
    if not settings.chesscom_enabled or not settings.chesscom_username:
        raise SyncError("Chess.com is not configured")

    username = settings.chesscom_username
    first_sync = settings.chesscom_last_synced_at is None

    # One cheap request, rather than constructing month URLs: months with no
    # games have no archive at all and would 404.
    archives = client.archive_urls(username)
    if not archives:
        return _finish(
            conn,
            settings,
            Platform.CHESSCOM,
            first_sync,
            SyncCounts(),
            collected_oldest=None,
        )

    if first_sync:
        wanted = list(reversed(archives))
    else:
        wanted = _archives_since(archives, settings.chesscom_last_synced_at)

    counts = SyncCounts()
    oldest_seen: datetime | None = None

    for url in wanted:
        etag, last_modified = get_http_cache(conn, url)
        archive = client.fetch_archive(url, etag=etag, last_modified=last_modified)

        if not archive.modified:
            counts.archives_unchanged += 1
            continue

        counts.archives_read += 1
        counts.entries_seen += len(archive.entries)

        games = parse_archive(archive.entries, username)
        counts.inserted_ids.extend(insert_games(conn, games))
        put_http_cache(conn, url, archive.etag, archive.last_modified)

        if games:
            month_oldest = min(game.played_at for game in games)
            oldest_seen = min(oldest_seen or month_oldest, month_oldest)

        # First sync stops as soon as it has enough; the walk is newest-first.
        if first_sync and counts.inserted >= target:
            break

    return _finish(conn, settings, Platform.CHESSCOM, first_sync, counts, oldest_seen)


def sync_lichess(
    conn: sqlite3.Connection,
    client: LichessClient,
    *,
    target: int = FIRST_SYNC_TARGET,
) -> SyncResult:
    """Pull Lichess games into the database.

    One request either way: bounded by `max` on a first sync, by `since` on
    every one after it (PRD 4.2).
    """
    settings = load_settings(conn)
    if not settings.lichess_enabled or not settings.lichess_username:
        raise SyncError("Lichess is not configured")

    username = settings.lichess_username
    since = settings.lichess_last_synced_at
    first_sync = since is None

    if since is None:
        entries = client.export_games(username, max_games=target)
    else:
        entries = client.export_games(username, since=since - LICHESS_SYNC_OVERLAP)

    counts = SyncCounts(entries_seen=len(entries), archives_read=1)
    games = parse_games(entries, username)
    counts.inserted_ids.extend(insert_games(conn, games))

    oldest = min((game.played_at for game in games), default=None)
    return _finish(conn, settings, Platform.LICHESS, first_sync, counts, oldest)


@dataclass
class SyncCounts:
    entries_seen: int = 0
    inserted_ids: list[int] = field(default_factory=list)
    archives_read: int = 0
    archives_unchanged: int = 0

    @property
    def inserted(self) -> int:
        return len(self.inserted_ids)


def _finish(
    conn: sqlite3.Connection,
    settings: Settings,
    platform: Platform,
    first_sync: bool,
    counts: SyncCounts,
    collected_oldest: datetime | None,
) -> SyncResult:
    """Commit the sync's effect on one platform's cursors.

    Only reached when every request succeeded, so a partial failure leaves
    `last_synced_at` where it was and the games already inserted are retained
    (PRD 7).
    """
    synced_at = now()
    updates: dict[str, object] = {f"{platform}_last_synced_at": synced_at}

    # The backfill cursor is only established here, on the first sync. After
    # that it belongs to "Load older games" and sync must not touch it.
    cursor = getattr(settings, f"{platform}_backfill_cursor")
    if first_sync:
        cursor = collected_oldest or oldest_played_at(conn, platform)
        updates[f"{platform}_backfill_cursor"] = cursor

    save_settings(conn, **updates)

    return SyncResult(
        platform=platform,
        entries_seen=counts.entries_seen,
        inserted=counts.inserted,
        inserted_ids=list(counts.inserted_ids),
        archives_read=counts.archives_read,
        archives_unchanged=counts.archives_unchanged,
        first_sync=first_sync,
        last_synced_at=synced_at,
        backfill_cursor=cursor,
    )


def _archives_since(archives: list[str], since: datetime | None) -> list[str]:
    """Archives covering `since` through now, newest first.

    Re-fetching the current month is required; re-fetching the months since the
    last sync matters too, because a user who has not opened the app since the
    month rolled over would otherwise never see the tail of the old month.
    """
    if since is None:
        return list(reversed(archives))

    boundary = (since.year, since.month)
    wanted = [url for url in archives if _month_of(url) >= boundary]
    # Always include the newest archive, even if the month has no games yet.
    if not wanted and archives:
        wanted = [archives[-1]]
    return list(reversed(wanted))


def _month_of(url: str) -> tuple[int, int]:
    match = _ARCHIVE_MONTH.search(url)
    if match is None:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))
