"""Focused tests for atomic manual metadata editing."""

import sqlite3

import pytest

from party_player.database.connection import Database
from party_player.metadata_editor import (
    MetadataEditorService,
    StagedSuggestionAction,
    SuggestionEditorAction,
    TrackMetadataChanges,
    ValueRemovalMode,
)
from party_player.metadata_persistence import (
    AnalysisRunRepository,
    MetadataRevisionConflict,
    MetadataSuggestionRepository,
    SuggestionStatus,
)
from party_player.metadata_rules import (
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
)


def _add_track(database: Database) -> int:
    with database.connect() as connection:
        cursor = connection.execute(
            """INSERT INTO tracks
                   (file_path, title, artist, album, genre, year, original_release_year)
               VALUES ('editor.mp3', 'Titel', 'Interpret', 'Album', 'Rock', 2001, 1998)"""
        )
        return int(cursor.lastrowid)


def test_load_exposes_all_fields_sources_and_derived_decade(
    temporary_database: Database,
) -> None:
    track_id = _add_track(temporary_database)

    model = MetadataEditorService(temporary_database).load(track_id)

    assert {field.key for field in model.fields} == set(MetadataFieldKey)
    assert model.field(MetadataFieldKey.TITLE).value == "Titel"
    assert model.field(MetadataFieldKey.TITLE).status_text == "Fehlt / ungeprüft"
    assert model.release_decade == 1990
    assert model.suggestions == ()


def test_one_atomic_save_updates_scalar_multivalue_state_and_revision_once(
    temporary_database: Database,
) -> None:
    track_id = _add_track(temporary_database)
    service = MetadataEditorService(temporary_database)

    result = service.save(
        track_id,
        TrackMetadataChanges(
            0,
            scalar_values={MetadataFieldKey.BPM: 126.5},
            multivalue_values={MetadataFieldKey.TAGS: ("Party", "Favorit")},
            confirmations=frozenset({MetadataFieldKey.TITLE}),
        ),
    )

    assert result.revision_changed
    assert result.view_model.revision == 1
    assert result.view_model.field(MetadataFieldKey.BPM).value == 126.5
    assert result.view_model.field(MetadataFieldKey.TAGS).value == ("Favorit", "Party")
    assert result.view_model.field(MetadataFieldKey.TITLE).review_status is (
        MetadataReviewStatus.CONFIRMED_WITH_VALUE
    )
    with temporary_database.connect() as connection:
        revision = connection.execute(
            "SELECT metadata_revision FROM tracks WHERE id = ?", (track_id,)
        ).fetchone()[0]
    assert revision == 1

    unchanged = service.save(
        track_id,
        TrackMetadataChanges(1, confirmations=frozenset({MetadataFieldKey.TITLE})),
    )
    assert not unchanged.revision_changed
    assert unchanged.view_model.revision == 1


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        (ValueRemovalMode.MISSING, MetadataReviewStatus.MISSING),
        (
            ValueRemovalMode.CONFIRMED_EMPTY,
            MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE,
        ),
    ],
)
def test_removal_distinguishes_missing_from_confirmed_empty(
    temporary_database: Database,
    mode: ValueRemovalMode,
    expected_status: MetadataReviewStatus,
) -> None:
    track_id = _add_track(temporary_database)
    service = MetadataEditorService(temporary_database)

    result = service.save(
        track_id,
        TrackMetadataChanges(0, removals={MetadataFieldKey.MAIN_GENRE: mode}),
    )

    field = result.view_model.field(MetadataFieldKey.MAIN_GENRE)
    assert field.value == ""
    assert field.review_status is expected_status


def test_stale_revision_rolls_back_complete_change_set(
    temporary_database: Database,
) -> None:
    track_id = _add_track(temporary_database)
    service = MetadataEditorService(temporary_database)
    service.save(
        track_id,
        TrackMetadataChanges(0, scalar_values={MetadataFieldKey.RATING: 4}),
    )

    with pytest.raises(MetadataRevisionConflict):
        service.save(
            track_id,
            TrackMetadataChanges(
                0,
                scalar_values={MetadataFieldKey.RATING: 2},
                multivalue_values={MetadataFieldKey.TAGS: ("Nicht speichern",)},
            ),
        )

    current = service.load(track_id)
    assert current.field(MetadataFieldKey.RATING).value == 4
    assert current.field(MetadataFieldKey.TAGS).value == ()
    assert current.revision == 1


