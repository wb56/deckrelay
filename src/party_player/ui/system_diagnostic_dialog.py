"""Worker-driven system diagnostic dialog with generation-safe closing."""

from collections.abc import Callable
from queue import Empty, SimpleQueue
from threading import Thread

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.system_diagnostic_service import SystemDiagnosticReport
from party_player.diagnostic_export import DiagnosticExportMode
from party_player.ui.responsive_dialog import apply_responsive_dialog_geometry, bind_dialog_escape


def format_system_report(report: SystemDiagnosticReport) -> str:
    dependency = report.dependencies
    database = report.database
    audio = report.audio
    device_lines = (
        "\n".join(f"  • {name} [{device_id}]" for device_id, name in audio.devices) or "  • keine"
    )
    network_lines = (
        "\n".join(f"  • {item.source}: {item.message}" for item in report.network_sources)
        or "  • keine konfigurierten UNC-Quellen"
    )
    return (
        f"Geprüft: {report.checked_at}\n"
        f"Betriebssystem: {report.operating_system} ({report.architecture})\n"
        f"DeckRelay: {report.application_version}\n\n"
        f"VLC / libVLC: {dependency.vlc.status.value}\n"
        f"  Version: {dependency.vlc.version or 'unbekannt'}\n"
        f"  Quelle: {dependency.vlc.source or 'unbekannt'}\n"
        f"  Pfad: {dependency.vlc.installation_directory or 'nicht gefunden'}\n\n"
        f"FFmpeg: {dependency.ffmpeg.status.value}\n"
        f"  Version: {dependency.ffmpeg.version or 'unbekannt'}\n"
        f"  Quelle: {dependency.ffmpeg.source or 'unbekannt'}\n"
        f"  Programm: {dependency.ffmpeg.executable_path or 'nicht gefunden'}\n\n"
        f"FFprobe: {dependency.ffprobe.status.value}\n"
        f"  Version: {dependency.ffprobe.version or 'unbekannt'}\n"
        f"  Programm: {dependency.ffprobe.executable_path or 'nicht gefunden'}\n\n"
        f"SQLite: {database.status.value}\n"
        f"  Version: {database.sqlite_version}\n"
        f"  Schema: {database.schema_version} / {database.expected_schema_version}\n"
        f"  quick_check: {database.integrity_result or 'nicht ausgeführt'}\n\n"
        f"Audiogeräte: {audio.status.value} ({audio.device_count})\n"
        f"  Standardgerät: {audio.default_device_id or 'unbekannt (read-only nicht eindeutig)'}\n"
        f"  Hinweis: {audio.message or '—'}\n"
        f"{device_lines}\n\n"
        f"Netzwerkquellen:\n{network_lines}\n"
    )


class SystemDiagnosticDialog(ctk.CTkToplevel):  # type: ignore[misc]
    def __init__(
        self,
        parent: ctk.CTk,
        initial_report: SystemDiagnosticReport,
        check: Callable[[], SystemDiagnosticReport],
        export: Callable[[SystemDiagnosticReport, DiagnosticExportMode], object],
    ) -> None:
        super().__init__(parent)
        self.title("DeckRelay – Systemdiagnose")
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=(820, 700), minimum_size=(600, 420)
        )
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._check = check
        self._export = export
        self._current_report = initial_report
        self._generation = 0
        self._closed = False
        self._results: SimpleQueue[tuple[int, SystemDiagnosticReport | BaseException]] = (
            SimpleQueue()
        )
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            self,
            text="Systemdiagnose",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(20, 8), sticky="w")
        self._report = ctk.CTkTextbox(self, wrap="word")
        self._report.grid(row=1, column=0, padx=20, pady=8, sticky="nsew")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, padx=20, pady=(8, 20), sticky="ew")
        self._check_button = ctk.CTkButton(actions, text="Erneut prüfen", command=self._start_check)
        self._check_button.pack(side="left")
        ctk.CTkButton(
            actions,
            text="Intern exportieren",
            command=lambda: self._export_report(DiagnosticExportMode.INTERNAL),
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            actions,
            text="Supportbericht exportieren",
            command=lambda: self._export_report(DiagnosticExportMode.SUPPORT),
        ).pack(side="left", padx=(8, 0))
        self._export_status = ctk.CTkLabel(actions, text="")
        self._export_status.pack(side="left", padx=10)
        ctk.CTkButton(actions, text="Schließen", command=self._close).pack(side="right")
        self._render(initial_report)
        bind_dialog_escape(self, self._close)
        self.focus_force()

    def _render(self, report: SystemDiagnosticReport) -> None:
        self._current_report = report
        self._report.configure(state="normal")
        self._report.delete("1.0", "end")
        self._report.insert("1.0", format_system_report(report))
        self._report.configure(state="disabled")

    def _start_check(self) -> None:
        self._generation += 1
        generation = self._generation
        self._check_button.configure(state="disabled", text="Prüfung läuft …")
        Thread(
            target=self._run_check,
            args=(generation,),
            name=f"system-diagnostic-{generation}",
            daemon=True,
        ).start()
        self.after(50, self._poll)

    def _run_check(self, generation: int) -> None:
        try:
            self._results.put((generation, self._check()))
        except BaseException as exc:
            self._results.put((generation, exc))

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            generation, result = self._results.get_nowait()
        except Empty:
            self.after(50, self._poll)
            return
        if generation != self._generation:
            return
        self._check_button.configure(state="normal", text="Erneut prüfen")
        if isinstance(result, BaseException):
            self._report.configure(state="normal")
            self._report.insert("end", f"\n\nPrüfung fehlgeschlagen: {result}")
            self._report.configure(state="disabled")
            return
        self._render(result)

    def _export_report(self, mode: DiagnosticExportMode) -> None:
        try:
            target = self._export(self._current_report, mode)
        except (OSError, ValueError) as exc:
            self._export_status.configure(text=f"Export fehlgeschlagen: {exc}")
            return
        self._export_status.configure(text=f"Gespeichert: {target}")

    def _close(self) -> None:
        self._closed = True
        self._generation += 1
        self.destroy()
