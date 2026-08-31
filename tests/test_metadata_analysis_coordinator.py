from pathlib import Path

from party_player.metadata_analysis_contracts import (
    MetadataAnalysisOutcome,
    MetadataAnalysisResult,
)
from party_player.metadata_analysis_coordinator import (
    AnalysisOperatingState,
    AnalysisStartPolicy,
    CoordinatorState,
    MetadataAnalysisCoordinator,
)
from tests.test_metadata_analysis_contracts import make_job


class SupervisorFake:
    def __init__(self) -> None:
        self.job = None
        self.result = None
        self.closed = False

    def submit(self, job):
        assert self.job is None
        self.job = job

    def poll(self):
        result, self.result = self.result, None
        if result is not None:
            self.job = None
        return result

    def cancel(self, grace_seconds=0.2):
        if self.job is None:
            return None
        result = result_for(self.job, MetadataAnalysisOutcome.CANCELLED)
        self.job = None
        return result

    def close(self, grace_seconds=0.2):
        self.closed = True


class RunPortFake:
    def __init__(self) -> None:
        self.running = []
        self.finished = []

    def create_job(self, request):
        raise NotImplementedError

    def recover_interrupted_runs(self):
        return 2

    def mark_running(self, job):
        self.running.append(job.job_id)

    def finish(self, result):
        self.finished.append(result)


class ResultPortFake:
    def __init__(self) -> None:
        self.results = []

    def persist_valid_result(self, result):
        self.results.append(result)


class RejectingResultPortFake(ResultPortFake):
    def persist_valid_result(self, result):
        raise ValueError("invalid result")


class OperatingStateFake:
    def __init__(self) -> None:
        self.value = AnalysisOperatingState()

    def snapshot(self):
        return self.value


class ProgressFake:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event, job_id, detail=""):
        self.events.append((event, job_id, detail))


def result_for(job, outcome=MetadataAnalysisOutcome.SUCCESS):
    return MetadataAnalysisResult(
        job.job_id,
        job.run_id,
        job.track_id,
        job.input_snapshot,
        job.analysis_profile,
        job.analysis_version,
        job.created_at,
        job.created_at,
        outcome,
        error_code="ERROR" if outcome is not MetadataAnalysisOutcome.SUCCESS else "",
        backend_name="fake",
        backend_version="1",
    )


def build_coordinator():
    supervisor = SupervisorFake()
    runs = RunPortFake()
    results = ResultPortFake()
    operating = OperatingStateFake()
    progress = ProgressFake()
    coordinator = MetadataAnalysisCoordinator(  # type: ignore[arg-type]
        supervisor, runs, results, operating, progress
    )
    return coordinator, supervisor, runs, results, operating, progress


def test_coordinator_serializes_jobs_and_persists_only_valid_success(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    first = make_job(path)
    second = make_job(path, job_id="job-2", run_id=2)
    coordinator, supervisor, runs, results, _operating, _progress = build_coordinator()
    coordinator.enqueue(first)
    coordinator.enqueue(second)
    coordinator.tick()
    assert runs.running == ["job-1"]
    supervisor.result = result_for(first)
    coordinator.tick()
    assert len(results.results) == 1
    coordinator.tick()
    assert runs.running == ["job-1", "job-2"]


def test_pause_and_operating_policy_block_only_new_jobs(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    job = make_job(path)
    coordinator, supervisor, runs, _results, operating, _progress = build_coordinator()
    coordinator.enqueue(job)
    coordinator.pause()
    coordinator.tick()
    assert not runs.running
    coordinator.resume()
    operating.value = AnalysisOperatingState(production_mode=True)
    coordinator.tick()
    assert not runs.running
    operating.value = AnalysisOperatingState()
    coordinator.tick()
    coordinator.pause()
    supervisor.result = result_for(job)
    coordinator.tick()
    assert coordinator.state is CoordinatorState.PAUSED


def test_changed_file_is_rejected_before_and_after_analysis(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    job = make_job(path)
    coordinator, supervisor, runs, results, _operating, _progress = build_coordinator()
    coordinator.enqueue(job)
    path.write_bytes(b"changed")
    before = coordinator.tick()
    assert before is not None and before.outcome is MetadataAnalysisOutcome.FILE_CHANGED
    assert not results.results
    assert runs.finished[-1].outcome is MetadataAnalysisOutcome.FILE_CHANGED

    path.write_bytes(b"new stable")
    later = make_job(path, job_id="job-2", run_id=2)
    coordinator.enqueue(later)
    coordinator.tick()
    path.write_bytes(b"changed after")
    supervisor.result = result_for(later)
    coordinator.tick()
    assert not results.results
    assert runs.finished[-1].outcome is MetadataAnalysisOutcome.FILE_CHANGED


def test_invalid_success_result_is_rolled_back_and_run_is_failed(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    job = make_job(path)
    coordinator, supervisor, runs, _results, _operating, progress = build_coordinator()
    coordinator._results = RejectingResultPortFake()  # noqa: SLF001 - focused boundary test
    coordinator.enqueue(job)
    coordinator.tick()
    supervisor.result = result_for(job)
    result = coordinator.tick()
    assert result is not None
    assert runs.finished[-1].outcome is MetadataAnalysisOutcome.ANALYSIS_ERROR
    assert runs.finished[-1].error_code == "RESULT_REJECTED"
    assert progress.events[-1][-1] == MetadataAnalysisOutcome.ANALYSIS_ERROR.value


def test_cancel_close_recovery_and_policy_matrix(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    coordinator, supervisor, runs, results, _operating, _progress = build_coordinator()
    assert coordinator.recover_interrupted_runs() == 2
    coordinator.enqueue(make_job(path))
    coordinator.tick()
    result = coordinator.cancel_current()
    assert result is not None and result.outcome is MetadataAnalysisOutcome.CANCELLED
    assert not results.results
    coordinator.close()
    coordinator.close()
    assert supervisor.closed and coordinator.state is CoordinatorState.CLOSED
    assert (
        AnalysisStartPolicy.block_reason(AnalysisOperatingState(automation_active=True), batch=True)
        == "AUTOMATION_ACTIVE"
    )
    assert (
        AnalysisStartPolicy.block_reason(
            AnalysisOperatingState(automation_active=True), batch=False
        )
        is None
    )
