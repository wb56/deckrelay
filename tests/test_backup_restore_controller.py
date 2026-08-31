from pathlib import Path
import logging
from threading import Event, get_ident

from party_player.backup_restore_controller import (
    BackupRestoreController,
    BackupRestoreOperation,
    BackupRestoreUiState,
)
from party_player.backup_service import BackupErrorCode, BackupOperationState, BackupResult
from party_player.backup_service import BackupService, validate_backup_archive
from party_player.database.connection import Database
from party_player.restore_pipeline import RestorePipelineErrorCode, RestorePipelineResult
from party_player.database_maintenance import DatabaseMaintenanceService
from party_player.equalizer import EqualizerPreset
from party_player.equalizer_transfer import (
    EqualizerConflictStrategy,
    EqualizerImportPreview,
    EqualizerTransferErrorCode,
    EqualizerTransferResult,
)
from party_player.performance_monitor import PerformanceMonitor
from party_player.playlist_transfer import (
    PlaylistConflictStrategy,
    PlaylistImportPreview,
    PlaylistTransferErrorCode,
    PlaylistTransferFormat,
    PlaylistTransferResult,
)
from party_player.media_path_remap import (
    MediaPathRemapErrorCode,
    MediaPathRemapPreview,
    MediaPathRemapResult,
)


class RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def operation_logger() -> tuple[logging.Logger, RecordHandler]:
    logger = logging.Logger("party_player.tests.data_operations")
    handler = RecordHandler()
    logger.addHandler(handler)
    return logger, handler


class BackupStub:
    def __init__(self, result: BackupResult) -> None:
        self.result = result
        self.worker_thread = 0

    def create_backup(self, destination: Path) -> BackupResult:
        self.worker_thread = get_ident()
        return self.result


class RestoreStub:
    def __init__(self, result: RestorePipelineResult) -> None:
        self.result = result

    def execute(self, archive: Path, safety: Path) -> RestorePipelineResult:
        return self.result


class PlaylistTransferStub:
    def __init__(self, preview: PlaylistImportPreview) -> None:
        self.preview = preview
        self.import_calls: list[tuple[PlaylistImportPreview, PlaylistConflictStrategy]] = []

    def preview_import(
        self, _source: Path, _format: PlaylistTransferFormat
    ) -> PlaylistImportPreview:
        return self.preview

    def import_preview(
        self, preview: PlaylistImportPreview, conflict: PlaylistConflictStrategy
    ) -> PlaylistTransferResult:
        self.import_calls.append((preview, conflict))
        return PlaylistTransferResult(
            True, PlaylistTransferErrorCode.NONE, "Playlist wurde importiert."
        )

    def export(
        self, _saved_queue_id: int, destination: Path, _format: PlaylistTransferFormat
    ) -> PlaylistTransferResult:
        return PlaylistTransferResult(
            True,
            PlaylistTransferErrorCode.NONE,
            "Playlist wurde exportiert.",
            path=destination,
        )


class MediaPathRemapStub:
    def __init__(self, preview: MediaPathRemapPreview) -> None:
        self.preview_result = preview
        self.commit_calls: list[MediaPathRemapPreview] = []

    def preview(self, _old_base: str, _new_base: str) -> MediaPathRemapPreview:
        return self.preview_result

    def commit(self, preview: MediaPathRemapPreview) -> MediaPathRemapResult:
        self.commit_calls.append(preview)
        return MediaPathRemapResult(
            True, MediaPathRemapErrorCode.NONE, "Pfade geändert.", preview.affected_count
        )


class EqualizerTransferStub:
    def __init__(self, preview: EqualizerImportPreview) -> None:
        self.preview = preview
        self.import_calls: list[tuple[EqualizerImportPreview, EqualizerConflictStrategy]] = []

    def preview_import(self, _source: Path) -> EqualizerImportPreview:
        return self.preview

    def import_preview(
        self, preview: EqualizerImportPreview, strategy: EqualizerConflictStrategy
    ) -> EqualizerTransferResult:
        self.import_calls.append((preview, strategy))
        return EqualizerTransferResult(
            True, EqualizerTransferErrorCode.NONE, "Equalizer-Preset wurde importiert."
        )

    def export(self, _preset_key: str, destination: Path) -> EqualizerTransferResult:
        return EqualizerTransferResult(
            True,
            EqualizerTransferErrorCode.NONE,
            "Equalizer-Preset wurde exportiert.",
            path=destination,
        )


