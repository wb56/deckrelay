"""Focused schema and transactional catalog-metadata persistence tests."""

from pathlib import Path
import sqlite3

import pytest

from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.metadata_persistence import (
    AnalysisRunRepository,
    AnalysisRunStatus,
    EffectiveMetadataRepository,
    MetadataFieldState,
    MetadataFieldStateRepository,
    MetadataPersistenceService,
    MetadataRevisionConflict,
    MetadataSuggestionRepository,
    MultiValueMetadataRepository,
    SuggestionStatus,
)
from party_player.metadata_rules import (
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
    RecordingClassification,
    RecordingKind,
    RecordingTrait,
)


def add_track(database: Database, suffix: str = "one") -> int:
    with database.connect() as connection:
        cursor = connection.execute(
            """INSERT INTO tracks
                   (file_path, title, artist, album, genre, year, original_release_year)
               VALUES (?, ?, 'Artist', 'Album', 'Rock', 2001, 1998)""",
            (f"{suffix}.mp3", suffix.title()),
        )
        return int(cursor.lastrowid)


def state(
    track_id: int,
    field: MetadataFieldKey,
    status: MetadataReviewStatus = MetadataReviewStatus.CONFIRMED_WITH_VALUE,
    source: MetadataSource = MetadataSource.MANUAL_CONFIRMATION,
) -> MetadataFieldState:
    return MetadataFieldState(track_id, field, source, "test", 0.95, status, "test-v1")


def test_new_install_creates_current_metadata_structures(tmp_path: Path) -> None:
    database = Database(tmp_path / "new.db")
    migrate(database)
    with database.connect() as connection:
        version = int(connection.execute("SELECT version FROM schema_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(tracks)")}
    assert version == LATEST_SCHEMA_VERSION == 41
    assert {
        "metadata_terms",
        "track_metadata_terms",
        "track_metadata_field_state",
        "metadata_analysis_runs",
        "track_metadata_suggestions",
    } <= tables
    assert {"recording_type", "bpm", "rating", "metadata_revision"} <= columns
    migrate(database)


def test_schema_34_migration_preserves_existing_track_and_unrelated_data(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.db")
    with database.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (34);
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY, file_path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL, artist TEXT NOT NULL DEFAULT '',
                album TEXT NOT NULL DEFAULT '', duration_seconds REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                genre TEXT NOT NULL DEFAULT '', year INTEGER,
                original_release_year INTEGER, catalog_visible INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO tracks
                (id, file_path, title, artist, album, genre, year, original_release_year)
            VALUES (7, 'legacy.mp3', 'Legacy', 'Artist', 'Album', 'Soul', 2004, 1968);
            CREATE TABLE track_cue_points (
                id INTEGER PRIMARY KEY, track_id INTEGER, manual_cue_in REAL
            );
            INSERT INTO track_cue_points VALUES (1, 7, 1.5);
            CREATE TABLE party_queue (id INTEGER PRIMARY KEY, track_id INTEGER, status TEXT);
            INSERT INTO party_queue VALUES (2, 7, 'waiting');
            CREATE TABLE play_history (id INTEGER PRIMARY KEY, track_id INTEGER, deck_id TEXT);
            INSERT INTO play_history VALUES (3, 7, 'A');
            """
        )

    migrate(database)

    with database.connect() as connection:
        track = connection.execute(
            """SELECT genre, year, original_release_year, recording_type,
                      is_remastered, metadata_revision FROM tracks WHERE id = 7"""
        ).fetchone()
        cue = connection.execute("SELECT manual_cue_in FROM track_cue_points").fetchone()[0]
        queue = connection.execute("SELECT status FROM party_queue").fetchone()[0]
        history = connection.execute("SELECT deck_id FROM play_history").fetchone()[0]
    assert tuple(track) == ("Soul", 2004, 1968, "UNKNOWN", 0, 0)
    assert (cue, queue, history) == (1.5, "waiting", "A")


def test_schema_35_upgrade_preserves_metadata_state_and_suggestions(tmp_path: Path) -> None:
    database = Database(tmp_path / "schema-35.db")
    migrate(database)
    track_id = add_track(database)
    run = AnalysisRunRepository(database).create(track_id, "tempo", "tempo-v1", "one.mp3", 100, 200)
    EffectiveMetadataRepository(database).save(
        track_id,
        MetadataFieldKey.BPM,
        120,
        state(track_id, MetadataFieldKey.BPM),
    )
    suggestion = MetadataSuggestionRepository(database).save(
        track_id,
        run.run_id,
        MetadataFieldKey.BPM,
        125,
        MetadataSource.AUDIO_ANALYSIS,
        0.9,
    )
    with database.connect() as connection:
        connection.execute("UPDATE schema_version SET version = 35")

    migrate(database)

    assert MetadataFieldStateRepository(database).get(track_id, MetadataFieldKey.BPM) is not None
    assert MetadataSuggestionRepository(database).get(suggestion.suggestion_id).value == 125.0
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO track_metadata_field_state
                   (track_id, field_key, source_type, review_status)
               VALUES (?, 'title', 'FILE_TAG', 'IMPORTED')""",
            (track_id,),
        )


