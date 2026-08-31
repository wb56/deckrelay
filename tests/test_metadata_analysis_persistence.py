from pathlib import Path

from party_player.database.connection import Database
from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisKind,
    MetadataAnalysisOutcome,
    MetadataAnalysisRequest,
)
from party_player.metadata_analysis_persistence import SqliteAnalysisRunPersistencePort
from party_player.metadata_persistence import AnalysisRunRepository, AnalysisRunStatus
from tests.test_metadata_analysis_contracts import make_job
from tests.test_metadata_persistence import add_track


def test_sqlite_run_port_creates_starts_and_finishes_run(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id = add_track(temporary_database)
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    port = SqliteAnalysisRunPersistencePort(temporary_database)
    request = MetadataAnalysisRequest(
        track_id,
        FileSnapshot.capture(str(path)),
        "diagnostic",
        "diagnostic-v1",
        (MetadataAnalysisKind.BPM,),
    )
    job = port.create_job(request)
    assert job.track_id == track_id
    port.mark_running(job)
    run = AnalysisRunRepository(temporary_database).get(job.run_id)
    assert run.status is AnalysisRunStatus.RUNNING
    result = make_job(path, run_id=job.run_id, track_id=track_id)
    from party_player.metadata_analysis_coordinator import MetadataAnalysisCoordinator

    completed = MetadataAnalysisCoordinator._local_result(  # noqa: SLF001
        result, MetadataAnalysisOutcome.FILE_CHANGED
    )
    port.finish(completed)
    assert AnalysisRunRepository(temporary_database).get(job.run_id).status is (
        AnalysisRunStatus.FAILED
    )


def test_sqlite_run_port_recovers_interrupted_runs(
    temporary_database: Database, tmp_path: Path
) -> None:
    track_id = add_track(temporary_database)
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    runs = AnalysisRunRepository(temporary_database)
    run = runs.create(track_id, "diagnostic", "v1", str(path), 4, path.stat().st_mtime_ns)
    runs.start(run.run_id)
    port = SqliteAnalysisRunPersistencePort(temporary_database)
    assert port.recover_interrupted_runs() == 1
    assert runs.get(run.run_id).status is AnalysisRunStatus.FAILED
