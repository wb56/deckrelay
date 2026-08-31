"""Scope, signature, persistence and pure tempo-resolution tests."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from party_player.database.connection import Database
from party_player.cue_points import CuePointRepository, CuePointService
from party_player.metadata_analysis_contracts import FileSnapshot, TempoAnalysisScope
from party_player.tempo_context import (
    PartyQueueTempoSnapshot,
    SavedQueueManualTempo,
    TempoAnalysisValue,
    TempoContextRepository,
    TempoAnalysisContextResolver,
    TempoResolver,
    TempoValueSource,
    cue_milliseconds,
    resolved_now,
    tempo_range_signature,
)
from party_player.models import SavedQueueEntry, Track


def _file_snapshot(tmp_path: Path) -> FileSnapshot:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"unchanged audio")
    return FileSnapshot.capture(str(path))


def _value(
    bpm: float,
    source: TempoValueSource,
    scope: TempoAnalysisScope,
    *,
    current: bool = True,
) -> TempoAnalysisValue:
    return TempoAnalysisValue(bpm, bpm * 2, source, scope, 0.8, current, "a" * 64)


def test_cue_milliseconds_and_signature_are_stable(tmp_path: Path) -> None:
    snapshot = _file_snapshot(tmp_path)
    first = resolved_now(1.2344, 120.0004, 7.0, 180.0, "cue-rev-4")
    same_milliseconds = resolved_now(1.23449, 120.00049, 7.0004, 180.0, "cue-rev-4")

    assert cue_milliseconds(1.2345) == 1235
    assert tempo_range_signature(
        TempoAnalysisScope.TRACK_DEFAULT_CUES, 7, snapshot, first, "v1"
    ) == tempo_range_signature(
        TempoAnalysisScope.TRACK_DEFAULT_CUES, 7, snapshot, same_milliseconds, "v1"
    )
    assert tempo_range_signature(
        TempoAnalysisScope.TRACK_FULL, 7, snapshot, first, "v1"
    ) != tempo_range_signature(TempoAnalysisScope.TRACK_DEFAULT_CUES, 7, snapshot, first, "v1")


def test_same_file_with_different_playlist_cues_has_different_signature(tmp_path: Path) -> None:
    snapshot = _file_snapshot(tmp_path)
    first = resolved_now(10, 100, 5, 180, "entry-1", saved_queue_entry_id=11)
    second = resolved_now(20, 100, 5, 180, "entry-2", saved_queue_entry_id=12)
    assert tempo_range_signature(
        TempoAnalysisScope.SAVED_QUEUE_ENTRY, 1, snapshot, first, "v1"
    ) != tempo_range_signature(TempoAnalysisScope.SAVED_QUEUE_ENTRY, 1, snapshot, second, "v1")


@pytest.mark.parametrize(("cue_in", "cue_out"), ((20.0, 10.0), (100.0, 120.0), (-1.0, 20.0)))
def test_invalid_resolved_ranges_are_rejected(cue_in: float, cue_out: float) -> None:
    with pytest.raises(ValueError):
        resolved_now(cue_in, cue_out, 2.0, 100.0, "revision")


def test_catalog_resolver_separates_confirmed_proposal_and_planning() -> None:
    cue = _value(122.0, TempoValueSource.TRACK_DEFAULT_CUES, TempoAnalysisScope.TRACK_DEFAULT_CUES)
    full = _value(120.0, TempoValueSource.TRACK_FULL, TempoAnalysisScope.TRACK_FULL)

    resolution = TempoResolver.catalog(121.0, cue, full)

    assert resolution.confirmed.bpm == 121.0
    assert resolution.confirmed.confirmed
    assert resolution.best_analysis_proposal.bpm == 122.0
    assert resolution.planning.source is TempoValueSource.MANUAL_CATALOG


def test_stale_cue_result_is_ignored_and_full_title_is_fallback() -> None:
    stale = _value(
        122.0,
        TempoValueSource.TRACK_DEFAULT_CUES,
        TempoAnalysisScope.TRACK_DEFAULT_CUES,
        current=False,
    )
    full = _value(120.0, TempoValueSource.TRACK_FULL, TempoAnalysisScope.TRACK_FULL)
    resolution = TempoResolver.catalog(None, stale, full)
    assert resolution.planning.bpm == 120.0
    assert resolution.planning.scope is TempoAnalysisScope.TRACK_FULL


def test_unreliable_cue_does_not_displace_reliable_full_value() -> None:
    cue = TempoAnalysisValue(
        112.7,
        225.4,
        TempoValueSource.TRACK_DEFAULT_CUES,
        TempoAnalysisScope.TRACK_DEFAULT_CUES,
        0.9,
        True,
        "c" * 64,
        ("Möglicher Tempowechsel oder instabiles Tempo.",),
        rhythm_stability=0.4,
    )
    full = _value(83.9, TempoValueSource.TRACK_FULL, TempoAnalysisScope.TRACK_FULL)

    resolution = TempoResolver.catalog(None, cue, full)

    assert resolution.best_analysis_proposal is cue
    assert resolution.planning is full


def test_only_unreliable_result_is_proposal_but_not_normal_planning_value() -> None:
    uncertain = TempoAnalysisValue(
        115.385,
        230.77,
        TempoValueSource.TRACK_DEFAULT_CUES,
        TempoAnalysisScope.TRACK_DEFAULT_CUES,
        0.9,
        True,
        "c" * 64,
        ("Möglicher Tempowechsel oder instabiles Tempo.",),
        rhythm_stability=0.4,
    )

    resolution = TempoResolver.catalog(None, uncertain, None)

    assert resolution.best_analysis_proposal is uncertain
    assert resolution.planning.bpm is None


def test_high_calibrated_confidence_and_stable_family_are_planable() -> None:
    stable = TempoAnalysisValue(
        92.2,
        184.4,
        TempoValueSource.TRACK_DEFAULT_CUES,
        TempoAnalysisScope.TRACK_DEFAULT_CUES,
        0.895,
        True,
        "c" * 64,
        rhythm_stability=0.74,
    )

    resolution = TempoResolver.catalog(None, stable, None)

    assert resolution.planning is stable


def test_saved_queue_manual_value_has_priority_and_can_fall_back() -> None:
    catalog = TempoResolver.catalog(
        120.0, None, _value(119.0, TempoValueSource.TRACK_FULL, TempoAnalysisScope.TRACK_FULL)
    )
    saved = _value(124.0, TempoValueSource.SAVED_QUEUE_ENTRY, TempoAnalysisScope.SAVED_QUEUE_ENTRY)
    manual = SavedQueueManualTempo(
        5, 126.0, True, TempoValueSource.MANUAL_SAVED_QUEUE, "now", "b" * 64
    )
    assert TempoResolver.saved_queue(manual, saved, catalog, None).planning.bpm == 126.0
    assert TempoResolver.saved_queue(None, saved, catalog, None).planning.bpm == 124.0
    changed = TempoResolver.saved_queue(
        manual, saved, catalog, None, current_range_signature="d" * 64
    )
    assert "geänderten Cue-Bereich" in changed.confirmed.warnings[0]


def test_manual_playlist_bpm_persists_resets_and_never_updates_track(
    temporary_database: Database,
) -> None:
    with temporary_database.connect() as connection:
        track_id = int(
            connection.execute(
                "INSERT INTO tracks(file_path,title,bpm) VALUES ('x.mp3','x',100) RETURNING id"
            ).fetchone()[0]
        )
        queue_id = int(
            connection.execute(
                "INSERT INTO saved_queues(name) VALUES ('Plan') RETURNING id"
            ).fetchone()[0]
        )
        entry_id = int(
            connection.execute(
                """INSERT INTO saved_queue_entries(saved_queue_id,track_id,position)
                   VALUES (?,?,1) RETURNING id""",
                (queue_id, track_id),
            ).fetchone()[0]
        )
    repository = TempoContextRepository(temporary_database)
    repository.save_manual_saved_queue_bpm(entry_id, 128.0, based_on_signature="c" * 64)
    assert repository.manual_saved_queue_bpm(entry_id).bpm == 128.0  # type: ignore[union-attr]
    with temporary_database.connect() as connection:
        assert float(connection.execute("SELECT bpm FROM tracks").fetchone()[0]) == 100.0
    repository.reset_manual_saved_queue_bpm(entry_id)
    assert repository.manual_saved_queue_bpm(entry_id) is None


def test_global_cue_staleness_only_affects_inherited_saved_queue_results(
    temporary_database: Database, tmp_path: Path
) -> None:
    with temporary_database.connect() as connection:
        track_id = int(
            connection.execute(
                "INSERT INTO tracks(file_path,title) VALUES ('x.mp3','x') RETURNING id"
            ).fetchone()[0]
        )
    repository = TempoContextRepository(temporary_database)
    snapshot = _file_snapshot(tmp_path)
    for entry_id, inherited in ((10, True), (11, False)):
        area = resolved_now(
            5,
            100,
            5,
            180,
            f"entry-{entry_id}",
            saved_queue_entry_id=entry_id,
            inherited_track_cues=inherited,
        )
        repository.save_result(
            track_id=track_id,
            scope=TempoAnalysisScope.SAVED_QUEUE_ENTRY,
            context_id=entry_id,
            run_id=None,
            signature=tempo_range_signature(
                TempoAnalysisScope.SAVED_QUEUE_ENTRY, track_id, snapshot, area, "v1"
            ),
            analysis_range=area,
            bpm=120.0,
            alternative_bpm=240.0,
            confidence=0.8,
            rhythm_stability=0.8,
            warnings=(),
            experimental_energy=None,
            backend="test",
            algorithm_version="v1",
            analyzed_at=area.resolved_at,
        )
    assert (
        repository.mark_scope_stale(
            track_id,
            TempoAnalysisScope.SAVED_QUEUE_ENTRY,
            "Globale Cues geändert",
            inherited_only=True,
        )
        == 1
    )
    assert not repository.current_value(
        track_id, TempoAnalysisScope.SAVED_QUEUE_ENTRY, context_id=10
    ).current  # type: ignore[union-attr]
    assert repository.current_value(
        track_id, TempoAnalysisScope.SAVED_QUEUE_ENTRY, context_id=11
    ).current  # type: ignore[union-attr]


def test_party_queue_tempo_snapshot_is_immutable() -> None:
    area = resolved_now(0, 100, 5, 120, "party-1", party_queue_id=9)
    resolution = TempoResolver.catalog(
        120.0, None, _value(119.0, TempoValueSource.TRACK_FULL, TempoAnalysisScope.TRACK_FULL)
    )
    snapshot = TempoResolver.party_queue_snapshot(1, area, resolution, "v1")
    assert isinstance(snapshot, PartyQueueTempoSnapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.bpm = 130.0  # type: ignore[misc]


def test_context_resolver_uses_manual_then_automatic_then_file_boundaries(
    temporary_database: Database,
) -> None:
    track = Track(1, "song.mp3", "Song", "", "", 180.0)
    with temporary_database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks(id,file_path,title,duration_seconds)
               VALUES (1,'song.mp3','Song',180)"""
        )
        connection.execute(
            """INSERT INTO track_cue_points
                   (track_id,manual_cue_in,automatic_cue_in,automatic_cue_out,
                    automatic_fade_duration)
               VALUES (1,10,5,170,8)"""
        )
    resolver = TempoAnalysisContextResolver(
        CuePointService(CuePointRepository(temporary_database), 7.0)
    )

    area = resolver.track_default_cues(track, "cue-rev-1")

    assert (area.cue_in, area.cue_out, area.fade_duration) == (10.0, 170.0, 8.0)


def test_saved_queue_own_snapshot_is_distinguished_from_inherited_cues(
    temporary_database: Database,
) -> None:
    track = Track(1, "song.mp3", "Song", "", "", 180.0)
    with temporary_database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks(id,file_path,title,duration_seconds) VALUES (1,'x','x',180)"
        )
    resolver = TempoAnalysisContextResolver(
        CuePointService(CuePointRepository(temporary_database), 7.0)
    )
    own = resolver.saved_queue_entry(
        track, SavedQueueEntry(1, 1, 20, 150, 6, "snapshot", 9), "entry-rev-1"
    )
    inherited = resolver.saved_queue_entry(
        track, SavedQueueEntry(1, 2, saved_queue_entry_id=10), "entry-rev-2"
    )

    assert (own.cue_in, own.cue_out, own.fade_duration) == (20.0, 150.0, 6.0)
    assert not own.inherited_track_cues
    assert inherited.inherited_track_cues
