from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from party_player.database.connection import Database
from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisOutcome,
    MetadataAnalysisRequest,
    MetadataAnalysisResult,
    MetadataAnalysisSource,
    MetadataFieldSuggestion,
    TechnicalAudioMetric,
    TempoSegmentDiagnostic,
)
from party_player.metadata_analysis_coordinator import AnalysisOperatingState
from party_player.metadata_analysis_persistence import (
    SqliteAnalysisResultPersistencePort,
    SqliteAnalysisRunPersistencePort,
)
from party_player.metadata_analysis_profiles import (
    ALGORITHM_VERSION,
    ConfidenceBand,
    MetadataAnalysisProfile,
    PROFILE_CONFIGURATIONS,
    confidence_band,
)
from party_player.metadata_analysis_service import MetadataAnalysisService
from party_player.metadata_persistence import AnalysisRunRepository, AnalysisRunStatus
from party_player.repositories.track_repository import TrackRepository
from party_player.worker_diagnostics import WorkerRegistry
from tests.test_metadata_persistence import add_track


def create_job(database: Database, track_id: int, path: Path, run_number: int = 1):
    configuration = PROFILE_CONFIGURATIONS[MetadataAnalysisProfile.TEMPO_AND_ENERGY_EXPERIMENTAL]
    return SqliteAnalysisRunPersistencePort(database).create_job(
        MetadataAnalysisRequest(
            track_id,
            FileSnapshot.capture(str(path)),
            MetadataAnalysisProfile.TEMPO_AND_ENERGY_EXPERIMENTAL.value,
            ALGORITHM_VERSION,
            configuration.requested_kinds,
            priority=run_number,
            timeout_seconds=configuration.timeout_seconds,
            backend=MetadataAnalysisBackendKind.FFMPEG_TEMPO,
        )
    )


def successful_result(job, bpm: float = 120.0) -> MetadataAnalysisResult:
    now = datetime.now(timezone.utc).isoformat()
    return MetadataAnalysisResult(
        job.job_id,
        job.run_id,
        job.track_id,
        job.input_snapshot,
        job.analysis_profile,
        job.analysis_version,
        now,
        now,
        MetadataAnalysisOutcome.SUCCESS,
        suggestions=(
            MetadataFieldSuggestion("bpm", bpm, MetadataAnalysisSource.AUDIO_ANALYSIS, 0.9),
            MetadataFieldSuggestion(
                "alternative_bpm", bpm * 2, MetadataAnalysisSource.AUDIO_ANALYSIS, 0.72
            ),
            MetadataFieldSuggestion(
                "energy_experimental", 55, MetadataAnalysisSource.AUDIO_ANALYSIS, 0.63
            ),
        ),
        technical_metrics=(
            TechnicalAudioMetric("rms_mean", 0.1, "linear"),
            TechnicalAudioMetric("rms_variability", 0.02, "linear"),
            TechnicalAudioMetric("peak", 0.8, "linear"),
            TechnicalAudioMetric("crest_factor", 8.0, "ratio"),
            TechnicalAudioMetric("transient_density", 2.0, "events/s"),
            TechnicalAudioMetric("bpm", bpm, "BPM"),
            TechnicalAudioMetric("energy_experimental", 55.0, "percent"),
        ),
        rhythm_stability=0.9,
        backend_name="ffmpeg-onset-autocorrelation",
        backend_version=ALGORITHM_VERSION,
    )


def prepare_track(database: Database, tmp_path: Path) -> tuple[int, Path]:
    track_id = add_track(database)
    path = tmp_path / "one.mp3"
    path.write_bytes(b"audio snapshot")
    with database.connect() as connection:
        connection.execute("UPDATE tracks SET file_path=? WHERE id=?", (str(path), track_id))
    return track_id, path


