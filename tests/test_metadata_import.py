"""Focused tests for safe, atomic file-tag catalog imports."""

import pytest
from _pytest.monkeypatch import MonkeyPatch

from party_player.database.connection import Database
from party_player.metadata_import import (
    FileImportSnapshot,
    ImportedFieldValue,
    ImportedTrackData,
    MetadataImportOperation,
    MetadataImportOutcome,
)
from party_player.metadata_persistence import (
    EffectiveMetadataRepository,
    MetadataFieldState,
    MetadataFieldStateRepository,
    MetadataSuggestionRepository,
    SuggestionStatus,
)
from party_player.metadata_rules import (
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
)
from party_player.repositories.track_repository import TrackRepository


def snapshot(
    path: str = "C:/Music/one.mp3", size: int = 100, modified: int = 200
) -> FileImportSnapshot:
    return FileImportSnapshot(path, path.casefold(), size, modified)


def imported(
    *,
    title: object = "Title",
    artist: object = "Artist",
    album: object = "Album",
    genre: object = "Rock",
    year: object = 2001,
    original_year: object = 1998,
    duration: float = 123.5,
) -> ImportedTrackData:
    return ImportedTrackData(
        ImportedFieldValue(title, source_detail="id3:title"),
        ImportedFieldValue(artist, source_detail="id3:artist"),
        ImportedFieldValue(album, source_detail="id3:album"),
        ImportedFieldValue(genre, source_detail="id3:genre"),
        ImportedFieldValue(year, source_detail="id3:year"),
        ImportedFieldValue(original_year, source_detail="id3:originaldate"),
        duration,
    )


def apply(
    database: Database,
    data: ImportedTrackData | None = None,
    file_snapshot: FileImportSnapshot | None = None,
):
    selected = file_snapshot or snapshot()
    return MetadataImportOperation(database).apply(
        selected,
        data or imported(),
        current_snapshot=selected,
    )


def test_new_file_persists_tags_sources_technical_data_and_one_revision(
    temporary_database: Database,
) -> None:
    result = apply(temporary_database)
    assert result.outcome is MetadataImportOutcome.NEW_TRACK
    assert result.revision == 1
    assert not result.partial_tags
    assert result.track is not None
    assert (
        result.track.file_path,
        result.track.title,
        result.track.artist,
        result.track.album,
        result.track.duration_seconds,
        result.track.genre,
        result.track.year,
        result.track.original_release_year,
    ) == ("C:/Music/one.mp3", "Title", "Artist", "Album", 123.5, "Rock", 2001, 1998)
    states = MetadataFieldStateRepository(temporary_database)
    for field in (
        MetadataFieldKey.TITLE,
        MetadataFieldKey.ARTIST,
        MetadataFieldKey.ALBUM,
        MetadataFieldKey.MAIN_GENRE,
        MetadataFieldKey.YEAR,
        MetadataFieldKey.ORIGINAL_RELEASE_YEAR,
    ):
        stored = states.get(result.track.id, field)
        assert stored is not None
        assert stored.source is MetadataSource.FILE_TAG
        assert stored.review_status is MetadataReviewStatus.IMPORTED


def test_new_file_with_partial_tags_does_not_confirm_missing_values(
    temporary_database: Database,
) -> None:
    data = imported(artist=None, album=" ", genre="", year=None, original_year=None)
    result = apply(temporary_database, data)
    assert result.outcome is MetadataImportOutcome.NEW_TRACK
    assert result.partial_tags
    assert result.track is not None
    states = MetadataFieldStateRepository(temporary_database)
    assert states.get(result.track.id, MetadataFieldKey.ARTIST) is None
    assert states.get(result.track.id, MetadataFieldKey.YEAR) is None


def test_identical_and_normalized_equal_tags_do_not_change_revision(
    temporary_database: Database,
) -> None:
    first = apply(temporary_database)
    second = apply(
        temporary_database,
        imported(title="  Title ", artist="Artist", album="Album", genre="Rock"),
    )
    assert second.outcome is MetadataImportOutcome.UNCHANGED
    assert second.revision == first.revision == 1
    assert not second.updated_fields
    assert not second.proposal_fields


def test_changed_imported_fields_update_atomically_with_one_revision(
    temporary_database: Database,
) -> None:
    first = apply(temporary_database)
    changed = apply(
        temporary_database,
        imported(title="New Title", artist="New Artist", genre="Soul"),
    )
    assert changed.outcome is MetadataImportOutcome.UPDATED
    assert changed.revision == 2
    assert set(changed.updated_fields) == {
        MetadataFieldKey.TITLE,
        MetadataFieldKey.ARTIST,
        MetadataFieldKey.MAIN_GENRE,
    }
    assert changed.track is not None
    assert (changed.track.title, changed.track.artist, changed.track.genre) == (
        "New Title",
        "New Artist",
        "Soul",
    )
    assert EffectiveMetadataRepository(temporary_database).revision(first.track.id) == 2  # type: ignore[union-attr]


def test_legacy_value_without_state_is_preserved_and_proposed(
    temporary_database: Database,
) -> None:
    legacy = TrackRepository(temporary_database).upsert_file(
        snapshot().resolved_path, "Possibly Manual", "Artist", "Album", 100, "Rock", 2001, 1998
    )
    result = apply(temporary_database, imported(title="Tag Title"))
    assert result.outcome is MetadataImportOutcome.PROPOSALS_CREATED
    assert result.track is not None and result.track.title == "Possibly Manual"
    assert result.revision == 0
    assert result.proposal_fields == (MetadataFieldKey.TITLE,)
    with temporary_database.connect() as connection:
        proposal = connection.execute(
            """SELECT suggestion.source_type, suggestion.source_detail, run.file_path_snapshot,
                      run.file_size, run.file_modified_ns
               FROM track_metadata_suggestions AS suggestion
               JOIN metadata_analysis_runs AS run ON run.id = suggestion.analysis_run_id
               WHERE suggestion.track_id = ? AND suggestion.field_key = 'title'""",
            (legacy.id,),
        ).fetchone()
    assert tuple(proposal) == ("FILE_TAG", "id3:title", "c:/music/one.mp3", 100, 200)


