"""Database and repository tests."""

from pathlib import Path
import sqlite3

import pytest

from party_player.database.connection import Database
from party_player.database import migrations
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.repository import PartyPlayerRepository
from party_player.repositories.track_repository import TrackRepository
from party_player.settings_service import SettingsService


def test_cached_connection_can_be_closed_only_by_owning_thread(tmp_path: Path) -> None:
    database = Database(tmp_path / "cached-close.db")
    with database.connect_cached() as connection:
        connection.execute("CREATE TABLE probe (id INTEGER)")

    assert database.close_cached_connection()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    assert database.close_cached_connection()


def test_migration_creates_empty_catalog(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)

    assert TrackRepository(database).count() == 0


def test_repository_returns_bounded_track_page(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title, artist) VALUES (?, ?, ?)",
            ("C:/Music/song.mp3", "Song", "Artist"),
        )

    tracks = TrackRepository(database).find_page(limit=1)

    assert len(tracks) == 1
    assert tracks[0].title == "Song"


def test_catalog_keeps_same_title_versions_individually_selectable(tmp_path: Path) -> None:
    database = Database(tmp_path / "versions.db")
    migrate(database)
    repository = TrackRepository(database)
    repository.upsert_file("C:/Music/Song.flac", "Song", "Artist", "Album", 120)
    repository.upsert_file("C:/Music/Song - VBR.mp3", "Song", "Artist", "Album", 120)
    repository.upsert_file("C:/Music/Song - 320.mp3", "Song", "Artist", "Album", 120)

    tracks = repository.search("Song")

    assert repository.count() == 3
    assert repository.search_count("Song") == 3
    assert [Path(track.file_path).name for track in tracks] == [
        "Song.flac",
        "Song - VBR.mp3",
        "Song - 320.mp3",
    ]
    assert [Path(track.file_path).name for track in repository.search("VBR")] == ["Song - VBR.mp3"]


