from pathlib import Path
import sqlite3
from zipfile import ZipFile

from party_player.backup_service import (
    DATABASE_ARCHIVE_PATH,
    BackupService,
    RestoreMaterializer,
)
from party_player.database.connection import Database
from party_player.performance_monitor import PerformanceMonitor
from party_player.restore_commit import RestoreCommitService
from party_player.restore_pipeline import AtomicRestorePipeline, RestorePipelineErrorCode
from party_player.restore_safety import RestoreSafetyGate, RestoreSafetySnapshot


def test_materializer_produces_current_valid_database(
    temporary_database: Database, tmp_path: Path
) -> None:
    backup = BackupService(temporary_database).create_backup(tmp_path / "candidate")
    assert backup.backup_path is not None
    destination = tmp_path / "materialized.db"

    result = RestoreMaterializer().materialize(backup.backup_path, destination)

    assert result.success
    assert result.database_path == destination
    connection = sqlite3.connect(destination)
    try:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert connection.execute("SELECT MAX(version) FROM schema_version").fetchone() == (41,)
    finally:
        connection.close()


def test_atomic_pipeline_binds_safety_backup_directly_to_commit(
    temporary_database: Database, tmp_path: Path
) -> None:
    candidate = BackupService(temporary_database).create_backup(tmp_path / "candidate")
    assert candidate.backup_path is not None
    with temporary_database.connect() as connection:
        connection.execute("CREATE TABLE state_after_candidate (value TEXT)")
        connection.execute("INSERT INTO state_after_candidate VALUES ('must be in safety backup')")
    commit = RestoreCommitService(
        temporary_database.path,
        quiesce=lambda: True,
        resume_after_rollback=lambda: True,
    )
    pipeline = AtomicRestorePipeline(
        temporary_database.path, BackupService(temporary_database), commit
    )

    result = pipeline.execute(candidate.backup_path, tmp_path / "safety")

    assert result.success
    assert result.commit is not None and result.commit.restart_required
    assert result.safety_backup_path is not None
    assert result.safety_backup_path.name.startswith("deckrelay-safety-backup-")
    with ZipFile(result.safety_backup_path) as archive:
        safety_db = tmp_path / "safety.db"
        safety_db.write_bytes(archive.read(DATABASE_ARCHIVE_PATH))
    safety_connection = sqlite3.connect(safety_db)
    active_connection = sqlite3.connect(temporary_database.path)
    try:
        assert safety_connection.execute("SELECT value FROM state_after_candidate").fetchone() == (
            "must be in safety backup",
        )
        assert (
            active_connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'state_after_candidate'"
            ).fetchone()
            is None
        )
    finally:
        safety_connection.close()
        active_connection.close()


def test_pipeline_records_each_executed_internal_restore_phase(
    temporary_database: Database, tmp_path: Path
) -> None:
    candidate = BackupService(temporary_database).create_backup(tmp_path / "candidate")
    assert candidate.backup_path is not None
    performance = PerformanceMonitor()
    commit = RestoreCommitService(
        temporary_database.path,
        quiesce=lambda: True,
        resume_after_rollback=lambda: True,
    )
    pipeline = AtomicRestorePipeline(
        temporary_database.path,
        BackupService(temporary_database, performance_monitor=performance),
        commit,
        performance_monitor=performance,
    )

    result = pipeline.execute(candidate.backup_path, tmp_path / "safety")

    assert result.success
    statistics = performance.statistics()
    assert statistics["restore.validate"].count == 2
    assert statistics["restore.safety_backup"].count == 1
    assert statistics["restore.database_replace"].count == 1
    assert "restore.migration" not in statistics


def test_pipeline_does_not_commit_when_safety_backup_fails(
    temporary_database: Database, tmp_path: Path
) -> None:
    candidate = BackupService(temporary_database).create_backup(tmp_path / "candidate")
    assert candidate.backup_path is not None
    active_before = temporary_database.path.read_bytes()
    commit = RestoreCommitService(
        temporary_database.path,
        quiesce=lambda: True,
        resume_after_rollback=lambda: True,
    )
    pipeline = AtomicRestorePipeline(
        temporary_database.path,
        BackupService(Database(tmp_path / "missing-active.db")),
        commit,
    )

    result = pipeline.execute(candidate.backup_path, tmp_path / "safety")

    assert result.error_code is RestorePipelineErrorCode.PREPARATION_FAILED
    assert temporary_database.path.read_bytes() == active_before
    assert not temporary_database.path.with_name(
        f".{temporary_database.path.name}.restore-staging"
    ).exists()


def test_pipeline_checks_safety_before_start_and_again_before_commit(
    temporary_database: Database, tmp_path: Path
) -> None:
    candidate = BackupService(temporary_database).create_backup(tmp_path / "candidate")
    assert candidate.backup_path is not None
    snapshots = [
        RestoreSafetySnapshot(True, True, False, False, False, False, False, False, False),
        RestoreSafetySnapshot(True, True, True, False, False, False, False, False, False),
    ]
    gate = RestoreSafetyGate(lambda: snapshots.pop(0))
    commit_calls = 0

    class CommitSpy:
        def commit(self, *args: object) -> object:
            nonlocal commit_calls
            commit_calls += 1
            raise AssertionError("commit must remain blocked")

    pipeline = AtomicRestorePipeline(
        temporary_database.path,
        BackupService(temporary_database),
        CommitSpy(),  # type: ignore[arg-type]
        safety_gate=gate,
    )

    result = pipeline.execute(candidate.backup_path, tmp_path / "safety")

    assert result.error_code is RestorePipelineErrorCode.SAFETY_GATE_BLOCKED
    assert result.safety is not None
    assert result.safety_backup_path is not None
    assert commit_calls == 0
    assert snapshots == []