@pytest.mark.parametrize(
    ("review_status", "initial_value"),
    [
        (MetadataReviewStatus.CONFIRMED_WITH_VALUE, "Manual"),
        (MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE, None),
    ],
)
def test_confirmed_value_and_confirmed_absence_are_protected(
    temporary_database: Database,
    review_status: MetadataReviewStatus,
    initial_value: str | None,
) -> None:
    created = apply(temporary_database)
    assert created.track is not None
    effective = EffectiveMetadataRepository(temporary_database)
    field_state = MetadataFieldState(
        created.track.id,
        MetadataFieldKey.ALBUM,
        MetadataSource.MANUAL_CONFIRMATION,
        "editor",
        None,
        review_status,
    )
    effective.save(
        created.track.id,
        MetadataFieldKey.ALBUM,
        initial_value,
        field_state,
        expected_revision=1,
    )
    result = apply(temporary_database, imported(album="Tag Album"))
    assert result.outcome is MetadataImportOutcome.PROPOSALS_CREATED
    assert result.track is not None and result.track.album == (initial_value or "")
    stored_state = MetadataFieldStateRepository(temporary_database).get(
        created.track.id, MetadataFieldKey.ALBUM
    )
    assert stored_state is not None and stored_state.review_status is review_status


def test_missing_import_value_never_deletes_existing_value(temporary_database: Database) -> None:
    apply(temporary_database)
    result = apply(temporary_database, imported(genre=None, year=None))
    assert result.outcome is MetadataImportOutcome.UNCHANGED
    assert result.track is not None
    assert (result.track.genre, result.track.year, result.revision) == ("Rock", 2001, 1)


def test_identical_pending_and_rejected_proposals_are_not_recreated(
    temporary_database: Database,
) -> None:
    legacy = TrackRepository(temporary_database).upsert_file(
        snapshot().resolved_path, "Legacy", "Artist", "Album", 100, "Rock", 2001, 1998
    )
    first = apply(temporary_database, imported(title="Tag"))
    second = apply(temporary_database, imported(title="Tag"))
    assert first.proposal_fields == (MetadataFieldKey.TITLE,)
    assert second.outcome is MetadataImportOutcome.UNCHANGED
    with temporary_database.connect() as connection:
        suggestion_id = int(
            connection.execute(
                "SELECT id FROM track_metadata_suggestions WHERE track_id = ?", (legacy.id,)
            ).fetchone()[0]
        )
    MetadataSuggestionRepository(temporary_database).decide(
        suggestion_id, SuggestionStatus.REJECTED, "reviewed"
    )
    third = apply(temporary_database, imported(title="Tag"))
    assert third.outcome is MetadataImportOutcome.UNCHANGED
    with temporary_database.connect() as connection:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM track_metadata_suggestions WHERE track_id = ?", (legacy.id,)
            ).fetchone()[0]
        )
    assert count == 1


def test_changed_new_proposal_supersedes_previous_pending(temporary_database: Database) -> None:
    legacy = TrackRepository(temporary_database).upsert_file(
        snapshot().resolved_path, "Legacy", "Artist", "Album", 100, "Rock", 2001, 1998
    )
    apply(temporary_database, imported(title="First"))
    newer = snapshot(size=101, modified=201)
    second = apply(temporary_database, imported(title="Second"), newer)
    assert second.proposal_fields == (MetadataFieldKey.TITLE,)
    with temporary_database.connect() as connection:
        rows = connection.execute(
            """SELECT status, serialized_value FROM track_metadata_suggestions
               WHERE track_id = ? ORDER BY id""",
            (legacy.id,),
        ).fetchall()
    assert [(row["status"], row["serialized_value"]) for row in rows] == [
        ("SUPERSEDED", '"First"'),
        ("PENDING", '"Second"'),
    ]


def test_changed_file_snapshot_aborts_without_partial_write(temporary_database: Database) -> None:
    before = snapshot(size=100, modified=200)
    after = snapshot(size=101, modified=201)
    result = MetadataImportOperation(temporary_database).apply(
        before, imported(), current_snapshot=after
    )
    assert result.outcome is MetadataImportOutcome.FILE_CHANGED
    assert result.file_changed
    assert TrackRepository(temporary_database).count() == 0


def test_persistence_failure_rolls_back_complete_import(
    temporary_database: Database, monkeypatch: MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr("party_player.metadata_import._write_state", fail)
    with pytest.raises(RuntimeError, match="injected"):
        apply(temporary_database)
    assert TrackRepository(temporary_database).count() == 0


def test_unknown_new_path_is_not_merged_by_matching_tags(temporary_database: Database) -> None:
    apply(temporary_database, file_snapshot=snapshot("C:/Music/one.mp3"))
    apply(temporary_database, file_snapshot=snapshot("C:/Moved/one.mp3"))
    with temporary_database.connect() as connection:
        rows = connection.execute("SELECT file_path FROM tracks ORDER BY id").fetchall()
    assert [str(row["file_path"]) for row in rows] == [
        "C:/Music/one.mp3",
        "C:/Moved/one.mp3",
    ]