def test_catalog_search_includes_genre_and_release_years(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = TrackRepository(database)
    repository.upsert_file("C:/Music/song.mp3", "Song", "Artist", "Album", 120, "Disco", 2001, 1978)

    assert repository.search("Disco")[0].title == "Song"
    assert repository.search("2001")[0].title == "Song"
    assert repository.search("1978")[0].title == "Song"


def test_catalog_search_includes_multivalue_metadata_and_count(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = TrackRepository(database)
    track = repository.upsert_file("C:/Music/boogie.mp3", "Boogie Man", "AC/DC", "Ballbreaker", 120)
    with database.connect() as connection:
        cursor = connection.execute(
            """INSERT INTO metadata_terms(term_type,normalized_key,display_name)
               VALUES ('ADDITIONAL_GENRE','dance','Dance')"""
        )
        connection.execute(
            "INSERT INTO track_metadata_terms(track_id,term_id) VALUES (?,?)",
            (track.id, int(cursor.lastrowid)),
        )

    matches = repository.search("dance")

    assert [item.id for item in matches] == [track.id]
    assert repository.search_count("dance") == 1


def test_migration_creates_indexes_for_catalog_search_fields(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        indexes = {
            str(row["name"]) for row in connection.execute("PRAGMA index_list(tracks)").fetchall()
        }

    assert {
        "idx_tracks_title",
        "idx_tracks_artist",
        "idx_tracks_album",
        "idx_tracks_genre",
        "idx_tracks_year",
        "idx_tracks_original_release_year",
    } <= indexes


def test_hidden_track_disappears_from_catalog_but_remains_addressable(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = TrackRepository(database)
    track = repository.upsert_file("C:/Music/song.mp3", "Song", "Artist", "", 120)

    repository.hide_from_catalog(track.id)

    assert repository.count() == 0
    assert repository.find_page(10) == []
    assert repository.search("Song") == []
    assert repository.get(track.id) == track


def test_reimport_makes_hidden_track_visible_again(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = TrackRepository(database)
    track = repository.upsert_file("C:/Music/song.mp3", "Song", "Artist", "", 120)
    repository.hide_from_catalog(track.id)

    restored = repository.upsert_file("C:/Music/song.mp3", "Song neu", "Artist", "", 120)

    assert restored.id == track.id
    assert repository.count() == 1
    assert repository.find_page(10)[0].title == "Song neu"


def test_existing_version_one_database_is_migrated_without_data_loss(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.db")
    with database.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (1);
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                album TEXT NOT NULL DEFAULT '',
                duration_seconds REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE play_history (
                id INTEGER PRIMARY KEY,
                queue_id INTEGER
            );
            CREATE TABLE saved_queue_entries (
                id INTEGER PRIMARY KEY,
                track_id INTEGER NOT NULL
            );
            INSERT INTO tracks (file_path, title) VALUES ('song.mp3', 'Legacy Song');
            """
        )

    migrate(database)

    with database.connect() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(tracks)").fetchall()
        }
        track = connection.execute("SELECT title, catalog_visible FROM tracks").fetchone()
    assert version is not None and version["version"] == LATEST_SCHEMA_VERSION
    assert {"genre", "year", "original_release_year", "catalog_visible"} <= columns
    assert track is not None and tuple(track) == ("Legacy Song", 1)


def test_populated_schema_six_migrates_through_loudness_schema_without_data_loss(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "populated-v6.db")
    with database.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (0);
            """
        )
        for version, migration in enumerate(
            (
                migrations._migrate_to_v1,
                migrations._migrate_to_v2,
                migrations._migrate_to_v3,
                migrations._migrate_to_v4,
                migrations._migrate_to_v5,
                migrations._migrate_to_v6,
            ),
            start=1,
        ):
            migration(connection)
            connection.execute("UPDATE schema_version SET version = ?", (version,))
        connection.executescript(
            """
            INSERT INTO tracks
                (id, file_path, title, artist, album, duration_seconds,
                 genre, year, original_release_year, catalog_visible)
            VALUES
                (1, 'music/one.mp3', 'One', 'Artist A', 'Album', 181.5,
                 'Pop', 2001, 1999, 1),
                (2, 'music/two.flac', 'Two', 'Artist B', 'Album', 242.0,
                 'Rock', 2002, 2002, 1);
            INSERT INTO party_sessions
                (id, name, status, settings_snapshot)
            VALUES (1, 'Migration Party', 'active', '{"mode":"automatic"}');
            INSERT INTO party_queue
                (id, session_id, track_id, position, status, source, requested_by)
            VALUES
                (1, 1, 1, 1, 'played', 'catalog', 'Alice'),
                (2, 1, 2, 2, 'waiting', 'saved_queue', 'Bob');
            INSERT INTO play_history
                (id, session_id, track_id, deck_id, started_at, play_duration,
                 completion_status, queue_id)
            VALUES (1, 1, 1, 'A', '2026-01-01 20:00:00', 180.0, 'played', 1);
            INSERT INTO saved_queues (id, name) VALUES (1, 'Favoriten');
            INSERT INTO saved_queue_entries
                (id, saved_queue_id, track_id, position)
            VALUES (1, 1, 2, 1);
            INSERT INTO party_settings (key, value)
            VALUES ('fade_duration', '7');
            INSERT INTO track_cue_points
                (track_id, manual_cue_in, manual_cue_out, manual_fade_duration,
                 confidence, analysis_version)
            VALUES (1, 1.25, 178.5, 6.0, 0.92, 'v6-fixture');
            """
        )
        before = {
            "tracks": connection.execute(
                "SELECT id, title, artist, duration_seconds FROM tracks ORDER BY id"
            ).fetchall(),
            "queue": connection.execute(
                "SELECT id, track_id, position, status, requested_by "
                "FROM party_queue ORDER BY id"
            ).fetchall(),
            "history": connection.execute(
                "SELECT track_id, deck_id, play_duration, completion_status " "FROM play_history"
            ).fetchall(),
            "cues": connection.execute(
                "SELECT track_id, manual_cue_in, manual_cue_out, confidence "
                "FROM track_cue_points"
            ).fetchall(),
            "saved": connection.execute(
                "SELECT saved_queue_id, track_id, position FROM saved_queue_entries"
            ).fetchall(),
        }
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'track_loudness'"
            ).fetchone()
            is None
        )

    migrate(database)

    with database.connect() as connection:
        after = {
            "tracks": connection.execute(
                "SELECT id, title, artist, duration_seconds FROM tracks ORDER BY id"
            ).fetchall(),
            "queue": connection.execute(
                "SELECT id, track_id, position, status, requested_by "
                "FROM party_queue ORDER BY id"
            ).fetchall(),
            "history": connection.execute(
                "SELECT track_id, deck_id, play_duration, completion_status " "FROM play_history"
            ).fetchall(),
            "cues": connection.execute(
                "SELECT track_id, manual_cue_in, manual_cue_out, confidence "
                "FROM track_cue_points"
            ).fetchall(),
            "saved": connection.execute(
                "SELECT saved_queue_id, track_id, position FROM saved_queue_entries"
            ).fetchall(),
        }
        loudness_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(track_loudness)").fetchall()
        }
        loudness_count = connection.execute("SELECT COUNT(*) FROM track_loudness").fetchone()
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert {key: value for key, value in after.items() if key != "history"} == {
        key: value for key, value in before.items() if key != "history"
    }
    assert [tuple(row) for row in after["history"]] == [(1, "A", 180.0, "PLAYED")]
    assert {
        "integrated_loudness_lufs",
        "loudness_range_lu",
        "true_peak_dbfs",
        "replaygain_track_gain_db",
        "manual_gain_db",
        "metadata_status",
    } <= loudness_columns
    assert loudness_count is not None and loudness_count[0] == 0
    assert version is not None and version["version"] == LATEST_SCHEMA_VERSION
    assert foreign_key_errors == []


def test_completion_status_migration_normalizes_all_legacy_values(tmp_path: Path) -> None:
    database = Database(tmp_path / "history-results.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (id, file_path, title) VALUES (1, 'song.mp3', 'Song')"
        )
        connection.execute(
            """INSERT INTO party_sessions (id, name, status, settings_snapshot)
               VALUES (1, 'Test', 'active', '{}')"""
        )
        for history_id, status in enumerate(
            (
                "completed",
                "played",
                "partially_played",
                "skipped",
                "error",
                "failed",
                "stopped",
                "aborted",
                "unexpected",
            ),
            start=1,
        ):
            connection.execute(
                """INSERT INTO play_history
                   (id, session_id, track_id, deck_id, started_at, completion_status)
                   VALUES (?, 1, 1, 'A', '2026-01-01T20:00:00', ?)""",
                (history_id, status),
            )
        connection.execute("UPDATE schema_version SET version = 24")

    migrate(database)

    with database.connect() as connection:
        statuses = [
            str(row["completion_status"])
            for row in connection.execute("SELECT completion_status FROM play_history ORDER BY id")
        ]
    assert statuses == [
        "PLAYED",
        "PLAYED",
        "PARTIALLY_PLAYED",
        "SKIPPED",
        "FAILED",
        "FAILED",
        "ABORTED",
        "ABORTED",
        "ABORTED",
    ]


def test_history_reason_migration_replaces_free_text_with_stable_code(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "history-reasons.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (id, file_path, title) VALUES (1, 'song.mp3', 'Song')"
        )
        connection.execute(
            """INSERT INTO party_sessions (id, name, status, settings_snapshot)
               VALUES (1, 'Test', 'active', '{}')"""
        )
        connection.execute(
            """INSERT INTO play_history
               (session_id, track_id, deck_id, started_at, completion_status,
                skip_reason, skip_code)
               VALUES (1, 1, 'A', '2026-01-01T20:00:00', 'SKIPPED',
                       'Alter deutscher Freitext', 'beliebiger-wert')"""
        )
        connection.execute("UPDATE schema_version SET version = 27")

    migrate(database)

    with database.connect() as connection:
        row = connection.execute("SELECT skip_reason, skip_code FROM play_history").fetchone()
    assert row is not None
    assert tuple(row) == ("Alter deutscher Freitext", "LEGACY_REASON")


def test_session_audit_attributes_queue_lock_and_explicit_events(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "session-audit.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (id, file_path, title) VALUES (1, 'song.mp3', 'Song')"
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Audit")
    entry = repository.add_queue_entry(session.session_id, 1)
    with database.connect() as connection:
        connection.execute(
            """UPDATE party_queue
               SET locked = 1, lock_source = 'MANUAL'
               WHERE id = ?""",
            (entry.queue_id,),
        )
    repository.record_session_event(
        session.session_id,
        "MANUAL_OVERRIDE",
        details={"reason": "Test"},
    )

    with database.connect() as connection:
        rows = connection.execute(
            """SELECT session_id, event_code, entity_type, entity_id, details
               FROM session_audit_events ORDER BY id"""
        ).fetchall()
    assert [str(row["event_code"]) for row in rows] == [
        "QUEUE_ADDED",
        "QUEUE_LOCK_CHANGED",
        "MANUAL_OVERRIDE",
    ]
    assert all(int(row["session_id"]) == session.session_id for row in rows)
    assert tuple(rows[1])[2:4] == ("QUEUE", entry.queue_id)
    assert '"lock_source":"MANUAL"' in str(rows[1]["details"]).replace(" ", "")


def test_party_queue_migration_adds_optional_cue_override_columns(tmp_path: Path) -> None:
    database = Database(tmp_path / "queue-cues.db")
    migrate(database)

    with database.connect() as connection:
        columns = {
            str(row["name"]): str(row["dflt_value"])
            for row in connection.execute("PRAGMA table_info(party_queue)").fetchall()
        }

    assert {
        "cue_in_override",
        "cue_out_override",
        "fade_duration_override",
        "cue_override_source",
    } <= columns.keys()
    assert columns["cue_override_source"] == "'inherited'"


def test_saved_queue_migration_adds_cue_snapshot_columns(tmp_path: Path) -> None:
    database = Database(tmp_path / "saved-queue-cues.db")
    migrate(database)

    with database.connect() as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(saved_queue_entries)").fetchall()
        }

    assert {"cue_in", "cue_out", "fade_duration", "cue_source"} <= columns


def test_cue_analysis_migration_adds_technical_result_columns(tmp_path: Path) -> None:
    database = Database(tmp_path / "cue-analysis.db")
    migrate(database)

    with database.connect() as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(track_cue_points)").fetchall()
        }

    assert {
        "minimum_level_dbfs",
        "maximum_level_dbfs",
        "peak",
        "measured_window_count",
        "analysis_backend",
    } <= columns


def test_replaygain_cache_migration_adds_scan_timestamp(tmp_path: Path) -> None:
    database = Database(tmp_path / "replaygain-cache.db")
    migrate(database)

    with database.connect() as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(track_loudness)").fetchall()
        }

    assert "replaygain_scanned_at" in columns