def test_confidence_thresholds_are_central_and_conservative() -> None:
    assert confidence_band(0.80) is ConfidenceBand.HIGH
    assert confidence_band(0.55) is ConfidenceBand.MEDIUM
    assert confidence_band(0.549) is ConfidenceBand.LOW


def test_result_persistence_is_atomic_and_does_not_change_effective_revision(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    with temporary_database.connect() as connection:
        connection.execute(
            "UPDATE tracks SET bpm=98.0,energy=22,metadata_revision=7 WHERE id=?",
            (track_id,),
        )
    job = create_job(temporary_database, track_id, path)
    runs = SqliteAnalysisRunPersistencePort(temporary_database)
    runs.mark_running(job)
    SqliteAnalysisResultPersistencePort(temporary_database).persist_valid_result(
        successful_result(job)
    )
    with temporary_database.connect() as connection:
        suggestions = connection.execute(
            "SELECT field_key,status,source_detail FROM track_metadata_suggestions ORDER BY id"
        ).fetchall()
        metrics = connection.execute(
            "SELECT metric_key,experimental FROM metadata_analysis_run_metrics WHERE run_id=?",
            (job.run_id,),
        ).fetchall()
        revision = connection.execute(
            "SELECT metadata_revision,bpm,energy FROM tracks WHERE id=?", (track_id,)
        ).fetchone()
    assert [str(row["field_key"]) for row in suggestions] == [
        "bpm",
        "alternative_bpm",
        "energy",
    ]
    assert "energy_experimental" in str(suggestions[-1]["source_detail"])
    assert any(
        row["metric_key"] == "energy_experimental" and row["experimental"] for row in metrics
    )
    assert tuple(revision) == (7, 98.0, 22)
    assert AnalysisRunRepository(temporary_database).get(job.run_id).status is (
        AnalysisRunStatus.COMPLETED
    )


def test_raw_tempo_diagnostics_remain_attached_to_the_analysis_run(
    temporary_database: Database, tmp_path: Path
) -> None:
    from dataclasses import replace

    track_id, path = prepare_track(temporary_database, tmp_path)
    job = create_job(temporary_database, track_id, path)
    runs = SqliteAnalysisRunPersistencePort(temporary_database)
    runs.mark_running(job)
    result = replace(
        successful_result(job),
        probed_duration_seconds=180.001,
        segment_diagnostics=(TempoSegmentDiagnostic(0, 12.0, 36.0, 120.0, 240.0, 0.88, 0.71),),
        decision_reasons=("HIGH_CONFIDENCE",),
        effective_parameters=(("profile_version", "tempo-profile-v3"),),
        aggregated_bpm=120.0,
        aggregated_alternative_bpm=240.0,
        aggregated_confidence=0.88,
        confidence_components=(
            ("family_consensus", 1.0),
            ("robust_window_confidence", 0.36),
            ("usable_window_count", 5),
        ),
    )

    SqliteAnalysisResultPersistencePort(temporary_database).persist_valid_result(result)

    with temporary_database.connect() as connection:
        raw = connection.execute(
            "SELECT diagnostics_json FROM metadata_analysis_runs WHERE id=?", (job.run_id,)
        ).fetchone()[0]
    diagnostic = json.loads(str(raw))
    assert diagnostic["run_id"] == job.run_id
    assert diagnostic["decoded_segments"][0]["raw_bpm"] == 120.0
    assert diagnostic["aggregated"]["confidence"] == 0.88
    assert diagnostic["confidence_components"]["family_consensus"] == 1.0
    assert diagnostic["confidence_components"]["usable_window_count"] == 5
    assert diagnostic["decision_reasons"] == ["HIGH_CONFIDENCE"]


def test_pending_run_is_named_explicitly_in_diagnostic_text(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    job = create_job(temporary_database, track_id, path)
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=None,
        ffprobe=None,
        operating_state=lambda: AnalysisOperatingState(),
    )
    try:
        text = service.tempo_diagnostics_text(track_id)
    finally:
        service.close()

    assert f"Run-ID: {job.run_id}" in text
    assert "Analyse wartet" in text


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("RUNNING", "Analyse läuft"),
        ("FAILED", "Analyse fehlgeschlagen"),
        ("CANCELLED", "Analyse abgebrochen"),
    ],
)
def test_non_completed_run_states_are_explicit_in_diagnostic_text(
    temporary_database: Database,
    tmp_path: Path,
    status: str,
    expected: str,
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    job = create_job(temporary_database, track_id, path)
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=None,
        ffprobe=None,
        operating_state=lambda: AnalysisOperatingState(),
    )
    try:
        with temporary_database.connect() as connection:
            connection.execute(
                """UPDATE metadata_analysis_runs
                   SET status=?,error_code='TEST_STATE',error_text='Gezielter Testzustand'
                   WHERE id=?""",
                (status, job.run_id),
            )
        text = service.tempo_diagnostics_text(track_id)
    finally:
        service.close()

    assert expected in text
    assert f"Run-ID: {job.run_id}" in text


