"""Versioned database schema migrations."""

import sqlite3

from party_player.database.connection import Database

LATEST_SCHEMA_VERSION = 41


def migrate(database: Database) -> None:
    """Create or upgrade the DeckRelay database schema."""
    with database.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version)
            SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
            """
        )
        row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
        version = int(row["version"])
        if version > LATEST_SCHEMA_VERSION:
            raise RuntimeError(
                f"Datenbankschema {version} ist neuer als die Anwendung ({LATEST_SCHEMA_VERSION})"
            )

        if version < 1:
            _migrate_to_v1(connection)
            _set_version(connection, 1)
            version = 1
        if version < 2:
            _migrate_to_v2(connection)
            _set_version(connection, 2)
            version = 2
        if version < 3:
            _migrate_to_v3(connection)
            _set_version(connection, 3)
            version = 3
        if version < 4:
            _migrate_to_v4(connection)
            _set_version(connection, 4)
            version = 4
        if version < 5:
            _migrate_to_v5(connection)
            _set_version(connection, 5)
            version = 5
        if version < 6:
            _migrate_to_v6(connection)
            _set_version(connection, 6)
            version = 6
        if version < 7:
            _migrate_to_v7(connection)
            _set_version(connection, 7)
            version = 7
        if version < 8:
            _migrate_to_v8(connection)
            _set_version(connection, 8)
            version = 8
        if version < 9:
            _migrate_to_v9(connection)
            _set_version(connection, 9)
            version = 9
        if version < 10:
            _migrate_to_v10(connection)
            _set_version(connection, 10)
            version = 10
        if version < 11:
            _migrate_to_v11(connection)
            _set_version(connection, 11)
            version = 11
        if version < 12:
            _migrate_to_v12(connection)
            _set_version(connection, 12)
            version = 12
        if version < 13:
            _migrate_to_v13(connection)
            _set_version(connection, 13)
            version = 13
        if version < 14:
            _migrate_to_v14(connection)
            _set_version(connection, 14)
            version = 14
        if version < 15:
            _migrate_to_v15(connection)
            _set_version(connection, 15)
            version = 15
        if version < 16:
            _migrate_to_v16(connection)
            _set_version(connection, 16)
            version = 16
        if version < 17:
            _migrate_to_v17(connection)
            _set_version(connection, 17)
            version = 17
        if version < 18:
            _migrate_to_v18(connection)
            _set_version(connection, 18)
            version = 18
        if version < 19:
            _migrate_to_v19(connection)
            _set_version(connection, 19)
            version = 19
        if version < 20:
            _migrate_to_v20(connection)
            _set_version(connection, 20)
            version = 20
        if version < 21:
            _migrate_to_v21(connection)
            _set_version(connection, 21)
            version = 21
        if version < 22:
            _migrate_to_v22(connection)
            _set_version(connection, 22)
            version = 22
        if version < 23:
            _migrate_to_v23(connection)
            _set_version(connection, 23)
            version = 23
        if version < 24:
            _migrate_to_v24(connection)
            _set_version(connection, 24)
            version = 24
        if version < 25:
            _migrate_to_v25(connection)
            _set_version(connection, 25)
            version = 25
        if version < 26:
            _migrate_to_v26(connection)
            _set_version(connection, 26)
            version = 26
        if version < 27:
            _migrate_to_v27(connection)
            _set_version(connection, 27)
            version = 27
        if version < 28:
            _migrate_to_v28(connection)
            _set_version(connection, 28)
            version = 28
        if version < 29:
            _migrate_to_v29(connection)
            _set_version(connection, 29)
            version = 29
        if version < 30:
            _migrate_to_v30(connection)
            _set_version(connection, 30)
            version = 30
        if version < 31:
            _migrate_to_v31(connection)
            _set_version(connection, 31)
            version = 31
        if version < 32:
            _migrate_to_v32(connection)
            _set_version(connection, 32)
            version = 32
        if version < 33:
            _migrate_to_v33(connection)
            _set_version(connection, 33)
            version = 33
        if version < 34:
            _migrate_to_v34(connection)
            _set_version(connection, 34)
            version = 34
        if version < 35:
            _migrate_to_v35(connection)
            _set_version(connection, 35)
            version = 35
        if version < 36:
            _migrate_to_v36(connection)
            _set_version(connection, 36)
            version = 36
        if version < 37:
            _migrate_to_v37(connection)
            _set_version(connection, 37)
            version = 37
        if version < 38:
            _migrate_to_v38(connection)
            _set_version(connection, 38)
            version = 38
        if version < 39:
            _migrate_to_v39(connection)
            _set_version(connection, 39)
            version = 39
        if version < 40:
            _migrate_to_v40(connection)
            _set_version(connection, 40)
            version = 40
        if version < 41:
            _migrate_to_v41(connection)
            _set_version(connection, 41)


def _migrate_to_v1(connection: sqlite3.Connection) -> None:
    """Create the original catalog, party, history and settings schema."""
    connection.executescript(
        """
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                album TEXT NOT NULL DEFAULT '',
                duration_seconds REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS queue_entries (
                id INTEGER PRIMARY KEY,
                track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                requested_by TEXT NOT NULL DEFAULT '',
                votes INTEGER NOT NULL DEFAULT 0 CHECK (votes >= 0),
                status TEXT NOT NULL DEFAULT 'waiting',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_queue_status_votes
                ON queue_entries(status, votes DESC, created_at);

            CREATE TABLE IF NOT EXISTS party_sessions (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                selected_playlist INTEGER,
                settings_snapshot TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_party_sessions_status
                ON party_sessions(status, started_at DESC);

            CREATE TABLE IF NOT EXISTS party_queue (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES party_sessions(id) ON DELETE CASCADE,
                track_id INTEGER NOT NULL REFERENCES tracks(id),
                position INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                source TEXT NOT NULL DEFAULT 'catalog',
                requested_by TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                loaded_deck TEXT,
                played_at TEXT,
                skip_reason TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_party_queue_session_position
                ON party_queue(session_id, position);
            CREATE INDEX IF NOT EXISTS idx_party_queue_status
                ON party_queue(session_id, status, position);
            CREATE INDEX IF NOT EXISTS idx_party_queue_track ON party_queue(track_id);

            CREATE TABLE IF NOT EXISTS play_history (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES party_sessions(id),
                track_id INTEGER NOT NULL REFERENCES tracks(id),
                deck_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                play_duration REAL NOT NULL DEFAULT 0,
                completion_status TEXT NOT NULL,
                queue_id INTEGER REFERENCES party_queue(id),
                skip_reason TEXT,
                error_message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_play_history_session ON play_history(session_id);
            CREATE INDEX IF NOT EXISTS idx_play_history_track ON play_history(track_id);
            CREATE INDEX IF NOT EXISTS idx_play_history_started ON play_history(started_at);

            CREATE TABLE IF NOT EXISTS party_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS saved_queues (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS saved_queue_entries (
                id INTEGER PRIMARY KEY,
                saved_queue_id INTEGER NOT NULL
                    REFERENCES saved_queues(id) ON DELETE CASCADE,
                track_id INTEGER NOT NULL REFERENCES tracks(id),
                position INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_saved_queue_entries_order
                ON saved_queue_entries(saved_queue_id, position);
        """
    )


def _migrate_to_v2(connection: sqlite3.Connection) -> None:
    """Add extended track metadata fields."""
    track_columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(tracks)").fetchall()
    }
    optional_columns = {
        "genre": "TEXT NOT NULL DEFAULT ''",
        "year": "INTEGER",
        "original_release_year": "INTEGER",
    }
    for name, definition in optional_columns.items():
        if name not in track_columns:
            connection.execute(f"ALTER TABLE tracks ADD COLUMN {name} {definition}")


def _migrate_to_v3(connection: sqlite3.Connection) -> None:
    """Add removable catalog entries and extended search indexes."""
    track_columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(tracks)").fetchall()
    }
    if "catalog_visible" not in track_columns:
        connection.execute(
            "ALTER TABLE tracks ADD COLUMN catalog_visible INTEGER NOT NULL DEFAULT 1"
        )
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_tracks_genre ON tracks(genre COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_tracks_year ON tracks(year);
        CREATE INDEX IF NOT EXISTS idx_tracks_original_release_year
            ON tracks(original_release_year);
        """
    )


def _set_version(connection: sqlite3.Connection, version: int) -> None:
    connection.execute("UPDATE schema_version SET version = ?", (version,))


def _migrate_to_v4(connection: sqlite3.Connection) -> None:
    """Add indexes for every foreign-key lookup used by party workflows."""
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_play_history_queue ON play_history(queue_id);
        CREATE INDEX IF NOT EXISTS idx_saved_queue_entries_track
            ON saved_queue_entries(track_id);
        """
    )


def _migrate_to_v5(connection: sqlite3.Connection) -> None:
    """Add composite indexes for the hot queue read paths."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'party_queue'"
    ).fetchone()
    if table is None:
        return
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_party_queue_session_position
            ON party_queue(session_id, position, id);
        CREATE INDEX IF NOT EXISTS idx_party_queue_active_track
            ON party_queue(session_id, track_id, status);
        """
    )


def _migrate_to_v6(connection: sqlite3.Connection) -> None:
    """Store DeckRelay cue points separately from music-file metadata."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS track_cue_points (
            id INTEGER PRIMARY KEY,
            track_id INTEGER NOT NULL UNIQUE REFERENCES tracks(id) ON DELETE CASCADE,
            manual_cue_in REAL,
            manual_cue_out REAL,
            manual_fade_duration REAL,
            automatic_cue_in REAL,
            automatic_cue_out REAL,
            automatic_fade_duration REAL,
            leading_level_db REAL,
            trailing_level_db REAL,
            confidence REAL,
            analysis_version TEXT,
            analysed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_track_cue_points_track
            ON track_cue_points(track_id);
        """
    )


def _migrate_to_v7(connection: sqlite3.Connection) -> None:
    """Store playback loudness data separately from source-file metadata."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS track_loudness (
            id INTEGER PRIMARY KEY,
            track_id INTEGER NOT NULL UNIQUE REFERENCES tracks(id) ON DELETE CASCADE,
            integrated_loudness_lufs REAL,
            loudness_range_lu REAL,
            true_peak_dbfs REAL,
            sample_peak_dbfs REAL,
            replaygain_track_gain_db REAL,
            replaygain_track_peak REAL,
            replaygain_album_gain_db REAL,
            replaygain_album_peak REAL,
            manual_gain_db REAL,
            analysis_source TEXT NOT NULL DEFAULT 'NONE',
            analysis_version TEXT,
            analysed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_track_loudness_track
            ON track_loudness(track_id);
        """
    )


def _migrate_to_v8(connection: sqlite3.Connection) -> None:
    """Add cue overrides owned by individual party-queue entries."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'party_queue'"
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(party_queue)").fetchall()
    }
    optional_columns = {
        "cue_in_override": "REAL",
        "cue_out_override": "REAL",
        "fade_duration_override": "REAL",
        "cue_override_source": "TEXT NOT NULL DEFAULT 'inherited'",
    }
    for name, definition in optional_columns.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE party_queue ADD COLUMN {name} {definition}")


def _migrate_to_v9(connection: sqlite3.Connection) -> None:
    """Add persistent cue snapshots to reusable saved-queue entries."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'saved_queue_entries'"
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(saved_queue_entries)").fetchall()
    }
    optional_columns = {
        "cue_in": "REAL",
        "cue_out": "REAL",
        "fade_duration": "REAL",
        "cue_source": "TEXT NOT NULL DEFAULT 'inherited'",
    }
    for name, definition in optional_columns.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE saved_queue_entries ADD COLUMN {name} {definition}")


def _migrate_to_v10(connection: sqlite3.Connection) -> None:
    """Add technical metadata for versioned automatic cue analysis."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'track_cue_points'"
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(track_cue_points)").fetchall()
    }
    optional_columns = {
        "minimum_level_dbfs": "REAL",
        "maximum_level_dbfs": "REAL",
        "peak": "REAL",
        "measured_window_count": "INTEGER",
        "analysis_backend": "TEXT",
    }
    for name, definition in optional_columns.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE track_cue_points ADD COLUMN {name} {definition}")


def _migrate_to_v11(connection: sqlite3.Connection) -> None:
    """Remember completed ReplayGain tag reads, including files without tags."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'track_loudness'"
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(track_loudness)").fetchall()
    }
    if "replaygain_scanned_at" not in columns:
        connection.execute("ALTER TABLE track_loudness ADD COLUMN replaygain_scanned_at TEXT")


def _migrate_to_v12(connection: sqlite3.Connection) -> None:
    """Separate explicit headroom from the legacy combined peak ceiling."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'party_settings'"
    ).fetchone()
    if table is None:
        return
    legacy_peak = connection.execute(
        "SELECT 1 FROM party_settings WHERE key = 'maximum_output_peak_db'"
    ).fetchone()
    connection.execute(
        """INSERT OR IGNORE INTO party_settings (key, value)
           VALUES ('headroom_db', ?)""",
        ("0" if legacy_peak is not None else "1",),
    )


def _migrate_to_v13(connection: sqlite3.Connection) -> None:
    """Persist loudness metadata availability independently from playback data."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'track_loudness'"
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(track_loudness)").fetchall()
    }
    if "metadata_status" not in columns:
        connection.execute(
            """ALTER TABLE track_loudness
               ADD COLUMN metadata_status TEXT NOT NULL DEFAULT 'NOT_ANALYSED'"""
        )
    connection.execute(
        """UPDATE track_loudness
           SET metadata_status = CASE
               WHEN (replaygain_track_gain_db IS NOT NULL
                     AND replaygain_track_peak IS NOT NULL)
                 OR (replaygain_album_gain_db IS NOT NULL
                     AND replaygain_album_peak IS NOT NULL) THEN 'COMPLETE'
               WHEN replaygain_scanned_at IS NOT NULL THEN 'INCOMPLETE'
               ELSE 'NOT_ANALYSED'
           END
           WHERE metadata_status = 'NOT_ANALYSED'"""
    )


def _migrate_to_v14(connection: sqlite3.Connection) -> None:
    """Add queue presentation metadata used by incremental row updates."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'party_queue'"
    ).fetchone()
    if table is None:
        return
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(party_queue)")}
    optional_columns = {
        "priority": "INTEGER NOT NULL DEFAULT 0",
        "locked": "INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1))",
        "request_count": "INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0)",
    }
    for name, definition in optional_columns.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE party_queue ADD COLUMN {name} {definition}")


