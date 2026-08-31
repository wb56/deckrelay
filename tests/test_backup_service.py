from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
from _pytest.monkeypatch import MonkeyPatch

from party_player.backup_service import (
    DATABASE_ARCHIVE_PATH,
    MANIFEST_ARCHIVE_PATH,
    BACKUP_MANIFEST_JSON_SCHEMA,
    BackupCompatibility,
    BackupErrorCode,
    BackupOperationState,
    BackupPurpose,
    BackupService,
    RestorePreparationService,
    RestoreValidator,
    validate_backup_archive,
)
from party_player.database.connection import Database
from party_player.performance_monitor import PerformanceMonitor


FIXED_TIME = datetime(2026, 8, 9, 14, 30, 45, tzinfo=timezone.utc)


def test_backup_records_each_internal_phase_metric(
    temporary_database: Database, tmp_path: Path
) -> None:
    performance = PerformanceMonitor()

    result = BackupService(temporary_database, performance_monitor=performance).create_backup(
        tmp_path
    )

    assert result.success
    statistics = performance.statistics()
    assert statistics["backup.database.snapshot"].count == 1
    assert statistics["backup.archive.create"].count == 1
    assert statistics["backup.integrity_check"].count == 1


def test_live_wal_database_is_backed_up_consistently(
    temporary_database: Database, tmp_path: Path
) -> None:
    with temporary_database.connect() as connection:
        connection.execute("CREATE TABLE backup_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO backup_probe VALUES ('committed')")

    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(
        tmp_path / "backups"
    )

    assert result.success
    assert result.state is BackupOperationState.COMPLETED
    assert result.backup_path is not None
    assert result.backup_path.name == "deckrelay-backup-2026-08-09-143045.partyplayer-backup"
    validation = validate_backup_archive(result.backup_path)
    assert validation.valid
    with ZipFile(result.backup_path) as archive:
        extracted = tmp_path / "snapshot.db"
        extracted.write_bytes(archive.read(DATABASE_ARCHIVE_PATH))
    with sqlite3.connect(extracted) as connection:
        assert connection.execute("SELECT value FROM backup_probe").fetchone() == ("committed",)
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_backup_manifest_contains_size_checksum_and_schema(
    temporary_database: Database, tmp_path: Path
) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None

    with ZipFile(result.backup_path) as archive:
        database_bytes = archive.read(DATABASE_ARCHIVE_PATH)
        manifest = json.loads(archive.read(MANIFEST_ARCHIVE_PATH))

    assert manifest["format_version"] == 1
    assert manifest["product_name"] == "DeckRelay"
    assert manifest["product_slug"] == "deckrelay"
    assert manifest["database_schema_version"] == 41
    assert manifest["included_sections"] == ["database"]
    assert BACKUP_MANIFEST_JSON_SCHEMA["additionalProperties"] is False
    assert manifest["files"] == [
        {
            "path": DATABASE_ARCHIVE_PATH,
            "size": len(database_bytes),
            "sha256": sha256(database_bytes).hexdigest(),
        }
    ]


def test_destination_name_is_collision_safe(temporary_database: Database, tmp_path: Path) -> None:
    service = BackupService(temporary_database, now=lambda: FIXED_TIME)

    first = service.create_backup(tmp_path)
    second = service.create_backup(tmp_path)

    assert first.backup_path is not None and second.backup_path is not None
    assert second.backup_path.name.endswith("-1.partyplayer-backup")
    assert first.backup_path != second.backup_path
    assert first.purpose is BackupPurpose.MANUAL


def test_restore_preparation_marks_automatic_backup_as_safety(
    temporary_database: Database, tmp_path: Path
) -> None:
    service = BackupService(temporary_database, now=lambda: FIXED_TIME)

    result = service.create_backup(tmp_path, purpose=BackupPurpose.SAFETY)

    assert result.success
    assert result.purpose is BackupPurpose.SAFETY
    assert result.backup_path is not None
    assert result.backup_path.name.startswith("deckrelay-safety-backup-")


def test_safety_retention_removes_only_old_exact_safety_names(
    temporary_database: Database, tmp_path: Path
) -> None:
    target = tmp_path / "retention"
    target.mkdir()
    manual = target / "partyplayer-backup-2026-08-09-143045.partyplayer-backup"
    manual.write_bytes(b"manual-must-stay")
    similar = target / "partyplayer-safety-backup-important.partyplayer-backup"
    similar.write_bytes(b"not-owned-by-retention")
    service = BackupService(
        temporary_database,
        now=lambda: FIXED_TIME,
        safety_retention_limit=2,
    )

    results = [service.create_backup(target, purpose=BackupPurpose.SAFETY) for _index in range(4)]

    safety_archives = sorted(target.glob("deckrelay-safety-backup-*.partyplayer-backup"))
    assert len([path for path in safety_archives if path != similar]) == 2
    assert manual.read_bytes() == b"manual-must-stay"
    assert similar.read_bytes() == b"not-owned-by-retention"
    assert all(result.success for result in results)
    assert results[-1].retention_removed
    assert not results[-1].retention_warning