def test_backup_runs_off_caller_thread_and_publishes_through_scheduler(tmp_path: Path) -> None:
    published = []
    scheduled = []
    ready = Event()
    backup = BackupStub(
        BackupResult(
            True,
            BackupOperationState.COMPLETED,
            BackupErrorCode.NONE,
            "Backup erstellt.",
            tmp_path / "result.partyplayer-backup",
        )
    )
    controller = BackupRestoreController(
        backup,  # type: ignore[arg-type]
        None,
        lambda _delay, callback: (scheduled.append(callback), ready.set()),
        published.append,
    )

    assert controller.start_backup(tmp_path)
    assert ready.wait(2)
    scheduled.pop()()

    assert backup.worker_thread != get_ident()
    assert published[0].state is BackupRestoreUiState.COMPLETED
    controller.close()


def test_successful_restore_reports_only_restart_required(tmp_path: Path) -> None:
    published = []
    ready = Event()
    safety = tmp_path / "safety.partyplayer-backup"
    logger, handler = operation_logger()
    restore = RestoreStub(
        RestorePipelineResult(
            True,
            BackupOperationState.COMPLETED,
            RestorePipelineErrorCode.NONE,
            "Restore abgeschlossen. Neustart erforderlich.",
            safety_backup_path=safety,
            database_schema_version=39,
        )
    )
    controller = BackupRestoreController(
        BackupStub(BackupResult(False, BackupOperationState.FAILED, BackupErrorCode.SNAPSHOT_FAILED, "x")),  # type: ignore[arg-type]
        restore,  # type: ignore[arg-type]
        lambda _delay, callback: (callback(), ready.set()),
        published.append,
        logger=logger,
    )

    assert controller.start_restore(tmp_path / "candidate", tmp_path)
    assert ready.wait(2)

    assert published[0].operation is BackupRestoreOperation.RESTORE
    assert published[0].state is BackupRestoreUiState.RESTART_REQUIRED
    assert published[0].path == safety
    completed = next(
        record for record in handler.records if record.event == "data_operation_completed"
    )
    assert completed.operation_type == "RESTORE"
    assert completed.operation_detail == "RESTORE"
    assert completed.operation_result == "RESTART_REQUIRED"
    assert completed.error_code == "none"
    assert completed.schema_version == 39
    assert completed.duration_ms >= 0
    assert str(tmp_path / "candidate") not in str(completed.__dict__)
    controller.close()


def test_restore_without_runtime_fails_without_background_work(tmp_path: Path) -> None:
    published = []
    controller = BackupRestoreController(
        BackupStub(BackupResult(False, BackupOperationState.FAILED, BackupErrorCode.SNAPSHOT_FAILED, "x")),  # type: ignore[arg-type]
        None,
        lambda _delay, callback: callback(),
        published.append,
        restore_unavailable_reason="Lifecycle fehlt.",
    )

    assert not controller.start_restore(tmp_path / "candidate", tmp_path)
    assert published[0].state is BackupRestoreUiState.FAILED
    assert published[0].message == "Lifecycle fehlt."
    controller.close()


def test_manual_backup_path_creates_and_validates_real_archive(
    temporary_database: Database, tmp_path: Path
) -> None:
    with temporary_database.connect() as connection:
        connection.execute("CREATE TABLE manual_backup_acceptance (value TEXT NOT NULL)")
        connection.execute("INSERT INTO manual_backup_acceptance VALUES ('accepted')")
    callbacks = []
    ready = Event()
    published = []
    recorded: list[tuple[str, str]] = []
    performance = PerformanceMonitor()
    logger, handler = operation_logger()
    controller = BackupRestoreController(
        BackupService(temporary_database),
        None,
        lambda _delay, callback: (callbacks.append(callback), ready.set()),
        published.append,
        manual_backup_recorded=lambda created_at, path: recorded.append((created_at, path)),
        performance_monitor=performance,
        logger=logger,
    )

    assert controller.start_backup(tmp_path / "manual-backups")
    assert ready.wait(5)
    callbacks.pop()()

    result = published[0]
    assert result.state is BackupRestoreUiState.COMPLETED
    assert result.path is not None and result.path.exists()
    assert validate_backup_archive(result.path).valid
    assert result.created_at
    assert recorded == [(result.created_at, str(result.path))]
    assert controller.last_manual_backup() == recorded[0]
    assert "last_data_operation: BACKUP" in controller.diagnostic_status()
    assert performance.counters()["backup_created_total"] == 1
    assert performance.statistics()["backup.create.total"].count == 1
    events = [record.event for record in handler.records]
    assert events == ["data_operation_started", "data_operation_completed"]
    completed_log = handler.records[1]
    assert completed_log.operation_type == "BACKUP"
    assert completed_log.operation_detail == "CREATE"
    assert completed_log.operation_result == "COMPLETED"
    assert completed_log.error_code == "none"
    assert completed_log.backup_target == str(tmp_path / "manual-backups")
    assert completed_log.schema_version == 41
    assert completed_log.started_at
    assert completed_log.finished_at
    controller.close()


