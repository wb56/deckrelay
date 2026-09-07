from pathlib import Path

from party_player.backup_restore_controller import (
    BackupRestoreOperation,
    BackupRestoreUiResult,
    BackupRestoreUiState,
)
from party_player.equalizer import EqualizerPreset
from party_player.overlay import OverlayDefinition, OverlayRecord
from party_player.equalizer_transfer import (
    EqualizerConflictStrategy,
    EqualizerImportPreview,
    EqualizerTransferErrorCode,
)
from party_player.overlay_transfer import (
    OverlayConflictStrategy,
    OverlayImportPreview,
    OverlayTransferErrorCode,
)
from party_player.ui import main_window
from party_player.ui.main_window import MainWindow
from party_player.playlist_transfer import (
    PlaylistConflictStrategy,
    PlaylistImportPreview,
    PlaylistTransferErrorCode,
    PlaylistTransferFormat,
)
from party_player.media_path_remap import (
    MediaPathRemapChange,
    MediaPathRemapErrorCode,
    MediaPathRemapPreview,
)


class BackupRestoreBindingStub:
    def __init__(self) -> None:
        self.backup_destination: Path | None = None
        self.restore_request: tuple[Path, Path] | None = None
        self.vacuum_calls = 0
        self.reindex_calls = 0
        self.playlist_export_request = None
        self.playlist_preview_request = None
        self.playlist_import_request = None
        self.media_path_preview_request = None
        self.media_path_commit_request = None
        self.equalizer_export_request = None
        self.equalizer_preview_request = None
        self.equalizer_import_request = None
        self.overlay_export_request = None
        self.overlay_preview_request = None
        self.overlay_import_request = None

    def start_backup(self, destination: Path) -> bool:
        self.backup_destination = destination
        return True

    def start_restore(self, archive: Path, safety_directory: Path) -> bool:
        self.restore_request = (archive, safety_directory)
        return True

    def start_vacuum(self) -> bool:
        self.vacuum_calls += 1
        return True

    def start_reindex(self) -> bool:
        self.reindex_calls += 1
        return True

    def start_playlist_export(self, saved_queue_id, destination, format) -> bool:
        self.playlist_export_request = (saved_queue_id, destination, format)
        return True

    def start_playlist_import_preview(self, source, format) -> bool:
        self.playlist_preview_request = (source, format)
        return True

    def start_playlist_import(self, preview, conflict) -> bool:
        self.playlist_import_request = (preview, conflict)
        return True

    def start_media_path_remap_preview(self, old_base, new_base) -> bool:
        self.media_path_preview_request = (old_base, new_base)
        return True

    def start_media_path_remap(self, preview) -> bool:
        self.media_path_commit_request = preview
        return True

    def start_equalizer_export(self, preset_key, destination) -> bool:
        self.equalizer_export_request = (preset_key, destination)
        return True

    def start_equalizer_import_preview(self, source) -> bool:
        self.equalizer_preview_request = source
        return True

    def start_equalizer_import(self, preview, strategy) -> bool:
        self.equalizer_import_request = (preview, strategy)
        return True

    def start_overlay_export(self, destination) -> bool:
        self.overlay_export_request = destination
        return True

    def start_overlay_import_preview(self, source) -> bool:
        self.overlay_preview_request = source
        return True

    def start_overlay_import(self, preview, strategy) -> bool:
        self.overlay_import_request = (preview, strategy)
        return True


def test_database_dialog_is_exposed_from_extras_menu(monkeypatch) -> None:
    commands: list[tuple[str, object]] = []
    popups: list[tuple[int, int]] = []

    class MenuDouble:
        def __init__(self, _parent, *, tearoff: bool) -> None:
            assert not tearoff

        def add_command(self, *, label: str, command) -> None:
            commands.append((label, command))

        def add_separator(self) -> None:
            pass

        def tk_popup(self, x: int, y: int) -> None:
            popups.append((x, y))

    class ButtonDouble:
        def winfo_rootx(self) -> int:
            return 10

        def winfo_rooty(self) -> int:
            return 20

        def winfo_height(self) -> int:
            return 30

    window = object.__new__(MainWindow)
    window._show_database_backup = lambda: None
    monkeypatch.setattr(main_window.tk, "Menu", MenuDouble)

    window._show_extras_menu(ButtonDouble())

    assert commands[-1][0] == "Datenbank und Sicherung…"
    assert popups == [(10, 50)]