def _migrate_to_v15(connection: sqlite3.Connection) -> None:
    """Persist complete offline loudness-analysis lifecycle metadata."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'track_loudness'"
    ).fetchone()
    if table is None:
        return
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(track_loudness)")}
    optional_columns = {
        "analysis_method": "TEXT",
        "analysis_status": "TEXT NOT NULL DEFAULT 'NOT_ANALYSED'",
        "analysis_error": "TEXT",
    }
    for name, definition in optional_columns.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE track_loudness ADD COLUMN {name} {definition}")


def _migrate_to_v16(connection: sqlite3.Connection) -> None:
    """Add queue ordering and normalized-artist lookup indexes."""
    party_queue = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'party_queue'"
    ).fetchone()
    if party_queue is not None:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(party_queue)")}
        if {"session_id", "status", "priority", "position", "id"} <= columns:
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_party_queue_selection
                   ON party_queue(session_id, status, priority DESC, position, id)"""
            )
    tracks = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tracks'"
    ).fetchone()
    if tracks is not None:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(tracks)")}
        if "artist" in columns:
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_tracks_normalized_artist
                   ON tracks(lower(trim(artist)))"""
            )


def _migrate_to_v17(connection: sqlite3.Connection) -> None:
    """Normalize legacy queue origins to stable domain enum values."""
    party_queue = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'party_queue'"
    ).fetchone()
    if party_queue is None:
        return
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(party_queue)")}
    if "source" not in columns:
        return
    connection.execute(
        """UPDATE party_queue
           SET source = CASE
               WHEN lower(trim(source)) LIKE 'saved_queue:%' THEN 'PLAYLIST'
               WHEN lower(trim(source)) LIKE 'directory:%' THEN 'PLAYLIST'
               ELSE CASE lower(trim(source))
               WHEN 'guest' THEN 'GUEST_REQUEST'
               WHEN 'guest_request' THEN 'GUEST_REQUEST'
               WHEN 'request' THEN 'GUEST_REQUEST'
               WHEN 'automatic' THEN 'AUTOMATIC'
               WHEN 'auto' THEN 'AUTOMATIC'
               WHEN 'playlist' THEN 'PLAYLIST'
               WHEN 'directory' THEN 'PLAYLIST'
               WHEN 'saved_queue' THEN 'PLAYLIST'
               WHEN 'emergency' THEN 'EMERGENCY'
               WHEN 'manual' THEN 'MANUAL'
               WHEN 'catalog' THEN 'MANUAL'
               WHEN 'queue' THEN 'MANUAL'
               ELSE 'MANUAL'
               END
           END"""
    )


def _migrate_to_v18(connection: sqlite3.Connection) -> None:
    """Migrate ambiguous legacy queue states to the explicit lifecycle."""
    party_queue = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'party_queue'"
    ).fetchone()
    if party_queue is None:
        return
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(party_queue)")}
    if "status" not in columns:
        return
    connection.execute(
        """UPDATE party_queue
           SET status = CASE lower(trim(status))
               WHEN 'loaded' THEN 'ready'
               WHEN 'error' THEN 'failed'
               WHEN 'waiting' THEN 'waiting'
               WHEN 'preparing' THEN 'preparing'
               WHEN 'ready' THEN 'ready'
               WHEN 'playing' THEN 'playing'
               WHEN 'played' THEN 'played'
               WHEN 'skipped' THEN 'skipped'
               WHEN 'failed' THEN 'failed'
               WHEN 'removed' THEN 'removed'
               ELSE 'failed'
           END"""
    )


def _migrate_to_v19(connection: sqlite3.Connection) -> None:
    """Add complete queue lock, request, preparation and outcome metadata."""
    party_queue = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'party_queue'"
    ).fetchone()
    if party_queue is None:
        return
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(party_queue)")}
    optional_columns = {
        "lock_source": "TEXT NOT NULL DEFAULT 'NONE'",
        "unique_requester_count": "INTEGER NOT NULL DEFAULT 0",
        "last_requested_at": "TEXT",
        "updated_at": "TEXT",
        "preparation_attempts": "INTEGER NOT NULL DEFAULT 0",
        "failure_code": "TEXT",
        "skip_code": "TEXT",
    }
    for name, definition in optional_columns.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE party_queue ADD COLUMN {name} {definition}")
    connection.execute(
        """UPDATE party_queue
           SET updated_at = COALESCE(updated_at, added_at),
               last_requested_at = CASE
                   WHEN request_count > 0 THEN COALESCE(last_requested_at, added_at)
                   ELSE last_requested_at
               END"""
    )


def _migrate_to_v20(connection: sqlite3.Connection) -> None:
    """Persist automatic-playback policy for individual catalog tracks."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS track_playback_policies (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'ALLOWED'
                CHECK (status IN ('ALLOWED', 'BLOCKED', 'RESTRICTED')),
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_track_playback_policies_status
            ON track_playback_policies(status);
        """
    )


def _migrate_to_v21(connection: sqlite3.Connection) -> None:
    """Persist normalized artist policies with explicit lifetime scopes."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS artist_playback_policies (
            normalized_artist TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            scope TEXT NOT NULL
                CHECK (scope IN ('PERMANENT', 'SESSION', 'TEMPORARY')),
            session_id INTEGER REFERENCES party_sessions(id) ON DELETE CASCADE,
            expires_at TEXT,
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_artist_playback_policies_scope
            ON artist_playback_policies(scope, session_id, expires_at);
        """
    )


def _migrate_to_v22(connection: sqlite3.Connection) -> None:
    """Store playback suitability independently from audio metadata."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS track_suitability (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'UNKNOWN'
                CHECK (status IN ('SUITABLE', 'MANUAL_ONLY', 'UNSUITABLE', 'UNKNOWN')),
            reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_track_suitability_status
            ON track_suitability(status);
        """
    )


def _migrate_to_v23(connection: sqlite3.Connection) -> None:
    """Track unique requesters when duplicate guest wishes are merged."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS queue_guest_requesters (
            queue_id INTEGER NOT NULL REFERENCES party_queue(id) ON DELETE CASCADE,
            requester_key TEXT NOT NULL,
            requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (queue_id, requester_key)
        );
        CREATE INDEX IF NOT EXISTS idx_queue_guest_requesters_queue
            ON queue_guest_requesters(queue_id);
        """
    )


def _migrate_to_v24(connection: sqlite3.Connection) -> None:
    """Record every identified guest request for rate and fairness rules."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS guest_request_events (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES party_sessions(id) ON DELETE CASCADE,
            queue_id INTEGER NOT NULL REFERENCES party_queue(id) ON DELETE CASCADE,
            requester_key TEXT NOT NULL,
            requested_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_guest_request_events_requester
            ON guest_request_events(session_id, requester_key, requested_at DESC);
        """
    )


def _migrate_to_v25(connection: sqlite3.Connection) -> None:
    """Normalize playback results to stable, language-independent codes."""
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(play_history)")}
    if "completion_status" not in columns:
        return
    connection.execute(
        """
        UPDATE play_history
        SET completion_status = CASE LOWER(TRIM(completion_status))
            WHEN 'completed' THEN 'PLAYED'
            WHEN 'played' THEN 'PLAYED'
            WHEN 'partially_played' THEN 'PARTIALLY_PLAYED'
            WHEN 'skipped' THEN 'SKIPPED'
            WHEN 'error' THEN 'FAILED'
            WHEN 'failed' THEN 'FAILED'
            WHEN 'stopped' THEN 'ABORTED'
            WHEN 'aborted' THEN 'ABORTED'
            ELSE 'ABORTED'
        END
        """
    )


def _migrate_to_v26(connection: sqlite3.Connection) -> None:
    """Snapshot playback metrics, origin, stable codes and cue overrides."""
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(play_history)")}
    if not columns:
        return
    optional_columns = {
        "effective_duration": "REAL",
        "playback_ratio": "REAL",
        "queue_source": "TEXT",
        "result_code": "TEXT",
        "skip_code": "TEXT",
        "cue_in_override": "REAL",
        "cue_out_override": "REAL",
        "fade_duration_override": "REAL",
        "cue_override_source": "TEXT",
        "override_applied": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in optional_columns.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE play_history ADD COLUMN {name} {definition}")

    history_fields = {
        "track_id",
        "play_duration",
        "completion_status",
        "skip_reason",
    }
    if not history_fields <= columns:
        return
    connection.executescript(
        """
        UPDATE play_history
        SET effective_duration = (
                SELECT duration_seconds FROM tracks WHERE tracks.id = play_history.track_id
            ),
            result_code = completion_status,
            skip_code = CASE
                WHEN completion_status IN ('SKIPPED', 'ABORTED')
                    THEN CASE WHEN skip_reason IS NULL THEN 'UNSPECIFIED' ELSE 'LEGACY_REASON' END
                ELSE NULL
            END;
        UPDATE play_history
        SET playback_ratio = CASE
            WHEN effective_duration > 0
                THEN MIN(1.0, MAX(0.0, play_duration / effective_duration))
            ELSE NULL
        END;
        """
    )

    queue_columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(party_queue)")
    }
    required = {
        "source",
        "cue_in_override",
        "cue_out_override",
        "fade_duration_override",
        "cue_override_source",
    }
    if required <= queue_columns:
        connection.executescript(
            """
            UPDATE play_history
            SET queue_source = (
                    SELECT source FROM party_queue WHERE party_queue.id = play_history.queue_id
                ),
                cue_in_override = (
                    SELECT cue_in_override
                    FROM party_queue WHERE party_queue.id = play_history.queue_id
                ),
                cue_out_override = (
                    SELECT cue_out_override
                    FROM party_queue WHERE party_queue.id = play_history.queue_id
                ),
                fade_duration_override = (
                    SELECT fade_duration_override
                    FROM party_queue WHERE party_queue.id = play_history.queue_id
                ),
                cue_override_source = (
                    SELECT cue_override_source
                    FROM party_queue WHERE party_queue.id = play_history.queue_id
                );
            UPDATE play_history
            SET override_applied = CASE
                WHEN cue_override_source IN ('queue', 'snapshot')
                 AND (cue_in_override IS NOT NULL
                      OR cue_out_override IS NOT NULL
                      OR fade_duration_override IS NOT NULL)
                    THEN 1
                ELSE 0
            END;
            """
        )


def _migrate_to_v27(connection: sqlite3.Connection) -> None:
    """Record the effective cue boundaries used for completion metrics."""
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(play_history)")}
    if not columns:
        return
    for name in ("effective_cue_in", "effective_cue_out"):
        if name not in columns:
            connection.execute(f"ALTER TABLE play_history ADD COLUMN {name} REAL")


def _migrate_to_v28(connection: sqlite3.Connection) -> None:
    """Normalize history skip and abort reasons to the stable code catalog."""
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(play_history)")}
    if not {"completion_status", "skip_code", "skip_reason"} <= columns:
        return
    connection.execute(
        """
        UPDATE play_history
        SET skip_code = CASE
            WHEN skip_code IN (
                'OPERATOR_SKIP', 'DECK_EJECT', 'DECK_STOP',
                'APPLICATION_SHUTDOWN', 'TRACK_REPLACED', 'PLAYBACK_ERROR',
                'UNSPECIFIED', 'LEGACY_REASON'
            ) THEN skip_code
            WHEN skip_reason IS NOT NULL THEN 'LEGACY_REASON'
            WHEN completion_status IN ('SKIPPED', 'ABORTED', 'PARTIALLY_PLAYED')
                THEN 'UNSPECIFIED'
            ELSE NULL
        END
        """
    )


def _migrate_to_v29(connection: sqlite3.Connection) -> None:
    """Add a session-owned audit trail for operational decisions."""
    tables = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "party_sessions" not in tables:
        return
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS session_audit_events (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES party_sessions(id) ON DELETE CASCADE,
            event_code TEXT NOT NULL,
            entity_type TEXT,
            entity_id INTEGER,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_session_audit_events_session
            ON session_audit_events(session_id, created_at, id);
        """
    )
    if "party_queue" not in tables:
        return
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS trg_party_queue_audit_insert
        AFTER INSERT ON party_queue
        BEGIN
            INSERT INTO session_audit_events
                (session_id, event_code, entity_type, entity_id, details)
            VALUES
                (NEW.session_id, 'QUEUE_ADDED', 'QUEUE', NEW.id,
                 json_object('track_id', NEW.track_id, 'source', NEW.source));
        END;

        CREATE TRIGGER IF NOT EXISTS trg_party_queue_audit_change
        AFTER UPDATE ON party_queue
        WHEN OLD.position IS NOT NEW.position
          OR OLD.status IS NOT NEW.status
          OR OLD.source IS NOT NEW.source
          OR OLD.priority IS NOT NEW.priority
        BEGIN
            INSERT INTO session_audit_events
                (session_id, event_code, entity_type, entity_id, details)
            VALUES
                (NEW.session_id, 'QUEUE_CHANGED', 'QUEUE', NEW.id,
                 json_object('position', NEW.position, 'status', NEW.status,
                             'source', NEW.source, 'priority', NEW.priority));
        END;

        CREATE TRIGGER IF NOT EXISTS trg_party_queue_audit_lock
        AFTER UPDATE ON party_queue
        WHEN OLD.locked IS NOT NEW.locked OR OLD.lock_source IS NOT NEW.lock_source
        BEGIN
            INSERT INTO session_audit_events
                (session_id, event_code, entity_type, entity_id, details)
            VALUES
                (NEW.session_id, 'QUEUE_LOCK_CHANGED', 'QUEUE', NEW.id,
                 json_object('locked', NEW.locked, 'lock_source', NEW.lock_source));
        END;
        """
    )


def _migrate_to_v30(connection: sqlite3.Connection) -> None:
    """Guarantee that an active deck belongs to at most one queue entry."""
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(party_queue)")}
    if not {"id", "session_id", "status", "loaded_deck"} <= columns:
        return
    connection.executescript(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY session_id, loaded_deck
                       ORDER BY CASE status
                           WHEN 'playing' THEN 0
                           WHEN 'ready' THEN 1
                           ELSE 2
                       END, id
                   ) AS assignment_rank
            FROM party_queue
            WHERE loaded_deck IS NOT NULL
              AND status IN ('preparing', 'ready', 'playing')
        )
        UPDATE party_queue
        SET status = 'waiting', loaded_deck = NULL,
            locked = CASE
                WHEN lock_source IN ('MANUAL', 'MANUAL_SYSTEM') THEN 1 ELSE 0
            END,
            lock_source = CASE
                WHEN lock_source IN ('MANUAL', 'MANUAL_SYSTEM') THEN 'MANUAL' ELSE 'NONE'
            END
        WHERE id IN (SELECT id FROM ranked WHERE assignment_rank > 1);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_party_queue_active_deck
            ON party_queue(session_id, loaded_deck)
            WHERE loaded_deck IS NOT NULL
              AND status IN ('preparing', 'ready', 'playing');
        """
    )