def test_effective_values_state_empty_confirmation_and_revisions(
    temporary_database: Database,
) -> None:
    track_id = add_track(temporary_database)
    effective = EffectiveMetadataRepository(temporary_database)
    fields = MetadataFieldStateRepository(temporary_database)

    revision = effective.save(
        track_id,
        MetadataFieldKey.BPM,
        128,
        state(track_id, MetadataFieldKey.BPM),
        expected_revision=0,
    )
    assert revision == 1
    assert effective.get(track_id, MetadataFieldKey.BPM) == 128.0
    assert fields.get(track_id, MetadataFieldKey.BPM).review_status is (
        MetadataReviewStatus.CONFIRMED_WITH_VALUE
    )

    revision = effective.save_confirmed_empty(
        track_id, MetadataFieldKey.LANGUAGE, expected_revision=1
    )
    assert revision == 2
    assert effective.get(track_id, MetadataFieldKey.LANGUAGE) is None
    assert fields.get(track_id, MetadataFieldKey.LANGUAGE).review_status is (
        MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE
    )
    with pytest.raises(MetadataRevisionConflict):
        effective.save(
            track_id,
            MetadataFieldKey.RATING,
            5,
            state(track_id, MetadataFieldKey.RATING),
            expected_revision=1,
        )


def test_recording_classification_uses_typed_columns(temporary_database: Database) -> None:
    track_id = add_track(temporary_database)
    repository = EffectiveMetadataRepository(temporary_database)
    recording = RecordingClassification(RecordingKind.LIVE, frozenset({RecordingTrait.REMASTERED}))
    repository.save(
        track_id,
        MetadataFieldKey.RECORDING_CLASSIFICATION,
        recording,
        state(track_id, MetadataFieldKey.RECORDING_CLASSIFICATION),
    )
    assert repository.get(track_id, MetadataFieldKey.RECORDING_CLASSIFICATION) == recording
    with pytest.raises(ValueError):
        repository.save(
            track_id,
            MetadataFieldKey.RATING,
            6,
            state(track_id, MetadataFieldKey.RATING),
        )


