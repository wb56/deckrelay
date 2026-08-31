"""Settings dialog for validated next-start VLC and FFmpeg configuration."""

from collections.abc import Callable
from queue import Empty, SimpleQueue
from threading import Thread
from tkinter import filedialog
from typing import Any
from dataclasses import dataclass

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.settings_service import DependencySettings
from party_player.system_diagnostic_service import SystemDiagnosticReport
from party_player.system_dependencies import DependencyStatus
from party_player.capability_snapshots import CapabilitySnapshotState
from party_player.ui.responsive_dialog import apply_responsive_dialog_geometry, bind_dialog_escape


@dataclass(frozen=True, slots=True)
class VlcCandidateChoice:
    label: str
    directory: str


def valid_vlc_candidate_choices(
    report: SystemDiagnosticReport,
) -> tuple[VlcCandidateChoice, ...]:
    """Return valid VLC candidates in the locator's deterministic rank order."""
    choices: list[VlcCandidateChoice] = []
    seen: set[str] = set()
    for rank, candidate in enumerate(report.resolution.vlc.attempts, start=1):
        directory = candidate.installation_directory
        if candidate.status != DependencyStatus.AVAILABLE or directory is None:
            continue
        normalized = str(directory)
        deduplication_key = normalized.casefold()
        if deduplication_key in seen:
            continue
        seen.add(deduplication_key)
        source = candidate.source or "unbekannt"
        version = candidate.version or "Version unbekannt"
        choices.append(
            VlcCandidateChoice(
                f"{rank}. {source} · {version} · {normalized}",
                normalized,
            )
        )
    return tuple(choices)


def format_runtime_program_status(
    *,
    selection_mode: str,
    active_status: str,
    active_source: str | None,
    active_path: object | None,
    active_version: str | None,
    next_status: str,
    next_source: str | None,
    next_path: object | None,
    next_version: str | None,
) -> str:
    """Render active and next-start state without implying a live tool swap."""
    return (
        "Aktiv in dieser Sitzung:\n"
        f"  Status: {active_status} · Quelle: {active_source or '—'} · "
        f"Version: {active_version or 'unbekannt'}\n"
        f"  Pfad: {active_path or '—'}\n"
        f"Für nächsten Start ({selection_mode}):\n"
        f"  Status: {next_status} · Quelle: {next_source or '—'} · "
        f"Version: {next_version or 'unbekannt'}\n"
        f"  Pfad: {next_path or '—'}"
    )


