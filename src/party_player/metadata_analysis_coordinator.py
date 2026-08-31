"""UI-independent orchestration and persistence ports for metadata analysis."""

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import sqlite3
from typing import Protocol

from party_player.metadata_analysis_contracts import (
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisRequest,
    MetadataAnalysisResult,
)
from party_player.metadata_analysis_supervisor import MetadataAnalysisProcessSupervisor


class CoordinatorState(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    CLOSED = "CLOSED"


class AnalysisRunPersistencePort(Protocol):
    def create_job(self, request: MetadataAnalysisRequest) -> MetadataAnalysisJob: ...

    def recover_interrupted_runs(self) -> int: ...

    def mark_running(self, job: MetadataAnalysisJob) -> None: ...

    def finish(self, result: MetadataAnalysisResult) -> None: ...


class AnalysisResultPersistencePort(Protocol):
    def persist_valid_result(self, result: MetadataAnalysisResult) -> None: ...


@dataclass(frozen=True, slots=True)
class AnalysisOperatingState:
    production_mode: bool = False
    audio_recovery: bool = False
    database_maintenance: bool = False
    restore_or_migration: bool = False
    automation_active: bool = False
    playback_active: bool = False


class AnalysisOperatingStatePort(Protocol):
    def snapshot(self) -> AnalysisOperatingState: ...


class AnalysisProgressPort(Protocol):
    def publish(self, event: str, job_id: str, detail: str = "") -> None: ...


class AnalysisStartPolicy:
    """Pure policy that protects playback and maintenance priorities."""

    @staticmethod
    def block_reason(state: AnalysisOperatingState, *, batch: bool) -> str | None:
        if state.production_mode:
            return "PRODUCTION_MODE"
        if state.audio_recovery:
            return "AUDIO_RECOVERY"
        if state.database_maintenance:
            return "DATABASE_MAINTENANCE"
        if state.restore_or_migration:
            return "RESTORE_OR_MIGRATION"
        if state.playback_active:
            return "PLAYBACK_ACTIVE"
        if batch and state.automation_active:
            return "AUTOMATION_ACTIVE"
        return None


class MetadataAnalysisCoordinator:
    """Serialize persistent jobs around an isolated process supervisor."""

    def __init__(
        self,
        supervisor: MetadataAnalysisProcessSupervisor,
        runs: AnalysisRunPersistencePort,
        results: AnalysisResultPersistencePort,
        operating_state: AnalysisOperatingStatePort,
        progress: AnalysisProgressPort,
    ) -> None:
        self._supervisor = supervisor
        self._runs = runs
        self._results = results
        self._operating_state = operating_state
        self._progress = progress
        self._pending: deque[tuple[MetadataAnalysisJob, bool]] = deque()
        self._current: MetadataAnalysisJob | None = None
        self.state = CoordinatorState.IDLE
        self.snapshot_conflicts = 0

    def recover_interrupted_runs(self) -> int:
        """Mark formerly RUNNING jobs through the main-process persistence port."""
        return self._runs.recover_interrupted_runs()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def current_job(self) -> MetadataAnalysisJob | None:
        return self._current

    @property
    def pending_jobs(self) -> tuple[MetadataAnalysisJob, ...]:
        return tuple(job for job, _batch in self._pending)

    def enqueue(self, job: MetadataAnalysisJob, *, batch: bool = True) -> None:
        if self.state in {CoordinatorState.STOPPING, CoordinatorState.CLOSED}:
            raise RuntimeError("Koordinator nimmt keine neuen Analysejobs an")
        self._pending.append((job, batch))
        self._progress.publish("QUEUED", job.job_id)

    def block_reason(self, *, batch: bool) -> str | None:
        return AnalysisStartPolicy.block_reason(self._operating_state.snapshot(), batch=batch)

    def create_and_enqueue(
        self, request: MetadataAnalysisRequest, *, batch: bool = True
    ) -> MetadataAnalysisJob:
        job = self._runs.create_job(request)
        self.enqueue(job, batch=batch)
        return job

    def pause(self) -> None:
        if self.state is not CoordinatorState.CLOSED:
            self.state = CoordinatorState.PAUSED

    def resume(self) -> None:
        if self.state is CoordinatorState.PAUSED:
            self.state = CoordinatorState.IDLE

    def tick(self) -> MetadataAnalysisResult | None:
        if self.state is CoordinatorState.CLOSED:
            return None
        if self._current is not None:
            result = self._supervisor.poll()
            if result is not None:
                result = self._complete(result)
            return result
        if self.state is CoordinatorState.PAUSED or not self._pending:
            return None
        job, batch = self._pending[0]
        blocked = AnalysisStartPolicy.block_reason(self._operating_state.snapshot(), batch=batch)
        if blocked is not None:
            self._progress.publish("BLOCKED", job.job_id, blocked)
            return None
        if not job.input_snapshot.matches_file():
            self._pending.popleft()
            self.snapshot_conflicts += 1
            result = self._local_result(job, MetadataAnalysisOutcome.FILE_CHANGED)
            self._runs.finish(result)
            self._progress.publish("REJECTED", job.job_id, "FILE_CHANGED_BEFORE_START")
            return result
        self._pending.popleft()
        self._runs.mark_running(job)
        self._supervisor.submit(job)
        self._current = job
        self.state = CoordinatorState.RUNNING
        self._progress.publish("STARTED", job.job_id)
        return None

    def cancel_current(self) -> MetadataAnalysisResult | None:
        if self._current is None:
            return None
        result = self._supervisor.cancel()
        if result is not None:
            result = self._complete(result)
        return result

    def cancel_pending(self) -> tuple[MetadataAnalysisResult, ...]:
        cancelled = tuple(
            self._local_result(
                job,
                MetadataAnalysisOutcome.CANCELLED,
                error_code="BATCH_CANCELLED",
                error_text="Wartender Analyseauftrag wurde abgebrochen.",
            )
            for job, _batch in self._pending
        )
        self._pending.clear()
        for result in cancelled:
            self._runs.finish(result)
            self._progress.publish("FINISHED", result.job_id, result.outcome.value)
        return cancelled

    def close(self) -> None:
        if self.state is CoordinatorState.CLOSED:
            return
        self.state = CoordinatorState.STOPPING
        if self._current is not None:
            result = self._supervisor.cancel()
            if result is not None:
                self._complete(result)
        self._supervisor.close()
        self.state = CoordinatorState.CLOSED

    def _complete(self, result: MetadataAnalysisResult) -> MetadataAnalysisResult:
        current = self._current
        if current is None or result.job_id != current.job_id:
            return result
        valid = result.input_snapshot.matches_file()
        if not valid:
            self.snapshot_conflicts += 1
            result = self._local_result(current, MetadataAnalysisOutcome.FILE_CHANGED)
        if (
            result.outcome
            in {
                MetadataAnalysisOutcome.SUCCESS,
                MetadataAnalysisOutcome.PARTIAL_SUCCESS,
            }
            and valid
        ):
            try:
                self._results.persist_valid_result(result)
            except (ValueError, RuntimeError, sqlite3.Error):
                result = self._local_result(
                    current,
                    MetadataAnalysisOutcome.ANALYSIS_ERROR,
                    error_code="RESULT_REJECTED",
                    error_text="Analyseergebnis wurde verworfen und nicht gespeichert.",
                )
        self._runs.finish(result)
        self._progress.publish("FINISHED", result.job_id, result.outcome.value)
        self._current = None
        if self.state not in {CoordinatorState.PAUSED, CoordinatorState.STOPPING}:
            self.state = CoordinatorState.IDLE
        return result

    @staticmethod
    def _local_result(
        job: MetadataAnalysisJob,
        outcome: MetadataAnalysisOutcome,
        *,
        error_code: str | None = None,
        error_text: str = "Dateisnapshot ist nicht mehr aktuell.",
    ) -> MetadataAnalysisResult:
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
            error_code=error_code or outcome.value,
            error_text=error_text,
            backend_name="coordinator",
            backend_version="1",
            scope=job.scope,
            analysis_range=job.analysis_range,
            range_signature=job.range_signature,
        )


def normalized_existing_path(path: str) -> str:
    """Normalize without leaking path handling into persistence or GUI objects."""
    return str(Path(path).resolve())