def test_parallel_manual_operation_is_reported_as_busy(tmp_path: Path) -> None:
    entered = Event()
    release = Event()
    callbacks = []
    published = []

    class BlockingBackup(BackupStub):
        def create_backup(self, destination: Path) -> BackupResult:
            entered.set()
            assert release.wait(2)
            return self.result

    backup = BlockingBackup(
        BackupResult(
            True,
            BackupOperationState.COMPLETED,
            BackupErrorCode.NONE,
            "Backup erstellt.",
            tmp_path / "result.partyplayer-backup",
        )
    )
    controller = BackupRestoreController(
        backup,  # type: ignore[arg-type]
        None,
        lambda _delay, callback: callbacks.append(callback),
        published.append,
    )

    assert controller.start_backup(tmp_path)
    assert entered.wait(2)
    assert not controller.start_backup(tmp_path)
    callbacks.pop()()
    assert published[0].state is BackupRestoreUiState.BUSY
    assert published[0].error_code == "BACKUP_RESTORE_BUSY"
    release.set()
    controller.close()


def test_manual_maintenance_runs_on_shared_background_worker(
    temporary_database: Database,
) -> None:
    callbacks = []
    published = []
    ready = Event()
    caller_thread = get_ident()
    maintenance_threads: list[int] = []
    logger, handler = operation_logger()

    class MaintenanceSpy(DatabaseMaintenanceService):
        def quick_check(self):
            maintenance_threads.append(get_ident())
            return super().quick_check()

    controller = BackupRestoreController(
        BackupService(temporary_database),
        None,
        lambda _delay, callback: (callbacks.append(callback), ready.set()),
        published.append,
        maintenance_service=MaintenanceSpy(temporary_database.path),
        logger=logger,
    )

    assert controller.start_quick_check()
    assert ready.wait(2)
    callbacks.pop()()

    assert maintenance_threads and maintenance_threads[0] != caller_thread
    assert published[0].operation is BackupRestoreOperation.MAINTENANCE
    assert published[0].state is BackupRestoreUiState.COMPLETED
    completed_log = handler.records[1]
    assert completed_log.operation_type == "MAINTENANCE"
    assert completed_log.operation_detail == "QUICK_CHECK"
    assert completed_log.schema_version == 41
    assert completed_log.backup_target == "none"
    controller.close()


def test_unexpected_backup_failure_is_timed_and_counted(tmp_path: Path) -> None:
    class ExplodingBackup(BackupStub):
        def create_backup(self, destination: Path) -> BackupResult:
            raise RuntimeError("boom")

    callbacks = []
    ready = Event()
    published = []
    performance = PerformanceMonitor()
    logger, handler = operation_logger()
    controller = BackupRestoreController(
        ExplodingBackup(
            BackupResult(
                False,
                BackupOperationState.FAILED,
                BackupErrorCode.SNAPSHOT_FAILED,
                "unused",
            )
        ),  # type: ignore[arg-type]
        None,
        lambda _delay, callback: (callbacks.append(callback), ready.set()),
        published.append,
        performance_monitor=performance,
        logger=logger,
    )

    assert controller.start_backup(tmp_path)
    assert ready.wait(2)
    callbacks.pop()()

    assert published[0].error_code == "BACKUP_RESTORE_UNEXPECTED_ERROR"
    assert performance.counters()["backup_failed_total"] == 1
    assert performance.statistics()["backup.create.total"].count == 1
    completed_log = handler.records[1]
    assert completed_log.operation_result == "FAILED"
    assert completed_log.error_code == "BACKUP_RESTORE_UNEXPECTED_ERROR"
    assert "boom" not in completed_log.getMessage()
    controller.close()