class ExternalProgramsDialog(ctk.CTkToplevel):  # type: ignore[misc]
    def __init__(
        self,
        parent: ctk.CTk,
        settings: Callable[[], DependencySettings],
        initial_report: SystemDiagnosticReport,
        check: Callable[[], SystemDiagnosticReport],
        select_vlc: Callable[[str], SystemDiagnosticReport],
        select_ffmpeg: Callable[[str], SystemDiagnosticReport],
        reset_vlc: Callable[[], SystemDiagnosticReport],
        reset_ffmpeg: Callable[[], SystemDiagnosticReport],
        can_change_vlc: Callable[[], bool],
        can_change_ffmpeg: Callable[[], bool],
        capability_snapshots: CapabilitySnapshotState,
    ) -> None:
        super().__init__(parent)
        self.title("Einstellungen – System / Externe Programme")
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=(900, 680), minimum_size=(620, 440)
        )
        self.transient(parent)
        self._settings = settings
        self._active_report = initial_report
        self._report = initial_report
        self._check = check
        self._select_vlc = select_vlc
        self._select_ffmpeg = select_ffmpeg
        self._reset_vlc = reset_vlc
        self._reset_ffmpeg = reset_ffmpeg
        self._can_change_vlc = can_change_vlc
        self._can_change_ffmpeg = can_change_ffmpeg
        self._capability_snapshots = capability_snapshots
        self._results: SimpleQueue[tuple[int, SystemDiagnosticReport | BaseException]] = (
            SimpleQueue()
        )
        self._running = False
        self._closed = False
        self._generation = capability_snapshots.view().pending_generation
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            self,
            text="System / Externe Programme",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(20, 8), sticky="w")
        self._content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._content.grid(row=1, column=0, padx=4, sticky="nsew")
        self._content.grid_columnconfigure(0, weight=1)
        self._vlc = self._program_group("VLC / libVLC", 0)
        self._ffmpeg = self._program_group("FFmpeg / FFprobe", 1)
        self._vlc_candidate_paths: dict[str, str] = {}
        self._vlc_candidates = ctk.CTkOptionMenu(
            self._vlc["frame"],
            values=["Keine gültige Installation erkannt"],
        )
        self._vlc_candidates.grid(row=1, column=1, padx=12, pady=(0, 10), sticky="ew")
        self._vlc_candidate_button = ctk.CTkButton(
            self._vlc["frame"],
            text="Erkannte auswählen",
            width=130,
            command=self._select_detected_vlc,
            state="disabled",
        )
        self._vlc_candidate_button.grid(row=1, column=2, padx=6, pady=(0, 10))
        self._message = ctk.CTkLabel(self._content, text="", wraplength=800, justify="left")
        self._message.grid(row=2, column=0, padx=20, pady=8, sticky="w")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, padx=20, pady=(8, 20), sticky="ew")
        self._check_button = ctk.CTkButton(
            actions, text="Prüfen", command=lambda: self._start(self._check)
        )
        self._check_button.pack(side="left")
        ctk.CTkButton(actions, text="Schließen", command=self._close).pack(side="right")
        bind_dialog_escape(self, self._close)
        self._render()

    def _program_group(self, title: str, row: int) -> dict[str, Any]:
        frame = ctk.CTkFrame(self._content)
        frame.grid(row=row, column=0, padx=20, pady=8, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frame, text=title, font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=12, pady=10, sticky="w"
        )
        status = ctk.CTkLabel(frame, text="", justify="left", wraplength=500)
        status.grid(row=0, column=1, padx=12, pady=10, sticky="w")
        browse = ctk.CTkButton(frame, text="Durchsuchen", width=100)
        browse.grid(row=0, column=2, padx=6, pady=10)
        reset = ctk.CTkButton(frame, text="Benutzerpfad zurücksetzen", width=175)
        reset.grid(row=0, column=3, padx=(6, 12), pady=10)
        automatic = ctk.CTkButton(frame, text="Automatisch suchen", width=130)
        automatic.grid(row=1, column=3, padx=(6, 12), pady=(0, 10))
        return {
            "frame": frame,
            "status": status,
            "browse": browse,
            "reset": reset,
            "automatic": automatic,
        }

    def _render(self) -> None:
        configured = self._settings()
        snapshot = self._report.dependencies
        capability_view = self._capability_snapshots.view()
        active_snapshot = capability_view.active
        snapshot = capability_view.pending or snapshot
        vlc_text = format_runtime_program_status(
            selection_mode=configured.vlc_selection_mode.value,
            active_status=active_snapshot.vlc.status.value,
            active_source=active_snapshot.vlc.source,
            active_path=active_snapshot.vlc.installation_directory,
            active_version=active_snapshot.vlc.version,
            next_status=snapshot.vlc.status.value,
            next_source=snapshot.vlc.source,
            next_path=snapshot.vlc.installation_directory,
            next_version=snapshot.vlc.version,
        )
        ffmpeg_text = format_runtime_program_status(
            selection_mode=configured.ffmpeg_selection_mode.value,
            active_status=active_snapshot.ffmpeg.status.value,
            active_source=active_snapshot.ffmpeg.source,
            active_path=active_snapshot.ffmpeg.executable_path,
            active_version=active_snapshot.ffmpeg.version,
            next_status=snapshot.ffmpeg.status.value,
            next_source=snapshot.ffmpeg.source,
            next_path=snapshot.ffmpeg.executable_path,
            next_version=snapshot.ffmpeg.version,
        )
        self._vlc["status"].configure(text=vlc_text)
        self._ffmpeg["status"].configure(text=ffmpeg_text)
        self._vlc["browse"].configure(command=self._choose_vlc)
        self._ffmpeg["browse"].configure(command=self._choose_ffmpeg)
        self._vlc["reset"].configure(command=self._reset_vlc_safely)
        self._ffmpeg["reset"].configure(command=self._reset_ffmpeg_safely)
        self._vlc["automatic"].configure(command=self._reset_vlc_safely)
        self._ffmpeg["automatic"].configure(command=self._reset_ffmpeg_safely)
        choices = valid_vlc_candidate_choices(self._report)
        self._vlc_candidate_paths = {choice.label: choice.directory for choice in choices}
        if choices:
            labels = [choice.label for choice in choices]
            self._vlc_candidates.configure(values=labels, state="normal")
            self._vlc_candidates.set(labels[0])
            self._vlc_candidate_button.configure(state="normal")
        else:
            missing = "Keine gültige Installation erkannt"
            self._vlc_candidates.configure(values=[missing], state="disabled")
            self._vlc_candidates.set(missing)
            self._vlc_candidate_button.configure(state="disabled")
        if capability_view.restart_required:
            self._message.configure(
                text=(
                    "Konfiguration validiert und gespeichert. Neustart erforderlich: "
                    "Die aktive Sitzung bleibt bis zum Beenden unverändert."
                )
            )

    def _select_detected_vlc(self) -> None:
        if not self._can_change_vlc():
            self._message.configure(
                text="VLC kann während aktiver Audioaktionen nicht geändert werden."
            )
            return
        directory = self._vlc_candidate_paths.get(self._vlc_candidates.get())
        if directory is not None:
            self._start(lambda: self._select_vlc(directory))

    def _choose_vlc(self) -> None:
        if not self._can_change_vlc():
            self._message.configure(
                text="VLC kann während Wiedergabe, Übergang, Recovery oder Notfallaktion nicht geändert werden."
            )
            return
        directory = filedialog.askdirectory(parent=self, title="VLC-Verzeichnis auswählen")
        if directory:
            self._start(lambda: self._select_vlc(directory))

    def _choose_ffmpeg(self) -> None:
        if not self._can_change_ffmpeg():
            self._message.configure(
                text="FFmpeg kann während laufender Cue- oder Lautheitsanalysen nicht geändert werden."
            )
            return
        directory = filedialog.askdirectory(parent=self, title="FFmpeg-bin auswählen")
        if directory:
            self._start(lambda: self._select_ffmpeg(directory))

    def _reset_vlc_safely(self) -> None:
        if not self._can_change_vlc():
            self._message.configure(
                text="VLC kann während aktiver Audioaktionen nicht geändert werden."
            )
            return
        self._start(self._reset_vlc)

    def _reset_ffmpeg_safely(self) -> None:
        if not self._can_change_ffmpeg():
            self._message.configure(
                text="FFmpeg kann während laufender Analysen nicht geändert werden."
            )
            return
        self._start(self._reset_ffmpeg)

    def _start(self, operation: Callable[[], SystemDiagnosticReport]) -> None:
        if self._running:
            return
        self._running = True
        self._generation += 1
        generation = self._generation
        self._check_button.configure(state="disabled")
        self._message.configure(text="Prüfung läuft …")
        Thread(target=self._run, args=(generation, operation), daemon=True).start()
        self.after(50, self._poll)

    def _run(self, generation: int, operation: Callable[[], SystemDiagnosticReport]) -> None:
        try:
            self._results.put((generation, operation()))
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
        self._running = False
        self._check_button.configure(state="normal")
        if isinstance(result, BaseException):
            self._message.configure(text=f"Änderung fehlgeschlagen: {result}")
            return
        if not self._capability_snapshots.publish_pending(generation, result.dependencies):
            return
        self._report = result
        self._message.configure(
            text="Konfiguration geprüft; die aktive Laufzeitkonfiguration ist unverändert."
        )
        self._render()

    def _close(self) -> None:
        self._closed = True
        self._generation += 1
        self.destroy()
