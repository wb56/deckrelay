"""Focused package-5 catalog-maintenance tests."""

import pytest
import party_player.catalog_maintenance as maintenance

from party_player.catalog_maintenance import (
    BatchAction,
    CatalogMaintenanceRepository,
    CatalogMaintenanceService,
    MaintenanceFilter,
    MetadataBatchRequest,
    SelectionDescription,
    WorkQueue,
    format_metadata_value,
    shorten_display_value,
)
from party_player.metadata_editor import MetadataEditorService, TrackMetadataChanges
from party_player.metadata_persistence import (
    AnalysisRunRepository,
    MetadataSuggestionRepository,
)
from party_player.metadata_rules import (
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
    RecordingClassification,
    RecordingKind,
    RecordingTrait,
)
from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION
from party_player.database.migrations import migrate


def _track(database: Database, title: str, **values: object) -> int:
    columns = ["file_path", "title"] + list(values)
    parameters = [f"{title}.mp3", title] + list(values.values())
    with database.connect() as connection:
        cursor = connection.execute(
            f"INSERT INTO tracks ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            parameters,
        )
        return int(cursor.lastrowid)


def test_schema_39_contains_bounded_batch_suggestion_and_analysis_history(
    temporary_database: Database,
) -> None:
    with temporary_database.connect() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == LATEST_SCHEMA_VERSION == 41
    assert {
        "metadata_batch_actions",
        "metadata_batch_changes",
        "metadata_batch_suggestion_changes",
    } <= tables