def test_multivalues_are_normalized_deduplicated_and_isolated(
    temporary_database: Database,
) -> None:
    first = add_track(temporary_database, "first")
    second = add_track(temporary_database, "second")
    repository = MultiValueMetadataRepository(temporary_database)
    repository.replace(
        first,
        MetadataFieldKey.MOODS,
        [" Gute  Laune ", "gute laune", "Ruhig"],
        state(first, MetadataFieldKey.MOODS),
    )
    repository.replace(
        second,
        MetadataFieldKey.MOODS,
        ["Gute Laune"],
        state(second, MetadataFieldKey.MOODS),
    )
    repository.remove(
        first,
        MetadataFieldKey.MOODS,
        ["GUTE LAUNE"],
        state(first, MetadataFieldKey.MOODS),
        expected_revision=1,
    )
    assert repository.get(first, MetadataFieldKey.MOODS) == ("Ruhig",)
    assert repository.get(second, MetadataFieldKey.MOODS) == ("Gute Laune",)
    repository.replace(
        first,
        MetadataFieldKey.MUSICAL_DECADES,
        [1990, 1980, 1990],
        state(first, MetadataFieldKey.MUSICAL_DECADES),
        expected_revision=2,
    )
    assert repository.get(first, MetadataFieldKey.MUSICAL_DECADES) == (1980, 1990)
    with pytest.raises(ValueError):
        repository.add(
            first,
            MetadataFieldKey.TAGS,
            [""],
            state(first, MetadataFieldKey.TAGS),
        )


def test_analysis_run_and_suggestion_lifecycle(temporary_database: Database) -> None:
    track_id = add_track(temporary_database)
    runs = AnalysisRunRepository(temporary_database)
    suggestions = MetadataSuggestionRepository(temporary_database)
    run = runs.create(track_id, "tempo", "tempo-v1", "one.mp3", 1234, 5678, priority=10)
    assert runs.start(run.run_id).attempt_count == 1
    assert runs.finish(run.run_id, AnalysisRunStatus.COMPLETED).status is (
        AnalysisRunStatus.COMPLETED
    )
    rejected = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.BPM,
        125,
        MetadataSource.AUDIO_ANALYSIS,
        0.95,
    )
    suggestions.decide(rejected.suggestion_id, SuggestionStatus.REJECTED, "falsch")
    assert suggestions.get(rejected.suggestion_id).status is SuggestionStatus.REJECTED
    superseded = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.BPM,
        126,
        MetadataSource.AUDIO_ANALYSIS,
        0.95,
    )
    suggestions.decide(superseded.suggestion_id, SuggestionStatus.SUPERSEDED)
    assert suggestions.get(superseded.suggestion_id).status is SuggestionStatus.SUPERSEDED


def test_accept_suggestion_is_atomic_and_supersedes_competitors(
    temporary_database: Database,
) -> None:
    track_id = add_track(temporary_database)
    runs = AnalysisRunRepository(temporary_database)
    suggestions = MetadataSuggestionRepository(temporary_database)
    service = MetadataPersistenceService(temporary_database)
    run = runs.create(track_id, "tempo", "tempo-v1", "one.mp3", 100, 200)
    accepted = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.BPM,
        128,
        MetadataSource.AUDIO_ANALYSIS,
        0.95,
    )
    competing = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.BPM,
        64,
        MetadataSource.AUDIO_ANALYSIS,
        0.95,
    )

    assert service.accept_suggestion(accepted.suggestion_id, expected_revision=0) == 1
    assert service.effective.get(track_id, MetadataFieldKey.BPM) == 128.0
    assert suggestions.get(accepted.suggestion_id).status is SuggestionStatus.ACCEPTED
    assert suggestions.get(competing.suggestion_id).status is SuggestionStatus.SUPERSEDED


def test_protected_value_blocks_later_suggestion(temporary_database: Database) -> None:
    track_id = add_track(temporary_database)
    service = MetadataPersistenceService(temporary_database)
    service.effective.save(
        track_id,
        MetadataFieldKey.BPM,
        120,
        state(track_id, MetadataFieldKey.BPM),
    )
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "tempo", "tempo-v2", "one.mp3", 100, 200
    )
    proposal = service.suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.BPM,
        128,
        MetadataSource.AUDIO_ANALYSIS,
        0.99,
    )
    with pytest.raises(MetadataRevisionConflict):
        service.accept_suggestion(proposal.suggestion_id, expected_revision=1)
    assert service.effective.get(track_id, MetadataFieldKey.BPM) == 120.0
    assert service.suggestions.get(proposal.suggestion_id).status is SuggestionStatus.PENDING