def test_empty_change_set_does_not_increment_revision(
    temporary_database: Database,
) -> None:
    track_id = _add_track(temporary_database)

    result = MetadataEditorService(temporary_database).save(track_id, TrackMetadataChanges(0))

    assert not result.revision_changed
    assert result.changed_fields == frozenset()
    assert result.view_model.revision == 0


def test_database_failure_rolls_back_all_metadata_changes(
    temporary_database: Database,
) -> None:
    track_id = _add_track(temporary_database)
    with temporary_database.connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_rating BEFORE UPDATE OF rating ON tracks
               BEGIN SELECT RAISE(ABORT, 'rating rejected'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="rating rejected"):
        MetadataEditorService(temporary_database).save(
            track_id,
            TrackMetadataChanges(
                0,
                scalar_values={
                    MetadataFieldKey.BPM: 120,
                    MetadataFieldKey.RATING: 5,
                },
            ),
        )

    current = MetadataEditorService(temporary_database).load(track_id)
    assert current.field(MetadataFieldKey.BPM).value is None
    assert current.field(MetadataFieldKey.RATING).value is None
    assert current.revision == 0


def test_suggestion_accept_requires_override_for_protected_value_and_supersedes(
    temporary_database: Database,
) -> None:
    track_id = _add_track(temporary_database)
    service = MetadataEditorService(temporary_database)
    service.save(
        track_id,
        TrackMetadataChanges(0, confirmations=frozenset({MetadataFieldKey.MAIN_GENRE})),
    )
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "genre", "genre-v1", "editor.mp3", 1, 1
    )
    suggestions = MetadataSuggestionRepository(temporary_database)
    accepted = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Soul",
        MetadataSource.EXTERNAL_MUSIC_DATABASE,
        0.95,
    )
    competing = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Funk",
        MetadataSource.EXTERNAL_MUSIC_DATABASE,
        0.9,
    )
    action = StagedSuggestionAction(accepted.suggestion_id, SuggestionEditorAction.ACCEPT)

    with pytest.raises(MetadataRevisionConflict):
        service.save(track_id, TrackMetadataChanges(1, suggestion_actions=(action,)))
    result = service.save(
        track_id,
        TrackMetadataChanges(
            1,
            suggestion_actions=(
                StagedSuggestionAction(
                    accepted.suggestion_id,
                    SuggestionEditorAction.ACCEPT_AND_CONFIRM,
                    allow_protected_override=True,
                ),
            ),
        ),
    )

    assert result.view_model.field(MetadataFieldKey.MAIN_GENRE).value == "Soul"
    assert result.view_model.revision == 2
    assert suggestions.get(accepted.suggestion_id).status is SuggestionStatus.ACCEPTED
    assert suggestions.get(competing.suggestion_id).status is SuggestionStatus.SUPERSEDED


def test_reject_suggestion_keeps_effective_value_and_revision(
    temporary_database: Database,
) -> None:
    track_id = _add_track(temporary_database)
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "genre", "genre-v1", "editor.mp3", 1, 1
    )
    suggestions = MetadataSuggestionRepository(temporary_database)
    suggestion = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Soul",
        MetadataSource.EXTERNAL_MUSIC_DATABASE,
        0.95,
    )

    result = MetadataEditorService(temporary_database).save(
        track_id,
        TrackMetadataChanges(
            0,
            suggestion_actions=(
                StagedSuggestionAction(suggestion.suggestion_id, SuggestionEditorAction.REJECT),
            ),
        ),
    )

    assert result.view_model.field(MetadataFieldKey.MAIN_GENRE).value == "Rock"
    assert result.view_model.revision == 0
    assert suggestions.get(suggestion.suggestion_id).status is SuggestionStatus.REJECTED


def test_bpm_confidence_cannot_be_manually_written(temporary_database: Database) -> None:
    track_id = _add_track(temporary_database)

    with pytest.raises(ValueError, match="schreibgeschützter"):
        MetadataEditorService(temporary_database).save(
            track_id,
            TrackMetadataChanges(0, scalar_values={MetadataFieldKey.BPM_CONFIDENCE: 0.75}),
        )
