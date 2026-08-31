"""Database backup and safe maintenance dialog."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.ui.responsive_dialog import apply_responsive_dialog_geometry, bind_dialog_escape

from party_player.backup_restore_controller import BackupRestoreUiResult
from party_player.equalizer_transfer import (
    EqualizerConflictStrategy,
    EqualizerImportPreview,
)
from party_player.restore_safety import RestoreSafetyResult
from party_player.playlist_transfer import (
    PlaylistConflictStrategy,
    PlaylistImportPreview,
)


PLAYLIST_IMPORT_PREPARATION = (
    "RECHNERWECHSEL – WICHTIGE REIHENFOLGE\n"
    "1. Zuerst den Ordner mit den Musikdateien einlesen.\n"
    "2. Danach die vorbereitete Playlist importieren.\n"
    "Bei einem anderen Laufwerk oder Basisordner anschließend die Medienpfade neu zuordnen."
)

FULL_EVENT_BACKUP_EXPLANATION = (
    "KOMPLETTE VERANSTALTUNG ÜBERTRAGEN\n"
    "Die Sicherung enthält den Musikkatalog, Playlists, Cue-/Gain-Werte, "
    "Equalizer, Jingles und Einstellungen. Musik- und Jingle-Dateien müssen "
    "separat auf den Veranstaltungsrechner kopiert werden."
)


class PlaylistConflictDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Return one explicit strategy without native alert sounds."""

    def __init__(self, parent: Any, preview: PlaylistImportPreview) -> None:
        super().__init__(parent)
        self.result: PlaylistConflictStrategy | None = None
        self.title("Playlist-Konflikt entscheiden")
        self.geometry("620x430")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self,
            text=f"Playlist „{preview.name}“ ist bereits vorhanden.",
            font=("Segoe UI", 17, "bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=24, pady=(24, 10), sticky="ew")
        ctk.CTkLabel(
            self,
            text=(
                f"Einträge: {preview.entry_count} · Duplikate: {preview.duplicate_count}\n\n"
                "Bitte ausdrücklich festlegen, wie der Namenskonflikt behandelt wird."
            ),
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, padx=24, pady=(0, 12), sticky="ew")
        choices = (
            ("Vorhandene Playlist unverändert überspringen", PlaylistConflictStrategy.SKIP),
            ("Vorhandene Playlist vollständig ersetzen", PlaylistConflictStrategy.REPLACE),
            ("Neue Einträge an vorhandene Playlist anhängen", PlaylistConflictStrategy.APPEND),
            ("Als umbenannte Kopie importieren", PlaylistConflictStrategy.RENAME),
        )
        for row, (label, strategy) in enumerate(choices, start=2):
            ctk.CTkButton(
                self,
                text=label,
                command=lambda selected=strategy: self._choose(selected),
            ).grid(row=row, column=0, padx=24, pady=5, sticky="ew")
        ctk.CTkButton(self, text="Abbrechen", fg_color="#555555", command=self._cancel).grid(
            row=6, column=0, padx=24, pady=(12, 24), sticky="e"
        )
        self.grab_set()
        self.focus_force()

    def _choose(self, strategy: PlaylistConflictStrategy) -> None:
        self.result = strategy
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


def choose_playlist_conflict(
    parent: Any, preview: PlaylistImportPreview
) -> PlaylistConflictStrategy | None:
    dialog = PlaylistConflictDialog(parent, preview)
    parent.wait_window(dialog)
    return dialog.result


class EqualizerConflictDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Require one explicit strategy for an equalizer preset conflict."""

    def __init__(self, parent: Any, preview: EqualizerImportPreview) -> None:
        super().__init__(parent)
        self.result: EqualizerConflictStrategy | None = None
        preset_name = preview.preset.name if preview.preset is not None else "Unbekannt"
        self.title("Equalizer-Konflikt entscheiden")
        self.geometry("620x360")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self,
            text=f"Equalizer-Preset „{preset_name}“ ist bereits vorhanden.",
            font=("Segoe UI", 17, "bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=24, pady=(24, 10), sticky="ew")
        detail = (
            "Ein eingebautes Preset ist betroffen und kann nicht ersetzt werden."
            if preview.builtin_conflict
            else "Bitte ausdrücklich festlegen, wie der Konflikt behandelt wird."
        )
        ctk.CTkLabel(self, text=detail, justify="left", anchor="w").grid(
            row=1, column=0, padx=24, pady=(0, 12), sticky="ew"
        )
        choices = [
            ("Vorhandenes Preset unverändert überspringen", EqualizerConflictStrategy.SKIP),
            ("Als umbenannte Kopie importieren", EqualizerConflictStrategy.COPY),
        ]
        if not preview.builtin_conflict:
            choices.insert(
                1,
                ("Vorhandenes Preset vollständig ersetzen", EqualizerConflictStrategy.REPLACE),
            )
        for row, (label, strategy) in enumerate(choices, start=2):
            ctk.CTkButton(
                self,
                text=label,
                command=lambda selected=strategy: self._choose(selected),
            ).grid(row=row, column=0, padx=24, pady=5, sticky="ew")
        ctk.CTkButton(self, text="Abbrechen", fg_color="#555555", command=self._cancel).grid(
            row=5, column=0, padx=24, pady=(12, 24), sticky="e"
        )
        self.grab_set()
        self.focus_force()

    def _choose(self, strategy: EqualizerConflictStrategy) -> None:
        self.result = strategy
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


def choose_equalizer_conflict(
    parent: Any, preview: EqualizerImportPreview
) -> EqualizerConflictStrategy | None:
    dialog = EqualizerConflictDialog(parent, preview)
    parent.wait_window(dialog)
    return dialog.result


@dataclass(frozen=True, slots=True)
class DatabaseBackupDialogState:
    busy: bool = False
    status: str = "Bereit"


class DatabaseBackupDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Expose only currently safe backup, restore, check, and ANALYZE actions."""

    def __init__(
        self,
        parent: Any,
        backup_default: Callable[[], bool],
        backup_other: Callable[[], bool],
        restore: Callable[[], bool],
        playlist_export: Callable[[], bool],
        playlist_music_directory: Callable[[], bool],
        playlist_import: Callable[[], bool],
        equalizer_export: Callable[[], bool],
        equalizer_import: Callable[[], bool],
        overlay_export: Callable[[], bool],
        overlay_import: Callable[[], bool],
        media_path_remap: Callable[[], bool],
        quick_check: Callable[[], bool],
        integrity_check: Callable[[], bool],
        analyze: Callable[[], bool],
        vacuum: Callable[[], bool],
        reindex: Callable[[], bool],
        safety: Callable[[], RestoreSafetyResult],
        last_manual_backup: tuple[str, str] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("Datenbank und Sicherung")
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=(680, 900), minimum_size=(560, 480)
        )
        self.transient(parent)
        self._on_close = on_close
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._state = DatabaseBackupDialogState()
        self._buttons: list[Any] = []
        self._danger_buttons: list[Any] = []
        self._safety = safety
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._content.grid(row=0, column=0, padx=4, pady=(4, 0), sticky="nsew")
        self._content.grid_columnconfigure(0, weight=1)

        backup = self._group("KOMPLETTE VERANSTALTUNG SICHERN / WIEDERHERSTELLEN", 0)
        ctk.CTkLabel(
            backup,
            text=FULL_EVENT_BACKUP_EXPLANATION,
            justify="left",
            anchor="w",
            wraplength=570,
        ).grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        self._button(
            backup,
            "Komplette Sicherung im Standardordner erstellen",
            backup_default,
            1,
        )
        self._button(backup, "Komplette Sicherung in anderem Ordner…", backup_other, 2)
        self._button(
            backup,
            "Komplette Sicherung wiederherstellen…",
            restore,
            3,
            color=("#B91C1C", "#DC2626"),
        )

        transfer = self._group("EINZELNE PLAYLIST ODER EINSTELLUNG ÜBERTRAGEN", 1)
        preparation = ctk.CTkFrame(transfer, border_width=2, border_color=("#B45309", "#F59E0B"))
        preparation.grid(row=1, column=0, padx=12, pady=(4, 8), sticky="ew")
        preparation.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            preparation,
            text=PLAYLIST_IMPORT_PREPARATION,
            justify="left",
            anchor="w",
            wraplength=570,
        ).grid(row=0, column=0, padx=12, pady=(10, 6), sticky="ew")
        self._button(
            preparation,
            "1. Musikordner jetzt einlesen (MP3/FLAC)…",
            playlist_music_directory,
            1,
            color=("#B45309", "#D97706"),
        )
        self._button(
            preparation,
            "2. Vorbereitete Playlist importieren und prüfen…",
            playlist_import,
            2,
        )
        self._button(transfer, "Ausgewählte Playlist exportieren…", playlist_export, 1)
        self._button(transfer, "Equalizer-Preset exportieren…", equalizer_export, 2)
        self._button(transfer, "Equalizer-Preset importieren und prüfen…", equalizer_import, 3)
        self._button(transfer, "Overlays/Jingles exportieren…", overlay_export, 4)
        self._button(transfer, "Overlays/Jingles importieren und prüfen…", overlay_import, 5)
        self._button(transfer, "Medienpfade nach Rechnerwechsel neu zuordnen…", media_path_remap, 6)

        maintenance = self._group("SICHERE DATENBANKWARTUNG", 2)
        self._button(maintenance, "Schnellprüfung", quick_check, 0)
        self._button(maintenance, "Vollständige Prüfung", integrity_check, 1)
        self._button(maintenance, "Statistiken aktualisieren (ANALYZE)", analyze, 2)
        danger = self._group("GEFAHRENBEREICH – DESTRUKTIVE WARTUNG", 3)
        danger.configure(border_width=2, border_color=("#B91C1C", "#EF4444"))
        self._button(danger, "VACUUM…", vacuum, 0, color=("#B91C1C", "#DC2626"), dangerous=True)
        self._button(danger, "REINDEX…", reindex, 1, color=("#B91C1C", "#DC2626"), dangerous=True)
        self._safety_status = ctk.CTkLabel(
            danger, text="", wraplength=570, justify="left", anchor="w"
        )
        self._safety_status.grid(row=3, column=0, padx=12, pady=(6, 4), sticky="ew")
        ctk.CTkButton(
            danger,
            text="Sicherheitsstatus aktualisieren",
            fg_color="transparent",
            command=self.refresh_safety,
        ).grid(row=4, column=0, padx=12, pady=(0, 10), sticky="ew")

        footer = ctk.CTkFrame(self)
        footer.grid(row=1, column=0, padx=18, pady=(8, 18), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self._status = ctk.CTkLabel(footer, text="Bereit", anchor="w", justify="left")
        self._status.grid(row=0, column=0, padx=8, pady=(6, 2), sticky="ew")
        self._last_backup = ctk.CTkLabel(footer, text="", anchor="w", justify="left")
        self._last_backup.grid(row=1, column=0, padx=8, pady=(2, 6), sticky="ew")
        self._show_last_backup(last_manual_backup)
        self.refresh_safety()
        ctk.CTkButton(footer, text="Schließen", command=self._close).grid(
            row=0, column=1, rowspan=2, padx=8, pady=6, sticky="e"
        )
        bind_dialog_escape(self, self._close)
        self.focus_force()

    def _close(self) -> None:
        if self._on_close is not None:
            self._on_close()
        self.destroy()

    def _group(self, title: str, row: int) -> Any:
        frame = ctk.CTkFrame(self._content)
        frame.grid(row=row, column=0, padx=18, pady=(18 if row == 0 else 8, 0), sticky="ew")
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(frame, text=title, font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, padx=12, pady=(10, 6), sticky="w"
        )
        return frame

    def _button(
        self,
        parent: Any,
        text: str,
        action: Callable[[], bool],
        position: int,
        *,
        color: Any = None,
        dangerous: bool = False,
    ) -> None:
        options = {"fg_color": color} if color is not None else {}
        button = ctk.CTkButton(
            parent,
            text=text,
            command=lambda: self._start(text, action),
            **options,
        )
        button.grid(row=position + 1, column=0, padx=12, pady=4, sticky="ew")
        self._buttons.append(button)
        if dangerous:
            self._danger_buttons.append(button)

    def _start(self, label: str, action: Callable[[], bool]) -> None:
        if self._state.busy:
            return
        if action():
            self._state = DatabaseBackupDialogState(True, f"Läuft: {label}")
            self._render_state()

    def start_followup(self, label: str, action: Callable[[], bool]) -> None:
        self._start(label, action)

    def complete(self, result: BackupRestoreUiResult) -> None:
        self._state = DatabaseBackupDialogState(False, result.message)
        self._render_state()
        if result.operation.value == "BACKUP" and result.path is not None and result.created_at:
            self._show_last_backup((result.created_at, str(result.path)))

    def _show_last_backup(self, backup: tuple[str, str] | None) -> None:
        text = "Letzte manuelle Sicherung: noch keine"
        if backup is not None:
            text = f"Letzte manuelle Sicherung: {backup[0]}\n{backup[1]}"
        self._last_backup.configure(text=text)

    def _render_state(self) -> None:
        state = "disabled" if self._state.busy else "normal"
        for button in self._buttons:
            button.configure(state=state)
        self._status.configure(text=self._state.status)
        if not self._state.busy:
            self._render_safety(self._safety())

    def refresh_safety(self) -> None:
        self._render_safety(self._safety())

    def _render_safety(self, safety: RestoreSafetyResult) -> None:
        if safety.allowed:
            text = "Freigegeben: Decks und Audioaktionen sind sicher gestoppt."
        else:
            text = "Gesperrt:\n" + "\n".join(f"• {reason.message}" for reason in safety.reasons)
        self._safety_status.configure(text=text)
        state = "normal" if safety.allowed and not self._state.busy else "disabled"
        for button in self._danger_buttons:
            button.configure(state=state)