def test_playlist_preview_and_confirmed_import_use_dispatcher_and_shared_worker(
    tmp_path: Path,
) -> None:
    source = tmp_path / "playlist.json"
    preview = PlaylistImportPreview(
        True,
        True,
        PlaylistTransferErrorCode.NONE,
        "Playlist kann importiert werden.",
        source,
        PlaylistTransferFormat.JSON,
        source_sha256="digest",
        name="Set",
        entry_count=2,
        duplicate_count=1,
        name_conflict=True,
    )
    transfer = PlaylistTransferStub(preview)
    callbacks = []
    published = []
    ready = Event()
    performance = PerformanceMonitor()
    controller = BackupRestoreController(
        BackupStub(
            BackupResult(
                False,
                BackupOperationState.FAILED,
                BackupErrorCode.SNAPSHOT_FAILED,
                "unused",
            )
        ),  # type: ignore[arg-type]
        None,
        lambda _delay, callback: (callbacks.append(callback), ready.set()),
        published.append,
        playlist_transfer_service=transfer,  # type: ignore[arg-type]
        performance_monitor=performance,
    )

    assert controller.start_playlist_import_preview(source, PlaylistTransferFormat.JSON)
    assert ready.wait(2)
    callbacks.pop(0)()
    result = published.pop(0)
    assert result.operation is BackupRestoreOperation.PLAYLIST_IMPORT_PREVIEW
    assert result.playlist_preview is preview
    assert result.findings == (
        "Einträge: 2",
        "Duplikate: 1",
        "Unbekannte Pfade: 0",
        "Namenskonflikt: ja",
    )

    ready.clear()
    assert controller.start_playlist_import(preview, PlaylistConflictStrategy.RENAME)
    assert ready.wait(2)
    callbacks.pop(0)()
    result = published.pop(0)
    assert result.operation is BackupRestoreOperation.PLAYLIST_IMPORT
    assert result.state is BackupRestoreUiState.COMPLETED
    assert transfer.import_calls == [(preview, PlaylistConflictStrategy.RENAME)]
    assert performance.statistics()["playlist.import.preview"].count == 1
    assert performance.statistics()["playlist.import.total"].count == 1
    controller.close()


def test_running_playlist_export_blocks_backup_on_same_busy_boundary(tmp_path: Path) -> None:
    entered = Event()
    release = Event()
    scheduled = []
    published = []
    preview = PlaylistImportPreview(
        True,
        True,
        PlaylistTransferErrorCode.NONE,
        "ok",
        tmp_path / "source.json",
        PlaylistTransferFormat.JSON,
    )

    class BlockingTransfer(PlaylistTransferStub):
        def export(
            self, _saved_queue_id: int, destination: Path, _format: PlaylistTransferFormat
        ) -> PlaylistTransferResult:
            entered.set()
            assert release.wait(2)
            return super().export(_saved_queue_id, destination, _format)

    controller = BackupRestoreController(
        BackupStub(
            BackupResult(
                True,
                BackupOperationState.COMPLETED,
                BackupErrorCode.NONE,
                "Backup erstellt.",
            )
        ),  # type: ignore[arg-type]
        None,
        lambda _delay, callback: scheduled.append(callback),
        published.append,
        playlist_transfer_service=BlockingTransfer(preview),  # type: ignore[arg-type]
    )

    assert controller.start_playlist_export(
        1, tmp_path / "playlist.json", PlaylistTransferFormat.JSON
    )
    assert entered.wait(2)
    assert not controller.start_backup(tmp_path / "backup")
    scheduled.pop(0)()
    assert published[0].state is BackupRestoreUiState.BUSY
    release.set()
    controller.close()


def test_media_path_preview_and_commit_use_exact_preview_on_shared_dispatcher(
    tmp_path: Path,
) -> None:
    preview = MediaPathRemapPreview(
        True,
        True,
        MediaPathRemapErrorCode.NONE,
        "3 Pfade.",
        r"D:\Musik",
        r"E:\Musik",
        state_token="token",
        track_count=2,
        overlay_count=1,
    )
    remap = MediaPathRemapStub(preview)
    callbacks = []
    published = []
    ready = Event()
    performance = PerformanceMonitor()
    controller = BackupRestoreController(
        BackupStub(
            BackupResult(
                False,
                BackupOperationState.FAILED,
                BackupErrorCode.SNAPSHOT_FAILED,
                "unused",
            )
        ),  # type: ignore[arg-type]
        None,
        lambda _delay, callback: (callbacks.append(callback), ready.set()),
        published.append,
        media_path_remap_service=remap,  # type: ignore[arg-type]
        performance_monitor=performance,
    )

    assert controller.start_media_path_remap_preview(r"D:\Musik", r"E:\Musik")
    assert ready.wait(2)
    callbacks.pop(0)()
    result = published.pop(0)
    assert result.operation is BackupRestoreOperation.MEDIA_PATH_REMAP_PREVIEW
    assert result.media_path_preview is preview
    assert result.findings == (
        "Katalogtitel: 2",
        "Overlays: 1",
        "Notfallhistorie: 0",
        "Kollisionen: 0",
    )

    ready.clear()
    assert controller.start_media_path_remap(preview)
    assert ready.wait(2)
    callbacks.pop(0)()
    result = published.pop(0)
    assert result.operation is BackupRestoreOperation.MEDIA_PATH_REMAP
    assert result.state is BackupRestoreUiState.RESTART_REQUIRED
    assert remap.commit_calls == [preview]
    assert performance.statistics()["media_path.remap.preview"].count == 1
    assert performance.statistics()["media_path.remap.total"].count == 1
    controller.close()


