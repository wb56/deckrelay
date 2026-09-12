"""Focused tests for immutable automatic-selection metadata snapshots."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
from typing import Iterator

import pytest

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.metadata_rules import MetadataReviewStatus, MetadataSource
from party_player.repositories.track_repository import TrackRepository
from party_player.selection_metadata import (
    SelectionMetadataDisposition,
    selection_metadata_terms,
    selection_metadata_value,
)


def _database(path: Path) -> Database:
    database = Database(path)
    migrate(database)
    return database


def _state(
    connection: sqlite3.Connection,
    track_id: int,
    field_key: str,
    status: str,
    *,
    source: str = "AUDIO_ANALYSIS",
    confidence: float | None = None,
) -> None:
    connection.execute(
        """INSERT INTO track_metadata_field_state
           (track_id,field_key,source_type,source_detail,confidence,review_status)
           VALUES (?,?,?,?,?,?)""",
        (track_id, field_key, source, r"C:\private\do-not-project", confidence, status),
    )


def test_scalar_usability_policy_is_conservative_and_deterministic() -> None:
    confirmed = selection_metadata_value(
        120.0,
        source=MetadataSource.MANUAL_CONFIRMATION,
        confidence=None,
        review_status=MetadataReviewStatus.CONFIRMED_WITH_VALUE,
        minimum_analysis_confidence=0.9,
    )
    imported = selection_metadata_value(
        "House",
        source=MetadataSource.FILE_TAG,
        confidence=None,
        review_status=MetadataReviewStatus.IMPORTED,
    )
    legacy = selection_metadata_value("Rock", source=None, confidence=None, review_status=None)
    confirmed_empty = selection_metadata_value(
        None,
        source=MetadataSource.MANUAL_CONFIRMATION,
        confidence=None,
        review_status=MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE,
    )

    assert confirmed.usable and imported.usable
    assert legacy.disposition is SelectionMetadataDisposition.UNCONFIRMED_LEGACY_VALUE
    assert confirmed_empty.disposition is SelectionMetadataDisposition.CONFIRMED_WITHOUT_VALUE
    assert "private" not in repr((confirmed, imported, legacy, confirmed_empty)).casefold()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (MetadataReviewStatus.MISSING, SelectionMetadataDisposition.MISSING),
        (MetadataReviewStatus.SUGGESTED, SelectionMetadataDisposition.REVIEW_REQUIRED),
        (MetadataReviewStatus.REVIEW_REQUIRED, SelectionMetadataDisposition.REVIEW_REQUIRED),
        (MetadataReviewStatus.CONFLICTING, SelectionMetadataDisposition.CONFLICTING),
        (MetadataReviewStatus.FAILED, SelectionMetadataDisposition.FAILED),
        (MetadataReviewStatus.OUTDATED, SelectionMetadataDisposition.OUTDATED),
    ],
)
def test_non_usable_review_states_have_machine_readable_reasons(
    status: MetadataReviewStatus, expected: SelectionMetadataDisposition
) -> None:
    value = selection_metadata_value(
        50,
        source=MetadataSource.AUDIO_ANALYSIS,
        confidence=1.0,
        review_status=status,
    )

    assert not value.usable
    assert value.disposition is expected


@pytest.mark.parametrize(
    ("confidence", "minimum", "expected"),
    [
        (0.899999, 0.9, SelectionMetadataDisposition.CONFIDENCE_TOO_LOW),
        (0.9, 0.9, SelectionMetadataDisposition.USABLE),
        (0.799999, 0.8, SelectionMetadataDisposition.CONFIDENCE_TOO_LOW),
        (0.8, 0.8, SelectionMetadataDisposition.USABLE),
    ],
)
def test_analysis_confidence_thresholds_are_exact(
    confidence: float, minimum: float, expected: SelectionMetadataDisposition
) -> None:
    value = selection_metadata_value(
        100.0,
        source=MetadataSource.AUDIO_ANALYSIS,
        confidence=confidence,
        review_status=MetadataReviewStatus.ANALYSED,
        minimum_analysis_confidence=minimum,
    )

    assert value.disposition is expected


def test_terms_are_normalized_deduplicated_and_immutable() -> None:
    terms = selection_metadata_terms(
        (" Deep   House ", "deep house", " Ruhig "),
        source=MetadataSource.MANUAL_CONFIRMATION,
        confidence=None,
        review_status=MetadataReviewStatus.CONFIRMED_WITH_VALUE,
    )

    assert terms.values == ("Deep House", "Ruhig")
    with pytest.raises(FrozenInstanceError):
        terms.values = ()  # type: ignore[misc]


def test_repository_projects_effective_values_quality_and_safe_origin(tmp_path: Path) -> None:
    database = _database(tmp_path / "metadata.db")
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks
               (id,file_path,title,artist,genre,bpm,bpm_confidence,alternative_bpm,energy)
               VALUES (1,'private.mp3','One','Artist','House',128,0.9,64,80)"""
        )
        _state(connection, 1, "main_genre", "IMPORTED", source="FILE_TAG")
        _state(connection, 1, "additional_genres", "CONFIRMED_WITH_VALUE")
        _state(connection, 1, "bpm", "ANALYSED", confidence=0.95)
        _state(connection, 1, "alternative_bpm", "REVIEW_REQUIRED", confidence=0.95)
        _state(connection, 1, "energy", "ANALYSED", confidence=0.8)
        _state(connection, 1, "moods", "CONFIRMED_WITH_VALUE")
        connection.executemany(
            """INSERT INTO metadata_terms(term_type,normalized_key,display_name)
               VALUES (?,?,?)""",
            [
                ("ADDITIONAL_GENRE", "deep house", "Deep House"),
                ("MOOD", "relaxed", "Relaxed"),
            ],
        )
        connection.execute(
            """INSERT INTO track_metadata_terms(track_id,term_id)
               SELECT 1,id FROM metadata_terms"""
        )

    tracks, catalog = TrackRepository(database).automatic_selection_snapshot()
    metadata = catalog.metadata[0]

    assert len(tracks) == 1
    assert metadata.main_genre.usable
    assert metadata.additional_genres.values == ("Deep House",)
    assert metadata.bpm.usable and metadata.bpm.confidence == 0.9
    assert metadata.alternative_bpm.disposition is SelectionMetadataDisposition.REVIEW_REQUIRED
    assert metadata.energy.usable
    assert metadata.moods.values == ("Relaxed",)
    assert "private" not in repr(metadata).casefold()
    with pytest.raises(FrozenInstanceError):
        metadata.track_id = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        catalog.metadata = ()  # type: ignore[misc]


