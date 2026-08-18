from datetime import UTC, datetime, timedelta

import pytest

from chess_analysis import db, store
from chess_analysis.models import Platform
from chess_analysis.platforms.chesscom import ArchiveResponse, ChessComError
from chess_analysis.platforms.lichess import LichessError
from chess_analysis.sync import SyncError, sync_chesscom, sync_lichess

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


# --- Lichess ---------------------------------------------------------------


LICHESS_PGN = '[White "alice"]\n[Black "bob"]\n\n1. e4 d5 *'


def lichess_entry(game_id: str, played_at: datetime, **overrides):
    base = {
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
            "black": {"user": {"name": "bob"}},
        },
        "pgn": LICHESS_PGN,
    }
    return base | overrides


class FakeLichessClient:
    """Stands in for LichessClient; sync only needs the one method."""

    def __init__(self, entries: list[dict] | None = None):
        self.entries = entries or []
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def export_games(self, username, *, max_games=None, since=None, until=None):
        self.calls.append({"max_games": max_games, "since": since, "until": until})
        if self.error is not None:
            raise self.error
        return list(self.entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def lichess_games(count: int, start: int = 0):
    base = datetime(2026, 8, 1, 12, tzinfo=UTC)
    return [
        lichess_entry(f"lg{i}", base + timedelta(days=i))
        for i in range(start, start + count)
    ]


@pytest.fixture
def lichess_conn(tmp_path):
    connection = db.connect(tmp_path / "lichess.db")
    store.save_settings(connection, lichess_enabled=True, lichess_username="alice")
    yield connection
    connection.close()


def test_unconfigured_lichess_is_refused(tmp_path):
    connection = db.connect(tmp_path / "x.db")
    with pytest.raises(SyncError):
        sync_lichess(connection, FakeLichessClient())


def test_first_lichess_sync_asks_for_the_target_and_no_more(lichess_conn):
    client = FakeLichessClient(lichess_games(60))

    result = sync_lichess(lichess_conn, client, target=50)

    # One request, bounded by `max` rather than by a walk backward (PRD 4.2).
    assert client.calls == [{"max_games": 50, "since": None, "until": None}]
    assert result.first_sync is True
    assert result.inserted == 60


def test_first_lichess_sync_sets_both_cursors(lichess_conn):
    result = sync_lichess(lichess_conn, FakeLichessClient(lichess_games(5)))

    settings = store.load_settings(lichess_conn)
    assert settings.lichess_last_synced_at is not None
    assert settings.lichess_backfill_cursor == datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert result.backfill_cursor == settings.lichess_backfill_cursor
    assert settings.lichess_last_synced_at != settings.lichess_backfill_cursor


def test_subsequent_lichess_sync_asks_only_for_what_is_new(lichess_conn):
    store.save_settings(
        lichess_conn, lichess_last_synced_at=datetime(2026, 8, 10, tzinfo=UTC)
    )
    client = FakeLichessClient(lichess_games(2))

    sync_lichess(lichess_conn, client)

    call = client.calls[0]
    assert call["max_games"] is None
    # Lichess narrows on when a game started, so the window reaches back far
    # enough to catch one that began before the last sync and ended after it.
    assert call["since"] == datetime(2026, 8, 9, tzinfo=UTC)


def test_subsequent_lichess_sync_leaves_the_backfill_cursor_alone(lichess_conn):
    sync_lichess(lichess_conn, FakeLichessClient(lichess_games(3)))
    original = store.load_settings(lichess_conn).lichess_backfill_cursor

    sync_lichess(lichess_conn, FakeLichessClient(lichess_games(5)))

    assert store.load_settings(lichess_conn).lichess_backfill_cursor == original


def test_resyncing_lichess_does_not_duplicate(lichess_conn):
    entries = lichess_games(4)
    sync_lichess(lichess_conn, FakeLichessClient(entries))

    result = sync_lichess(lichess_conn, FakeLichessClient(entries))

    assert result.entries_seen == 4  # the overlap re-reads them
    assert result.inserted == 0  # and stores none of them twice
    assert store.count_games(lichess_conn) == 4


def test_lichess_variants_are_filtered_before_storage(lichess_conn):
    entries = lichess_games(2) + [
        lichess_entry("v1", datetime(2026, 8, 9, tzinfo=UTC), variant="atomic")
    ]

    result = sync_lichess(lichess_conn, FakeLichessClient(entries))

    assert result.entries_seen == 3
    assert result.inserted == 2


def test_failed_lichess_sync_does_not_advance_the_cursor(lichess_conn):
    sync_lichess(lichess_conn, FakeLichessClient(lichess_games(2)))
    before = store.load_settings(lichess_conn).lichess_last_synced_at

    client = FakeLichessClient(lichess_games(4))
    client.error = LichessError("boom")
    with pytest.raises(LichessError):
        sync_lichess(lichess_conn, client)

    assert store.load_settings(lichess_conn).lichess_last_synced_at == before


def test_empty_lichess_account_syncs_cleanly(lichess_conn):
    result = sync_lichess(lichess_conn, FakeLichessClient())

    assert result.inserted == 0
    assert store.load_settings(lichess_conn).lichess_last_synced_at is not None


def test_the_two_platforms_keep_separate_cursors(tmp_path):
    """Both accounts land in one list, but neither's freshness describes the
    other's — a Lichess sync must not make Chess.com look just-synced."""
    connection = db.connect(tmp_path / "both.db")
    store.save_settings(
        connection,
        chesscom_enabled=True,
        chesscom_username="alice",
        lichess_enabled=True,
        lichess_username="alice",
    )

    sync_lichess(connection, FakeLichessClient(lichess_games(2)))

    settings = store.load_settings(connection)
    assert settings.lichess_last_synced_at is not None
    assert settings.chesscom_last_synced_at is None

    sync_chesscom(connection, FakeClient({url_for(2026, 8): month_of_games(2026, 8, 2)}))

    settings = store.load_settings(connection)
    assert settings.chesscom_last_synced_at is not None
    assert store.count_games(connection) == 4
    # History depth spans both platforms; each backfill cursor is its own.
    assert store.oldest_played_at(connection) == datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert store.oldest_played_at(connection, Platform.LICHESS) == datetime(
        2026, 8, 1, 12, tzinfo=UTC
    )
    connection.close()
