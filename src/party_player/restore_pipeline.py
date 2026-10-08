"""Serialized end-to-end restore pipeline, intentionally independent from the UI."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Lock

from party_player.backup_service import (
    BackupOperationState,
    BackupService,
    RestoreMaterializer,
    RestorePreparationService,
    RestoreValidator,
)
from party_player.restore_commit import RestoreCommitResult, RestoreCommitService
from party_player.restore_safety import RestoreSafetyGate, RestoreSafetyResult
from party_player.performance_monitor import PerformanceMonitor


class RestorePipelineErrorCode(str, Enum):
    NONE = ""
    BUSY = "RESTORE_PIPELINE_BUSY"
    MATERIALIZATION_FAILED = "RESTORE_PIPELINE_MATERIALIZATION_FAILED"
    PREPARATION_FAILED = "RESTORE_PIPELINE_PREPARATION_FAILED"
    COMMIT_FAILED = "RESTORE_PIPELINE_COMMIT_FAILED"
    SAFETY_GATE_BLOCKED = "RESTORE_PIPELINE_SAFETY_GATE_BLOCKED"


@dataclass(frozen=True, slots=True)
class RestorePipelineResult:
    success: bool
    state: BackupOperationState
    error_code: RestorePipelineErrorCode
    message: str
    commit: RestoreCommitResult | None = None
    safety_backup_path: Path | None = None
    safety: RestoreSafetyResult | None = None
    database_schema_version: int | None = None


class AtomicRestorePipeline:
    """Materialize, safety-backup, and commit under one operation lock."""

    def __init__(
        self,
        active_database: Path,
        backup_service: BackupService,
        commit_service: RestoreCommitService,
        *,
        validator: RestoreValidator | None = None,
        safety_gate: RestoreSafetyGate | None = None,
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        self._active_database = active_database.resolve()
        self._performance = performance_monitor or PerformanceMonitor(enabled=False)
        self._validator = validator or RestoreValidator(performance_monitor=self._performance)
        self._materializer = RestoreMaterializer(self._validator, self._performance)
        self._preparation = RestorePreparationService(
            self._validator, backup_service, self._performance
        )
        self._commit = commit_service
        self._safety_gate = safety_gate
        self._lock = Lock()

    def execute(
        self, candidate_archive: Path, safety_backup_directory: Path
    ) -> RestorePipelineResult:
        if not self._lock.acquire(blocking=False):
            return RestorePipelineResult(
                False,
                BackupOperationState.FAILED,
                RestorePipelineErrorCode.BUSY,
                "Eine Restore-Operation läuft bereits.",
            )
        staging = self._active_database.with_name(f".{self._active_database.name}.restore-staging")
        preserve_staging = False
        try:
            blocked = self._evaluate_safety()
            if blocked is not None:
                return blocked
            staging.unlink(missing_ok=True)
            materialized = self._materializer.materialize(candidate_archive, staging)
            if not materialized.success or materialized.database_path is None:
                return RestorePipelineResult(
                    False,
                    BackupOperationState.FAILED,
                    RestorePipelineErrorCode.MATERIALIZATION_FAILED,
                    materialized.message,
                )
            preparation = self._preparation.prepare(candidate_archive, safety_backup_directory)
            if not preparation.success:
                return RestorePipelineResult(
                    False,
                    BackupOperationState.FAILED,
                    RestorePipelineErrorCode.PREPARATION_FAILED,
                    preparation.message,
                )
            blocked = self._evaluate_safety(preparation.safety_backup_path)
            if blocked is not None:
                return blocked
            with self._performance.measure("restore.database_replace", warning_threshold_ms=1000.0):
                committed = self._commit.commit(
                    preparation, candidate_archive, materialized.database_path
                )
            preserve_staging = committed.preserve_staging
            return RestorePipelineResult(
                committed.success,
                committed.state,
                (
                    RestorePipelineErrorCode.NONE
                    if committed.success
                    else RestorePipelineErrorCode.COMMIT_FAILED
                ),
                committed.message,
                committed,
                preparation.safety_backup_path,
                database_schema_version=(
                    preparation.candidate.manifest.database_schema_version
                    if preparation.candidate is not None
                    and preparation.candidate.manifest is not None
                    else None
                ),
            )
        finally:
            if not preserve_staging:
                for path in (staging, Path(f"{staging}-wal"), Path(f"{staging}-shm")):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            self._lock.release()

    def _evaluate_safety(
        self, safety_backup_path: Path | None = None
    ) -> RestorePipelineResult | None:
        if self._safety_gate is None:
            return None
        safety = self._safety_gate.evaluate()
        if safety.allowed:
            return None
        return RestorePipelineResult(
            False,
            BackupOperationState.FAILED,
            RestorePipelineErrorCode.SAFETY_GATE_BLOCKED,
            "Restore ist aus Sicherheitsgründen blockiert: "
            + " ".join(reason.message for reason in safety.reasons),
            safety_backup_path=safety_backup_path,
            safety=safety,
        )