def test_repository_ignores_open_suggestions_and_legacy_values(tmp_path: Path) -> None:
    database = _database(tmp_path / "legacy.db")
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks(id,file_path,title,artist,genre,bpm,energy)
               VALUES (1,'one.mp3','One','Artist','Legacy',123,70)"""
        )
        run = connection.execute(
            """INSERT INTO metadata_analysis_runs
               (track_id,analysis_profile,analysis_version,file_path_snapshot,
                file_size,file_modified_ns)
               VALUES (1,'tempo','v1','one.mp3',1,1)"""
        )
        connection.execute(
            """INSERT INTO track_metadata_suggestions
               (track_id,analysis_run_id,field_key,serialized_value,source_type,
                confidence,review_status,status)
               VALUES (1,?,'energy','90','AUDIO_ANALYSIS',1,'SUGGESTED','PENDING')""",
            (run.lastrowid,),
        )

    _tracks, catalog = TrackRepository(database).automatic_selection_snapshot()
    metadata = catalog.metadata[0]

    assert metadata.main_genre.disposition is SelectionMetadataDisposition.UNCONFIRMED_LEGACY_VALUE
    assert metadata.bpm.disposition is SelectionMetadataDisposition.UNCONFIRMED_LEGACY_VALUE
    assert metadata.energy.disposition is SelectionMetadataDisposition.UNCONFIRMED_LEGACY_VALUE
    assert metadata.energy.value == 70


def test_snapshot_uses_two_queries_for_candidates_and_one_when_empty(tmp_path: Path) -> None:
    database = _database(tmp_path / "queries.db")
    statements: list[str] = []
    original_connect = database.connect

    @contextmanager
    def traced_connect() -> Iterator[sqlite3.Connection]:
        with original_connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    database.connect = traced_connect  # type: ignore[method-assign]
    tracks, _catalog = TrackRepository(database).automatic_selection_snapshot()
    assert tracks == ()
    assert len([item for item in statements if item.lstrip().upper().startswith("WITH")]) == 1

    database.connect = original_connect  # type: ignore[method-assign]
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks(id,file_path,title,artist) VALUES (1,'one.mp3','One','A')"
        )
    statements.clear()
    database.connect = traced_connect  # type: ignore[method-assign]

    tracks, _catalog = TrackRepository(database).automatic_selection_snapshot()
    assert len(tracks) == 1
    assert len([item for item in statements if item.lstrip().upper().startswith("WITH")]) == 2


def test_snapshot_includes_hidden_predecessor_metadata_without_making_it_candidate(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "predecessor.db")
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO tracks(id,file_path,title,artist,catalog_visible)
               VALUES (?,?,?,?,?)""",
            [(1, "one.mp3", "One", "A", 1), (2, "two.mp3", "Two", "B", 0)],
        )

    tracks, catalog = TrackRepository(database).automatic_selection_snapshot(previous_track_id=2)

    assert [track.id for track in tracks] == [1]
    assert [item.track_id for item in catalog.metadata] == [1, 2]
    assert catalog.for_track(2) is not None


def test_track_removed_between_snapshot_queries_gets_neutral_metadata(tmp_path: Path) -> None:
    database = _database(tmp_path / "removed.db")
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks(id,file_path,title,artist,genre)
               VALUES (1,'one.mp3','One','Artist','Legacy')"""
        )
    original_connect = database.connect

    class RemovingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection
            self.selection_queries = 0

        def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            if statement.lstrip().upper().startswith("WITH"):
                self.selection_queries += 1
                if self.selection_queries == 2:
                    with original_connect() as remover:
                        remover.execute("DELETE FROM tracks WHERE id=1")
            return self.connection.execute(statement, parameters)

    @contextmanager
    def removing_connect() -> Iterator[RemovingConnection]:
        with original_connect() as connection:
            yield RemovingConnection(connection)

    database.connect = removing_connect  # type: ignore[method-assign]

    tracks, catalog = TrackRepository(database).automatic_selection_snapshot()

    assert len(tracks) == 1
    assert catalog.metadata[0].main_genre.disposition is SelectionMetadataDisposition.MISSING