def test_manual_backup_never_applies_safety_retention(
    temporary_database: Database, tmp_path: Path
) -> None:
    target = tmp_path / "manual-no-retention"
    target.mkdir()
    old_safety = target / "partyplayer-safety-backup-2026-01-01-000000.partyplayer-backup"
    old_safety.write_bytes(b"old")

    result = BackupService(
        temporary_database,
        now=lambda: FIXED_TIME,
        safety_retention_limit=1,
    ).create_backup(target)

    assert result.success
    assert old_safety.read_bytes() == b"old"
    assert result.retention_removed == ()


def test_safety_retention_runs_only_after_successful_atomic_publish(
    temporary_database: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = tmp_path / "publish-before-retention"
    target.mkdir()
    existing = tuple(
        target / f"partyplayer-safety-backup-2026-01-0{day}-000000.partyplayer-backup"
        for day in (1, 2)
    )
    for archive in existing:
        archive.write_bytes(b"existing")

    def fail_publish(_source: Path, _destination: Path) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr("party_player.backup_service.os.replace", fail_publish)

    result = BackupService(
        temporary_database,
        now=lambda: FIXED_TIME,
        safety_retention_limit=1,
    ).create_backup(target, purpose=BackupPurpose.SAFETY)

    assert result.error_code is BackupErrorCode.ARCHIVE_WRITE_FAILED
    assert all(archive.read_bytes() == b"existing" for archive in existing)


def test_safety_retention_never_deletes_just_published_backup_for_future_mtime(
    temporary_database: Database, tmp_path: Path
) -> None:
    target = tmp_path / "future-mtime"
    target.mkdir()
    future = target / "partyplayer-safety-backup-2099-01-01-000000.partyplayer-backup"
    future.write_bytes(b"future timestamp")
    os.utime(future, (4_102_444_800, 4_102_444_800))

    result = BackupService(
        temporary_database,
        now=lambda: FIXED_TIME,
        safety_retention_limit=1,
    ).create_backup(target, purpose=BackupPurpose.SAFETY)

    assert result.success
    assert result.backup_path is not None and result.backup_path.exists()
    assert not future.exists()


@pytest.mark.parametrize("limit", [0, 1001])
def test_invalid_safety_retention_limit_is_rejected(
    temporary_database: Database, limit: int
) -> None:
    with pytest.raises(ValueError, match="Retention"):
        BackupService(temporary_database, safety_retention_limit=limit)


def test_checksum_tampering_is_rejected(temporary_database: Database, tmp_path: Path) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    with ZipFile(result.backup_path) as source:
        manifest = source.read(MANIFEST_ARCHIVE_PATH)
    tampered = tmp_path / "tampered.partyplayer-backup"
    with ZipFile(tampered, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(DATABASE_ARCHIVE_PATH, b"not the database")
        archive.writestr(MANIFEST_ARCHIVE_PATH, manifest)

    validation = validate_backup_archive(tampered)

    assert not validation.valid
    assert validation.error_code is BackupErrorCode.CHECKSUM_MISMATCH


def test_unexpected_archive_entry_is_rejected(temporary_database: Database, tmp_path: Path) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    with ZipFile(result.backup_path, "a") as archive:
        archive.writestr("../outside.txt", "unsafe")

    validation = validate_backup_archive(result.backup_path)

    assert not validation.valid
    assert validation.error_code is BackupErrorCode.MANIFEST_INVALID


def test_missing_source_does_not_publish_archive(tmp_path: Path) -> None:
    target = tmp_path / "backups"
    result = BackupService(Database(tmp_path / "missing.db"), now=lambda: FIXED_TIME).create_backup(
        target
    )

    assert result.error_code is BackupErrorCode.SOURCE_DATABASE_MISSING
    assert not list(target.glob("*.partyplayer-backup")) if target.exists() else True


def test_backup_preflight_rejects_insufficient_space_without_snapshot(
    temporary_database: Database, tmp_path: Path
) -> None:
    service = BackupService(temporary_database, free_space=lambda _path: 1024)

    result = service.create_backup(tmp_path / "too-small")

    assert result.error_code is BackupErrorCode.INSUFFICIENT_SPACE
    assert not list((tmp_path / "too-small").glob("*.partyplayer-backup"))
    assert not list((tmp_path / "too-small").glob(".*.tmp"))


def test_backup_preflight_rejects_unwritable_target_with_stable_code(
    temporary_database: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = tmp_path / "unwritable"
    original_open = Path.open

    def reject_probe(path: Path, *args: object, **kwargs: object):
        if path.name.startswith(".deckrelay-write-probe-"):
            raise PermissionError(13, "denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_probe)

    result = BackupService(temporary_database).create_backup(target)

    assert result.error_code is BackupErrorCode.TARGET_NOT_WRITABLE
    assert not list(target.iterdir())


def test_backup_rechecks_remaining_space_after_snapshot(
    temporary_database: Database, tmp_path: Path
) -> None:
    observed_free_space = iter((100 * 1024 * 1024, 0))
    target = tmp_path / "space-changed"
    service = BackupService(
        temporary_database,
        free_space=lambda _path: next(observed_free_space),
    )

    result = service.create_backup(target)

    assert result.error_code is BackupErrorCode.INSUFFICIENT_SPACE
    assert "Nach dem Snapshot" in result.message
    assert not list(target.glob("*.partyplayer-backup"))
    assert not list(target.glob(".*.tmp"))


def _rewrite_manifest(source: Path, destination: Path, **updates: object) -> None:
    with ZipFile(source) as archive:
        database = archive.read(DATABASE_ARCHIVE_PATH)
        manifest = json.loads(archive.read(MANIFEST_ARCHIVE_PATH))
    manifest.update(updates)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(DATABASE_ARCHIVE_PATH, database)
        archive.writestr(MANIFEST_ARCHIVE_PATH, json.dumps(manifest))


def _create_older_schema_backup(source: Path, destination: Path, work: Path) -> None:
    with ZipFile(source) as archive:
        database_bytes = archive.read(DATABASE_ARCHIVE_PATH)
        manifest = json.loads(archive.read(MANIFEST_ARCHIVE_PATH))
    database_path = work / "older-schema.db"
    database_path.write_bytes(database_bytes)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("UPDATE schema_version SET version = 33")
        connection.commit()
    finally:
        connection.close()
    older_database = database_path.read_bytes()
    manifest["database_schema_version"] = 33
    manifest["files"][0]["size"] = len(older_database)
    manifest["files"][0]["sha256"] = sha256(older_database).hexdigest()
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(DATABASE_ARCHIVE_PATH, older_database)
        archive.writestr(MANIFEST_ARCHIVE_PATH, json.dumps(manifest))


def test_older_application_backup_requires_migration(
    temporary_database: Database, tmp_path: Path
) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None

    validation = validate_backup_archive(result.backup_path, current_application_version="2.0.0")

    assert validation.valid
    assert validation.compatibility is BackupCompatibility.MIGRATION_REQUIRED


def test_newer_application_backup_is_rejected(temporary_database: Database, tmp_path: Path) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None

    validation = validate_backup_archive(result.backup_path, current_application_version="0.9.0")

    assert not validation.valid
    assert validation.error_code is BackupErrorCode.APPLICATION_VERSION_TOO_NEW


@pytest.mark.parametrize(
    ("format_version", "expected"),
    [
        (0, BackupErrorCode.FORMAT_VERSION_UNSUPPORTED),
        (2, BackupErrorCode.FORMAT_VERSION_TOO_NEW),
    ],
)
def test_unknown_format_versions_have_stable_errors(
    temporary_database: Database,
    tmp_path: Path,
    format_version: int,
    expected: BackupErrorCode,
) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    changed = tmp_path / f"format-{format_version}.partyplayer-backup"
    _rewrite_manifest(result.backup_path, changed, format_version=format_version)

    validation = validate_backup_archive(changed)

    assert validation.error_code is expected


def test_corrupted_zip_and_symlink_are_rejected(
    temporary_database: Database, tmp_path: Path
) -> None:
    corrupt = tmp_path / "corrupt.partyplayer-backup"
    corrupt.write_bytes(b"not a zip")
    assert validate_backup_archive(corrupt).error_code is BackupErrorCode.MANIFEST_INVALID

    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    symlink_archive = tmp_path / "symlink.partyplayer-backup"
    with ZipFile(result.backup_path) as source, ZipFile(symlink_archive, "w") as target:
        manifest_info = ZipInfo(MANIFEST_ARCHIVE_PATH)
        manifest_info.create_system = 3
        manifest_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        target.writestr(manifest_info, source.read(MANIFEST_ARCHIVE_PATH))
        target.writestr(DATABASE_ARCHIVE_PATH, source.read(DATABASE_ARCHIVE_PATH))

    validation = validate_backup_archive(symlink_archive)

    assert validation.error_code is BackupErrorCode.MANIFEST_INVALID


def test_atomic_publish_failure_leaves_no_backup_or_temp_file(
    temporary_database: Database, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    def fail_replace(_source: Path, _destination: Path) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr("party_player.backup_service.os.replace", fail_replace)

    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)

    assert result.error_code is BackupErrorCode.ARCHIVE_WRITE_FAILED
    assert not list(tmp_path.glob("*.partyplayer-backup"))
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("updates", "current_schema", "expected"),
    [
        ({"created_at": "2026-08-09T14:30:45"}, 39, BackupErrorCode.MANIFEST_INVALID),
        ({"database_schema_version": -1}, 39, BackupErrorCode.MANIFEST_INVALID),
        ({"included_sections": ["database", "database"]}, 39, BackupErrorCode.MANIFEST_INVALID),
        ({"database_schema_version": 40}, 39, BackupErrorCode.SCHEMA_VERSION_TOO_NEW),
    ],
)
def test_manifest_field_contract_is_enforced(
    temporary_database: Database,
    tmp_path: Path,
    updates: dict[str, object],
    current_schema: int,
    expected: BackupErrorCode,
) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    changed = tmp_path / f"changed-{len(list(tmp_path.iterdir()))}.partyplayer-backup"
    _rewrite_manifest(result.backup_path, changed, **updates)

    validation = validate_backup_archive(changed, current_schema_version=current_schema)

    assert validation.error_code is expected


def test_extreme_compression_ratio_is_rejected(
    temporary_database: Database, tmp_path: Path
) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    with ZipFile(result.backup_path) as source:
        manifest = source.read(MANIFEST_ARCHIVE_PATH)
    bomb = tmp_path / "compression-bomb.partyplayer-backup"
    with ZipFile(bomb, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(DATABASE_ARCHIVE_PATH, b"0" * (1024 * 1024))
        archive.writestr(MANIFEST_ARCHIVE_PATH, manifest)

    validation = validate_backup_archive(bomb)

    assert validation.error_code is BackupErrorCode.MANIFEST_INVALID


def test_restore_validator_accepts_valid_backup_without_changing_active_database(
    temporary_database: Database, tmp_path: Path
) -> None:
    with temporary_database.connect() as connection:
        connection.execute("CREATE TABLE restore_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO restore_probe VALUES ('active')")
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    active_before = temporary_database.path.read_bytes()

    validation = RestoreValidator().validate(result.backup_path)

    assert validation.success
    assert validation.state is BackupOperationState.COMPLETED
    assert validation.compatibility is BackupCompatibility.EXACT
    assert not validation.migration_performed
    assert validation.prepared_schema_version == 41
    assert temporary_database.path.read_bytes() == active_before


def test_restore_validator_rejects_database_schema_mismatch(
    temporary_database: Database, tmp_path: Path
) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    changed = tmp_path / "schema-mismatch.partyplayer-backup"
    _rewrite_manifest(result.backup_path, changed, database_schema_version=33)

    validation = RestoreValidator().validate(changed)

    assert not validation.success
    assert validation.error_code is BackupErrorCode.RESTORE_SCHEMA_MISMATCH


def test_restore_validator_rejects_non_sqlite_payload_with_matching_checksum(
    temporary_database: Database, tmp_path: Path
) -> None:
    result = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert result.backup_path is not None
    with ZipFile(result.backup_path) as source:
        manifest = json.loads(source.read(MANIFEST_ARCHIVE_PATH))
    payload = b"not a sqlite database"
    manifest["files"][0]["size"] = len(payload)
    manifest["files"][0]["sha256"] = sha256(payload).hexdigest()
    changed = tmp_path / "invalid-database.partyplayer-backup"
    with ZipFile(changed, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(DATABASE_ARCHIVE_PATH, payload)
        archive.writestr(MANIFEST_ARCHIVE_PATH, json.dumps(manifest))

    validation = RestoreValidator().validate(changed)

    assert validation.error_code is BackupErrorCode.RESTORE_DATABASE_INVALID


def test_restore_validator_rejects_missing_archive(tmp_path: Path) -> None:
    validation = RestoreValidator().validate(tmp_path / "missing.partyplayer-backup")

    assert validation.error_code is BackupErrorCode.RESTORE_ARCHIVE_MISSING


def test_restore_validator_migrates_only_a_second_temporary_copy(
    temporary_database: Database, tmp_path: Path
) -> None:
    backup = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert backup.backup_path is not None
    older = tmp_path / "older.partyplayer-backup"
    _create_older_schema_backup(backup.backup_path, older, tmp_path)
    archive_before = older.read_bytes()
    active_before = temporary_database.path.read_bytes()
    performance = PerformanceMonitor()

    validation = RestoreValidator(performance_monitor=performance).validate(older)

    assert validation.success
    assert validation.compatibility is BackupCompatibility.MIGRATION_REQUIRED
    assert validation.migration_performed
    assert validation.prepared_schema_version == 41
    assert older.read_bytes() == archive_before
    assert temporary_database.path.read_bytes() == active_before
    assert performance.statistics()["restore.migration"].count == 1


def test_restore_validator_reports_incomplete_temporary_migration(
    temporary_database: Database, tmp_path: Path
) -> None:
    backup = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert backup.backup_path is not None
    older = tmp_path / "older-incomplete.partyplayer-backup"
    _create_older_schema_backup(backup.backup_path, older, tmp_path)

    validation = RestoreValidator(migrator=lambda _database: None).validate(older)

    assert not validation.success
    assert validation.error_code is BackupErrorCode.RESTORE_MIGRATION_INCOMPLETE


def test_restore_validator_isolates_migration_failure(
    temporary_database: Database, tmp_path: Path
) -> None:
    backup = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert backup.backup_path is not None
    older = tmp_path / "older-failing.partyplayer-backup"
    _create_older_schema_backup(backup.backup_path, older, tmp_path)

    def fail_migration(database: Database) -> None:
        with database.connect() as connection:
            connection.execute("CREATE TABLE migration_was_temporary (id INTEGER)")
        raise RuntimeError("simulated")

    active_before = temporary_database.path.read_bytes()
    validation = RestoreValidator(migrator=fail_migration).validate(older)

    assert validation.error_code is BackupErrorCode.RESTORE_MIGRATION_FAILED
    assert temporary_database.path.read_bytes() == active_before


def test_restore_preparation_creates_and_validates_current_safety_backup(
    temporary_database: Database, tmp_path: Path
) -> None:
    candidate = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(
        tmp_path / "candidates"
    )
    assert candidate.backup_path is not None
    with temporary_database.connect() as connection:
        connection.execute("CREATE TABLE safety_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO safety_probe VALUES ('current state')")
    active_before = temporary_database.path.read_bytes()
    service = RestorePreparationService(
        RestoreValidator(),
        BackupService(temporary_database, now=lambda: FIXED_TIME),
    )

    preparation = service.prepare(candidate.backup_path, tmp_path / "safety")

    assert preparation.success
    assert preparation.safety_backup_path is not None
    assert preparation.candidate_sha256 == sha256(candidate.backup_path.read_bytes()).hexdigest()
    assert validate_backup_archive(preparation.safety_backup_path).valid
    with ZipFile(preparation.safety_backup_path) as archive:
        safety_database = tmp_path / "safety.db"
        safety_database.write_bytes(archive.read(DATABASE_ARCHIVE_PATH))
    connection = sqlite3.connect(safety_database)
    try:
        assert connection.execute("SELECT value FROM safety_probe").fetchone() == ("current state",)
    finally:
        connection.close()
    assert temporary_database.path.read_bytes() == active_before


def test_restore_preparation_blocks_when_candidate_is_invalid(
    temporary_database: Database, tmp_path: Path
) -> None:
    invalid = tmp_path / "invalid.partyplayer-backup"
    invalid.write_bytes(b"invalid")
    safety_directory = tmp_path / "safety"
    service = RestorePreparationService(RestoreValidator(), BackupService(temporary_database))

    preparation = service.prepare(invalid, safety_directory)

    assert not preparation.success
    assert preparation.error_code is BackupErrorCode.MANIFEST_INVALID
    assert not safety_directory.exists()


def test_restore_preparation_blocks_when_safety_backup_fails(
    temporary_database: Database, tmp_path: Path
) -> None:
    candidate = BackupService(temporary_database, now=lambda: FIXED_TIME).create_backup(tmp_path)
    assert candidate.backup_path is not None
    missing_active_database = Database(tmp_path / "missing-current.db")
    service = RestorePreparationService(RestoreValidator(), BackupService(missing_active_database))

    preparation = service.prepare(candidate.backup_path, tmp_path / "safety")

    assert not preparation.success
    assert preparation.error_code is BackupErrorCode.RESTORE_SAFETY_BACKUP_FAILED
    assert preparation.safety_backup_path is None
    assert preparation.candidate_sha256
