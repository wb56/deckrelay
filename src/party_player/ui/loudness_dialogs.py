"""Dialogs for per-track loudness and normalization settings."""

from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.analysis.loudness_service import LoudnessAnalysisJob
from party_player.controllers.loudness_controller import LoudnessController
from party_player.ui.tooltip import Tooltip


def _parse_gain_db(raw: str) -> float:
    normalized = raw.strip().replace("−", "-")
    if normalized.casefold().endswith("db"):
        normalized = normalized[:-2].strip()
    if not normalized:
        raise ValueError("Bitte einen Gain-Wert eingeben.")
    try:
        return float(normalized.replace(",", "."))
    except ValueError as exc:
        raise ValueError("Ungültiger Gain-Wert. Beispiele: −4, −4 dB oder −4,5 dB.") from exc


class LoudnessDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Edit or reset a manual per-track gain without a native system dialog."""

    def __init__(self, parent: Any, controller: LoudnessController, track_id: int) -> None:
        super().__init__(parent)
        self._controller = controller
        self._track_id = track_id
        state = controller.state(track_id)
        self.title("Lautstärkeanpassung")
        self.geometry("560x390")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text=state.title, font=("Segoe UI", 18, "bold"), anchor="w").grid(
            row=0, column=0, padx=24, pady=(24, 12), sticky="ew"
        )
        self._status = ctk.CTkLabel(
            self,
            text=(
                f"Aktive Quelle: {state.source_text}\n"
                f"Metadatenstatus: {state.metadata_status_text}\n"
                f"Effektiver Gain: {state.resolved.effective_gain_db:+.2f} dB\n"
                f"{state.clip_protection_text}"
            ),
            justify="left",
            anchor="w",
            text_color="#b8c7d9",
        )
        self._status.grid(row=1, column=0, padx=24, pady=(0, 18), sticky="ew")
        self._analysis_job: LoudnessAnalysisJob | None = None
        ctk.CTkLabel(
            self,
            text="Manuelle Korrektur von −12 bis +12 dB:",
            anchor="w",
        ).grid(row=2, column=0, padx=24, pady=(0, 6), sticky="ew")
        self._gain = ctk.CTkEntry(self, placeholder_text="z. B. −2,50")
        self._gain.grid(row=3, column=0, padx=24, pady=(0, 6), sticky="ew")
        if state.manual_gain_db is not None:
            self._gain.insert(0, f"{state.manual_gain_db:.2f}")
        self._error = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            text_color="#ff8585",
            wraplength=500,
        )
        self._error.grid(row=4, column=0, padx=24, pady=(2, 14), sticky="ew")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, padx=24, pady=(8, 24), sticky="e")
        ctk.CTkButton(
            buttons,
            text="Abbrechen",
            fg_color="#555555",
            command=self._cancel,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            buttons,
            text="Speichern",
            fg_color="#2f7d4f",
            command=self._save,
        ).pack(side="right", padx=6)
        ctk.CTkButton(
            buttons,
            text="Zurücksetzen",
            fg_color="#7d3030",
            command=self._reset,
        ).pack(side="right", padx=(0, 6))
        self._analysis_button = ctk.CTkButton(
            buttons,
            text="EBU R128 analysieren",
            command=self._analyze,
        )
        self._analysis_button.pack(side="right", padx=(0, 6))
        analysis_available, analysis_message = controller.analysis_availability()
        if not analysis_available:
            self._analysis_button.configure(state="disabled")
            self._error.configure(text=analysis_message)
            Tooltip(self._analysis_button, analysis_message)

        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.grab_set()
        self._gain.focus_set()

    def _save(self) -> None:
        try:
            raw = self._gain.get().strip()
            if not raw:
                raise ValueError("Bitte einen Gain-Wert eingeben oder „Zurücksetzen“ wählen.")
            gain = _parse_gain_db(raw)
            self._controller.save_manual_gain(self._track_id, gain)
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self.destroy()

    def _reset(self) -> None:
        try:
            self._controller.save_manual_gain(self._track_id, None)
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self.destroy()

    def _analyze(self) -> None:
        self._analysis_button.configure(state="disabled")
        self._status.configure(text="EBU-R128-Analyse läuft im Hintergrund …")
        try:
            self._analysis_job = self._controller.analyze_track(
                self._track_id,
                self._analysis_completed,
            )
        except (ValueError, RuntimeError) as exc:
            self._analysis_button.configure(state="normal")
            self._error.configure(text=str(exc))

    def _analysis_completed(self, _result: object, error: str | None) -> None:
        if not self.winfo_exists():
            return
        self._analysis_job = None
        self._analysis_button.configure(state="normal")
        if error is not None:
            self._error.configure(text=error)
            self._status.configure(text="EBU-R128-Analyse fehlgeschlagen.")
            return
        state = self._controller.state(self._track_id)
        self._error.configure(text="")
        self._status.configure(
            text=(
                "EBU-R128-Analyse abgeschlossen.\n"
                f"Aktive Quelle: {state.source_text}\n"
                f"Effektiver Gain: {state.resolved.effective_gain_db:+.2f} dB\n"
                f"{state.clip_protection_text}"
            )
        )

    def _cancel(self) -> None:
        analysis_job = getattr(self, "_analysis_job", None)
        if analysis_job is not None:
            analysis_job.cancel()
        self.destroy()


class NormalizationSettingsDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Edit persistent normalization and safety settings in one transaction."""

    def __init__(self, parent: Any, controller: LoudnessController) -> None:
        super().__init__(parent)
        self._controller = controller
        state = controller.settings_state()
        self.title("Normalisierung einstellen")
        self.geometry("650x620")
        self.minsize(620, 590)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Lautheitsnormalisierung",
            font=("Segoe UI", 20, "bold"),
        ).grid(row=0, column=0, columnspan=2, padx=24, pady=(24, 16), sticky="w")
        self._enabled = ctk.CTkSwitch(self, text="Normalisierung aktiv")
        self._enabled.grid(row=1, column=0, columnspan=2, padx=24, pady=8, sticky="w")
        if state.enabled:
            self._enabled.select()
        self._clip_protection = ctk.CTkSwitch(self, text="Clip-Schutz aktiv")
        self._clip_protection.grid(row=1, column=1, padx=24, pady=8, sticky="e")
        if state.clip_protection_enabled:
            self._clip_protection.select()
        self._mode_labels = {"Titel": "TRACK", "Album": "ALBUM", "Aus": "OFF"}
        self._mode = ctk.CTkOptionMenu(self, values=list(self._mode_labels))
        self._mode.set(
            next(label for label, mode in self._mode_labels.items() if mode == state.mode)
        )
        self._mode.grid(row=2, column=1, padx=24, pady=8, sticky="ew")
        ctk.CTkLabel(self, text="Modus:", anchor="w").grid(
            row=2, column=0, padx=24, pady=8, sticky="w"
        )
        self._target_loudness = self._field(3, "Ziel-Lautheit (LUFS)", state.target_loudness_lufs)
        self._maximum_positive = self._field(
            4, "Maximale positive Verstärkung (dB)", state.maximum_positive_gain_db
        )
        self._maximum_negative = self._field(
            5, "Maximale negative Verstärkung (dB)", state.maximum_negative_gain_db
        )
        self._maximum_peak = self._field(
            6, "Absolute Peak-Grenze (dBFS)", state.maximum_output_peak_db
        )
        self._headroom = self._field(7, "Zusätzlicher Headroom (dB)", state.headroom_db)
        self._fallback = self._field(
            8, "Positiver Fallback ohne Peak (dB)", state.fallback_positive_gain_db
        )
        self._smoothing = self._field(9, "Gain-Glättung (Sekunden)", state.smoothing_seconds)
        ctk.CTkLabel(
            self,
            text=(
                "Sicherer Ausgangspegel = absolute Peak-Grenze minus Headroom. "
                "Änderungen an geladenen Titeln werden weich eingeblendet."
            ),
            justify="left",
            wraplength=590,
            text_color="#9fb3c8",
        ).grid(row=10, column=0, columnspan=2, padx=24, pady=(10, 4), sticky="w")
        self._error = ctk.CTkLabel(self, text="", text_color="#ff8585", wraplength=590, anchor="w")
        self._error.grid(row=11, column=0, columnspan=2, padx=24, pady=6, sticky="ew")
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=12, column=0, columnspan=2, padx=24, pady=(10, 24), sticky="e")
        ctk.CTkButton(buttons, text="Abbrechen", fg_color="#555555", command=self._cancel).pack(
            side="right", padx=(6, 0)
        )
        ctk.CTkButton(buttons, text="Speichern", fg_color="#2f7d4f", command=self._save).pack(
            side="right", padx=6
        )
        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.grab_set()

    def _field(self, row: int, label: str, value: float) -> ctk.CTkEntry:
        ctk.CTkLabel(self, text=f"{label}:", anchor="w").grid(
            row=row, column=0, padx=24, pady=7, sticky="w"
        )
        entry = ctk.CTkEntry(self)
        entry.insert(0, f"{value:.2f}")
        entry.grid(row=row, column=1, padx=24, pady=7, sticky="ew")
        return entry

    @staticmethod
    def _number(entry: Any) -> float:
        return float(entry.get().strip().replace(",", "."))

    def _save(self) -> None:
        try:
            self._controller.update_normalization_settings(
                enabled=bool(self._enabled.get()),
                clip_protection_enabled=bool(self._clip_protection.get()),
                mode=self._mode_labels[self._mode.get()],
                target_loudness_lufs=self._number(self._target_loudness),
                maximum_positive_gain_db=self._number(self._maximum_positive),
                maximum_negative_gain_db=self._number(self._maximum_negative),
                maximum_output_peak_db=self._number(self._maximum_peak),
                headroom_db=self._number(self._headroom),
                fallback_positive_gain_db=self._number(self._fallback),
                smoothing_seconds=self._number(self._smoothing),
            )
        except ValueError as exc:
            self._error.configure(text=str(exc))
            return
        self.destroy()

    def _cancel(self) -> None:
        self.destroy()