def test_existing_value_without_field_state_is_not_silently_replaced(
    temporary_database: Database,
) -> None:
    track_id = add_track(temporary_database)
    service = MetadataPersistenceService(temporary_database)
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "genre", "genre-v1", "one.mp3", 100, 200
    )
    proposal = service.suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Soul",
        MetadataSource.AUDIO_ANALYSIS,
        0.99,
    )
    with pytest.raises(ValueError, match="widerspricht"):
        service.accept_suggestion(proposal.suggestion_id)
    assert service.effective.get(track_id, MetadataFieldKey.MAIN_GENRE) == "Rock"
    assert service.suggestions.get(proposal.suggestion_id).status is SuggestionStatus.PENDING


def test_invalid_serialized_suggestion_rolls_back(temporary_database: Database) -> None:
    track_id = add_track(temporary_database)
    runs = AnalysisRunRepository(temporary_database)
    service = MetadataPersistenceService(temporary_database)
    run = runs.create(track_id, "tempo", "tempo-v1", "one.mp3", 100, 200)
    proposal = service.suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.BPM,
        128,
        MetadataSource.AUDIO_ANALYSIS,
        0.95,
    )
    with temporary_database.connect() as connection:
        connection.execute(
            "UPDATE track_metadata_suggestions SET serialized_value = '999' WHERE id = ?",
            (proposal.suggestion_id,),
        )
    with pytest.raises(ValueError):
        service.accept_suggestion(proposal.suggestion_id)
    assert service.effective.revision(track_id) == 0
    with temporary_database.connect() as connection:
        status = connection.execute(
            "SELECT status FROM track_metadata_suggestions WHERE id = ?", (proposal.suggestion_id,)
        ).fetchone()[0]
    assert status == "PENDING"


def test_track_delete_cascades_metadata_without_touching_shared_terms(
    temporary_database: Database,
) -> None:
    first = add_track(temporary_database, "first")
    second = add_track(temporary_database, "second")
    multivalues = MultiValueMetadataRepository(temporary_database)
    multivalues.replace(
        first, MetadataFieldKey.TAGS, ["Party"], state(first, MetadataFieldKey.TAGS)
    )
    multivalues.replace(
        second, MetadataFieldKey.TAGS, ["Party"], state(second, MetadataFieldKey.TAGS)
    )
    run = AnalysisRunRepository(temporary_database).create(first, "tempo", "v1", "first.mp3", 1, 1)
    MetadataSuggestionRepository(temporary_database).save(
        first,
        run.run_id,
        MetadataFieldKey.BPM,
        120,
        MetadataSource.AUDIO_ANALYSIS,
        0.9,
    )
    with temporary_database.connect() as connection:
        connection.execute("DELETE FROM tracks WHERE id = ?", (first,))
        assignments = connection.execute(
            "SELECT COUNT(*) FROM track_metadata_terms WHERE track_id = ?", (first,)
        ).fetchone()[0]
        states = connection.execute(
            "SELECT COUNT(*) FROM track_metadata_field_state WHERE track_id = ?", (first,)
        ).fetchone()[0]
        runs = connection.execute(
            "SELECT COUNT(*) FROM metadata_analysis_runs WHERE track_id = ?", (first,)
        ).fetchone()[0]
    assert (assignments, states, runs) == (0, 0, 0)
    assert multivalues.get(second, MetadataFieldKey.TAGS) == ("Party",)


def test_database_constraints_reject_invalid_direct_values(temporary_database: Database) -> None:
    track_id = add_track(temporary_database)
    with pytest.raises(sqlite3.IntegrityError):
        with temporary_database.connect() as connection:
            connection.execute("UPDATE tracks SET rating = 9 WHERE id = ?", (track_id,))
