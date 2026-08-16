from datetime import UTC, datetime, timedelta

import pytest

from chess_analysis import db, store
from chess_analysis.models import Platform
from chess_analysis.platforms.chesscom import ArchiveResponse, ChessComError
from chess_analysis.sync import SyncError, sync_chesscom

PGN = '[ECO "B01"]\n[White "alice"]\n[Black "bob"]\n\n1. e4 d5 *'


def entry(uuid: str, played_at: datetime, **overrides):
    base = {
        "uuid": uuid,
        "url": f"https://www.chess.com/game/live/{uuid}",
        "pgn": PGN,
        "time_control": "180",
        "end_time": int(played_at.timestamp()),
        "rules": "chess",
        "white": {"username": "alice", "result": "win"},
        "black": {"username": "bob", "result": "resigned"},
    }
    return base | overrides


class FakeClient:
    """Stands in for ChessComClient; sync only needs these two methods."""

    def __init__(self, archives: dict[str, list[dict]], unchanged: set[str] | None = None):
        self.archives = archives
        self.unchanged = unchanged or set()
        self.fetched: list[str] = []
        self.conditional: dict[str, str | None] = {}
        self.error: Exception | None = None

    def archive_urls(self, username):
        return list(self.archives)

    def fetch_archive(self, url, *, etag=None, last_modified=None):
        self.fetched.append(url)
        self.conditional[url] = etag
        if self.error is not None:
            raise self.error
        if url in self.unchanged:
            return ArchiveResponse(modified=False, etag=etag)
        return ArchiveResponse(
            modified=True,
            entries=self.archives[url],
            etag=f'W/"{url}"',
            last_modified=None,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def url_for(year: int, month: int) -> str:
    return f"https://api.chess.com/pub/player/alice/games/{year}/{month:02d}"


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    store.save_settings(connection, chesscom_enabled=True, chesscom_username="alice")
    yield connection
    connection.close()


def month_of_games(year, month, count, start=0):
    base = datetime(year, month, 1, 12, 0, tzinfo=UTC)
    return [entry(f"{year}-{month}-{i}", base + timedelta(days=i)) for i in range(start, start + count)]


def test_unconfigured_platform_is_refused(tmp_path):
    connection = db.connect(tmp_path / "x.db")
    with pytest.raises(SyncError):
        sync_chesscom(connection, FakeClient({}))


def test_first_sync_walks_backward_and_stops_at_the_target(conn):
    archives = {
        url_for(2026, 6): month_of_games(2026, 6, 20),
        url_for(2026, 7): month_of_games(2026, 7, 20),
        url_for(2026, 8): month_of_games(2026, 8, 20),
    }
    client = FakeClient(archives)

    result = sync_chesscom(conn, client, target=50)

    # Newest month first. 20 and 40 both fall short of 50, so all three months
    # are read and the archive that crosses the target is taken whole.
    assert client.fetched == [url_for(2026, 8), url_for(2026, 7), url_for(2026, 6)]
    assert result.first_sync is True
    assert result.inserted == 60


def test_first_sync_stops_early_when_one_month_suffices(conn):
    archives = {
        url_for(2026, 7): month_of_games(2026, 7, 40),
        url_for(2026, 8): month_of_games(2026, 8, 60),
    }
    client = FakeClient(archives)

    sync_chesscom(conn, client, target=50)

    assert client.fetched == [url_for(2026, 8)]


def test_first_sync_sets_both_cursors(conn):
    archives = {url_for(2026, 8): month_of_games(2026, 8, 5)}

    result = sync_chesscom(conn, FakeClient(archives), target=50)

    settings = store.load_settings(conn)
    assert settings.chesscom_last_synced_at is not None
    # Freshness and history depth are different values and must stay distinct.
    assert settings.chesscom_backfill_cursor == datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert result.backfill_cursor == settings.chesscom_backfill_cursor
    assert settings.chesscom_last_synced_at != settings.chesscom_backfill_cursor


def test_subsequent_sync_only_reads_months_since_the_last_one(conn):
    archives = {
        url_for(2026, 5): month_of_games(2026, 5, 3),
        url_for(2026, 6): month_of_games(2026, 6, 3),
        url_for(2026, 7): month_of_games(2026, 7, 3),
        url_for(2026, 8): month_of_games(2026, 8, 3),
    }
    store.save_settings(
        conn, chesscom_last_synced_at=datetime(2026, 7, 20, tzinfo=UTC)
    )
    client = FakeClient(archives)

    sync_chesscom(conn, client)

    assert client.fetched == [url_for(2026, 8), url_for(2026, 7)]


def test_a_month_boundary_does_not_lose_the_tail_of_the_old_month(conn):
    """Someone who last opened the app in July must still get their late-July
    games when they return in August."""
    archives = {
        url_for(2026, 7): month_of_games(2026, 7, 2, start=25),
        url_for(2026, 8): month_of_games(2026, 8, 2),
    }
    store.save_settings(conn, chesscom_last_synced_at=datetime(2026, 7, 1, tzinfo=UTC))

    result = sync_chesscom(conn, FakeClient(archives))

    assert result.inserted == 4


def test_subsequent_sync_leaves_the_backfill_cursor_alone(conn):
    """Sync moves freshness forward; only backfill moves history deeper."""
    sync_chesscom(conn, FakeClient({url_for(2026, 8): month_of_games(2026, 8, 3)}))
    original = store.load_settings(conn).chesscom_backfill_cursor

    sync_chesscom(conn, FakeClient({url_for(2026, 8): month_of_games(2026, 8, 5)}))

    assert store.load_settings(conn).chesscom_backfill_cursor == original


def test_unchanged_archives_are_not_reparsed(conn):
    archives = {url_for(2026, 8): month_of_games(2026, 8, 3)}
    sync_chesscom(conn, FakeClient(archives))

    client = FakeClient(archives, unchanged={url_for(2026, 8)})
    result = sync_chesscom(conn, client)

    # The stored ETag was offered back, and the 304 cost us no parsing.
    assert client.conditional[url_for(2026, 8)] is not None
    assert result.archives_unchanged == 1
    assert result.archives_read == 0
    assert result.inserted == 0


def test_resyncing_the_same_month_does_not_duplicate(conn):
    archives = {url_for(2026, 8): month_of_games(2026, 8, 4)}
    sync_chesscom(conn, FakeClient(archives))

    result = sync_chesscom(conn, FakeClient(archives))

    assert result.entries_seen == 4  # re-read the whole month
    assert result.inserted == 0  # but added nothing
    assert store.count_games(conn) == 4


def test_new_games_in_a_reread_month_are_added(conn):
    sync_chesscom(conn, FakeClient({url_for(2026, 8): month_of_games(2026, 8, 3)}))

    result = sync_chesscom(conn, FakeClient({url_for(2026, 8): month_of_games(2026, 8, 5)}))

    assert result.inserted == 2
    assert store.count_games(conn) == 5


def test_variants_are_filtered_before_storage(conn):
    games = month_of_games(2026, 8, 2) + [
        entry("v1", datetime(2026, 8, 9, tzinfo=UTC), rules="chess960")
    ]

    result = sync_chesscom(conn, FakeClient({url_for(2026, 8): games}))

    assert result.entries_seen == 3
    assert result.inserted == 2


def test_failure_does_not_advance_the_sync_cursor(conn):
    """A failed sync must not look like a successful one (PRD 4.2, 7)."""
    sync_chesscom(conn, FakeClient({url_for(2026, 8): month_of_games(2026, 8, 2)}))
    before = store.load_settings(conn).chesscom_last_synced_at

    client = FakeClient({url_for(2026, 8): month_of_games(2026, 8, 4)})
    client.error = ChessComError("boom")
    with pytest.raises(ChessComError):
        sync_chesscom(conn, client)

    assert store.load_settings(conn).chesscom_last_synced_at == before


def test_partial_failure_retains_already_fetched_games(conn):
    archives = {
        url_for(2026, 7): month_of_games(2026, 7, 3),
        url_for(2026, 8): month_of_games(2026, 8, 3),
    }

    class FailsOnSecond(FakeClient):
        def fetch_archive(self, url, *, etag=None, last_modified=None):
            if len(self.fetched) == 1:
                raise ChessComError("connection reset")
            return super().fetch_archive(url, etag=etag, last_modified=last_modified)

    with pytest.raises(ChessComError):
        sync_chesscom(conn, FailsOnSecond(archives), target=50)

    assert store.count_games(conn) == 3  # August survived
    assert store.load_settings(conn).chesscom_last_synced_at is None


def test_empty_account_syncs_cleanly(conn):
    result = sync_chesscom(conn, FakeClient({}))

    assert result.inserted == 0
    assert store.load_settings(conn).chesscom_last_synced_at is not None


def test_history_depth_is_reported_from_stored_games(conn):
    sync_chesscom(conn, FakeClient({url_for(2026, 8): month_of_games(2026, 8, 3)}))

    assert store.oldest_played_at(conn, Platform.CHESSCOM) == datetime(
        2026, 8, 1, 12, tzinfo=UTC
    )
