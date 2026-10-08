"""Atomic SQLite restore commit with explicit lifecycle gate and rollback."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import logging
import os
from pathlib import Path
import sqlite3

from party_player.backup_service import (
    BackupOperationState,
    RestorePreparationResult,
    validate_backup_archive,
)
from party_player.database.migrations import LATEST_SCHEMA_VERSION


class RestoreCommitErrorCode(str, Enum):
    NONE = ""
    PREPARATION_INVALID = "RESTORE_COMMIT_PREPARATION_INVALID"
    CANDIDATE_CHANGED = "RESTORE_COMMIT_CANDIDATE_CHANGED"
    SAFETY_BACKUP_INVALID = "RESTORE_COMMIT_SAFETY_BACKUP_INVALID"
    STAGED_DATABASE_INVALID = "RESTORE_COMMIT_STAGED_DATABASE_INVALID"
    ACTIVE_DATABASE_MISSING = "RESTORE_COMMIT_ACTIVE_DATABASE_MISSING"
    LIFECYCLE_GATE_FAILED = "RESTORE_COMMIT_LIFECYCLE_GATE_FAILED"
    EXCHANGE_FAILED = "RESTORE_COMMIT_EXCHANGE_FAILED"
    RESUME_FAILED = "RESTORE_COMMIT_RESUME_FAILED"
    ROLLBACK_FAILED = "RESTORE_COMMIT_ROLLBACK_FAILED"
    ROLLBACK_UNCONFIRMED = "RESTORE_COMMIT_ROLLBACK_UNCONFIRMED"
    ROLLBACK_RESUME_FAILED = "RESTORE_COMMIT_ROLLBACK_RESUME_FAILED"
    CLEANUP_PENDING = "RESTORE_COMMIT_CLEANUP_PENDING"


@dataclass(frozen=True, slots=True)
class RestoreCommitResult:
    success: bool
    state: BackupOperationState
    error_code: RestoreCommitErrorCode
    message: str
    restart_required: bool = False
    rollback_performed: bool = False
    preserve_staging: bool = False


class _RollbackState(str, Enum):
    RESTORED = "RESTORED"
    FAILED = "FAILED"
    UNCONFIRMED = "UNCONFIRMED"


@dataclass(frozen=True, slots=True)
class _RollbackResult:
    state: _RollbackState
    stage: str
    error: OSError | None = None


logger = logging.getLogger(__name__)


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _database_is_current_and_valid(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            quick = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check"))
            integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
            row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.Error:
        return False
    return (
        quick == ("ok",)
        and integrity == ("ok",)
        and row is not None
        and row[0] == LATEST_SCHEMA_VERSION
    )


class RestoreCommitService:
    """Commit one already materialized database and preserve rollback until verified."""

    def __init__(
        self,
        active_database: Path,
        *,
        quiesce: Callable[[], bool],
        resume_after_rollback: Callable[[], bool],
    ) -> None:
        self._active_database = active_database.resolve()
        self._quiesce = quiesce
        self._resume_after_rollback = resume_after_rollback

    def commit(
        self,
        preparation: RestorePreparationResult,
        candidate_archive: Path,
        staged_database: Path,
    ) -> RestoreCommitResult:
        precondition = self._check_preconditions(preparation, candidate_archive, staged_database)
        if precondition is not None:
            return precondition
        try:
            quiesced = self._quiesce()
        except Exception:  # lifecycle boundary must become a stable result
            quiesced = False
        if not quiesced:
            return self._failure(
                RestoreCommitErrorCode.LIFECYCLE_GATE_FAILED,
                "Datenbankverbindungen oder Persistenzjobs konnten nicht sicher angehalten werden.",
            )

        staged_database = staged_database.resolve()
        rollback = self._available_rollback_path()
        moved_sidecars: list[tuple[Path, Path]] = []
        active_moved = False
        candidate_installed = False
        exchange_stage = "move_active_database"
        try:
            os.replace(self._active_database, rollback)
            active_moved = True
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self._active_database}{suffix}")
                if sidecar.exists():
                    exchange_stage = f"move_active_sidecar{suffix}"
                    rollback_sidecar = Path(f"{rollback}{suffix}")
                    os.replace(sidecar, rollback_sidecar)
                    moved_sidecars.append((sidecar, rollback_sidecar))
            exchange_stage = "install_candidate"
            os.replace(staged_database, self._active_database)
            candidate_installed = True
            exchange_stage = "validate_candidate"
            if not _database_is_current_and_valid(self._active_database):
                raise sqlite3.DatabaseError("committed database validation failed")
        except (OSError, sqlite3.Error) as exchange_error:
            self._log_exchange_failure(
                exchange_stage,
                exchange_error,
                active_moved=active_moved,
                candidate_installed=candidate_installed,
                moved_sidecars=moved_sidecars,
            )
            if not active_moved:
                resumed = self._resume("resume_after_unchanged_exchange")
                if not resumed:
                    return self._failure(
                        RestoreCommitErrorCode.RESUME_FAILED,
                        "Datenbankaustausch wurde nicht begonnen; der Datenbankbetrieb "
                        "konnte aber nicht fortgesetzt werden.",
                    )
                return self._failure(
                    RestoreCommitErrorCode.EXCHANGE_FAILED,
                    "Datenbankaustausch konnte nicht begonnen werden; die aktive "
                    "Datenbank blieb unverändert.",
                )
            rollback_result = self._rollback(
                rollback, moved_sidecars, candidate_installed=candidate_installed
            )
            if rollback_result.state is not _RollbackState.RESTORED:
                self._log_rollback_failure(
                    rollback_result,
                    active_moved=active_moved,
                    candidate_installed=candidate_installed,
                    moved_sidecars=moved_sidecars,
                )
                code = (
                    RestoreCommitErrorCode.ROLLBACK_UNCONFIRMED
                    if rollback_result.state is _RollbackState.UNCONFIRMED
                    else RestoreCommitErrorCode.ROLLBACK_FAILED
                )
                return self._failure(
                    code,
                    "Datenbankaustausch fehlgeschlagen; Rollback konnte nicht bestätigt werden.",
                    preserve_staging=True,
                )
            resumed = self._resume("resume_after_rollback")
            if not resumed:
                return RestoreCommitResult(
                    False,
                    BackupOperationState.FAILED,
                    RestoreCommitErrorCode.ROLLBACK_RESUME_FAILED,
                    "Rollback war erfolgreich, Datenbankbetrieb konnte aber nicht fortgesetzt werden.",
                    rollback_performed=True,
                )
            return RestoreCommitResult(
                False,
                BackupOperationState.FAILED,
                RestoreCommitErrorCode.EXCHANGE_FAILED,
                "Datenbankaustausch fehlgeschlagen; der vorherige Stand wurde wiederhergestellt.",
                rollback_performed=True,
            )

        cleanup_ok = self._cleanup_rollback(rollback, moved_sidecars)
        return RestoreCommitResult(
            True,
            BackupOperationState.COMPLETED,
            RestoreCommitErrorCode.NONE if cleanup_ok else RestoreCommitErrorCode.CLEANUP_PENDING,
            "Datenbank wurde atomar ersetzt; Neustart ist erforderlich.",
            restart_required=True,
        )

    def _check_preconditions(
        self,
        preparation: RestorePreparationResult,
        candidate_archive: Path,
        staged_database: Path,
    ) -> RestoreCommitResult | None:
        if not preparation.success or not preparation.candidate_sha256:
            return self._failure(
                RestoreCommitErrorCode.PREPARATION_INVALID,
                "Eine erfolgreiche Restore-Vorbereitung fehlt.",
            )
        if (
            preparation.safety_backup_path is None
            or not validate_backup_archive(preparation.safety_backup_path).valid
        ):
            return self._failure(
                RestoreCommitErrorCode.SAFETY_BACKUP_INVALID,
                "Das obligatorische Sicherheitsbackup ist nicht mehr gültig.",
            )
        try:
            if _digest(candidate_archive) != preparation.candidate_sha256:
                return self._failure(
                    RestoreCommitErrorCode.CANDIDATE_CHANGED,
                    "Der Restore-Kandidat wurde nach der Vorprüfung verändert.",
                )
        except OSError:
            return self._failure(
                RestoreCommitErrorCode.CANDIDATE_CHANGED,
                "Der Restore-Kandidat ist nicht mehr lesbar.",
            )
        staged_database = staged_database.resolve()
        if (
            staged_database == self._active_database
            or staged_database.parent != self._active_database.parent
            or not _database_is_current_and_valid(staged_database)
        ):
            return self._failure(
                RestoreCommitErrorCode.STAGED_DATABASE_INVALID,
                "Die vorbereitete Datenbank ist ungültig oder nicht atomar austauschbar.",
            )
        if not self._active_database.is_file():
            return self._failure(
                RestoreCommitErrorCode.ACTIVE_DATABASE_MISSING,
                "Die aktive Datenbank fehlt.",
            )
        return None

    def _available_rollback_path(self) -> Path:
        candidate = self._active_database.with_name(f".{self._active_database.name}.rollback")
        suffix = 1
        while candidate.exists():
            candidate = self._active_database.with_name(
                f".{self._active_database.name}.rollback-{suffix}"
            )
            suffix += 1
        return candidate

    def _rollback(
        self,
        rollback: Path,
        sidecars: list[tuple[Path, Path]],
        *,
        candidate_installed: bool,
    ) -> _RollbackResult:
        stage = "locate_rollback_database"
        try:
            if not rollback.is_file():
                return _RollbackResult(_RollbackState.FAILED, stage)
            if candidate_installed:
                for suffix in ("-wal", "-shm"):
                    stage = f"remove_candidate_sidecar{suffix}"
                    Path(f"{self._active_database}{suffix}").unlink(missing_ok=True)
            stage = "restore_active_database"
            os.replace(rollback, self._active_database)
            for active_sidecar, rollback_sidecar in sidecars:
                if rollback_sidecar.exists():
                    stage = f"restore_active_sidecar{active_sidecar.suffix}"
                    os.replace(rollback_sidecar, active_sidecar)
            stage = "validate_rollback"
            if not _database_is_current_and_valid(self._active_database):
                return _RollbackResult(_RollbackState.UNCONFIRMED, stage)
            return _RollbackResult(_RollbackState.RESTORED, stage)
        except OSError as error:
            return _RollbackResult(_RollbackState.FAILED, stage, error)

    def _resume(self, stage: str) -> bool:
        error: Exception | None = None
        try:
            resumed = self._resume_after_rollback()
        except Exception as exc:
            error = exc
            resumed = False
        if not resumed:
            logger.warning(
                "Restore-Lifecycle konnte nicht fortgesetzt werden",
                exc_info=(type(error), error, error.__traceback__) if error is not None else None,
                extra={
                    "event": "restore_resume_failed",
                    "exchange_stage": stage,
                    "exception_type": type(error).__name__ if error is not None else "none",
                    "os_error": self._os_error(error),
                    "resume_result": False,
                },
            )
        return resumed

    @staticmethod
    def _log_exchange_failure(
        stage: str,
        error: OSError | sqlite3.Error,
        *,
        active_moved: bool,
        candidate_installed: bool,
        moved_sidecars: list[tuple[Path, Path]],
    ) -> None:
        logger.warning(
            "Datenbankaustausch fehlgeschlagen",
            exc_info=(type(error), error, error.__traceback__),
            extra={
                "event": "restore_exchange_failed",
                "exchange_stage": stage,
                "exception_type": type(error).__name__,
                "os_error": RestoreCommitService._os_error(error),
                "active_moved": active_moved,
                "candidate_installed": candidate_installed,
                "sidecar_state": tuple(source.suffix for source, _target in moved_sidecars),
                "rollback_result": "not_started",
                "resume_result": "not_started",
            },
        )

    @staticmethod
    def _log_rollback_failure(
        result: _RollbackResult,
        *,
        active_moved: bool,
        candidate_installed: bool,
        moved_sidecars: list[tuple[Path, Path]],
    ) -> None:
        logger.warning(
            "Datenbank-Rollback fehlgeschlagen oder nicht bestätigt",
            exc_info=(
                (type(result.error), result.error, result.error.__traceback__)
                if result.error is not None
                else None
            ),
            extra={
                "event": "restore_rollback_failed",
                "exchange_stage": result.stage,
                "exception_type": (
                    type(result.error).__name__ if result.error is not None else "none"
                ),
                "os_error": RestoreCommitService._os_error(result.error),
                "active_moved": active_moved,
                "candidate_installed": candidate_installed,
                "sidecar_state": tuple(source.suffix for source, _target in moved_sidecars),
                "rollback_result": result.state.value,
                "resume_result": "not_attempted",
            },
        )

    @staticmethod
    def _os_error(error: BaseException | None) -> int | str:
        if error is None:
            return "none"
        return getattr(error, "winerror", None) or getattr(error, "errno", None) or "unknown"

    @staticmethod
    def _cleanup_rollback(rollback: Path, sidecars: list[tuple[Path, Path]]) -> bool:
        try:
            rollback.unlink(missing_ok=True)
            for _active_sidecar, rollback_sidecar in sidecars:
                rollback_sidecar.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    @staticmethod
    def _failure(
        code: RestoreCommitErrorCode,
        message: str,
        *,
        preserve_staging: bool = False,
    ) -> RestoreCommitResult:
        return RestoreCommitResult(
            False,
            BackupOperationState.FAILED,
            code,
            message,
            preserve_staging=preserve_staging,
        )
