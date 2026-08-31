"""Modal first-run dependency setup shown before playback is constructed."""

from collections.abc import Callable
from queue import Empty, SimpleQueue
from threading import Thread
import webbrowser

import customtkinter as ctk  # type: ignore[import-untyped]
from tkinter import filedialog

from party_player.system_dependencies import FFMPEG_DOWNLOAD_URL, VLC_DOWNLOAD_URL
from party_player.system_dependency_service import SystemDependencyResolution
from party_player.system_diagnostic_service import SystemDiagnosticReport
from party_player.diagnostic_export import DiagnosticExportMode
from party_player.ui.responsive_dialog import apply_responsive_dialog_geometry, bind_dialog_escape


def open_official_download(
    url: str,
    opener: Callable[[str], object] = webbrowser.open,
) -> object:
    """Open only centrally configured official HTTPS dependency pages."""
    allowed = {VLC_DOWNLOAD_URL, FFMPEG_DOWNLOAD_URL}
    if url not in allowed or not url.startswith("https://"):
        raise ValueError("Nicht erlaubtes Downloadziel")
    return opener(url)


class FirstRunSetupDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Keep normal party operation blocked until VLC setup is confirmed."""

    def __init__(
        self,
        parent: ctk.CTk,
        initial_resolution: SystemDependencyResolution | None,
        recheck: Callable[[], SystemDiagnosticReport],
        complete: Callable[[SystemDependencyResolution], None],
        select_vlc: Callable[[str], SystemDependencyResolution],
        select_ffmpeg: Callable[[str], SystemDependencyResolution],
        export_diagnostic: (
            Callable[[SystemDiagnosticReport, DiagnosticExportMode], object] | None
        ) = None,
    ) -> None:
        super().__init__(parent)
        self.title("DeckRelay – Einrichtung")
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=(760, 680), minimum_size=(600, 440)
        )
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._resolution = initial_resolution
        self._recheck = recheck
        self._complete = complete
        self._select_vlc = select_vlc
        self._select_ffmpeg = select_ffmpeg
        self._export_diagnostic = export_diagnostic
        self._results: SimpleQueue[
            tuple[
                int,
                SystemDependencyResolution | SystemDiagnosticReport | BaseException,
            ]
        ] = SimpleQueue()
        self._diagnostic: SystemDiagnosticReport | None = None
        self._generation = 0
        self._closed = False
        self._running = False
        self.completed = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._content.grid(row=1, column=0, padx=4, sticky="nsew")
        self._content.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self._content,
            text="DeckRelay – Einrichtung",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).grid(row=0, column=0, padx=24, pady=(24, 8), sticky="w")
        ctk.CTkLabel(
            self._content,
            text=(
                "VLC ist für die Wiedergabe erforderlich. FFmpeg und FFprobe werden "
                "für Cue- und Lautheitsanalysen benötigt, sind aber optional."
            ),
            justify="left",
            wraplength=650,
        ).grid(row=1, column=0, padx=24, pady=(0, 16), sticky="w")

        status = ctk.CTkFrame(self._content)
        status.grid(row=2, column=0, padx=24, pady=8, sticky="ew")
        status.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(status, text="Komponente", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=10, sticky="w"
        )
        ctk.CTkLabel(status, text="Status", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=1, padx=12, pady=10, sticky="w"
        )
        self._vlc_status = self._status_row(status, 1, "VLC / libVLC")
        self._ffmpeg_status = self._status_row(status, 2, "FFmpeg")
        self._ffprobe_status = self._status_row(status, 3, "FFprobe")
        self._os_status = self._status_row(status, 4, "Betriebssystem")
        self._app_status = self._status_row(status, 5, "DeckRelay")
        self._database_status = self._status_row(status, 6, "SQLite")
        self._audio_status = self._status_row(status, 7, "Audiogeräte")
        self._message = ctk.CTkLabel(status, text="", justify="left", wraplength=610)
        self._message.grid(row=8, column=0, columnspan=2, padx=12, pady=12, sticky="w")

        links = ctk.CTkFrame(self._content, fg_color="transparent")
        links.grid(row=3, column=0, padx=24, pady=4, sticky="ew")
        self._vlc_download_button = ctk.CTkButton(
            links,
            text="VLC herunterladen",
            command=lambda: open_official_download(VLC_DOWNLOAD_URL),
        )
        self._vlc_download_button.pack(side="left", padx=(0, 8))
        self._ffmpeg_download_button = ctk.CTkButton(
            links,
            text="FFmpeg herunterladen",
            command=lambda: open_official_download(FFMPEG_DOWNLOAD_URL),
        )
        self._ffmpeg_download_button.pack(side="left")
        self._vlc_select_button = ctk.CTkButton(
            links, text="VLC-Verzeichnis wählen", command=self._choose_vlc
        )
        self._vlc_select_button.pack(side="left", padx=(16, 8))
        self._ffmpeg_select_button = ctk.CTkButton(
            links, text="FFmpeg-bin wählen", command=self._choose_ffmpeg
        )
        self._ffmpeg_select_button.pack(side="left")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, padx=24, pady=(8, 24), sticky="ew")
        self._check_button = ctk.CTkButton(
            actions, text="Installation erneut prüfen", command=self._start_recheck
        )
        self._check_button.pack(side="left")
        self._internal_export_button = ctk.CTkButton(
            actions,
            text="Diagnose intern exportieren",
            command=lambda: self._export_report(DiagnosticExportMode.INTERNAL),
            state="disabled",
        )
        self._internal_export_button.pack(side="left", padx=(8, 0))
        self._support_export_button = ctk.CTkButton(
            actions,
            text="Supportbericht exportieren",
            command=lambda: self._export_report(DiagnosticExportMode.SUPPORT),
            state="disabled",
        )
        self._support_export_button.pack(side="left", padx=(8, 0))
        self._continue_button = ctk.CTkButton(
            actions, text="Einrichtung abschließen", command=self._finish
        )
        self._continue_button.pack(side="right")
        ctk.CTkButton(actions, text="DeckRelay beenden", command=self._cancel).pack(
            side="right", padx=8
        )
        bind_dialog_escape(self, self._cancel)
        self._render()

    @staticmethod
    def _status_row(parent: ctk.CTkFrame, row: int, name: str) -> ctk.CTkLabel:
        ctk.CTkLabel(parent, text=name).grid(row=row, column=0, padx=12, pady=8, sticky="w")
        label = ctk.CTkLabel(parent, text="", justify="left", wraplength=520)
        label.grid(row=row, column=1, padx=12, pady=8, sticky="w")
        return label

    def show(self) -> bool:
        self.grab_set()
        self.focus_force()
        self.wait_window()
        return self.completed

    @property
    def resolution(self) -> SystemDependencyResolution:
        if self._resolution is None:
            raise RuntimeError("Die Installation wurde noch nicht geprüft")
        return self._resolution

    def _render(self) -> None:
        if self._resolution is None:
            self._vlc_status.configure(text="noch nicht geprüft")
            self._ffmpeg_status.configure(text="noch nicht geprüft")
            self._ffprobe_status.configure(text="noch nicht geprüft")
            self._os_status.configure(text="noch nicht geprüft")
            self._app_status.configure(text="noch nicht geprüft")
            self._database_status.configure(text="noch nicht geprüft")
            self._audio_status.configure(text="noch nicht geprüft")
            self._message.configure(
                text="Wähle bei Bedarf Verzeichnisse aus oder starte die Installationsprüfung."
            )
            self._continue_button.configure(state="disabled")
            self._set_export_state(False)
            return
        snapshot = self._resolution.snapshot
        self._vlc_status.configure(
            text=self._describe(
                snapshot.vlc,
                "VLC installieren oder ein gültiges VLC-Verzeichnis auswählen.",
            )
        )
        self._ffmpeg_status.configure(
            text=self._describe(
                snapshot.ffmpeg,
                "FFmpeg installieren oder den gemeinsamen bin-Ordner auswählen.",
            )
        )
        self._ffprobe_status.configure(
            text=self._describe(
                snapshot.ffprobe,
                "FFprobe muss im selben bin-Ordner wie FFmpeg liegen.",
            )
        )
        self._os_status.configure(
            text=(
                f"{self._diagnostic.operating_system} ({self._diagnostic.architecture})"
                if self._diagnostic
                else snapshot.operating_system or "nicht erfasst"
            )
        )
        self._app_status.configure(
            text=(
                self._diagnostic.application_version
                if self._diagnostic
                else snapshot.application_version or "nicht erfasst"
            )
        )
        self._database_status.configure(
            text=(
                f"{self._diagnostic.database.status.value} · SQLite "
                f"{self._diagnostic.database.sqlite_version} · Schema "
                f"{self._diagnostic.database.schema_version}"
                if self._diagnostic
                else "noch nicht vollständig geprüft"
            )
        )
        self._audio_status.configure(
            text=(
                f"{self._diagnostic.audio.status.value} · "
                f"{self._diagnostic.audio.device_count} Gerät(e) · Standard: "
                f"{self._diagnostic.audio.default_device_id or 'unbekannt'}"
                if self._diagnostic
                else "nicht geprüft"
            )
        )
        playback = snapshot.capabilities.playback_available
        analysis = snapshot.capabilities.cue_analysis_available
        if not playback:
            message = "VLC ist nicht einsatzbereit. Der Partybetrieb bleibt gesperrt."
        elif not analysis:
            message = (
                "Wiedergabe ist verfügbar. Ohne FFmpeg sind automatische Analysen "
                "vorerst deaktiviert."
            )
        else:
            message = "Alle benötigten Programme sind einsatzbereit."
        self._message.configure(text=message)
        self._continue_button.configure(state="normal" if playback else "disabled")
        self._set_export_state(self._diagnostic is not None)

    def _set_export_state(self, available: bool) -> None:
        state = "normal" if available and self._export_diagnostic is not None else "disabled"
        self._internal_export_button.configure(state=state)
        self._support_export_button.configure(state=state)

    @staticmethod
    def _describe(info: object, unavailable_action: str = "Installation prüfen.") -> str:
        status = getattr(info, "status").value
        version = getattr(info, "version", None)
        source = getattr(info, "source", None)
        details = [status]
        if version:
            details.append(str(version).splitlines()[0])
        if source:
            details.append(f"Quelle: {source}")
        path = getattr(info, "installation_directory", None) or getattr(
            info, "executable_path", None
        )
        if path:
            details.append(f"Pfad: {path}")
        text = " · ".join(details)
        message = getattr(info, "message", None)
        if message:
            text += f"\nHinweis: {message}"
        if status != "available":
            text += f"\nAktion: {unavailable_action}"
        return text

    def _start_recheck(self) -> None:
        self._start_operation(self._recheck)

    def _start_operation(
        self,
        operation: Callable[[], SystemDependencyResolution | SystemDiagnosticReport],
    ) -> None:
        if self._closed or self._running:
            return
        self._running = True
        self._generation += 1
        generation = self._generation
        self._set_operation_state(True)
        Thread(
            target=self._run_operation,
            args=(generation, operation),
            name="first-run-dependency-check",
            daemon=True,
        ).start()
        self.after(50, self._poll_recheck)

    def _run_operation(
        self,
        generation: int,
        operation: Callable[[], SystemDependencyResolution | SystemDiagnosticReport],
    ) -> None:
        try:
            self._results.put((generation, operation()))
        except BaseException as exc:
            self._results.put((generation, exc))

    def _poll_recheck(self) -> None:
        if self._closed:
            return
        try:
            generation, result = self._results.get_nowait()
        except Empty:
            self.after(50, self._poll_recheck)
            return
        if generation != self._generation:
            self.after(0, self._poll_recheck)
            return
        self._running = False
        self._set_operation_state(False)
        if isinstance(result, BaseException):
            self._message.configure(text=f"Prüfung fehlgeschlagen: {result}")
            return
        if isinstance(result, SystemDiagnosticReport):
            self._diagnostic = result
            self._resolution = result.resolution
        else:
            self._diagnostic = None
            self._resolution = result
        self._render()

    def _set_operation_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self._check_button.configure(
            state=state,
            text="Prüfung läuft …" if running else "Installation erneut prüfen",
        )
        self._vlc_select_button.configure(state=state)
        self._ffmpeg_select_button.configure(state=state)

    def _choose_vlc(self) -> None:
        directory = filedialog.askdirectory(
            parent=self, title="VLC-Installationsverzeichnis auswählen"
        )
        if directory:
            self._start_operation(lambda: self._select_vlc(directory))

    def _choose_ffmpeg(self) -> None:
        directory = filedialog.askdirectory(parent=self, title="FFmpeg-bin-Verzeichnis auswählen")
        if directory:
            self._start_operation(lambda: self._select_ffmpeg(directory))

    def _finish(self) -> None:
        if self._resolution is None or self._running:
            return
        self._complete(self._resolution)
        self.completed = True
        self._closed = True
        self._generation += 1
        self.grab_release()
        self.destroy()

    def _export_report(self, mode: DiagnosticExportMode) -> None:
        if self._running:
            self._message.configure(text="Bitte warte, bis die laufende Prüfung beendet ist.")
            return
        if self._diagnostic is None or self._export_diagnostic is None:
            self._message.configure(text="Bitte zuerst „Installation erneut prüfen“ ausführen.")
            return
        try:
            target = self._export_diagnostic(self._diagnostic, mode)
        except (OSError, ValueError) as exc:
            self._message.configure(text=f"Diagnoseexport fehlgeschlagen: {exc}")
            return
        self._message.configure(text=f"Diagnosebericht gespeichert: {target}")

    def _cancel(self) -> None:
        self.completed = False
        self._closed = True
        self._generation += 1
        self.grab_release()
        self.destroy()