def _migrate_to_v31(connection: sqlite3.Connection) -> None:
    """Persist reusable equalizer presets and inherited assignments."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS equalizer_presets (
            id INTEGER PRIMARY KEY,
            preset_key TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description TEXT NOT NULL DEFAULT '',
            is_builtin INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
            is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
            preamp_db REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS equalizer_preset_bands (
            preset_id INTEGER NOT NULL
                REFERENCES equalizer_presets(id) ON DELETE CASCADE,
            band_index INTEGER NOT NULL CHECK (band_index >= 0),
            frequency_hz REAL NOT NULL CHECK (frequency_hz > 0),
            gain_db REAL NOT NULL,
            PRIMARY KEY (preset_id, band_index)
        );

        CREATE TABLE IF NOT EXISTS track_equalizer_assignments (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
            equalizer_preset_id INTEGER NOT NULL
                REFERENCES equalizer_presets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_track_equalizer_preset
            ON track_equalizer_assignments(equalizer_preset_id);

        CREATE TABLE IF NOT EXISTS genre_equalizer_assignments (
            genre_key TEXT PRIMARY KEY COLLATE NOCASE,
            genre_name TEXT NOT NULL,
            equalizer_preset_id INTEGER NOT NULL
                REFERENCES equalizer_presets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_genre_equalizer_preset
            ON genre_equalizer_assignments(equalizer_preset_id);
        """
    )
    saved_queues_exists = connection.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type = 'table' AND name = 'saved_queues'"""
    ).fetchone()
    saved_queue_columns = (
        {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(saved_queues)").fetchall()
        }
        if saved_queues_exists is not None
        else set()
    )
    if saved_queues_exists is not None and "equalizer_preset_id" not in saved_queue_columns:
        connection.execute(
            """ALTER TABLE saved_queues ADD COLUMN equalizer_preset_id INTEGER
               REFERENCES equalizer_presets(id) ON DELETE SET NULL"""
        )
    presets = (
        ("disabled", "Equalizer aus", "Equalizer vollständig deaktivieren", 0.0, ()),
        ("neutral", "Neutral", "Unveränderte Klangfarbe", 0.0, ()),
        (
            "rock",
            "Rock",
            "Konservatives Rock-Preset",
            -3.0,
            ((60.0, 2.0), (170.0, 1.5), (600.0, -1.0), (3000.0, 1.5), (16000.0, 2.0)),
        ),
        (
            "pop",
            "Pop",
            "Konservatives Pop-Preset",
            -3.0,
            ((60.0, 1.0), (170.0, 2.0), (1000.0, -0.5), (6000.0, 2.0), (16000.0, 1.0)),
        ),
        (
            "bluesrock",
            "Bluesrock",
            "Konservatives Bluesrock-Preset",
            -3.0,
            ((60.0, 1.5), (170.0, 2.0), (600.0, -0.5), (3000.0, 1.0), (12000.0, 1.0)),
        ),
        (
            "dance",
            "Dance",
            "Konservatives Dance-Preset",
            -3.0,
            ((60.0, 2.5), (170.0, 1.5), (600.0, -1.0), (3000.0, 1.0), (12000.0, 2.0)),
        ),
    )
    for preset_key, name, description, preamp_db, bands in presets:
        connection.execute(
            """INSERT INTO equalizer_presets
               (preset_key, name, description, is_builtin, preamp_db)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(preset_key) DO UPDATE SET
                   name = excluded.name,
                   description = excluded.description,
                   is_builtin = 1,
                   is_enabled = 1,
                   preamp_db = excluded.preamp_db,
                   updated_at = CURRENT_TIMESTAMP""",
            (preset_key, name, description, preamp_db),
        )
        row = connection.execute(
            "SELECT id FROM equalizer_presets WHERE preset_key = ?", (preset_key,)
        ).fetchone()
        assert row is not None
        preset_id = int(row["id"])
        connection.execute("DELETE FROM equalizer_preset_bands WHERE preset_id = ?", (preset_id,))
        connection.executemany(
            """INSERT INTO equalizer_preset_bands
               (preset_id, band_index, frequency_hz, gain_db)
               VALUES (?, ?, ?, ?)""",
            [
                (preset_id, index, frequency_hz, gain_db)
                for index, (frequency_hz, gain_db) in enumerate(bands)
            ],
        )


def _migrate_to_v32(connection: sqlite3.Connection) -> None:
    """Add independent audio overlays and their separate playback history."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS audio_overlays (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(name)) > 0),
            category TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL CHECK (length(trim(file_path)) > 0),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            volume_percent INTEGER NOT NULL DEFAULT 75
                CHECK (volume_percent BETWEEN 0 AND 100),
            fade_in_ms INTEGER NOT NULL DEFAULT 300
                CHECK (fade_in_ms BETWEEN 0 AND 60000),
            fade_out_ms INTEGER NOT NULL DEFAULT 500
                CHECK (fade_out_ms BETWEEN 0 AND 60000),
            ducking_enabled INTEGER NOT NULL DEFAULT 1
                CHECK (ducking_enabled IN (0, 1)),
            ducking_db REAL NOT NULL DEFAULT 0.0
                CHECK (ducking_db BETWEEN -60.0 AND 0.0),
            ducking_attack_ms INTEGER NOT NULL DEFAULT 200
                CHECK (ducking_attack_ms BETWEEN 0 AND 60000),
            ducking_release_ms INTEGER NOT NULL DEFAULT 500
                CHECK (ducking_release_ms BETWEEN 0 AND 60000),
            cue_in_ms INTEGER NOT NULL DEFAULT 0 CHECK (cue_in_ms >= 0),
            cue_out_ms INTEGER CHECK (cue_out_ms IS NULL OR cue_out_ms > cue_in_ms),
            favorite_position INTEGER CHECK (favorite_position BETWEEN 1 AND 6),
            keyboard_shortcut TEXT COLLATE NOCASE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_audio_overlays_category_name
            ON audio_overlays(category COLLATE NOCASE, name COLLATE NOCASE);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_overlays_favorite
            ON audio_overlays(favorite_position) WHERE favorite_position IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_overlays_shortcut
            ON audio_overlays(keyboard_shortcut COLLATE NOCASE)
            WHERE keyboard_shortcut IS NOT NULL;

        CREATE TABLE IF NOT EXISTS overlay_play_history (
            id INTEGER PRIMARY KEY,
            overlay_id INTEGER REFERENCES audio_overlays(id) ON DELETE SET NULL,
            overlay_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            trigger_type TEXT NOT NULL DEFAULT 'MANUAL'
                CHECK (trigger_type IN ('MANUAL', 'AUTOMATIC')),
            result TEXT NOT NULL
                CHECK (result IN ('COMPLETED', 'FADED_OUT', 'STOPPED', 'FAILED')),
            error_message TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_overlay_history_started
            ON overlay_play_history(started_at, id);
        """
    )


def _migrate_to_v33(connection: sqlite3.Connection) -> None:
    """Add emergency incidents independently from queue and normal session audit."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS emergency_incidents (
            id INTEGER PRIMARY KEY,
            session_id INTEGER,
            status TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE', 'RESOLVED')),
            system_state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            deck_a_health TEXT NOT NULL,
            deck_b_health TEXT NOT NULL,
            audio_device_id TEXT NOT NULL DEFAULT '',
            last_event_code TEXT NOT NULL DEFAULT '',
            last_result TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_emergency_incidents_status
            ON emergency_incidents(status, updated_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_emergency_incidents_session
            ON emergency_incidents(session_id, updated_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS emergency_incident_events (
            id INTEGER PRIMARY KEY,
            incident_id INTEGER NOT NULL
                REFERENCES emergency_incidents(id) ON DELETE CASCADE,
            session_id INTEGER,
            event_code TEXT NOT NULL,
            system_state TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_emergency_incident_events_incident
            ON emergency_incident_events(incident_id, created_at, id);
        """
    )


def _migrate_to_v34(connection: sqlite3.Connection) -> None:
    """Add queue-independent history for confirmed emergency playback."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS emergency_play_history (
            id INTEGER PRIMARY KEY,
            session_id INTEGER,
            track_id INTEGER NOT NULL,
            deck_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'EMERGENCY'
                CHECK (source = 'EMERGENCY'),
            title TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL DEFAULT '',
            cue_in REAL NOT NULL DEFAULT 0,
            effective_gain_db REAL NOT NULL DEFAULT 0,
            clip_protection_enabled INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_emergency_play_history_started
            ON emergency_play_history(started_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_emergency_play_history_session
            ON emergency_play_history(session_id, started_at DESC, id DESC);
        """
    )


def _migrate_to_v35(connection: sqlite3.Connection) -> None:
    """Add typed catalog metadata, provenance, and validated analysis proposals."""
    tracks_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tracks'"
    ).fetchone()
    if tracks_exists is None:
        return
    track_columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(tracks)").fetchall()
    }
    additions = {
        "recording_type": (
            "TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (recording_type IN "
            "('ORIGINAL', 'RE_RECORDING', 'LIVE', 'REMIX', 'RADIO_EDIT', 'UNKNOWN'))"
        ),
        "is_remastered": "INTEGER NOT NULL DEFAULT 0 CHECK (is_remastered IN (0, 1))",
        "bpm": "REAL CHECK (bpm IS NULL OR (bpm >= 20 AND bpm <= 300))",
        "bpm_confidence": (
            "REAL CHECK (bpm_confidence IS NULL OR (bpm_confidence >= 0 AND bpm_confidence <= 1))"
        ),
        "alternative_bpm": (
            "REAL CHECK (alternative_bpm IS NULL OR "
            "(alternative_bpm >= 20 AND alternative_bpm <= 300))"
        ),
        "energy": "INTEGER CHECK (energy IS NULL OR (energy >= 0 AND energy <= 100))",
        "danceability": (
            "INTEGER CHECK (danceability IS NULL OR (danceability >= 0 AND danceability <= 100))"
        ),
        "language": "TEXT",
        "rating": "INTEGER CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5))",
        "comment": "TEXT",
        "metadata_revision": "INTEGER NOT NULL DEFAULT 0 CHECK (metadata_revision >= 0)",
    }
    for name, definition in additions.items():
        if name not in track_columns:
            connection.execute(f"ALTER TABLE tracks ADD COLUMN {name} {definition}")

    field_keys = (
        "'year', 'original_release_year', 'recording_classification', 'bpm', "
        "'bpm_confidence', 'alternative_bpm', 'main_genre', 'energy', "
        "'danceability', 'language', 'rating', 'comment', 'musical_decades', "
        "'additional_genres', 'moods', 'tags'"
    )
    source_types = (
        "'FILE_TAG', 'AUDIO_ANALYSIS', 'EXTERNAL_MUSIC_DATABASE', "
        "'FILE_OR_FOLDER_DERIVATION', 'MANUAL_INPUT', 'MANUAL_CONFIRMATION'"
    )
    review_statuses = (
        "'MISSING', 'IMPORTED', 'ANALYSED', 'SUGGESTED', 'REVIEW_REQUIRED', "
        "'CONFIRMED_WITH_VALUE', 'CONFIRMED_WITHOUT_VALUE', 'CONFLICTING', "
        "'FAILED', 'OUTDATED'"
    )
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS metadata_terms (
            id INTEGER PRIMARY KEY,
            term_type TEXT NOT NULL CHECK (
                term_type IN ('MUSICAL_DECADE', 'ADDITIONAL_GENRE', 'MOOD', 'FREE_TAG')
            ),
            normalized_key TEXT NOT NULL CHECK (length(normalized_key) > 0),
            display_name TEXT NOT NULL CHECK (length(display_name) > 0),
            numeric_value INTEGER,
            CHECK (
                (term_type = 'MUSICAL_DECADE' AND numeric_value IS NOT NULL
                 AND numeric_value >= 1870 AND numeric_value <= 2100
                 AND numeric_value % 10 = 0)
                OR (term_type <> 'MUSICAL_DECADE' AND numeric_value IS NULL)
            ),
            UNIQUE (term_type, normalized_key)
        );

        CREATE TABLE IF NOT EXISTS track_metadata_terms (
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            term_id INTEGER NOT NULL REFERENCES metadata_terms(id) ON DELETE RESTRICT,
            PRIMARY KEY (track_id, term_id)
        );
        CREATE INDEX IF NOT EXISTS idx_track_metadata_terms_term
            ON track_metadata_terms(term_id, track_id);

        CREATE TABLE IF NOT EXISTS track_metadata_field_state (
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            field_key TEXT NOT NULL CHECK (field_key IN ({field_keys})),
            source_type TEXT NOT NULL CHECK (source_type IN ({source_types})),
            source_detail TEXT NOT NULL DEFAULT '',
            confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            review_status TEXT NOT NULL CHECK (review_status IN ({review_statuses})),
            analysis_version TEXT,
            confirmed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (track_id, field_key)
        );
        CREATE INDEX IF NOT EXISTS idx_track_metadata_field_state_review
            ON track_metadata_field_state(review_status, field_key);

        CREATE TABLE IF NOT EXISTS metadata_analysis_runs (
            id INTEGER PRIMARY KEY,
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            analysis_profile TEXT NOT NULL CHECK (length(analysis_profile) > 0),
            analysis_version TEXT NOT NULL CHECK (length(analysis_version) > 0),
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK (
                status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')
            ),
            priority INTEGER NOT NULL DEFAULT 0,
            file_path_snapshot TEXT NOT NULL,
            file_size INTEGER NOT NULL CHECK (file_size >= 0),
            file_modified_ns INTEGER NOT NULL CHECK (file_modified_ns >= 0),
            fingerprint TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            finished_at TEXT,
            error_code TEXT,
            error_text TEXT CHECK (error_text IS NULL OR length(error_text) <= 500)
        );
        CREATE INDEX IF NOT EXISTS idx_metadata_analysis_runs_pending
            ON metadata_analysis_runs(status, priority DESC, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_metadata_analysis_runs_track
            ON metadata_analysis_runs(track_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS track_metadata_suggestions (
            id INTEGER PRIMARY KEY,
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            analysis_run_id INTEGER NOT NULL
                REFERENCES metadata_analysis_runs(id) ON DELETE CASCADE,
            field_key TEXT NOT NULL CHECK (field_key IN ({field_keys})),
            serialized_value TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK (source_type IN ({source_types})),
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            review_status TEXT NOT NULL DEFAULT 'SUGGESTED'
                CHECK (review_status IN ({review_statuses})),
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'SUPERSEDED')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TEXT,
            decision_reason TEXT CHECK (
                decision_reason IS NULL OR length(decision_reason) <= 500
            )
        );
        CREATE INDEX IF NOT EXISTS idx_track_metadata_suggestions_open
            ON track_metadata_suggestions(track_id, field_key, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_track_metadata_suggestions_run
            ON track_metadata_suggestions(analysis_run_id);
        """
    )


def _migrate_to_v36(connection: sqlite3.Connection) -> None:
    """Allow core tag fields and retain bounded source detail on suggestions."""
    tables = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "track_metadata_field_state" not in tables:
        return
    field_keys = (
        "'title', 'artist', 'album', 'year', 'original_release_year', "
        "'recording_classification', 'bpm', 'bpm_confidence', 'alternative_bpm', "
        "'main_genre', 'energy', 'danceability', 'language', 'rating', 'comment', "
        "'musical_decades', 'additional_genres', 'moods', 'tags'"
    )
    source_types = (
        "'FILE_TAG', 'AUDIO_ANALYSIS', 'EXTERNAL_MUSIC_DATABASE', "
        "'FILE_OR_FOLDER_DERIVATION', 'MANUAL_INPUT', 'MANUAL_CONFIRMATION'"
    )
    review_statuses = (
        "'MISSING', 'IMPORTED', 'ANALYSED', 'SUGGESTED', 'REVIEW_REQUIRED', "
        "'CONFIRMED_WITH_VALUE', 'CONFIRMED_WITHOUT_VALUE', 'CONFLICTING', "
        "'FAILED', 'OUTDATED'"
    )
    connection.executescript(
        f"""
        DROP INDEX IF EXISTS idx_track_metadata_field_state_review;
        ALTER TABLE track_metadata_field_state RENAME TO track_metadata_field_state_v35;
        CREATE TABLE track_metadata_field_state (
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            field_key TEXT NOT NULL CHECK (field_key IN ({field_keys})),
            source_type TEXT NOT NULL CHECK (source_type IN ({source_types})),
            source_detail TEXT NOT NULL DEFAULT '',
            confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            review_status TEXT NOT NULL CHECK (review_status IN ({review_statuses})),
            analysis_version TEXT,
            confirmed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (track_id, field_key)
        );
        INSERT INTO track_metadata_field_state
            (track_id, field_key, source_type, source_detail, confidence, review_status,
             analysis_version, confirmed_at, updated_at)
        SELECT track_id, field_key, source_type, source_detail, confidence, review_status,
               analysis_version, confirmed_at, updated_at
        FROM track_metadata_field_state_v35;
        DROP TABLE track_metadata_field_state_v35;
        CREATE INDEX idx_track_metadata_field_state_review
            ON track_metadata_field_state(review_status, field_key);

        DROP INDEX IF EXISTS idx_track_metadata_suggestions_open;
        DROP INDEX IF EXISTS idx_track_metadata_suggestions_run;
        ALTER TABLE track_metadata_suggestions RENAME TO track_metadata_suggestions_v35;
        CREATE TABLE track_metadata_suggestions (
            id INTEGER PRIMARY KEY,
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            analysis_run_id INTEGER NOT NULL
                REFERENCES metadata_analysis_runs(id) ON DELETE CASCADE,
            field_key TEXT NOT NULL CHECK (field_key IN ({field_keys})),
            serialized_value TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK (source_type IN ({source_types})),
            source_detail TEXT NOT NULL DEFAULT '' CHECK (length(source_detail) <= 200),
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            review_status TEXT NOT NULL DEFAULT 'SUGGESTED'
                CHECK (review_status IN ({review_statuses})),
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'SUPERSEDED')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TEXT,
            decision_reason TEXT CHECK (
                decision_reason IS NULL OR length(decision_reason) <= 500
            )
        );
        INSERT INTO track_metadata_suggestions
            (id, track_id, analysis_run_id, field_key, serialized_value, source_type,
             confidence, review_status, status, created_at, decided_at, decision_reason)
        SELECT id, track_id, analysis_run_id, field_key, serialized_value, source_type,
               confidence, review_status, status, created_at, decided_at, decision_reason
        FROM track_metadata_suggestions_v35;
        DROP TABLE track_metadata_suggestions_v35;
        CREATE INDEX idx_track_metadata_suggestions_open
            ON track_metadata_suggestions(track_id, field_key, status, created_at);
        CREATE INDEX idx_track_metadata_suggestions_run
            ON track_metadata_suggestions(analysis_run_id);
        """
    )


def _migrate_to_v37(connection: sqlite3.Connection) -> None:
    """Persist bounded catalog-maintenance batches and reversible field changes."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata_batch_actions (
            id INTEGER PRIMARY KEY,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL,
            selection_json TEXT NOT NULL CHECK(length(selection_json) <= 20000),
            field_mask_json TEXT NOT NULL CHECK(length(field_mask_json) <= 4000),
            preview_token TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            planned_count INTEGER NOT NULL DEFAULT 0,
            changed_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            summary_json TEXT NOT NULL DEFAULT '{}' CHECK(length(summary_json) <= 20000),
            undone_by_batch_id INTEGER REFERENCES metadata_batch_actions(id)
        );
        CREATE TABLE IF NOT EXISTS metadata_batch_changes (
            id INTEGER PRIMARY KEY,
            batch_id INTEGER NOT NULL REFERENCES metadata_batch_actions(id) ON DELETE CASCADE,
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            field_key TEXT NOT NULL,
            previous_value_json TEXT,
            new_value_json TEXT,
            previous_state_json TEXT,
            new_state_json TEXT,
            revision_before INTEGER NOT NULL,
            revision_after INTEGER NOT NULL,
            result_status TEXT NOT NULL,
            UNIQUE(batch_id, track_id, field_key)
        );
        CREATE INDEX IF NOT EXISTS idx_metadata_batch_changes_batch
            ON metadata_batch_changes(batch_id, track_id);
        CREATE INDEX IF NOT EXISTS idx_metadata_batch_actions_finished
            ON metadata_batch_actions(status, finished_at DESC);
        """
    )


def _migrate_to_v38(connection: sqlite3.Connection) -> None:
    """Persist reversible proposal decisions made by catalog batches."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata_batch_suggestion_changes (
            id INTEGER PRIMARY KEY,
            batch_id INTEGER NOT NULL REFERENCES metadata_batch_actions(id) ON DELETE CASCADE,
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            suggestion_id INTEGER NOT NULL
                REFERENCES track_metadata_suggestions(id) ON DELETE RESTRICT,
            field_key TEXT NOT NULL,
            previous_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            previous_decided_at TEXT,
            new_decided_at TEXT,
            previous_decision_reason TEXT,
            new_decision_reason TEXT,
            superseded_by_acceptance INTEGER NOT NULL DEFAULT 0
                CHECK(superseded_by_acceptance IN (0, 1)),
            UNIQUE(batch_id, suggestion_id)
        );
        CREATE INDEX IF NOT EXISTS idx_metadata_batch_suggestion_changes_batch
            ON metadata_batch_suggestion_changes(batch_id, track_id, suggestion_id);
        """
    )


def _migrate_to_v39(connection: sqlite3.Connection) -> None:
    """Persist bounded, typed technical metrics and analyzed ranges per run."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata_analysis_run_metrics (
            run_id INTEGER NOT NULL
                REFERENCES metadata_analysis_runs(id) ON DELETE CASCADE,
            metric_key TEXT NOT NULL CHECK(metric_key IN (
                'rms_mean', 'rms_variability', 'peak', 'crest_factor',
                'transient_density', 'rhythm_stability', 'bpm',
                'energy_experimental'
            )),
            metric_value REAL NOT NULL,
            unit TEXT NOT NULL DEFAULT '' CHECK(length(unit) <= 24),
            algorithm_version TEXT NOT NULL CHECK(length(algorithm_version) BETWEEN 1 AND 80),
            experimental INTEGER NOT NULL DEFAULT 0 CHECK(experimental IN (0, 1)),
            PRIMARY KEY(run_id, metric_key)
        );
        CREATE TABLE IF NOT EXISTS metadata_analysis_run_ranges (
            run_id INTEGER NOT NULL
                REFERENCES metadata_analysis_runs(id) ON DELETE CASCADE,
            range_index INTEGER NOT NULL CHECK(range_index BETWEEN 0 AND 7),
            start_seconds REAL NOT NULL CHECK(start_seconds >= 0),
            duration_seconds REAL NOT NULL CHECK(duration_seconds > 0 AND duration_seconds <= 90),
            PRIMARY KEY(run_id, range_index)
        );
        """
    )


def _migrate_to_v40(connection: sqlite3.Connection) -> None:
    """Persist cue-aware tempo results and saved-queue-local manual BPM values."""
    has_analysis_runs = connection.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='metadata_analysis_runs'"""
    ).fetchone()
    run_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(metadata_analysis_runs)").fetchall()
    }
    additions = {
        "scope_type": "TEXT NOT NULL DEFAULT 'TRACK_FULL'",
        "context_id": "INTEGER",
        "range_signature": "TEXT NOT NULL DEFAULT ''",
        "cue_in_ms": "INTEGER",
        "cue_out_ms": "INTEGER",
        "fade_ms": "INTEGER",
        "physical_duration_ms": "INTEGER",
        "context_revision": "TEXT",
        "inherited_track_cues": "INTEGER NOT NULL DEFAULT 0",
        "range_resolved_at": "TEXT",
    }
    for name, definition in additions.items():
        if has_analysis_runs is not None and name not in run_columns:
            connection.execute(f"ALTER TABLE metadata_analysis_runs ADD COLUMN {name} {definition}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS tempo_analysis_results (
            id INTEGER PRIMARY KEY,
            track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            scope_type TEXT NOT NULL CHECK(scope_type IN (
                'TRACK_FULL','TRACK_DEFAULT_CUES','SAVED_QUEUE_ENTRY','PARTY_QUEUE_SNAPSHOT'
            )),
            context_id INTEGER,
            run_id INTEGER REFERENCES metadata_analysis_runs(id) ON DELETE SET NULL,
            range_signature TEXT NOT NULL CHECK(length(range_signature)=64),
            cue_in_ms INTEGER NOT NULL CHECK(cue_in_ms>=0),
            cue_out_ms INTEGER NOT NULL CHECK(cue_out_ms>cue_in_ms),
            fade_ms INTEGER NOT NULL CHECK(fade_ms>=0),
            physical_duration_ms INTEGER NOT NULL CHECK(physical_duration_ms>=cue_out_ms),
            context_revision TEXT NOT NULL,
            inherited_track_cues INTEGER NOT NULL DEFAULT 0
                CHECK(inherited_track_cues IN (0,1)),
            primary_bpm REAL,
            alternative_bpm REAL,
            confidence REAL CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1),
            rhythm_stability REAL CHECK(
                rhythm_stability IS NULL OR rhythm_stability BETWEEN 0 AND 1
            ),
            warnings_json TEXT NOT NULL DEFAULT '[]',
            experimental_energy REAL,
            backend TEXT NOT NULL,
            algorithm_version TEXT NOT NULL,
            analyzed_at TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
            stale_reason TEXT,
            UNIQUE(scope_type, context_id, range_signature, run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tempo_results_track_scope_current
            ON tempo_analysis_results(track_id,scope_type,is_current,analyzed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tempo_results_context
            ON tempo_analysis_results(scope_type,context_id,is_current);

        CREATE TABLE IF NOT EXISTS saved_queue_entry_tempo_overrides (
            saved_queue_entry_id INTEGER PRIMARY KEY
                REFERENCES saved_queue_entries(id) ON DELETE CASCADE,
            bpm REAL NOT NULL CHECK(bpm BETWEEN 20 AND 400),
            confirmed INTEGER NOT NULL DEFAULT 1 CHECK(confirmed IN (0,1)),
            source TEXT NOT NULL CHECK(source='MANUAL_SAVED_QUEUE'),
            changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            based_on_signature TEXT CHECK(
                based_on_signature IS NULL OR length(based_on_signature)=64
            )
        );
        """
    )


def _migrate_to_v41(connection: sqlite3.Connection) -> None:
    """Persist bounded structured diagnostics for each tempo-analysis run."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata_analysis_runs'"
    ).fetchone()
    if table is None:
        return
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(metadata_analysis_runs)").fetchall()
    }
    if "diagnostics_json" not in columns:
        connection.execute(
            "ALTER TABLE metadata_analysis_runs ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}'"
        )