def test_v03_correlation_score_is_read_with_clarified_legacy_semantics(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    job = create_job(temporary_database, track_id, path)
    legacy = json.dumps({"decoded_segments": [{"range_index": 0, "correlation_score": 1.05128}]})
    with temporary_database.connect() as connection:
        connection.execute(
            """UPDATE metadata_analysis_runs
               SET status='COMPLETED',diagnostics_json=? WHERE id=?""",
            (legacy, job.run_id),
        )
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=None,
        ffprobe=None,
        operating_state=lambda: AnalysisOperatingState(),
    )
    try:
        text = service.tempo_diagnostics_text(track_id)
    finally:
        service.close()

    assert '"harmonic_quality_score": 1.05128' in text
    assert "unbeschränkter harmonic_quality_score" in text


def test_identical_open_suggestions_are_not_duplicated_and_new_value_supersedes(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    runs = SqliteAnalysisRunPersistencePort(temporary_database)
    results = SqliteAnalysisResultPersistencePort(temporary_database)
    for number, bpm in ((1, 120.0), (2, 120.0), (3, 125.0)):
        job = create_job(temporary_database, track_id, path, number)
        runs.mark_running(job)
        results.persist_valid_result(successful_result(job, bpm))
    with temporary_database.connect() as connection:
        bpm_rows = connection.execute(
            """SELECT serialized_value,status FROM track_metadata_suggestions
               WHERE field_key='bpm' ORDER BY id"""
        ).fetchall()
    assert len(bpm_rows) == 2
    assert [row["status"] for row in bpm_rows] == ["SUPERSEDED", "PENDING"]


def test_invalid_metric_rolls_back_suggestions_and_run_completion(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    job = create_job(temporary_database, track_id, path)
    runs = SqliteAnalysisRunPersistencePort(temporary_database)
    runs.mark_running(job)
    result = successful_result(job)
    # Frozen DTO: rebuild only the bounded metric collection.
    from dataclasses import replace

    invalid = replace(result, technical_metrics=(TechnicalAudioMetric("unknown", 1.0),))
    with pytest.raises(ValueError, match="Messwert"):
        SqliteAnalysisResultPersistencePort(temporary_database).persist_valid_result(invalid)
    with temporary_database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM track_metadata_suggestions WHERE analysis_run_id=?",
                (job.run_id,),
            ).fetchone()[0]
            == 0
        )
    assert AnalysisRunRepository(temporary_database).get(job.run_id).status is (
        AnalysisRunStatus.RUNNING
    )


def test_missing_backend_finishes_run_without_starting_process(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, _path = prepare_track(temporary_database, tmp_path)
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=None,
        ffprobe=None,
        operating_state=lambda: AnalysisOperatingState(),
    )
    try:
        job = service.analyze_track(track_id)
        assert AnalysisRunRepository(temporary_database).get(job.run_id).status is (
            AnalysisRunStatus.FAILED
        )
        assert not service.available
        assert service.support_snapshot()["failed_runs"] == 1
    finally:
        service.close()


