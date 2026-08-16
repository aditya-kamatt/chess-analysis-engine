"""Storage-level behaviour that the rest of the app depends on."""

import sqlite3
import threading

from chess_analysis import db, store


def test_concurrent_connections_to_a_fresh_database(tmp_path):
    """The worker thread and a request both open the database at startup. With
    no busy timeout in force during the WAL switch they collide on "database is
    locked" and the worker thread dies silently."""
    path = tmp_path / "race.db"
    errors: list[Exception] = []
    start = threading.Barrier(8)

    def connect():
        start.wait()
        try:
            conn = db.connect(path)
            conn.execute("SELECT COUNT(*) FROM games").fetchone()
            conn.close()
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=connect) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    first = db.connect(path)
    version = first.execute("PRAGMA user_version").fetchone()[0]
    first.close()

    second = db.connect(path)
    try:
        assert second.execute("PRAGMA user_version").fetchone()[0] == version
        assert store.load_settings(second).analysis_depth == 20
    finally:
        second.close()


def test_wal_is_enabled(tmp_path):
    """Reads during a long analysis write would otherwise block."""
    conn = db.connect(tmp_path / "wal.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_settings_row_always_exists(tmp_path):
    conn = db.connect(tmp_path / "settings.db")
    try:
        assert store.load_settings(conn).chesscom_enabled is False
        with_id = conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
        assert with_id == 1
    finally:
        conn.close()


def test_positions_are_removed_with_their_game(tmp_path):
    """Foreign keys are on, so deleting a game cannot orphan its analysis."""
    conn = db.connect(tmp_path / "cascade.db")
    try:
        conn.execute(
            "INSERT INTO games (platform, platform_game_id, played_at, pgn)"
            " VALUES ('chesscom', 'x', '2026-08-01T00:00:00+00:00', '*')"
        )
        game_id = conn.execute("SELECT id FROM games").fetchone()[0]
        conn.execute(
            "INSERT INTO positions (game_id, ply, fen, side_to_move, played_move,"
            " played_move_eval, lines, depth, win_percent_loss)"
            " VALUES (?, 0, 'x', 'white', 'e2e4', '{}', '[]', 20, 0.0)",
            (game_id,),
        )

        conn.execute("DELETE FROM games WHERE id = ?", (game_id,))

        assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    except sqlite3.Error:
        raise
    finally:
        conn.close()
