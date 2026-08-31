"""Service and presentation contracts used by the visible tempo controls."""

from pathlib import Path

from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.metadata_analysis_contracts import TempoAnalysisScope
from party_player.metadata_analysis_coordinator import AnalysisOperatingState
from party_player.metadata_analysis_service import MetadataAnalysisService, TempoAnalysisView
from party_player.repositories.track_repository import TrackRepository
from party_player.ui.dialogs import CuePointDialog


def _service(database: Database) -> MetadataAnalysisService:
    return MetadataAnalysisService(
        database,
        TrackRepository(database),
        ffmpeg=None,
        ffprobe=None,
        operating_state=lambda: AnalysisOperatingState(),
        cue_points=CuePointService(CuePointRepository(database), 7.0),
    )


def _track(database: Database, tmp_path: Path) -> int:
    path = tmp_path / "song.mp3"
    path.write_bytes(b"unchanged")
    with database.connect() as connection:
        return int(
            connection.execute(
                """INSERT INTO tracks(file_path,title,artist,duration_seconds,bpm)
                   VALUES (?,'Song','Artist',180,120) RETURNING id""",
                (str(path),),
            ).fetchone()[0]
        )


def test_track_actions_create_distinct_full_and_cue_jobs(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id = _track(temporary_database, tmp_path)
    service = _service(temporary_database)
    try:
        full = service.analyze_track(track_id, scope=TempoAnalysisScope.TRACK_FULL)
        cue = service.analyze_track(track_id, scope=TempoAnalysisScope.TRACK_DEFAULT_CUES)
        assert full.scope is TempoAnalysisScope.TRACK_FULL
        assert cue.scope is TempoAnalysisScope.TRACK_DEFAULT_CUES
        assert cue.analysis_range is not None
        assert (cue.analysis_range.cue_in, cue.analysis_range.cue_out) == (0.0, 180.0)
        assert full.range_signature != cue.range_signature
    finally:
        service.close()


def test_saved_queue_manual_bpm_is_local_and_reset_uses_resolver_again(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id = _track(temporary_database, tmp_path)
    with temporary_database.connect() as connection:
        queue_id = int(
            connection.execute(
                "INSERT INTO saved_queues(name) VALUES ('Plan') RETURNING id"
            ).fetchone()[0]
        )
        entry_id = int(
            connection.execute(
                """INSERT INTO saved_queue_entries
                       (saved_queue_id,track_id,position,cue_in,cue_out,fade_duration,cue_source)
                   VALUES (?,?,1,20,150,6,'snapshot') RETURNING id""",
                (queue_id, track_id),
            ).fetchone()[0]
        )
    service = _service(temporary_database)
    try:
        service.save_manual_saved_queue_bpm(entry_id, 126.0)
        view = service.saved_queue_tempo_view(entry_id)
        assert view.resolution.planning.bpm == 126.0
        assert not view.inherited_cues
        service.reset_manual_saved_queue_bpm(entry_id)
        assert service.saved_queue_tempo_view(entry_id).resolution.planning.bpm == 120.0
        with temporary_database.connect() as connection:
            assert float(connection.execute("SELECT bpm FROM tracks").fetchone()[0]) == 120.0
    finally:
        service.close()


def test_tempo_scope_text_uses_required_non_guaranteeing_wording() -> None:
    view = TempoAnalysisView(
        1,
        2,
        "COMPLETED",
        "TEMPO",
        "v2",
        "backend",
        "now",
        120.0,
        240.0,
        0.7,
        0.5,
        73,
        0.5,
        (),
        "",
    )
    text = CuePointDialog._tempo_scope_line(view)
    assert "Prüfung erforderlich" in text
    assert "Möglicherweise halbes oder doppeltes Tempo" in text
    assert "Unterschiedliche Tempi erkannt" in text
    assert "Experimenteller Energievorschlag" in text
    assert "korrekt erkannt" not in text


def test_stale_cue_scope_has_explicit_reanalysis_wording() -> None:
    view = TempoAnalysisView(
        1,
        2,
        "COMPLETED",
        "TEMPO",
        "v2",
        "backend",
        "now",
        120.0,
        None,
        0.9,
        0.9,
        None,
        None,
        (),
        "",
        scope=TempoAnalysisScope.TRACK_DEFAULT_CUES,
        current=False,
    )
    assert CuePointDialog._tempo_scope_line(view) == (
        "Ergebnis wegen geänderter Cue-Punkte veraltet"
    )


def test_planning_presentation_rejects_high_confidence_unstable_tempo() -> None:
    view = TempoAnalysisView(
        1,
        2,
        "COMPLETED",
        "TEMPO",
        "v3",
        "backend",
        "now",
        115.385,
        230.77,
        0.9,
        0.4,
        None,
        None,
        (),
        "",
    )

    assert not CuePointDialog._tempo_view_reliable(view)