def test_production_mode_keeps_explicit_batch_pending_without_worker(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, _path = prepare_track(temporary_database, tmp_path)
    registry = WorkerRegistry()
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"placeholder")
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=executable,
        ffprobe=executable,
        operating_state=lambda: AnalysisOperatingState(production_mode=True),
        worker_registry=registry,
    )
    try:
        job = service.analyze_track(track_id, batch=True)
        assert service.tick() is None
        assert AnalysisRunRepository(temporary_database).get(job.run_id).status is (
            AnalysisRunStatus.PENDING
        )
        assert not registry.active()
        assert service.support_snapshot()["waiting_runs"] == 1
    finally:
        service.close()


def test_pending_runs_resume_only_after_explicit_request(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    create_job(temporary_database, track_id, path)
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"placeholder")
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=executable,
        ffprobe=executable,
        operating_state=lambda: AnalysisOperatingState(production_mode=True),
    )
    try:
        assert service.support_snapshot()["waiting_runs"] == 1
        assert service.resume_persistent_pending() == 1
        assert service.tick() is None
    finally:
        service.close()


def test_ui_view_exposes_persisted_proposals_without_applying_values(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    job = create_job(temporary_database, track_id, path)
    runs = SqliteAnalysisRunPersistencePort(temporary_database)
    runs.mark_running(job)
    SqliteAnalysisResultPersistencePort(temporary_database).persist_valid_result(
        successful_result(job)
    )
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=None,
        ffprobe=None,
        operating_state=lambda: AnalysisOperatingState(),
    )
    try:
        view = service.latest_for_track(track_id)
        assert view.status == "COMPLETED"
        assert view.bpm == 120.0
        assert view.alternative_bpm == 240.0
        assert view.experimental_energy == 55
        assert "Halb-/Doppeltempo-Alternative vorhanden." in view.warnings
        with temporary_database.connect() as connection:
            effective = connection.execute(
                "SELECT bpm,energy,metadata_revision FROM tracks WHERE id=?", (track_id,)
            ).fetchone()
        assert tuple(effective) == (None, None, 0)
    finally:
        service.close()


def test_batch_preview_skips_current_and_reports_missing_and_open_proposals(
    temporary_database: Database, tmp_path: Path
) -> None:
    current_id, current_path = prepare_track(temporary_database, tmp_path)
    current_job = create_job(temporary_database, current_id, current_path)
    runs = SqliteAnalysisRunPersistencePort(temporary_database)
    runs.mark_running(current_job)
    SqliteAnalysisResultPersistencePort(temporary_database).persist_valid_result(
        successful_result(current_job)
    )
    missing_id = add_track(temporary_database, "missing")
    with temporary_database.connect() as connection:
        connection.execute(
            "UPDATE tracks SET file_path=? WHERE id=?",
            (str(tmp_path / "missing.mp3"), missing_id),
        )
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=None,
        ffprobe=None,
        operating_state=lambda: AnalysisOperatingState(),
    )
    try:
        preview = service.preview_tracks((current_id, missing_id), skip_current=True)
        assert preview.selected == 2
        assert preview.current == 1
        assert preview.planned == 0
        assert preview.missing_files == 1
        assert preview.open_suggestions == 3
    finally:
        service.close()


def test_user_can_discard_only_restart_pending_runs(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id, path = prepare_track(temporary_database, tmp_path)
    create_job(temporary_database, track_id, path)
    service = MetadataAnalysisService(
        temporary_database,
        TrackRepository(temporary_database),
        ffmpeg=None,
        ffprobe=None,
        operating_state=lambda: AnalysisOperatingState(),
    )
    try:
        assert service.discard_persistent_pending() == 1
        assert service.support_snapshot()["waiting_runs"] == 0
    finally:
        service.close()
