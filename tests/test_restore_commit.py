from pathlib import Path
import logging
from zipfile import ZipFile

from _pytest.monkeypatch import MonkeyPatch

from party_player.backup_service import (
    DATABASE_ARCHIVE_PATH,
    BackupService,
    RestorePreparationService,
    RestorePreparationResult,
    RestoreValidator,
)
from party_player.database.connection import Database
from party_player.restore_commit import (
    RestoreCommitErrorCode,
    RestoreCommitService,
    _database_is_current_and_valid,
)


def _prepared_restore(database: Database, tmp_path: Path) -> tuple[RestorePreparationResult, Path]:
    candidate = BackupService(database).create_backup(tmp_path / "candidate")
    assert candidate.backup_path is not None
    preparation = RestorePreparationService(RestoreValidator(), BackupService(database)).prepare(
        candidate.backup_path, tmp_path / "safety"
    )
    assert preparation.success
    return preparation, candidate.backup_path


def _staged_database(candidate: Path, active: Path) -> Path:
    staged = active.with_name(".partyplayer.restore.tmp")
    with ZipFile(candidate) as archive:
        staged.write_bytes(archive.read(DATABASE_ARCHIVE_PATH))
    return staged


def test_atomic_commit_replaces_database_and_requires_restart(
    temporary_database: Database, tmp_path: Path
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    wal = Path(f"{temporary_database.path}-wal")
    shm = Path(f"{temporary_database.path}-shm")
    wal.write_bytes(b"old wal")
    shm.write_bytes(b"old shm")
    lifecycle_calls: list[str] = []

    def quiesce() -> bool:
        lifecycle_calls.append("quiesce")
        return True

    def resume() -> bool:
        lifecycle_calls.append("resume")
        return True

    service = RestoreCommitService(
        temporary_database.path,
        quiesce=quiesce,
        resume_after_rollback=resume,
    )

    result = service.commit(preparation, candidate, staged)

    assert result.success
    assert result.restart_required
    assert result.error_code is RestoreCommitErrorCode.NONE
    assert lifecycle_calls == ["quiesce"]
    assert _database_is_current_and_valid(temporary_database.path)
    assert not staged.exists()
    assert not wal.exists() or wal.read_bytes() != b"old wal"
    assert not shm.exists() or shm.read_bytes() != b"old shm"
    assert not list(temporary_database.path.parent.glob(".*.rollback*"))


def test_changed_candidate_is_rejected_before_lifecycle_gate(
    temporary_database: Database, tmp_path: Path
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    candidate.write_bytes(candidate.read_bytes() + b"changed")
    quiesced = False

    def quiesce() -> bool:
        nonlocal quiesced
        quiesced = True
        return True

    result = RestoreCommitService(
        temporary_database.path, quiesce=quiesce, resume_after_rollback=lambda: True
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.CANDIDATE_CHANGED
    assert not quiesced
    assert staged.exists()


def test_lifecycle_gate_blocks_exchange(temporary_database: Database, tmp_path: Path) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    active_before = temporary_database.path.read_bytes()

    result = RestoreCommitService(
        temporary_database.path, quiesce=lambda: False, resume_after_rollback=lambda: True
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.LIFECYCLE_GATE_FAILED
    assert temporary_database.path.read_bytes() == active_before
    assert staged.exists()


def test_exchange_failure_restores_previous_database(
    temporary_database: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path).resolve()
    active = temporary_database.path.resolve()
    active_before = active.read_bytes()
    resumed = False
    original_replace = __import__("os").replace

    def controlled_replace(source: Path, destination: Path) -> None:
        if Path(source).resolve() == staged and Path(destination).resolve() == active:
            raise PermissionError(13, "simulated exchange failure")
        original_replace(source, destination)

    def resume() -> bool:
        nonlocal resumed
        resumed = True
        return True

    monkeypatch.setattr("party_player.restore_commit.os.replace", controlled_replace)
    result = RestoreCommitService(
        active, quiesce=lambda: True, resume_after_rollback=resume
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.EXCHANGE_FAILED
    assert result.rollback_performed
    assert resumed
    assert active.read_bytes() == active_before
    assert _database_is_current_and_valid(active)


def test_failed_post_commit_validation_removes_candidate_sidecars_before_rollback(
    temporary_database: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    active = temporary_database.path.resolve()
    active_before = active.read_bytes()
    calls = 0

    def controlled_validation(path: Path) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            Path(f"{active}-wal").write_bytes(b"candidate wal")
            Path(f"{active}-shm").write_bytes(b"candidate shm")
            return False
        return True

    monkeypatch.setattr(
        "party_player.restore_commit._database_is_current_and_valid", controlled_validation
    )
    result = RestoreCommitService(
        active, quiesce=lambda: True, resume_after_rollback=lambda: True
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.EXCHANGE_FAILED
    assert result.rollback_performed
    assert active.read_bytes() == active_before
    assert not Path(f"{active}-wal").exists()
    assert not Path(f"{active}-shm").exists()


def test_first_exchange_failure_keeps_active_database_and_resumes_once(
    temporary_database: Database,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    active = temporary_database.path.resolve()
    active_before = active.read_bytes()
    replace_calls = 0
    resume_calls = 0

    def fail_first_replace(_source: Path, _destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        raise PermissionError(13, "database is locked")

    def resume() -> bool:
        nonlocal resume_calls
        resume_calls += 1
        return True

    monkeypatch.setattr("party_player.restore_commit.os.replace", fail_first_replace)

    result = RestoreCommitService(
        active, quiesce=lambda: True, resume_after_rollback=resume
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.EXCHANGE_FAILED
    assert not result.rollback_performed
    assert not result.preserve_staging
    assert replace_calls == 1
    assert resume_calls == 1
    assert active.read_bytes() == active_before
    assert not list(active.parent.glob(f".{active.name}.rollback*"))


def test_first_exchange_failure_reports_resume_failure_without_retry(
    temporary_database: Database,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    staged = _staged_database(candidate, temporary_database.path)
    replace_calls = 0
    resume_calls = 0

    def fail_first_replace(_source: Path, _destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        raise PermissionError(13, "database is locked")

    def fail_resume() -> bool:
        nonlocal resume_calls
        resume_calls += 1
        return False

    monkeypatch.setattr("party_player.restore_commit.os.replace", fail_first_replace)

    result = RestoreCommitService(
        temporary_database.path,
        quiesce=lambda: True,
        resume_after_rollback=fail_resume,
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.RESUME_FAILED
    assert not result.success
    assert replace_calls == 1
    assert resume_calls == 1


def test_sidecar_move_failure_restores_complete_previous_file_set(
    temporary_database: Database,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    active = temporary_database.path.resolve()
    staged = _staged_database(candidate, active).resolve()
    wal = Path(f"{active}-wal")
    shm = Path(f"{active}-shm")
    active_before = active.read_bytes()
    wal.write_bytes(b"old wal")
    shm.write_bytes(b"old shm")
    original_replace = __import__("os").replace

    for failing_suffix in ("-wal", "-shm"):
        current_staged = (
            staged if staged.exists() else _staged_database(candidate, active).resolve()
        )

        def controlled_replace(source: Path, destination: Path) -> None:
            if str(Path(source).resolve()).endswith(failing_suffix):
                raise PermissionError(13, f"blocked {failing_suffix}")
            original_replace(source, destination)

        monkeypatch.setattr("party_player.restore_commit.os.replace", controlled_replace)
        monkeypatch.setattr(
            "party_player.restore_commit._database_is_current_and_valid", lambda _path: True
        )

        result = RestoreCommitService(
            active, quiesce=lambda: True, resume_after_rollback=lambda: True
        ).commit(preparation, candidate, current_staged)

        assert result.error_code is RestoreCommitErrorCode.EXCHANGE_FAILED
        assert result.rollback_performed
        assert active.read_bytes() == active_before
        assert wal.read_bytes() == b"old wal"
        assert shm.read_bytes() == b"old shm"


def test_rollback_replace_failure_preserves_recovery_files_and_logs_both_errors(
    temporary_database: Database,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    caplog,
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    active = temporary_database.path.resolve()
    staged = _staged_database(candidate, active).resolve()
    original_replace = __import__("os").replace

    def controlled_replace(source: Path, destination: Path) -> None:
        resolved_source = Path(source).resolve()
        resolved_destination = Path(destination).resolve()
        if resolved_source == staged and resolved_destination == active:
            raise PermissionError(13, "candidate install blocked")
        if resolved_source.name.startswith(f".{active.name}.rollback"):
            raise PermissionError(13, "rollback replace blocked")
        original_replace(source, destination)

    monkeypatch.setattr("party_player.restore_commit.os.replace", controlled_replace)

    with caplog.at_level(logging.WARNING):
        result = RestoreCommitService(
            active, quiesce=lambda: True, resume_after_rollback=lambda: True
        ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.ROLLBACK_FAILED
    assert result.preserve_staging
    assert staged.exists()
    assert list(active.parent.glob(f".{active.name}.rollback*"))
    records_by_stage = {getattr(record, "exchange_stage", ""): record for record in caplog.records}
    assert {"install_candidate", "restore_active_database"}.issubset(records_by_stage)
    assert records_by_stage["install_candidate"].exception_type == "PermissionError"
    assert records_by_stage["install_candidate"].os_error == 13
    assert records_by_stage["restore_active_database"].exception_type == "PermissionError"
    assert records_by_stage["restore_active_database"].os_error == 13


def test_rollback_validation_failure_is_unconfirmed_and_preserves_staging(
    temporary_database: Database,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    preparation, candidate = _prepared_restore(temporary_database, tmp_path)
    active = temporary_database.path.resolve()
    staged = _staged_database(candidate, active)
    active_before = active.read_bytes()
    validation_results = iter((True, False, False))
    monkeypatch.setattr(
        "party_player.restore_commit._database_is_current_and_valid",
        lambda _path: next(validation_results),
    )

    result = RestoreCommitService(
        active, quiesce=lambda: True, resume_after_rollback=lambda: True
    ).commit(preparation, candidate, staged)

    assert result.error_code is RestoreCommitErrorCode.ROLLBACK_UNCONFIRMED
    assert result.preserve_staging
    assert not result.rollback_performed
    assert active.read_bytes() == active_before
