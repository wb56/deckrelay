from pathlib import Path
from time import monotonic, sleep

from party_player.metadata_analysis_contracts import MetadataAnalysisOutcome
from party_player.metadata_analysis_supervisor import (
    MetadataAnalysisProcessSupervisor,
    SupervisorState,
)
from party_player.worker_diagnostics import WorkerRegistry
from tests.test_metadata_analysis_contracts import make_job


def await_result(supervisor: MetadataAnalysisProcessSupervisor, timeout: float = 10.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        result = supervisor.poll()
        if result is not None:
            return result
        sleep(0.01)
    raise AssertionError("Analyseprozess lieferte kein Ergebnis")


def test_spawn_process_returns_serializable_result_and_can_restart(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    supervisor = MetadataAnalysisProcessSupervisor()
    try:
        supervisor.submit(make_job(path))
        first_pid = supervisor.worker_pid
        assert await_result(supervisor).outcome is MetadataAnalysisOutcome.SUCCESS
        supervisor.submit(make_job(path, job_id="job-2", run_id=2))
        second_pid = supervisor.worker_pid
        assert await_result(supervisor).outcome is MetadataAnalysisOutcome.SUCCESS
        assert first_pid is not None and second_pid is not None
        assert supervisor.metrics.worker_starts == 2
        assert supervisor.metrics.worker_restarts == 1
        assert supervisor.state is SupervisorState.READY
    finally:
        supervisor.close()


def test_supervisor_rejects_second_job_and_cancels_boundedly(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    supervisor = MetadataAnalysisProcessSupervisor()
    supervisor.submit(make_job(path, technical_options=(("test_delay_seconds", 5.0),)))
    try:
        try:
            supervisor.submit(make_job(path, job_id="job-2", run_id=2))
        except RuntimeError:
            pass
        else:
            raise AssertionError("Ein zweiter Prozessjob wurde angenommen")
        started = monotonic()
        result = supervisor.cancel(grace_seconds=0.01)
        assert result is not None
        assert result.outcome is MetadataAnalysisOutcome.CANCELLED
        assert monotonic() - started < 3.0
        assert supervisor.worker_pid is None
    finally:
        supervisor.close()


def test_cooperative_cancel_signal_releases_process(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    supervisor = MetadataAnalysisProcessSupervisor()
    supervisor.submit(make_job(path, technical_options=(("test_cooperative_delay_seconds", 5.0),)))
    result = supervisor.cancel(grace_seconds=0.5)
    assert result is not None and result.outcome is MetadataAnalysisOutcome.CANCELLED
    assert supervisor.worker_pid is None
    supervisor.close()


def test_timeout_terminates_worker_and_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    supervisor = MetadataAnalysisProcessSupervisor()
    supervisor.submit(
        make_job(
            path,
            timeout_seconds=0.1,
            technical_options=(("test_delay_seconds", 5.0),),
        )
    )
    result = await_result(supervisor)
    assert result.outcome is MetadataAnalysisOutcome.TIMEOUT
    assert supervisor.worker_pid is None
    supervisor.close()
    supervisor.close()
    assert supervisor.state is SupervisorState.STOPPED


def test_worker_crash_is_detected_and_supervisor_can_restart(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    supervisor = MetadataAnalysisProcessSupervisor()
    try:
        supervisor.submit(make_job(path, technical_options=(("test_crash", True),)))
        assert await_result(supervisor).outcome is MetadataAnalysisOutcome.WORKER_CRASHED
        assert supervisor.state is SupervisorState.FAILED
        supervisor.submit(make_job(path, job_id="job-2", run_id=2))
        assert await_result(supervisor).outcome is MetadataAnalysisOutcome.SUCCESS
        assert supervisor.metrics.crashes == 1
    finally:
        supervisor.close()


def test_stale_result_identity_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    supervisor = MetadataAnalysisProcessSupervisor()
    try:
        supervisor.submit(make_job(path, technical_options=(("test_result_job_id", "older-job"),)))
        assert await_result(supervisor).outcome is MetadataAnalysisOutcome.WORKER_CRASHED
        assert supervisor.metrics.ignored_late_results == 1
    finally:
        supervisor.close()


def test_worker_registry_is_cleared_after_spawn_result(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    registry = WorkerRegistry()
    supervisor = MetadataAnalysisProcessSupervisor(registry)
    try:
        job = make_job(path)
        supervisor.submit(job)
        assert len(registry.active()) == 1
        worker = registry.active()[0]
        assert "ffmpeg-onset-autocorrelation" in worker.name
        assert job.analysis_version in worker.name
        assert await_result(supervisor).outcome is MetadataAnalysisOutcome.SUCCESS
        assert not registry.active()
        assert registry.history()[-1].category == "metadata-analysis-process"
    finally:
        supervisor.close()