def test_schema_37_database_is_upgraded_without_reinterpreting_old_tables(
    temporary_database: Database,
) -> None:
    with temporary_database.connect() as connection:
        connection.execute("DROP TABLE metadata_batch_suggestion_changes")
        connection.execute("UPDATE schema_version SET version=37")

    migrate(temporary_database)

    with temporary_database.connect() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        table = connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name='metadata_batch_suggestion_changes'"""
        ).fetchone()
    assert version == 41
    assert table is not None


def test_work_queue_counts_are_computed_in_sql(temporary_database: Database) -> None:
    _track(temporary_database, "Unvollständig")
    _track(
        temporary_database,
        "Vollständig",
        original_release_year=1999,
        genre="Rock",
        bpm=120,
        energy=70,
        danceability=80,
        rating=4,
    )

    counts = {
        item.queue: item.count for item in CatalogMaintenanceRepository(temporary_database).counts()
    }

    assert counts[WorkQueue.MISSING_ORIGINAL_YEAR] == 1
    assert counts[WorkQueue.MISSING_BPM] == 1
    assert counts[WorkQueue.INCOMPLETE] == 1


def test_pages_and_text_filter_are_stable(temporary_database: Database) -> None:
    for number in range(55):
        _track(temporary_database, f"Titel {number:02d}", artist="Band")
    repository = CatalogMaintenanceRepository(temporary_database)
    filter_ = MaintenanceFilter(text="Titel")

    first = repository.page(filter_, 1)
    second = repository.page(filter_, 2)

    assert first.total == 55
    assert len(first.rows) == 50
    assert len(second.rows) == 5
    assert first.query_snapshot == second.query_snapshot
    assert {row.track_id for row in first.rows}.isdisjoint(row.track_id for row in second.rows)


def test_field_and_has_value_filters_are_applied_in_sql(
    temporary_database: Database,
) -> None:
    present = _track(temporary_database, "Rated", rating=5)
    _track(temporary_database, "Missing")

    page = CatalogMaintenanceRepository(temporary_database).page(
        MaintenanceFilter(field=MetadataFieldKey.RATING, has_value=True), 1
    )

    assert page.total == 1
    assert page.rows[0].track_id == present


def test_bpm_range_filter_is_inclusive(temporary_database: Database) -> None:
    below = _track(temporary_database, "Below", bpm=99.9)
    lower = _track(temporary_database, "Lower", bpm=100.0)
    upper = _track(temporary_database, "Upper", bpm=110.0)
    above = _track(temporary_database, "Above", bpm=110.1)

    page = CatalogMaintenanceRepository(temporary_database).page(
        MaintenanceFilter(minimum_bpm=100.0, maximum_bpm=110.0), 1
    )

    assert {row.track_id for row in page.rows} == {lower, upper}
    assert {below, above}.isdisjoint(row.track_id for row in page.rows)


def test_work_queue_row_displays_its_field_instead_of_unrelated_state(
    temporary_database: Database,
) -> None:
    track_id = _track(temporary_database, "Boogie Man", artist="AC/DC", genre="90s")
    with temporary_database.connect() as connection:
        connection.execute(
            """INSERT INTO track_metadata_field_state
               (track_id,field_key,source_type,source_detail,review_status)
               VALUES (?,'additional_genres','MANUAL_CONFIRMATION','editor',
                       'CONFIRMED_WITHOUT_VALUE')""",
            (track_id,),
        )

    page = CatalogMaintenanceRepository(temporary_database).page(
        MaintenanceFilter(work_queue=WorkQueue.MISSING_BPM), 1
    )

    row = next(item for item in page.rows if item.track_id == track_id)
    assert row.field == MetadataFieldKey.BPM.value
    assert row.current_value == "Fehlt / ungeprüft"
    assert row.review_status == "MISSING"
    assert row.source == ""


def test_explicit_field_filter_controls_value_source_and_status_display(
    temporary_database: Database,
) -> None:
    track_id = _track(temporary_database, "Boogie Man", artist="AC/DC", genre="Rock")
    with temporary_database.connect() as connection:
        connection.execute(
            """INSERT INTO track_metadata_field_state
               (track_id,field_key,source_type,source_detail,review_status)
               VALUES (?,'main_genre','MANUAL_INPUT','editor','CONFIRMED_WITH_VALUE')""",
            (track_id,),
        )
        connection.execute(
            """INSERT INTO track_metadata_field_state
               (track_id,field_key,source_type,source_detail,review_status)
               VALUES (?,'additional_genres','MANUAL_CONFIRMATION','editor',
                       'CONFIRMED_WITHOUT_VALUE')""",
            (track_id,),
        )

    page = CatalogMaintenanceRepository(temporary_database).page(
        MaintenanceFilter(field=MetadataFieldKey.MAIN_GENRE), 1
    )

    row = page.rows[0]
    assert row.field == MetadataFieldKey.MAIN_GENRE.value
    assert row.current_value == "Rock"
    assert row.review_status == "CONFIRMED_WITH_VALUE"
    assert row.source == "MANUAL_INPUT"


def test_selection_snapshot_depends_only_on_canonical_filter() -> None:
    first = SelectionDescription.for_filter(MaintenanceFilter(text="Soul"))
    same = SelectionDescription.for_filter(MaintenanceFilter(text="Soul"))
    other = SelectionDescription.for_filter(MaintenanceFilter(text="Funk"))

    assert first.query_snapshot == same.query_snapshot
    assert first.query_snapshot != other.query_snapshot


def test_all_matches_selection_keeps_only_explicit_exclusions() -> None:
    selection = SelectionDescription.for_filter(MaintenanceFilter(text="Soul"))

    selection = selection.select_all_matches().deselect(12).deselect(14).select(12)

    assert selection.all_matches
    assert selection.included_ids == frozenset()
    assert selection.excluded_ids == frozenset({14})


def test_repository_resolves_all_matches_without_track_materialization(
    temporary_database: Database,
) -> None:
    first = _track(temporary_database, "Soul One", artist="Band")
    excluded = _track(temporary_database, "Soul Two", artist="Band")
    _track(temporary_database, "Rock", artist="Band")
    repository = CatalogMaintenanceRepository(temporary_database)
    selection = (
        SelectionDescription.for_filter(MaintenanceFilter(text="Soul"))
        .select_all_matches()
        .deselect(excluded)
    )

    resolved = repository.resolve_selection(selection)

    assert resolved == ((first, 0),)


def test_repository_restricts_cross_page_selection_with_new_filter_in_sql(
    temporary_database: Database,
) -> None:
    soul = _track(temporary_database, "Soul", artist="Band")
    _track(temporary_database, "Rock", artist="Band")
    repository = CatalogMaintenanceRepository(temporary_database)
    selection = SelectionDescription.for_filter(MaintenanceFilter()).select_all_matches()

    restricted = repository.restrict_selection(selection, MaintenanceFilter(text="Soul"))

    assert restricted == ((soul, 0),)


def test_preview_execute_records_reversible_changes_and_rejects_reuse(
    temporary_database: Database,
) -> None:
    first = _track(temporary_database, "One")
    second = _track(temporary_database, "Two")
    selection = SelectionDescription.for_filter(MaintenanceFilter()).select(first).select(second)
    service = CatalogMaintenanceService(temporary_database)
    preview = service.preview(
        MetadataBatchRequest(
            selection,
            frozenset({MetadataFieldKey.RATING}),
            BatchAction.SET,
            ((MetadataFieldKey.RATING, 4),),
        )
    )

    result = service.execute(preview.request)

    assert result.changed == 2
    assert result.chunk_count == 1
    with temporary_database.connect() as connection:
        tracks = connection.execute(
            "SELECT rating, metadata_revision FROM tracks ORDER BY id"
        ).fetchall()
        changes = connection.execute(
            "SELECT COUNT(*) FROM metadata_batch_changes WHERE batch_id=?",
            (result.batch_id,),
        ).fetchone()[0]
    assert [tuple(row) for row in tracks] == [(4, 1), (4, 1)]
    assert changes == 2
    with pytest.raises(ValueError, match="veraltet"):
        service.execute(preview.request)

    undo_preview = service.preview_undo()
    assert undo_preview is not None
    undo = service.undo(undo_preview)
    assert undo.changed == 2
    with temporary_database.connect() as connection:
        restored = connection.execute(
            "SELECT rating, metadata_revision FROM tracks ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in restored] == [(None, 2), (None, 2)]
    assert service.preview_undo() is None


def test_cancel_stops_between_chunks_and_reports_partial_result(
    temporary_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _track(temporary_database, "First")
    second = _track(temporary_database, "Second")
    selection = SelectionDescription.for_filter(MaintenanceFilter()).select(first).select(second)
    service = CatalogMaintenanceService(temporary_database)
    preview = service.preview(
        MetadataBatchRequest(
            selection,
            frozenset({MetadataFieldKey.RATING}),
            BatchAction.SET,
            ((MetadataFieldKey.RATING, 5),),
        )
    )
    cancelled = False
    monkeypatch.setattr(maintenance, "BATCH_CHUNK_SIZE", 1)

    def progress(_done: int, _total: int) -> None:
        nonlocal cancelled
        cancelled = True

    result = service.execute(preview.request, cancel_requested=lambda: cancelled, progress=progress)

    assert result.status == "PARTIAL"
    assert result.changed == 1
    assert result.cancelled == 1
    assert result.chunk_count == 1


def test_revision_conflict_after_preview_is_reported_as_partial(
    temporary_database: Database,
) -> None:
    track_id = _track(temporary_database, "Concurrent")
    service = CatalogMaintenanceService(temporary_database)
    preview = service.preview(
        MetadataBatchRequest(
            SelectionDescription.for_filter(MaintenanceFilter()).select(track_id),
            frozenset({MetadataFieldKey.RATING}),
            BatchAction.SET,
            ((MetadataFieldKey.RATING, 5),),
        )
    )
    with temporary_database.connect() as connection:
        connection.execute(
            "UPDATE tracks SET metadata_revision=metadata_revision+1 WHERE id=?",
            (track_id,),
        )

    result = service.execute(preview.request)

    assert result.status == "PARTIAL"
    assert result.revision_conflicts == 1
    assert result.changed == 0


def test_preview_never_changes_catalog_data(temporary_database: Database) -> None:
    track_id = _track(temporary_database, "Preview only")
    service = CatalogMaintenanceService(temporary_database)

    service.preview(
        MetadataBatchRequest(
            SelectionDescription.for_filter(MaintenanceFilter()).select(track_id),
            frozenset({MetadataFieldKey.RATING}),
            BatchAction.SET,
            ((MetadataFieldKey.RATING, 5),),
        )
    )

    with temporary_database.connect() as connection:
        row = connection.execute(
            "SELECT rating,metadata_revision FROM tracks WHERE id=?", (track_id,)
        ).fetchone()
        batches = connection.execute("SELECT COUNT(*) FROM metadata_batch_actions").fetchone()[0]
    assert tuple(row) == (None, 0)
    assert batches == 0


def test_multivalue_batch_and_undo_preserve_other_metadata(
    temporary_database: Database,
) -> None:
    track_id = _track(temporary_database, "Terms", comment="Unverändert")
    editor = MetadataEditorService(temporary_database)
    with temporary_database.connect() as connection:
        cursor = connection.execute(
            """INSERT INTO metadata_terms(term_type,normalized_key,display_name)
               VALUES ('FREE_TAG','party','Party')"""
        )
        connection.execute(
            "INSERT INTO track_metadata_terms(track_id,term_id) VALUES (?,?)",
            (track_id, int(cursor.lastrowid)),
        )
    service = CatalogMaintenanceService(temporary_database)
    preview = service.preview(
        MetadataBatchRequest(
            SelectionDescription.for_filter(MaintenanceFilter()).select(track_id),
            frozenset({MetadataFieldKey.TAGS}),
            BatchAction.MULTI_ADD,
            ((MetadataFieldKey.TAGS, ("Sommer",)),),
        )
    )
    result = service.execute(preview.request)
    assert result.status == "COMPLETED"
    assert editor.load(track_id).field(MetadataFieldKey.TAGS).value == (
        "Party",
        "Sommer",
    )
    undo_preview = service.preview_undo()
    assert undo_preview is not None
    service.undo(undo_preview)
    model = editor.load(track_id)
    assert model.field(MetadataFieldKey.TAGS).value == ("Party",)
    assert model.field(MetadataFieldKey.COMMENT).value == "Unverändert"


def test_undo_skips_track_changed_after_batch(temporary_database: Database) -> None:
    track_id = _track(temporary_database, "Changed later")
    service = CatalogMaintenanceService(temporary_database)
    selection = SelectionDescription.for_filter(MaintenanceFilter()).select(track_id)
    preview = service.preview(
        MetadataBatchRequest(
            selection,
            frozenset({MetadataFieldKey.RATING}),
            BatchAction.SET,
            ((MetadataFieldKey.RATING, 4),),
        )
    )
    service.execute(preview.request)
    with temporary_database.connect() as connection:
        connection.execute(
            "UPDATE tracks SET rating=5, metadata_revision=metadata_revision+1 WHERE id=?",
            (track_id,),
        )

    undo_preview = service.preview_undo()

    assert undo_preview is not None
    assert undo_preview.changeable_tracks == 0
    assert undo_preview.conflict_tracks == 1
    result = service.undo(undo_preview)
    assert result.changed == 0
    assert result.revision_conflicts == 1


def test_batch_rejects_only_matching_pending_suggestions(
    temporary_database: Database,
) -> None:
    track_id = _track(temporary_database, "Suggested")
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "genre", "v1", "Suggested.mp3", 1, 1
    )
    suggestions = MetadataSuggestionRepository(temporary_database)
    suggestion = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Soul",
        MetadataSource.FILE_TAG,
        0.9,
    )
    service = CatalogMaintenanceService(temporary_database)
    selection = SelectionDescription.for_filter(MaintenanceFilter()).select(track_id)
    preview = service.preview(
        MetadataBatchRequest(
            selection,
            frozenset({MetadataFieldKey.MAIN_GENRE}),
            BatchAction.SUGGESTION_REJECT,
            minimum_confidence=0.8,
        )
    )

    result = service.execute(preview.request)

    assert result.changed == 1
    assert suggestions.get(suggestion.suggestion_id).status.value == "REJECTED"


@pytest.mark.parametrize(
    ("action", "expected_status", "expected_review"),
    [
        (BatchAction.SUGGESTION_ACCEPT, "ACCEPTED", "IMPORTED"),
        (
            BatchAction.SUGGESTION_ACCEPT_CONFIRM,
            "ACCEPTED",
            "CONFIRMED_WITH_VALUE",
        ),
        (BatchAction.SUGGESTION_REJECT, "REJECTED", None),
    ],
)
def test_suggestion_batch_records_and_safely_undoes_decision(
    temporary_database: Database,
    action: BatchAction,
    expected_status: str,
    expected_review: str | None,
) -> None:
    track_id = _track(temporary_database, "Suggested", genre="Rock")
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "genre", "v1", "Suggested.mp3", 1, 1
    )
    suggestions = MetadataSuggestionRepository(temporary_database)
    suggestion = suggestions.save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Soul",
        MetadataSource.FILE_TAG,
        0.9,
    )
    service = CatalogMaintenanceService(temporary_database)
    selection = SelectionDescription.for_filter(MaintenanceFilter()).select(track_id)
    preview = service.preview(
        MetadataBatchRequest(
            selection,
            frozenset({MetadataFieldKey.MAIN_GENRE}),
            action,
        )
    )

    result = service.execute(preview.request)

    assert result.changed == 1
    assert suggestions.get(suggestion.suggestion_id).status.value == expected_status
    with temporary_database.connect() as connection:
        history = connection.execute(
            "SELECT * FROM metadata_batch_suggestion_changes WHERE batch_id=?",
            (result.batch_id,),
        ).fetchone()
        track = connection.execute("SELECT genre FROM tracks WHERE id=?", (track_id,)).fetchone()
        state = connection.execute(
            """SELECT review_status FROM track_metadata_field_state
               WHERE track_id=? AND field_key='main_genre'""",
            (track_id,),
        ).fetchone()
    assert history is not None
    assert history["suggestion_id"] == suggestion.suggestion_id
    assert history["previous_status"] == "PENDING"
    assert history["new_status"] == expected_status
    assert track["genre"] == ("Rock" if action is BatchAction.SUGGESTION_REJECT else "Soul")
    if expected_review is not None:
        assert state["review_status"] == expected_review

    undo_preview = service.preview_undo()
    assert undo_preview is not None and undo_preview.conflict_tracks == 0
    undo = service.undo(undo_preview)
    assert undo.status == "COMPLETED"
    assert suggestions.get(suggestion.suggestion_id).status.value == "PENDING"
    with temporary_database.connect() as connection:
        restored = connection.execute("SELECT genre FROM tracks WHERE id=?", (track_id,)).fetchone()
    assert restored["genre"] == "Rock"
    assert service.preview_undo() is None


def test_accept_undo_restores_competing_superseded_suggestion(
    temporary_database: Database,
) -> None:
    track_id = _track(temporary_database, "Competing", genre="Rock")
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "genre", "v1", "Competing.mp3", 1, 1
    )
    repository = MetadataSuggestionRepository(temporary_database)
    chosen = repository.save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Soul",
        MetadataSource.FILE_TAG,
        0.95,
    )
    competitor = repository.save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Funk",
        MetadataSource.FILE_TAG,
        0.8,
    )
    service = CatalogMaintenanceService(temporary_database)
    preview = service.preview(
        MetadataBatchRequest(
            SelectionDescription.for_filter(MaintenanceFilter()).select(track_id),
            frozenset({MetadataFieldKey.MAIN_GENRE}),
            BatchAction.SUGGESTION_ACCEPT,
        )
    )

    result = service.execute(preview.request)

    assert repository.get(chosen.suggestion_id).status.value == "ACCEPTED"
    assert repository.get(competitor.suggestion_id).status.value == "SUPERSEDED"
    with temporary_database.connect() as connection:
        rows = connection.execute(
            """SELECT suggestion_id,superseded_by_acceptance
               FROM metadata_batch_suggestion_changes WHERE batch_id=? ORDER BY suggestion_id""",
            (result.batch_id,),
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (chosen.suggestion_id, 0),
        (competitor.suggestion_id, 1),
    ]
    undo_preview = service.preview_undo()
    assert undo_preview is not None
    service.undo(undo_preview)
    assert repository.get(chosen.suggestion_id).status.value == "PENDING"
    assert repository.get(competitor.suggestion_id).status.value == "PENDING"


def test_undo_skips_suggestion_changed_after_batch(
    temporary_database: Database,
) -> None:
    track_id = _track(temporary_database, "Later decision")
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "genre", "v1", "Later.mp3", 1, 1
    )
    suggestion = MetadataSuggestionRepository(temporary_database).save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Soul",
        MetadataSource.FILE_TAG,
        0.9,
    )
    service = CatalogMaintenanceService(temporary_database)
    preview = service.preview(
        MetadataBatchRequest(
            SelectionDescription.for_filter(MaintenanceFilter()).select(track_id),
            frozenset({MetadataFieldKey.MAIN_GENRE}),
            BatchAction.SUGGESTION_REJECT,
        )
    )
    service.execute(preview.request)
    with temporary_database.connect() as connection:
        connection.execute(
            """UPDATE track_metadata_suggestions SET decision_reason='Spätere Entscheidung'
               WHERE id=?""",
            (suggestion.suggestion_id,),
        )

    undo_preview = service.preview_undo()

    assert undo_preview is not None
    assert undo_preview.changeable_tracks == 0
    assert undo_preview.conflict_tracks == 1
    result = service.undo(undo_preview)
    assert result.status == "PARTIAL"
    assert result.revision_conflicts == 1
    assert (
        MetadataSuggestionRepository(temporary_database).get(suggestion.suggestion_id).status.value
        == "REJECTED"
    )


def test_defer_does_not_create_persistent_or_undoable_change(
    temporary_database: Database,
) -> None:
    track_id = _track(temporary_database, "Deferred")
    run = AnalysisRunRepository(temporary_database).create(
        track_id, "genre", "v1", "Deferred.mp3", 1, 1
    )
    suggestion = MetadataSuggestionRepository(temporary_database).save(
        track_id,
        run.run_id,
        MetadataFieldKey.MAIN_GENRE,
        "Soul",
        MetadataSource.FILE_TAG,
        0.9,
    )
    service = CatalogMaintenanceService(temporary_database)
    preview = service.preview(
        MetadataBatchRequest(
            SelectionDescription.for_filter(MaintenanceFilter()).select(track_id),
            frozenset({MetadataFieldKey.MAIN_GENRE}),
            BatchAction.SUGGESTION_DEFER,
        )
    )

    result = service.execute(preview.request)

    assert result.changed == 0
    assert (
        MetadataSuggestionRepository(temporary_database).get(suggestion.suggestion_id).status.value
        == "PENDING"
    )
    assert service.preview_undo() is None


def test_preview_reports_protected_invalid_and_unchanged_tracks(
    temporary_database: Database,
) -> None:
    protected = _track(temporary_database, "Protected", rating=4)
    unchanged = _track(temporary_database, "Unchanged", rating=4)
    MetadataEditorService(temporary_database).save(
        protected,
        TrackMetadataChanges(
            0,
            confirmations=frozenset({MetadataFieldKey.RATING}),
        ),
    )
    selection = (
        SelectionDescription.for_filter(MaintenanceFilter()).select(protected).select(unchanged)
    )
    service = CatalogMaintenanceService(temporary_database)

    preview = service.preview(
        MetadataBatchRequest(
            selection,
            frozenset({MetadataFieldKey.RATING}),
            BatchAction.SET,
            ((MetadataFieldKey.RATING, 4),),
        )
    )
    invalid = service.preview(
        MetadataBatchRequest(
            selection,
            frozenset({MetadataFieldKey.RATING}),
            BatchAction.SET,
            ((MetadataFieldKey.RATING, 9),),
        )
    )

    assert (preview.protected, preview.unchanged) == (1, 1)
    assert (invalid.protected, invalid.invalid) == (1, 1)


def test_value_formatter_covers_storage_types_without_internal_values() -> None:
    assert (
        format_metadata_value(
            MetadataFieldKey.COMMENT,
            None,
            MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE,
        )
        == "Bewusst ohne Wert bestätigt"
    )
    assert format_metadata_value(MetadataFieldKey.YEAR, None) == "Fehlt / ungeprüft"
    assert format_metadata_value(MetadataFieldKey.BPM, 123.4) == "123.4 BPM"
    assert format_metadata_value(MetadataFieldKey.BPM_CONFIDENCE, 0.87) == "87 %"
    assert format_metadata_value(MetadataFieldKey.ENERGY, 72) == "72 %"
    assert format_metadata_value(MetadataFieldKey.RATING, 4) == "★★★★☆ (4/5)"
    assert (
        format_metadata_value(
            MetadataFieldKey.RECORDING_CLASSIFICATION,
            RecordingClassification(RecordingKind.LIVE, frozenset({RecordingTrait.REMASTERED})),
        )
        == "Liveaufnahme · remastert"
    )
    assert format_metadata_value(MetadataFieldKey.MUSICAL_DECADES, (1970, 1980)) == "1970er, 1980er"
    assert format_metadata_value(MetadataFieldKey.TAGS, ("Soul", "Party")) == "Soul, Party"
    long_text = "Kommentar " * 40
    assert shorten_display_value(long_text).endswith("…")
    assert len(shorten_display_value(long_text)) <= 96