def test_headroom_migration_preserves_legacy_combined_peak_margin(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy-headroom.db")
    with database.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (11);
            CREATE TABLE party_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO party_settings VALUES ('maximum_output_peak_db', '-1');
            """
        )

    migrate(database)

    settings = SettingsService(PartyPlayerRepository(database))
    assert settings.maximum_output_peak() == -1.0
    assert settings.headroom() == 0.0


def test_loudness_metadata_status_migration_adds_playback_independent_state(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "loudness-status.db")
    migrate(database)

    with database.connect() as connection:
        columns = {
            str(row["name"]): str(row["dflt_value"])
            for row in connection.execute("PRAGMA table_info(track_loudness)").fetchall()
        }

    assert columns["metadata_status"] == "'NOT_ANALYSED'"


def test_database_from_newer_application_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "future.db")
    with database.connect() as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (?)", (LATEST_SCHEMA_VERSION + 1,))

    with pytest.raises(RuntimeError, match="neuer"):
        migrate(database)


def test_party_foreign_keys_and_lookup_indexes_are_complete(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        foreign_keys = {
            (table, str(row["from"]), str(row["table"]))
            for table in ("party_queue", "play_history", "saved_queue_entries")
            for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }
        indexes = {
            str(row["name"])
            for table in ("party_queue", "play_history", "saved_queue_entries")
            for row in connection.execute(f"PRAGMA index_list({table})").fetchall()
        }
        track_indexes = {
            str(row["name"]) for row in connection.execute("PRAGMA index_list(tracks)").fetchall()
        }

    assert {
        ("party_queue", "session_id", "party_sessions"),
        ("party_queue", "track_id", "tracks"),
        ("play_history", "session_id", "party_sessions"),
        ("play_history", "track_id", "tracks"),
        ("play_history", "queue_id", "party_queue"),
        ("saved_queue_entries", "saved_queue_id", "saved_queues"),
        ("saved_queue_entries", "track_id", "tracks"),
    } <= foreign_keys
    assert {
        "idx_party_queue_session_position",
        "idx_party_queue_selection",
        "idx_party_queue_track",
        "idx_play_history_session",
        "idx_play_history_track",
        "idx_play_history_queue",
        "idx_saved_queue_entries_order",
        "idx_saved_queue_entries_track",
    } <= indexes
    assert "idx_tracks_normalized_artist" in track_indexes


def test_party_queue_uses_track_id_as_its_only_catalog_identity(tmp_path: Path) -> None:
    database = Database(tmp_path / "queue-identity.db")
    migrate(database)

    with database.connect() as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(party_queue)").fetchall()
        }
        track_foreign_keys = [
            row
            for row in connection.execute("PRAGMA foreign_key_list(party_queue)").fetchall()
            if row["table"] == "tracks"
        ]

    assert "track_id" in columns
    assert "file_path" not in columns
    assert "path" not in columns
    assert len(track_foreign_keys) == 1
    assert track_foreign_keys[0]["from"] == "track_id"


def test_queue_source_migration_normalizes_legacy_and_unknown_values(tmp_path: Path) -> None:
    database = Database(tmp_path / "queue-sources.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks (file_path, title, artist, album)
               VALUES ('one.mp3', 'One', '', '')"""
        )
        connection.execute("""INSERT INTO party_sessions (name) VALUES ('Sources')""")
        for position, source in enumerate(
            ("catalog", "guest", "automatic", "directory", "emergency", "legacy"),
            start=1,
        ):
            connection.execute(
                """INSERT INTO party_queue
                   (session_id, track_id, position, source)
                   VALUES (1, 1, ?, ?)""",
                (position, source),
            )
        connection.execute("UPDATE schema_version SET version = 16")

    migrate(database)

    with database.connect() as connection:
        sources = [
            str(row["source"])
            for row in connection.execute(
                "SELECT source FROM party_queue ORDER BY position"
            ).fetchall()
        ]
    assert sources == [
        "MANUAL",
        "GUEST_REQUEST",
        "AUTOMATIC",
        "PLAYLIST",
        "EMERGENCY",
        "MANUAL",
    ]