def test_backup_path_selection_starts_only_after_directory_was_chosen(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    monkeypatch.setattr(main_window.filedialog, "askdirectory", lambda **_kwargs: "")

    window._request_backup()

    assert binding.backup_destination is None
    monkeypatch.setattr(
        main_window.filedialog, "askdirectory", lambda **_kwargs: r"C:\Party\Backups"
    )
    window._request_backup()
    assert binding.backup_destination == Path(r"C:\Party\Backups")


def test_default_backup_starts_without_opening_path_dialog(tmp_path: Path) -> None:
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    window._default_backup_directory = tmp_path / "data" / "Backups"

    window._request_default_backup()

    assert binding.backup_destination == tmp_path / "data" / "Backups"


def test_restore_requires_confirmation_and_uses_separate_safety_directory(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    archive = Path("selected.partyplayer-backup").resolve()
    monkeypatch.setattr(main_window.filedialog, "askopenfilename", lambda **_kwargs: str(archive))
    monkeypatch.setattr(main_window, "ask_silent_yes_no", lambda *_args, **_kwargs: False)

    window._request_restore()
    assert binding.restore_request is None

    monkeypatch.setattr(main_window, "ask_silent_yes_no", lambda *_args, **_kwargs: True)
    window._request_restore()

    assert binding.restore_request == (archive, archive.parent / "safety-backups")


def test_restart_is_offered_only_for_restart_required_result(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    window._restart_requested = False
    disposed: list[bool] = []
    destroyed: list[bool] = []
    window._dispose_resources = lambda: disposed.append(True)
    window.destroy = lambda: destroyed.append(True)
    confirmations: list[str] = []
    monkeypatch.setattr(
        main_window,
        "ask_silent_yes_no",
        lambda _parent, title, _message: confirmations.append(title) or True,
    )

    window.show_backup_restore_result(
        BackupRestoreUiResult(
            BackupRestoreOperation.RESTORE,
            BackupRestoreUiState.RESTART_REQUIRED,
            "Restore abgeschlossen.",
            Path("safety.partyplayer-backup"),
        )
    )

    assert window.restart_requested
    assert confirmations == ["Restore abgeschlossen – Neustart erforderlich"]
    assert disposed == [True]
    assert destroyed == [True]


def test_path_remap_restart_uses_own_message_without_restore_backup(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    window._restart_requested = False
    window._dispose_resources = lambda: None
    window.destroy = lambda: None
    prompts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_window,
        "ask_silent_yes_no",
        lambda _parent, title, message: prompts.append((title, message)) or False,
    )

    window.show_backup_restore_result(
        BackupRestoreUiResult(
            BackupRestoreOperation.MEDIA_PATH_REMAP,
            BackupRestoreUiState.RESTART_REQUIRED,
            "Pfade wurden geändert.",
        )
    )

    assert prompts[0][0] == "Pfad-Neuzuordnung abgeschlossen – Neustart erforderlich"
    assert "Sicherheitsbackup" not in prompts[0][1]
    assert not window.restart_requested


def test_failed_restore_never_offers_or_starts_restart(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    window._restart_requested = False
    shown: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        main_window,
        "show_silent_message",
        lambda _parent, title, _message, *, error=False: shown.append((title, error)),
    )
    monkeypatch.setattr(
        main_window,
        "ask_silent_yes_no",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no restart offer")),
    )

    window.show_backup_restore_result(
        BackupRestoreUiResult(
            BackupRestoreOperation.RESTORE,
            BackupRestoreUiState.FAILED,
            "Gate blockiert.",
            error_code="RESTORE_PIPELINE_SAFETY_GATE_BLOCKED",
        )
    )

    assert not window.restart_requested
    assert shown == [("Backup/Restore/Wartung nicht ausgeführt", True)]


def test_result_from_closed_dialog_generation_does_not_update_new_dialog(
    monkeypatch,
) -> None:
    class DialogDouble:
        def __init__(self) -> None:
            self.results: list[BackupRestoreUiResult] = []

        def winfo_exists(self) -> bool:
            return True

        def complete(self, result: BackupRestoreUiResult) -> None:
            self.results.append(result)

    window = object.__new__(MainWindow)
    dialog = DialogDouble()
    window._database_backup_dialog = dialog
    window._database_backup_dialog_generation = 4
    window._database_operation_generation = 2
    monkeypatch.setattr(main_window, "show_silent_message", lambda *_args, **_kwargs: None)

    window.show_backup_restore_result(
        BackupRestoreUiResult(
            BackupRestoreOperation.BACKUP,
            BackupRestoreUiState.COMPLETED,
            "Alte Operation abgeschlossen.",
        )
    )

    assert dialog.results == []
    assert window._database_operation_generation is None


def test_completed_backup_shows_clear_event_backup_confirmation(
    monkeypatch, tmp_path: Path
) -> None:
    window = object.__new__(MainWindow)
    shown: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        main_window,
        "show_silent_message",
        lambda _parent, title, message, *, error=False: shown.append((title, message, error)),
    )
    backup = tmp_path / "deckrelay.partyplayer-backup"

    window.show_backup_restore_result(
        BackupRestoreUiResult(
            BackupRestoreOperation.BACKUP,
            BackupRestoreUiState.COMPLETED,
            "Backup wurde erfolgreich erstellt.",
            backup,
        )
    )

    assert shown == [
        (
            "Sicherung erfolgreich",
            f"Die komplette Veranstaltungssicherung wurde erfolgreich erstellt.\n\nDatei: {backup}",
            False,
        )
    ]


def test_vacuum_and_reindex_require_explicit_confirmation(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    answers = iter((False, True, False, True))
    monkeypatch.setattr(main_window, "ask_silent_yes_no", lambda *_args, **_kwargs: next(answers))

    assert not window._request_vacuum()
    assert window._request_vacuum()
    assert not window._request_reindex()
    assert window._request_reindex()

    assert binding.vacuum_calls == 1
    assert binding.reindex_calls == 1


def test_playlist_export_and_preview_infer_format_from_selected_path(monkeypatch) -> None:
    class MenuDouble:
        def get(self) -> str:
            return "Abend"

    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    window._saved_queue_ids = {"Abend": 7}
    window._saved_queue_menu = MenuDouble()
    export_path = Path("abend.m3u8").resolve()
    import_path = Path("neu.json").resolve()
    monkeypatch.setattr(
        main_window.filedialog, "asksaveasfilename", lambda **_kwargs: str(export_path)
    )
    monkeypatch.setattr(
        main_window.filedialog, "askopenfilename", lambda **_kwargs: str(import_path)
    )

    assert window._request_playlist_export()
    assert window._request_playlist_import_preview()

    assert binding.playlist_export_request == (
        7,
        export_path,
        PlaylistTransferFormat.M3U8,
    )
    assert binding.playlist_preview_request == (
        import_path,
        PlaylistTransferFormat.JSON,
    )


def test_importable_preview_requires_choice_and_starts_exact_preview_commit(
    monkeypatch,
) -> None:
    class DialogDouble:
        def start_followup(self, _label: str, action) -> None:
            assert action()

    source = Path("preview.json")
    preview = PlaylistImportPreview(
        True,
        True,
        PlaylistTransferErrorCode.NONE,
        "ok",
        source,
        PlaylistTransferFormat.JSON,
        name="Abend",
        entry_count=4,
        duplicate_count=1,
        name_conflict=True,
    )
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    window._database_backup_dialog_generation = 5
    window._database_operation_generation = None
    monkeypatch.setattr(
        main_window,
        "choose_playlist_conflict",
        lambda _parent, selected: (
            PlaylistConflictStrategy.APPEND if selected is preview else None
        ),
    )

    window._handle_playlist_import_preview(
        BackupRestoreUiResult(
            BackupRestoreOperation.PLAYLIST_IMPORT_PREVIEW,
            BackupRestoreUiState.COMPLETED,
            "Vorschau fertig.",
            playlist_preview=preview,
        ),
        DialogDouble(),  # type: ignore[arg-type]
    )

    assert binding.playlist_import_request == (preview, PlaylistConflictStrategy.APPEND)
    assert window._database_operation_generation == 5


def test_unknown_preview_paths_block_commit_and_show_bounded_examples(monkeypatch) -> None:
    preview = PlaylistImportPreview(
        True,
        False,
        PlaylistTransferErrorCode.TRACK_NOT_FOUND,
        "Ein Titel fehlt.",
        Path("preview.json"),
        PlaylistTransferFormat.JSON,
        name="Abend",
        entry_count=2,
        unknown_path_count=1,
        unknown_path_examples=(r"Z:\Fehlt.mp3",),
    )
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    shown: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        main_window,
        "show_silent_message",
        lambda _parent, title, message, *, error=False: shown.append((title, message, error)),
    )

    window._handle_playlist_import_preview(
        BackupRestoreUiResult(
            BackupRestoreOperation.PLAYLIST_IMPORT_PREVIEW,
            BackupRestoreUiState.COMPLETED,
            preview.message,
            playlist_preview=preview,
        ),
        object(),  # type: ignore[arg-type]
    )

    assert binding.playlist_import_request is None
    assert shown[0][0] == "Zuerst Musikordner einlesen"
    assert "Musikordner jetzt einlesen" in shown[0][1]
    assert "Medienpfade nach Rechnerwechsel" in shown[0][1]
    assert r"Z:\Fehlt.mp3" in shown[0][1]
    assert shown[0][2]


def test_media_path_preview_collects_both_bases_before_controller_start(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    answers = iter((r"D:\Musik", r"\\nas\Audio"))
    monkeypatch.setattr(
        main_window.simpledialog,
        "askstring",
        lambda *_args, **_kwargs: next(answers),
    )

    assert window._request_media_path_remap_preview()

    assert binding.media_path_preview_request == (r"D:\Musik", r"\\nas\Audio")


def test_media_path_preview_confirmation_commits_exact_preview(monkeypatch) -> None:
    class DialogDouble:
        def start_followup(self, _label: str, action) -> None:
            assert action()

    preview = MediaPathRemapPreview(
        True,
        True,
        MediaPathRemapErrorCode.NONE,
        "Ein Pfad.",
        r"D:\Musik",
        r"E:\Musik",
        state_token="token",
        track_count=1,
        examples=(MediaPathRemapChange("tracks", 1, r"D:\Musik\eins.mp3", r"E:\Musik\eins.mp3"),),
    )
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    window._database_backup_dialog_generation = 8
    window._database_operation_generation = None
    confirmations: list[str] = []
    monkeypatch.setattr(
        main_window,
        "ask_silent_yes_no",
        lambda _parent, _title, message: confirmations.append(message) or True,
    )

    window._handle_media_path_remap_preview(
        BackupRestoreUiResult(
            BackupRestoreOperation.MEDIA_PATH_REMAP_PREVIEW,
            BackupRestoreUiState.COMPLETED,
            preview.message,
            media_path_preview=preview,
        ),
        DialogDouble(),  # type: ignore[arg-type]
    )

    assert binding.media_path_commit_request is preview
    assert window._database_operation_generation == 8
    assert "nicht verschoben oder kopiert" in confirmations[0]
    assert r"D:\Musik\eins.mp3" in confirmations[0]


def test_media_path_collision_preview_blocks_commit(monkeypatch) -> None:
    preview = MediaPathRemapPreview(
        True,
        False,
        MediaPathRemapErrorCode.COLLISION,
        "Kollision.",
        r"D:\Alt",
        r"E:\Neu",
        track_count=1,
        collisions=(r"E:\Neu\eins.mp3",),
    )
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    shown: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        main_window,
        "show_silent_message",
        lambda _parent, title, message, *, error=False: shown.append((title, message, error)),
    )

    window._handle_media_path_remap_preview(
        BackupRestoreUiResult(
            BackupRestoreOperation.MEDIA_PATH_REMAP_PREVIEW,
            BackupRestoreUiState.COMPLETED,
            preview.message,
            media_path_preview=preview,
        ),
        object(),  # type: ignore[arg-type]
    )

    assert binding.media_path_commit_request is None
    assert shown[0][0] == "Pfad-Neuzuordnung ist blockiert"
    assert r"E:\Neu\eins.mp3" in shown[0][1]
    assert shown[0][2]


def test_equalizer_export_and_import_preview_use_selected_json_paths(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    window._equalizer_preset_keys = {
        "Vererben": "inherit",
        "Equalizer aus": "disabled",
        "Party": "party",
    }
    export_path = Path("party.json").resolve()
    import_path = Path("new.json").resolve()
    monkeypatch.setattr(main_window.simpledialog, "askstring", lambda *_args, **_kwargs: "Party")
    monkeypatch.setattr(
        main_window.filedialog, "asksaveasfilename", lambda **_kwargs: str(export_path)
    )
    monkeypatch.setattr(
        main_window.filedialog, "askopenfilename", lambda **_kwargs: str(import_path)
    )

    assert window._request_equalizer_export()
    assert window._request_equalizer_import_preview()

    assert binding.equalizer_export_request == ("party", export_path)
    assert binding.equalizer_preview_request == import_path


def test_equalizer_conflict_choice_commits_exact_preview(monkeypatch) -> None:
    class DialogDouble:
        def start_followup(self, _label: str, action) -> None:
            assert action()

    preview = EqualizerImportPreview(
        True,
        EqualizerTransferErrorCode.NONE,
        "ok",
        Path("party.json"),
        preset=EqualizerPreset("party", "Party", -2.0, ((60.0, 1.0),)),
        conflicts=((3, "party", "Party", False),),
    )
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    window._database_backup_dialog_generation = 9
    window._database_operation_generation = None
    monkeypatch.setattr(
        main_window,
        "choose_equalizer_conflict",
        lambda _parent, selected: (EqualizerConflictStrategy.COPY if selected is preview else None),
    )

    window._handle_equalizer_import_preview(
        BackupRestoreUiResult(
            BackupRestoreOperation.EQUALIZER_IMPORT_PREVIEW,
            BackupRestoreUiState.COMPLETED,
            preview.message,
            equalizer_preview=preview,
        ),
        DialogDouble(),  # type: ignore[arg-type]
    )

    assert binding.equalizer_import_request == (preview, EqualizerConflictStrategy.COPY)
    assert window._database_operation_generation == 9


def test_invalid_equalizer_preview_never_starts_commit(monkeypatch) -> None:
    preview = EqualizerImportPreview(
        False,
        EqualizerTransferErrorCode.FORMAT_INVALID,
        "Ungültige Bandstruktur.",
        Path("bad.json"),
    )
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    shown = []
    monkeypatch.setattr(
        main_window,
        "show_silent_message",
        lambda *_args, **_kwargs: shown.append(True),
    )

    window._handle_equalizer_import_preview(
        BackupRestoreUiResult(
            BackupRestoreOperation.EQUALIZER_IMPORT_PREVIEW,
            BackupRestoreUiState.FAILED,
            preview.message,
            equalizer_preview=preview,
        ),
        object(),  # type: ignore[arg-type]
    )

    assert binding.equalizer_import_request is None
    assert shown == [True]


def test_overlay_export_and_preview_use_json_paths(monkeypatch) -> None:
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    export_path = Path("overlays.json").resolve()
    import_path = Path("import.json").resolve()
    monkeypatch.setattr(
        main_window.filedialog, "asksaveasfilename", lambda **_kwargs: str(export_path)
    )
    monkeypatch.setattr(
        main_window.filedialog, "askopenfilename", lambda **_kwargs: str(import_path)
    )

    assert window._request_overlay_export()
    assert window._request_overlay_import_preview()

    assert binding.overlay_export_request == export_path
    assert binding.overlay_preview_request == import_path


def test_overlay_conflict_confirmation_commits_exact_preview(monkeypatch) -> None:
    class DialogDouble:
        def start_followup(self, _label: str, action) -> None:
            assert action()

    record = OverlayRecord(
        OverlayDefinition(0, "Tusch", "C:/Jingles/Tusch.mp3"),
        favorite_position=1,
        keyboard_shortcut="Ctrl+1",
    )
    preview = OverlayImportPreview(
        True,
        OverlayTransferErrorCode.NONE,
        "ok",
        Path("overlays.json"),
        records=(record,),
        conflicts=((7, "Alt", 1, "Ctrl+1"),),
    )
    window = object.__new__(MainWindow)
    binding = BackupRestoreBindingStub()
    window._backup_restore_controller = binding
    window._database_backup_dialog_generation = 10
    window._database_operation_generation = None
    monkeypatch.setattr(main_window, "ask_silent_yes_no_cancel", lambda *_args, **_kwargs: True)

    window._handle_overlay_import_preview(
        BackupRestoreUiResult(
            BackupRestoreOperation.OVERLAY_IMPORT_PREVIEW,
            BackupRestoreUiState.COMPLETED,
            preview.message,
            overlay_preview=preview,
        ),
        DialogDouble(),  # type: ignore[arg-type]
    )

    assert binding.overlay_import_request == (
        preview,
        OverlayConflictStrategy.REPLACE_EXISTING,
    )
    assert window._database_operation_generation == 10