def test_equalizer_preview_and_confirmed_import_use_exact_preview_and_shared_worker(
    tmp_path: Path,
) -> None:
    preset = EqualizerPreset("party", "Party", -2.0, ((60.0, 1.5), (1000.0, -1.0)))
    preview = EqualizerImportPreview(
        True,
        EqualizerTransferErrorCode.NONE,
        "Preset kann importiert werden.",
        tmp_path / "party.json",
        source_sha256="digest",
        preset=preset,
        conflicts=((7, "party", "Party", False),),
        state_token="state",
    )
    transfer = EqualizerTransferStub(preview)
    callbacks = []
    published = []
    ready = Event()
    performance = PerformanceMonitor()
    controller = BackupRestoreController(
        BackupStub(
            BackupResult(
                False,
                BackupOperationState.FAILED,
                BackupErrorCode.SNAPSHOT_FAILED,
                "unused",
            )
        ),  # type: ignore[arg-type]
        None,
        lambda _delay, callback: (callbacks.append(callback), ready.set()),
        published.append,
        equalizer_transfer_service=transfer,  # type: ignore[arg-type]
        performance_monitor=performance,
    )

    assert controller.start_equalizer_import_preview(preview.source)
    assert ready.wait(2)
    callbacks.pop(0)()
    result = published.pop(0)
    assert result.operation is BackupRestoreOperation.EQUALIZER_IMPORT_PREVIEW
    assert result.equalizer_preview is preview
    assert result.findings == (
        "Preset: Party",
        "Bänder: 2",
        "Konflikte: 1",
        "Eingebauter Konflikt: nein",
    )

    ready.clear()
    assert controller.start_equalizer_import(preview, EqualizerConflictStrategy.REPLACE)
    assert ready.wait(2)
    callbacks.pop(0)()
    result = published.pop(0)
    assert result.operation is BackupRestoreOperation.EQUALIZER_IMPORT
    assert result.state is BackupRestoreUiState.COMPLETED
    assert transfer.import_calls == [(preview, EqualizerConflictStrategy.REPLACE)]
    assert performance.statistics()["equalizer.import.preview"].count == 1
    assert performance.statistics()["equalizer.import.total"].count == 1
    controller.close()


def test_running_equalizer_export_blocks_other_data_operation(tmp_path: Path) -> None:
    entered = Event()
    release = Event()
    scheduled = []
    published = []
    preview = EqualizerImportPreview(
        True,
        EqualizerTransferErrorCode.NONE,
        "ok",
        tmp_path / "source.json",
    )

    class BlockingEqualizerTransfer(EqualizerTransferStub):
        def export(self, preset_key: str, destination: Path) -> EqualizerTransferResult:
            entered.set()
            assert release.wait(2)
            return super().export(preset_key, destination)

    controller = BackupRestoreController(
        BackupStub(
            BackupResult(
                True,
                BackupOperationState.COMPLETED,
                BackupErrorCode.NONE,
                "Backup erstellt.",
            )
        ),  # type: ignore[arg-type]
        None,
        lambda _delay, callback: scheduled.append(callback),
        published.append,
        equalizer_transfer_service=BlockingEqualizerTransfer(preview),  # type: ignore[arg-type]
    )

    assert controller.start_equalizer_export("rock", tmp_path / "rock.json")
    assert entered.wait(2)
    assert not controller.start_backup(tmp_path / "backup")
    scheduled.pop(0)()
    assert published[0].state is BackupRestoreUiState.BUSY
    release.set()
    controller.close()