def test_queue_status_migration_maps_loaded_and_error_to_explicit_states(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "queue-statuses.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks (file_path, title, artist, album)
               VALUES ('one.mp3', 'One', '', '')"""
        )
        connection.execute("INSERT INTO party_sessions (name) VALUES ('Statuses')")
        for position, status in enumerate(("waiting", "loaded", "error", "unknown"), start=1):
            connection.execute(
                """INSERT INTO party_queue
                   (session_id, track_id, position, status, source)
                   VALUES (1, 1, ?, ?, 'MANUAL')""",
                (position, status),
            )
        connection.execute("UPDATE schema_version SET version = 17")

    migrate(database)

    with database.connect() as connection:
        statuses = [
            str(row["status"])
            for row in connection.execute(
                "SELECT status FROM party_queue ORDER BY position"
            ).fetchall()
        ]
    assert statuses == ["waiting", "ready", "failed", "failed"]


def test_queue_operational_metadata_migration_is_restart_safe(tmp_path: Path) -> None:
    database = Database(tmp_path / "queue-metadata.db")
    migrate(database)

    with database.connect() as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(party_queue)").fetchall()
        }

    assert {
        "lock_source",
        "unique_requester_count",
        "last_requested_at",
        "updated_at",
        "preparation_attempts",
        "failure_code",
        "skip_code",
    } <= columns
