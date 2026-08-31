from pathlib import Path
from datetime import datetime, timezone

import pytest

from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.models import Track
from party_player.models import QueueEntry
from party_player.enums import QueueStatus
from party_player.analysis import CueAnalysisResult


def _track(duration: float = 250.0) -> Track:
    return Track(1, "song.mp3", "Song", "Artist", "", duration)


def _service(tmp_path: Path) -> CuePointService:
    database = Database(tmp_path / "cue.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks
               (id, file_path, title, artist, album, duration_seconds)
               VALUES (1, 'song.mp3', 'Song', 'Artist', '', 250)"""
        )
    return CuePointService(CuePointRepository(database), 7.0)


def test_manual_cues_are_persistent_and_define_crossfade_end(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_manual(_track(), 1.8, 242.0, 7.0)

    resolved = service.resolve(_track())

    assert resolved.cue_in == 1.8
    assert resolved.cue_out == 242.0
    assert resolved.crossfade_start == 235.0
    assert resolved.fade_duration == 7.0
    assert resolved.cue_in_source == "MANUAL"
    assert resolved.cue_out_source == "MANUAL"
    assert service.manual_track_ids([1]) == {1}


def test_cue_changes_publish_targeted_tempo_invalidation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    changed: list[int] = []
    service._on_global_cues_changed = changed.append

    service.save_manual(_track(), 2.0, 240.0, 6.0)

    assert changed == [1]


def test_editor_save_atomically_updates_manual_values_and_discards_analysis(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    with service.repository._database.connect() as connection:
        connection.execute(
            """INSERT INTO track_cue_points
                   (track_id, automatic_cue_in, automatic_cue_out,
                    automatic_fade_duration, confidence, analysis_version)
               VALUES (1, 1.0, 245.0, 7.0, 0.9, 'test')"""
        )

    service.save_editor(
        _track(),
        2.0,
        240.0,
        6.0,
        discard_automatic=True,
    )

    stored = service.get(1)
    assert (
        stored.manual_cue_in,
        stored.manual_cue_out,
        stored.manual_fade_duration,
    ) == (2.0, 240.0, 6.0)
    assert stored.automatic_cue_in is None
    assert stored.automatic_cue_out is None
    assert stored.automatic_fade_duration is None
    assert stored.confidence is None
    assert stored.analysis_version is None


def test_editor_save_updates_only_explicitly_changed_manual_field(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_manual(_track(), 2.0, 240.0, 6.0)

    service.repository.save_editor(
        1,
        99.0,
        230.0,
        99.0,
        discard_automatic=False,
        changed_fields=frozenset({"cue_out"}),
    )

    stored = service.get(1)
    assert stored.manual_cue_in == 2.0
    assert stored.manual_cue_out == 230.0
    assert stored.manual_fade_duration == 6.0


def test_zero_fade_is_preserved_as_explicit_manual_value(tmp_path: Path) -> None:
    service = _service(tmp_path)

    service.save_manual(_track(), None, None, 0.0)

    stored = service.get(1)
    resolved = service.resolve(_track())
    assert stored.manual_cue_in is None
    assert stored.manual_cue_out is None
    assert stored.manual_fade_duration == 0.0
    assert resolved.cue_in == 0.0
    assert resolved.cue_out == 250.0
    assert resolved.fade_duration == 0.0
    assert resolved.fade_source == "MANUAL"
    assert not resolved.automatic_crossfade_allowed


def test_manual_values_override_automatic_values(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with service.repository._database.connect() as connection:
        connection.execute(
            """INSERT INTO track_cue_points
               (track_id, manual_cue_in, automatic_cue_in, automatic_cue_out)
               VALUES (1, 2.0, 4.0, 240.0)"""
        )

    resolved = service.resolve(_track())

    assert resolved.cue_in == 2.0
    assert resolved.cue_in_source == "MANUAL"
    assert resolved.cue_out == 240.0
    assert resolved.cue_out_source == "AUTOMATIC"


def test_automatic_analysis_result_is_persisted_separately_from_manual_cues(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    track = _track()
    service.save_manual(track, 2.0, None, None)
    result = CueAnalysisResult(
        file_path=Path(track.file_path),
        file_duration_seconds=250.0,
        cue_in=4.0,
        cue_out=242.0,
        suggested_fade_duration=7.0,
        minimum_level_dbfs=-120.0,
        maximum_level_dbfs=-8.0,
        peak=0.9,
        measured_window_count=600,
        confidence=0.88,
        analysis_version="silence-v1",
        analyzed_at=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc),
        backend_name="ffmpeg",
    )

    service.save_automatic(track, result)
    stored = CuePointRepository(service.repository._database).get(track.id)

    assert stored.manual_cue_in == 2.0
    assert (stored.automatic_cue_in, stored.automatic_cue_out) == (4.0, 242.0)
    assert stored.automatic_fade_duration == 7.0
    assert (stored.minimum_level_dbfs, stored.maximum_level_dbfs, stored.peak) == (
        -120.0,
        -8.0,
        0.9,
    )
    assert stored.measured_window_count == 600
    assert stored.confidence == 0.88
    assert stored.analysis_version == "silence-v1"
    assert stored.analysis_backend == "ffmpeg"
    assert stored.analysed_at == "2026-07-25T12:00:00+00:00"


def test_repeated_automatic_analysis_never_overwrites_any_manual_value(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    track = _track()
    service.save_manual(track, 2.0, 240.0, 6.0)

    for cue_in, cue_out, fade, version in (
        (4.0, 242.0, 7.0, "silence-v1"),
        (5.0, 238.0, 8.0, "silence-v2"),
    ):
        service.save_automatic(
            track,
            CueAnalysisResult(
                file_path=Path(track.file_path),
                file_duration_seconds=250.0,
                cue_in=cue_in,
                cue_out=cue_out,
                suggested_fade_duration=fade,
                minimum_level_dbfs=-120.0,
                maximum_level_dbfs=-8.0,
                peak=0.9,
                measured_window_count=600,
                confidence=0.9,
                analysis_version=version,
                analyzed_at=datetime.now(timezone.utc),
                backend_name="ffmpeg",
            ),
        )

    stored = service.get(track.id)
    resolved = service.resolve(track)
    assert (
        stored.manual_cue_in,
        stored.manual_cue_out,
        stored.manual_fade_duration,
    ) == (2.0, 240.0, 6.0)
    assert (
        stored.automatic_cue_in,
        stored.automatic_cue_out,
        stored.automatic_fade_duration,
    ) == (5.0, 238.0, 8.0)
    assert stored.analysis_version == "silence-v2"
    assert (resolved.cue_in, resolved.cue_out, resolved.fade_duration) == (
        2.0,
        240.0,
        6.0,
    )
    assert (
        resolved.cue_in_source,
        resolved.cue_out_source,
        resolved.fade_source,
    ) == ("MANUAL", "MANUAL", "MANUAL")


def test_invalid_manual_configuration_is_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ValueError):
        service.save_manual(_track(), 20.0, 10.0, 7.0)


def test_short_track_reduces_fade_duration_safely(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_manual(_track(250), 10.0, 12.0, None)

    resolved = service.resolve(_track(250))

    assert resolved.fade_duration == 2.0
    assert resolved.crossfade_start == 10.0
    assert not resolved.automatic_crossfade_allowed
    assert "Nutzbare Titellänge" in resolved.warning


def test_fade_below_configured_minimum_disables_automatic_crossfade(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.minimum_fade_duration = 1.0
    service.save_manual(_track(), 0.0, 200.0, 0.5)

    resolved = service.resolve(_track())

    assert resolved.fade_duration == 0.5
    assert not resolved.automatic_crossfade_allowed
    assert "Überblenddauer" in resolved.warning


def test_invalid_stored_cue_is_reported_but_resolves_safely(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with service.repository._database.connect() as connection:
        connection.execute(
            """INSERT INTO track_cue_points (track_id, manual_cue_in)
               VALUES (1, 999)"""
        )

    resolved = service.resolve(_track())

    assert resolved.cue_in == 0.0
    assert resolved.automatic_crossfade_allowed
    assert "Cue In ist ungültig" in resolved.warning


def test_queue_snapshot_overrides_title_values_per_field(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_manual(_track(), 2.0, 240.0, 6.0)
    entry = QueueEntry(
        10,
        1,
        1,
        QueueStatus.WAITING,
        cue_in_override=4.0,
        cue_out_override=220.0,
        fade_duration_override=9.0,
        cue_override_source="snapshot",
    )

    resolved = service.resolve(_track(), queue_entry=entry)

    assert (resolved.cue_in, resolved.cue_out, resolved.fade_duration) == (4.0, 220.0, 9.0)
    assert resolved.cue_in_source == "QUEUE_SNAPSHOT"
    assert resolved.cue_out_source == "QUEUE_SNAPSHOT"
    assert resolved.fade_source == "QUEUE_SNAPSHOT"


def test_missing_queue_override_inherits_manual_title_value(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_manual(_track(), 2.0, 240.0, 6.0)
    entry = QueueEntry(
        10,
        1,
        1,
        QueueStatus.WAITING,
        cue_out_override=220.0,
        cue_override_source="queue",
    )

    resolved = service.resolve(_track(), queue_entry=entry)

    assert resolved.cue_in == 2.0
    assert resolved.cue_in_source == "MANUAL"
    assert resolved.cue_out == 220.0
    assert resolved.cue_out_source == "QUEUE_OVERRIDE"
