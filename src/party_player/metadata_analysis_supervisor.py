"""Single-process supervisor for isolated metadata analysis."""

from dataclasses import dataclass
from enum import StrEnum
import multiprocessing
from time import monotonic
from typing import Any

from party_player.metadata_analysis_contracts import (
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisResult,
)
from party_player.metadata_analysis_worker import metadata_analysis_worker_entry
from party_player.worker_diagnostics import WorkerInfo, WorkerRegistry


class SupervisorState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    READY = "READY"
    SUBMITTING = "SUBMITTING"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    TERMINATING = "TERMINATING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(slots=True)
class SupervisorMetrics:
    worker_starts: int = 0
    worker_restarts: int = 0
    submitted_jobs: int = 0
    successful_results: int = 0
    partial_results: int = 0
    cancellations: int = 0
    timeouts: int = 0
    crashes: int = 0
    ignored_late_results: int = 0
    total_start_latency_seconds: float = 0.0
    total_job_duration_seconds: float = 0.0
    total_cancel_duration_seconds: float = 0.0


class MetadataAnalysisProcessSupervisor:
    """Run at most one spawn-based child process, one job per process."""

    def __init__(self, worker_registry: WorkerRegistry | None = None) -> None:
        self._context = multiprocessing.get_context("spawn")
        # Concrete spawn-process and Windows pipe types differ across platforms/typeshed.
        self._process: Any | None = None
        self._connection: Any | None = None
        self._cancel_event: Any | None = None
        self._job: MetadataAnalysisJob | None = None
        self._submitted_at = 0.0
        self._started_at = 0.0
        self._cancel_at = 0.0
        self._closed = False
        self._ever_started = False
        self._worker_registry = worker_registry
        self._registered_worker_id: str | None = None
        self.state = SupervisorState.NOT_STARTED
        self.metrics = SupervisorMetrics()

    @property
    def worker_pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def current_job(self) -> MetadataAnalysisJob | None:
        return self._job

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Supervisor ist geschlossen")
        if self.state is SupervisorState.NOT_STARTED:
            self.state = SupervisorState.READY

    def submit(self, job: MetadataAnalysisJob) -> None:
        self.start()
        if self._job is not None or self._process is not None:
            raise RuntimeError("Es darf höchstens ein Analysejob gleichzeitig laufen")
        self.state = SupervisorState.SUBMITTING
        parent, child = self._context.Pipe(duplex=False)
        cancellation = self._context.Event()
        process = self._context.Process(
            target=metadata_analysis_worker_entry,
            args=(child, cancellation, job),
            name="DeckRelayMetadataAnalysis",
            daemon=False,
        )
        self._connection = parent
        self._cancel_event = cancellation
        self._job = job
        self._submitted_at = monotonic()
        process.start()
        child.close()
        self._process = process
        if self._worker_registry is not None:
            worker_id = f"metadata-analysis:{job.run_id}:{process.pid}"
            self._registered_worker_id = worker_id
            self._worker_registry.started(
                WorkerInfo(
                    worker_id,
                    (
                        f"Metadatenanalyse {job.analysis_profile} · "
                        f"ffmpeg-onset-autocorrelation · {job.analysis_version}"
                    ),
                    "metadata-analysis-process",
                    self._submitted_at,
                    False,
                    str(job.run_id),
                )
            )
        self.metrics.worker_starts += 1
        if self._ever_started:
            self.metrics.worker_restarts += 1
        self._ever_started = True
        self.metrics.submitted_jobs += 1

    def poll(self) -> MetadataAnalysisResult | None:
        job = self._job
        process = self._process
        connection = self._connection
        if job is None or process is None or connection is None:
            return None
        while True:
            try:
                has_message = connection.poll()
            except OSError:
                break
            if not has_message:
                break
            try:
                message = connection.recv()
            except (EOFError, OSError):
                break
            if message[0] == "STARTED":
                self._started_at = monotonic()
                self.metrics.total_start_latency_seconds += self._started_at - self._submitted_at
                self.state = SupervisorState.RUNNING
            elif message[0] == "RESULT":
                result: MetadataAnalysisResult = message[1]
                if result.job_id != job.job_id or self.state is SupervisorState.CANCEL_REQUESTED:
                    self.metrics.ignored_late_results += 1
                    continue
                self._record_result(result)
                self._release_process(worker_state=result.outcome.value.lower())
                return result
            elif message[0] == "ERROR":
                result = self._failure_result(
                    MetadataAnalysisOutcome.ANALYSIS_ERROR, str(message[1]), str(message[2])
                )
                self.state = SupervisorState.FAILED
                self._release_process(ready=False, worker_state="analysis_error")
                return result
            elif message[0] == "CANCELLED":
                continue
        if monotonic() - self._submitted_at >= job.timeout_seconds:
            self.metrics.timeouts += 1
            return self._terminate_with(MetadataAnalysisOutcome.TIMEOUT, "TIMEOUT")
        if not process.is_alive():
            self.metrics.crashes += 1
            result = self._failure_result(
                MetadataAnalysisOutcome.WORKER_CRASHED,
                "WORKER_CRASHED",
                f"Analyseprozess endete mit Code {process.exitcode}",
            )
            self.state = SupervisorState.FAILED
            self._release_process(ready=False, worker_state="worker_crashed")
            return result
        return None

    def cancel(self, grace_seconds: float = 0.2) -> MetadataAnalysisResult | None:
        if self._job is None:
            return None
        self.state = SupervisorState.CANCEL_REQUESTED
        self._cancel_at = monotonic()
        if self._cancel_event is not None:
            self._cancel_event.set()
        process = self._process
        if process is not None:
            process.join(max(0.0, grace_seconds))
        self.metrics.cancellations += 1
        result = self._terminate_with(MetadataAnalysisOutcome.CANCELLED, "CANCELLED")
        self.metrics.total_cancel_duration_seconds += monotonic() - self._cancel_at
        return result

    def close(self, grace_seconds: float = 0.2) -> None:
        if self._closed:
            return
        if self._job is not None:
            self.cancel(grace_seconds)
        self._closed = True
        self.state = SupervisorState.STOPPED

    def _terminate_with(
        self, outcome: MetadataAnalysisOutcome, error_code: str
    ) -> MetadataAnalysisResult:
        self.state = SupervisorState.TERMINATING
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()
            process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(1.0)
        result = self._failure_result(outcome, error_code, "Analyse wurde beendet.")
        self._release_process(worker_state=outcome.value.lower())
        return result

    def _record_result(self, result: MetadataAnalysisResult) -> None:
        if result.outcome is MetadataAnalysisOutcome.SUCCESS:
            self.metrics.successful_results += 1
        elif result.outcome is MetadataAnalysisOutcome.PARTIAL_SUCCESS:
            self.metrics.partial_results += 1
        self.metrics.total_job_duration_seconds += monotonic() - self._submitted_at

    def _failure_result(
        self, outcome: MetadataAnalysisOutcome, code: str, text: str
    ) -> MetadataAnalysisResult:
        assert self._job is not None
        job = self._job
        from datetime import datetime, timezone

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
            outcome,
            error_code=code[:80],
            error_text=text[:500],
            backend_name="process-supervisor",
            backend_version="1",
            scope=job.scope,
            analysis_range=job.analysis_range,
            range_signature=job.range_signature,
        )

    def _release_process(self, *, ready: bool = True, worker_state: str = "completed") -> None:
        if self._worker_registry is not None and self._registered_worker_id is not None:
            self._worker_registry.finished(self._registered_worker_id, worker_state)
            self._registered_worker_id = None
        if self._process is not None:
            self._process.join(0.2)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(1.0)
            self._process.close()
        if self._connection is not None:
            self._connection.close()
        self._process = None
        self._connection = None
        self._cancel_event = None
        self._job = None
        if self._closed:
            self.state = SupervisorState.STOPPED
        elif ready:
            self.state = SupervisorState.READY
        else:
            self.state = SupervisorState.FAILED
