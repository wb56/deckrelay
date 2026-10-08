"""Coordinate database backup and restore presentation."""

from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, TclError
from typing import Any

from party_player.backup_restore_controller import (
    BackupRestoreController,
    BackupRestoreOperation,
    BackupRestoreUiResult,
    BackupRestoreUiState,
)
from party_player.ui.database_backup_dialog import DatabaseBackupDialog
from party_player.ui.dialogs import ask_silent_yes_no, show_silent_message


class BackupRestoreUiCoordinator:
    """Own the presentation lifecycle for backup and restore operations."""

    def __init__(
        self,
        parent: Any,
        *,
        playlist_export: Callable[[], bool],
        playlist_music_directory: Callable[[], bool],
        playlist_import_preview: Callable[[], bool],
        equalizer_export: Callable[[], bool],
        equalizer_import_preview: Callable[[], bool],
        overlay_export: Callable[[], bool],
        overlay_import_preview: Callable[[], bool],
        media_path_remap_preview: Callable[[], bool],
        vacuum: Callable[[], bool],
        reindex: Callable[[], bool],
        preview_result: Callable[[BackupRestoreUiResult, DatabaseBackupDialog], bool],
        refresh_equalizer_presets: Callable[[], None],
        refresh_overlays: Callable[[], None],
        request_restart: Callable[[], None],
    ) -> None:
        self._parent = parent
        self._playlist_export = playlist_export
        self._playlist_music_directory = playlist_music_directory
        self._playlist_import_preview = playlist_import_preview
        self._equalizer_export = equalizer_export
        self._equalizer_import_preview = equalizer_import_preview
        self._overlay_export = overlay_export
        self._overlay_import_preview = overlay_import_preview
        self._media_path_remap_preview = media_path_remap_preview
        self._vacuum = vacuum
        self._reindex = reindex
        self._preview_result = preview_result
        self._refresh_equalizer_presets = refresh_equalizer_presets
        self._refresh_overlays = refresh_overlays
        self._request_restart = request_restart
        self._controller: BackupRestoreController | None = None
        self._default_backup_directory: Path | None = None
        self._dialog: DatabaseBackupDialog | None = None
        self._dialog_generation = 0
        self._operation_generation: int | None = None

    def bind(self, controller: BackupRestoreController, default_backup_directory: Path) -> None:
        self._controller = controller
        self._default_backup_directory = default_backup_directory

    def show_dialog(self) -> None:
        controller = self._controller
        if controller is None:
            return
        current = self._dialog
        if current is not None:
            try:
                if current.winfo_exists():
                    current.focus_force()
                    return
            except (RuntimeError, TclError):
                pass
        self._dialog_generation += 1
        generation = self._dialog_generation

        def close() -> None:
            if self._dialog_generation == generation:
                self._dialog_generation += 1
                self._dialog = None

        self._dialog = DatabaseBackupDialog(
            self._parent,
            lambda: self.start_operation(self._request_default_backup),
            lambda: self.start_operation(self._request_backup),
            lambda: self.start_operation(self._request_restore),
            lambda: self.start_operation(self._playlist_export),
            self._playlist_music_directory,
            lambda: self.start_operation(self._playlist_import_preview),
            lambda: self.start_operation(self._equalizer_export),
            lambda: self.start_operation(self._equalizer_import_preview),
            lambda: self.start_operation(self._overlay_export),
            lambda: self.start_operation(self._overlay_import_preview),
            lambda: self.start_operation(self._media_path_remap_preview),
            lambda: self.start_operation(controller.start_quick_check),
            lambda: self.start_operation(controller.start_integrity_check),
            lambda: self.start_operation(controller.start_analyze),
            lambda: self.start_operation(self._vacuum),
            lambda: self.start_operation(self._reindex),
            controller.destructive_maintenance_safety,
            controller.last_manual_backup(),
            close,
        )

    def start_operation(self, action: Callable[[], bool]) -> bool:
        started = action()
        if started:
            self._operation_generation = self._dialog_generation
        return started

    def request_backup(self) -> bool:
        return self._request_backup()

    def request_default_backup(self) -> bool:
        return self._request_default_backup()

    def request_restore(self) -> bool:
        return self._request_restore()

    def _request_backup(self) -> bool:
        controller = self._controller
        if controller is None:
            return False
        selected = filedialog.askdirectory(title="Zielordner für DeckRelay-Backup wählen")
        return bool(selected) and controller.start_backup(Path(selected))

    def _request_default_backup(self) -> bool:
        controller = self._controller
        if controller is None or self._default_backup_directory is None:
            return False
        return controller.start_backup(self._default_backup_directory)

    def _request_restore(self) -> bool:
        controller = self._controller
        if controller is None:
            return False
        selected = filedialog.askopenfilename(
            title="DeckRelay-Backup wiederherstellen",
            filetypes=(
                ("DeckRelay-Backup", "*.partyplayer-backup"),
                ("Alle Dateien", "*.*"),
            ),
        )
        if not selected:
            return False
        if not ask_silent_yes_no(
            self._parent,
            "Backup wirklich wiederherstellen?",
            "Beide Decks müssen gestoppt und alle Audioaktionen beendet sein. "
            "Unmittelbar vor dem Austausch wird automatisch ein Sicherheitsbackup erstellt.\n\n"
            "Nach erfolgreichem Restore muss DeckRelay neu gestartet werden.",
        ):
            return False
        archive = Path(selected)
        safety_directory = archive.resolve().parent / "safety-backups"
        return controller.start_restore(archive, safety_directory)

    def show_result(self, result: BackupRestoreUiResult) -> None:
        dialog = self._dialog
        current_dialog = (
            dialog is not None and self._operation_generation == self._dialog_generation
        )
        if current_dialog and dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.complete(result)
            except (RuntimeError, TclError):
                pass
        if result.operation in {
            BackupRestoreOperation.PLAYLIST_IMPORT_PREVIEW,
            BackupRestoreOperation.MEDIA_PATH_REMAP_PREVIEW,
            BackupRestoreOperation.EQUALIZER_IMPORT_PREVIEW,
            BackupRestoreOperation.OVERLAY_IMPORT_PREVIEW,
        }:
            self._operation_generation = None
            if current_dialog and dialog is not None:
                self._preview_result(result, dialog)
            return
        if result.state is not BackupRestoreUiState.BUSY:
            self._operation_generation = None
        if result.state is BackupRestoreUiState.RESTART_REQUIRED:
            restore = result.operation is BackupRestoreOperation.RESTORE
            safety = f"\n\nSicherheitsbackup: {result.path}" if restore and result.path else ""
            title = (
                "Restore abgeschlossen – Neustart erforderlich"
                if restore
                else "Pfad-Neuzuordnung abgeschlossen – Neustart erforderlich"
            )
            if ask_silent_yes_no(
                self._parent,
                title,
                result.message + safety + "\n\nDeckRelay jetzt kontrolliert neu starten?",
            ):
                self._request_restart()
            return
        if result.state is BackupRestoreUiState.COMPLETED:
            title = {
                BackupRestoreOperation.BACKUP: "Sicherung erfolgreich",
                BackupRestoreOperation.MAINTENANCE: "Datenbankwartung abgeschlossen",
                BackupRestoreOperation.PLAYLIST_EXPORT: "Playlist exportiert",
                BackupRestoreOperation.PLAYLIST_IMPORT: "Playlist importiert",
                BackupRestoreOperation.MEDIA_PATH_REMAP: "Medienpfade neu zugeordnet",
                BackupRestoreOperation.EQUALIZER_EXPORT: "Equalizer-Preset exportiert",
                BackupRestoreOperation.EQUALIZER_IMPORT: "Equalizer-Preset importiert",
                BackupRestoreOperation.OVERLAY_EXPORT: "Overlays/Jingles exportiert",
                BackupRestoreOperation.OVERLAY_IMPORT: "Overlays/Jingles importiert",
            }.get(result.operation, "Datenoperation abgeschlossen")
        else:
            title = "Backup/Restore/Wartung nicht ausgeführt"
        path = f"\n\nDatei: {result.path}" if result.path else ""
        message = result.message
        if (
            result.operation is BackupRestoreOperation.BACKUP
            and result.state is BackupRestoreUiState.COMPLETED
        ):
            message = "Die komplette Veranstaltungssicherung wurde erfolgreich erstellt."
        show_silent_message(
            self._parent,
            title,
            message + path,
            error=result.state is not BackupRestoreUiState.COMPLETED,
        )
        if (
            result.operation is BackupRestoreOperation.EQUALIZER_IMPORT
            and result.state is BackupRestoreUiState.COMPLETED
        ):
            self._refresh_equalizer_presets()
        if (
            result.operation is BackupRestoreOperation.OVERLAY_IMPORT
            and result.state is BackupRestoreUiState.COMPLETED
        ):
            self._refresh_overlays()

    def dispose(self) -> None:
        dialog = self._dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.destroy()
        self._dialog = None
