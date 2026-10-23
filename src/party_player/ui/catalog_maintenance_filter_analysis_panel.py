"""Presentation-only filter and analysis controls for catalog maintenance."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from typing import Any, Literal, Protocol

import customtkinter as ctk  # type: ignore[import-untyped]


LOGGER = logging.getLogger(__name__)
ButtonState = Literal["normal", "disabled"]
_ANALYSIS_BUTTON_HEIGHT = 32
_ANALYSIS_CONTROL_PAD = 3


class AnalysisActions(Protocol):
    @property
    def analyze_cues_outdated(self) -> Callable[[], None]: ...

    @property
    def analyze_cues_all(self) -> Callable[[], None]: ...

    @property
    def cancel_cues(self) -> Callable[[], None]: ...

    @property
    def analyze_loudness_outdated(self) -> Callable[[], None]: ...

    @property
    def analyze_loudness_all(self) -> Callable[[], None]: ...

    @property
    def cancel_loudness(self) -> Callable[[], None]: ...


@dataclass(frozen=True, slots=True)
class CatalogFilterValues:
    """Unparsed values entered in the catalog-maintenance filter controls."""

    queue: str
    search: str
    field: str
    source: str
    status: str
    value: str
    confirmed: str
    conflict: str
    suggestion: str
    minimum_confidence: str
    changed_from: str
    changed_to: str
    minimum_bpm: str
    maximum_bpm: str


def _labeled_entry(
    parent: Any, label: str, *, placeholder: str = ""
) -> tuple[ctk.CTkFrame, ctk.CTkEntry]:
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(frame, text=label, anchor="w").grid(
        row=0, column=0, padx=2, pady=(0, 1), sticky="ew"
    )
    entry = ctk.CTkEntry(frame, placeholder_text=placeholder)
    entry.grid(row=1, column=0, sticky="ew")
    return frame, entry


def _analysis_callback(
    action: Callable[[], None], on_error: Callable[[Exception], None]
) -> Callable[[], None]:
    def callback() -> None:
        CatalogMaintenanceFilterAnalysisPanel.run_analysis_action(action, on_error)

    return callback


class CatalogMaintenanceFilterAnalysisPanel(ctk.CTkFrame):  # type: ignore[misc]
    """Render catalog filter controls and expose their presentation state."""

    def __init__(
        self,
        parent: Any,
        *,
        title: str,
        work_queue_labels: Mapping[Any, str],
        field_labels: Mapping[Any, str],
        source_labels: Mapping[Any, str],
        status_labels: Mapping[Any, str],
        suggestion_labels: Mapping[str, str],
        apply_filter: Callable[[], None],
        reset_filters: Callable[[], None],
        analysis_actions: AnalysisActions | None,
        metadata_analysis_available: bool,
        analysis_error: Callable[[Exception], None],
        prepare_tempo: Callable[[], None],
        pause_tempo: Callable[[], None],
        resume_tempo: Callable[[], None],
        abort_tempo: Callable[[], None],
        resume_pending_tempo: Callable[[], None],
        discard_pending_tempo: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, anchor="w").grid(
            row=0, column=0, padx=8, pady=(6, 3), sticky="ew"
        )
        self.grid(row=0, column=0, padx=12, pady=(8, 6), sticky="ew")
        self._compact = False
        self._filter_details_visible = False

        self._queue = ctk.CTkOptionMenu(
            self,
            values=["Alle Arbeitsvorräte", *work_queue_labels.values()],
            command=lambda _value: apply_filter(),
        )
        self._queue.grid(row=1, column=0, padx=5, pady=5, sticky="ew")
        self._search = ctk.CTkEntry(self, placeholder_text="Titel oder Interpret")
        self._search.grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        self.grid_columnconfigure(1, weight=1)
        self._counts = ctk.CTkLabel(
            self,
            text="Arbeitsvorräte werden gezählt …",
            anchor="w",
            justify="left",
            wraplength=900,
        )
        self._counts.grid(row=2, column=0, columnspan=4, padx=5, pady=(0, 5), sticky="ew")
        self._filter_toggle = ctk.CTkButton(
            self,
            text="Weitere Filter anzeigen ▾",
            command=lambda: self.toggle_filter_details(None),
            anchor="w",
        )
        self._filter_toggle.grid(row=3, column=0, columnspan=4, padx=3, pady=(2, 4), sticky="ew")
        self._filter_wrapper = ctk.CTkFrame(self, height=170)
        self._filter_wrapper.grid(row=4, column=0, columnspan=4, sticky="ew")
        self._filter_wrapper.grid_propagate(False)
        self._filter_details = ctk.CTkScrollableFrame(self._filter_wrapper, height=150)
        self._filter_details.pack(fill="both", expand=True)
        self._filter_details.grid_columnconfigure(0, weight=1)
        self._filter_wrapper.grid_remove()
        advanced = ctk.CTkFrame(self._filter_details, fg_color="transparent")
        advanced.grid(row=1, column=0, padx=3, pady=2, sticky="ew")
        for column in range(4):
            advanced.grid_columnconfigure(column, weight=1)
        self._filter_field = ctk.CTkOptionMenu(
            advanced, values=["Alle Felder", *field_labels.values()]
        )
        self._filter_source = ctk.CTkOptionMenu(
            advanced, values=["Alle Quellen", *source_labels.values()]
        )
        self._filter_status = ctk.CTkOptionMenu(
            advanced, values=["Alle Prüfstatus", *status_labels.values()]
        )
        self._filter_value = ctk.CTkOptionMenu(
            advanced, values=["Wert egal", "Mit Wert", "Ohne Wert"]
        )
        self._filter_confirmed = ctk.CTkOptionMenu(
            advanced, values=["Bestätigung egal", "Bestätigt", "Nicht bestätigt"]
        )
        self._filter_conflict = ctk.CTkOptionMenu(
            advanced, values=["Konflikt egal", "Mit Konflikt", "Ohne Konflikt"]
        )
        self._filter_suggestion = ctk.CTkOptionMenu(
            advanced, values=["Vorschlag egal", *suggestion_labels.values()]
        )
        for widget, row, column in (
            (self._filter_field, 0, 0),
            (self._filter_source, 0, 1),
            (self._filter_status, 0, 2),
            (self._filter_value, 0, 3),
            (self._filter_confirmed, 1, 0),
            (self._filter_conflict, 1, 1),
            (self._filter_suggestion, 1, 2),
        ):
            widget.grid(row=row, column=column, padx=3, pady=2, sticky="ew")
        confidence, self._filter_confidence = _labeled_entry(
            advanced, "Min. Konfidenz", placeholder="0–1"
        )
        changed_from, self._filter_changed_from = _labeled_entry(
            advanced, "Geändert von", placeholder="JJJJ-MM-TT"
        )
        changed_to, self._filter_changed_to = _labeled_entry(
            advanced, "Geändert bis", placeholder="JJJJ-MM-TT"
        )
        bpm_from, self._filter_bpm_from = _labeled_entry(advanced, "BPM von", placeholder="20–300")
        bpm_to, self._filter_bpm_to = _labeled_entry(advanced, "BPM bis", placeholder="20–300")
        confidence.grid(row=1, column=3, padx=3, pady=2, sticky="ew")
        changed_from.grid(row=2, column=0, columnspan=2, padx=3, pady=2, sticky="ew")
        changed_to.grid(row=2, column=2, columnspan=2, padx=3, pady=2, sticky="ew")
        bpm_from.grid(row=3, column=0, columnspan=2, padx=3, pady=2, sticky="ew")
        bpm_to.grid(row=3, column=2, columnspan=2, padx=3, pady=2, sticky="ew")
        self._analysis_host = ctk.CTkFrame(self._filter_details, fg_color="transparent")
        self._analysis_host.grid_columnconfigure(0, weight=1)
        self._analysis_host.grid(row=2, column=0, sticky="ew")
        self._build_analysis(
            analysis_actions,
            metadata_analysis_available=metadata_analysis_available,
            analysis_error=analysis_error,
            prepare_tempo=prepare_tempo,
            pause_tempo=pause_tempo,
            resume_tempo=resume_tempo,
            abort_tempo=abort_tempo,
            resume_pending_tempo=resume_pending_tempo,
            discard_pending_tempo=discard_pending_tempo,
        )
        self._filter_actions = ctk.CTkFrame(self, fg_color="transparent")
        self._filter_actions.grid(row=1, column=2, columnspan=2, padx=2, pady=0, sticky="e")
        ctk.CTkButton(self._filter_actions, text="Filtern", command=apply_filter).pack(
            side="left", padx=3
        )
        ctk.CTkButton(self._filter_actions, text="Filter zurücksetzen", command=reset_filters).pack(
            side="left", padx=3
        )

    def _build_analysis(
        self,
        actions: AnalysisActions | None,
        *,
        metadata_analysis_available: bool,
        analysis_error: Callable[[Exception], None],
        prepare_tempo: Callable[[], None],
        pause_tempo: Callable[[], None],
        resume_tempo: Callable[[], None],
        abort_tempo: Callable[[], None],
        resume_pending_tempo: Callable[[], None],
        discard_pending_tempo: Callable[[], None],
    ) -> None:
        self._analysis_toggle = ctk.CTkButton(
            self._analysis_host,
            text="Audioanalyse anzeigen ▾",
            command=self.toggle_analysis,
            anchor="w",
        )
        self._analysis_toggle.grid(row=0, column=0, padx=3, pady=(5, 2), sticky="ew")
        panel = ctk.CTkFrame(self._analysis_host)
        self._analysis_panel = panel
        panel.grid(row=1, column=0, padx=3, pady=3, sticky="ew")
        panel.grid_columnconfigure(1, weight=1)
        definitions = (
            (
                "Cues",
                "Neue/veraltete Cues",
                actions.analyze_cues_outdated if actions else None,
                "Alle Cues neu",
                actions.analyze_cues_all if actions else None,
                "Cue-Analyse abbrechen",
                actions.cancel_cues if actions else None,
            ),
            (
                "Lautheit",
                "Neue/veraltete Lautheit",
                actions.analyze_loudness_outdated if actions else None,
                "Alle Lautheiten neu",
                actions.analyze_loudness_all if actions else None,
                "Lautheit abbrechen",
                actions.cancel_loudness if actions else None,
            ),
        )
        for row, definition in enumerate(definitions):
            label, first_text, first, all_text, all_action, cancel_text, cancel = definition
            ctk.CTkLabel(panel, text=label, width=75, anchor="w").grid(
                row=row, column=0, padx=6, pady=3, sticky="w"
            )
            controls = ctk.CTkFrame(panel, fg_color="transparent")
            controls.grid(row=row, column=1, padx=3, pady=3, sticky="w")
            for text, command, danger in (
                (first_text, first, False),
                (all_text, all_action, False),
                (cancel_text, cancel, True),
            ):
                callback = (
                    _analysis_callback(command, analysis_error) if command is not None else None
                )
                ctk.CTkButton(
                    controls,
                    text=text,
                    command=callback,
                    state="normal" if command is not None else "disabled",
                    fg_color="#7d3030" if danger else None,
                    height=_ANALYSIS_BUTTON_HEIGHT,
                ).grid(
                    row=0,
                    column=controls.grid_size()[0],
                    padx=_ANALYSIS_CONTROL_PAD,
                    sticky="ew",
                )
            for column in range(3):
                controls.grid_columnconfigure(column, weight=1, uniform="analysis-actions")
            controls.grid_configure(sticky="ew")
        ctk.CTkLabel(panel, text="Tempo", width=75, anchor="w").grid(
            row=2, column=0, padx=6, pady=3, sticky="w"
        )
        bpm = ctk.CTkFrame(panel, fg_color="transparent")
        bpm.grid(row=2, column=1, padx=3, pady=3, sticky="ew")
        for column in range(2):
            bpm.grid_columnconfigure(column, weight=1, uniform="tempo-setup")
        self._tempo_profile = ctk.CTkOptionMenu(
            bpm, values=("Tempo", "Tempo und experimentelle Energie")
        )
        self._tempo_profile.grid(row=0, column=0, padx=3, pady=2, sticky="ew")
        self._tempo_scope = ctk.CTkOptionMenu(
            bpm,
            values=("Vollständige Aufnahme", "Wirksame Katalog-Cue-Bereiche"),
        )
        self._tempo_scope.grid(row=0, column=1, padx=3, pady=2, sticky="ew")
        self._tempo_skip_current = ctk.CTkSwitch(bpm, text="Aktuelle Ergebnisse überspringen")
        self._tempo_skip_current.select()
        self._tempo_skip_current.grid(row=1, column=0, padx=6, pady=2, sticky="w")
        available = "normal" if metadata_analysis_available else "disabled"
        self._tempo_start = ctk.CTkButton(
            bpm,
            text="Auswahl analysieren …",
            command=prepare_tempo,
            state=available,
            height=_ANALYSIS_BUTTON_HEIGHT,
        )
        self._tempo_start.grid(row=1, column=1, padx=3, pady=2, sticky="ew")
        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.grid(row=3, column=1, padx=3, pady=3, sticky="ew")
        for column in range(6):
            controls.grid_columnconfigure(column, weight=1, uniform="tempo-actions")
        self._tempo_pause = ctk.CTkButton(
            controls,
            text="Pause",
            state="disabled",
            command=pause_tempo,
            height=_ANALYSIS_BUTTON_HEIGHT,
        )
        self._tempo_resume = ctk.CTkButton(
            controls,
            text="Fortsetzen",
            state="disabled",
            command=resume_tempo,
            height=_ANALYSIS_BUTTON_HEIGHT,
        )
        self._tempo_abort = ctk.CTkButton(
            controls,
            text="Gesamten Auftrag abbrechen",
            state="disabled",
            fg_color="#7d3030",
            command=abort_tempo,
            height=_ANALYSIS_BUTTON_HEIGHT,
        )
        self._tempo_pending_resume = ctk.CTkButton(
            controls,
            text="Wartende Runs fortsetzen",
            command=resume_pending_tempo,
            state=available,
            height=_ANALYSIS_BUTTON_HEIGHT,
        )
        self._tempo_pending_discard = ctk.CTkButton(
            controls,
            text="Wartende Runs verwerfen",
            fg_color="#6b5b2a",
            command=discard_pending_tempo,
            state=available,
            height=_ANALYSIS_BUTTON_HEIGHT,
        )
        for button, row, column, columnspan in (
            (self._tempo_pause, 0, 0, 2),
            (self._tempo_resume, 0, 2, 2),
            (self._tempo_abort, 0, 4, 2),
            (self._tempo_pending_resume, 1, 0, 3),
            (self._tempo_pending_discard, 1, 3, 3),
        ):
            button.grid(
                row=row,
                column=column,
                columnspan=columnspan,
                padx=_ANALYSIS_CONTROL_PAD,
                pady=2,
                sticky="ew",
            )
        self._tempo_progress = ctk.CTkLabel(
            panel,
            text="Tempoanalyse startet nur nach ausdrücklicher Bedienung. Ergebnisse bleiben Vorschläge.",
            text_color="#9fb3c8",
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self._tempo_progress.grid(row=4, column=0, columnspan=2, padx=6, pady=(2, 6), sticky="ew")
        panel.grid_remove()

    def toggle_analysis(self) -> None:
        if self._analysis_panel.winfo_ismapped():
            self._analysis_panel.grid_remove()
            self._analysis_toggle.configure(text="Audioanalyse anzeigen ▾")
        else:
            self._analysis_panel.grid()
            self._analysis_toggle.configure(text="Audioanalyse ausblenden ▴")

    @staticmethod
    def run_analysis_action(
        action: Callable[[], None], on_error: Callable[[Exception], None]
    ) -> None:
        try:
            action()
        except Exception as error:
            on_error(error)

    def render_tempo_state(
        self,
        text: str | None,
        *,
        start: ButtonState | None = None,
        pause: ButtonState | None = None,
        resume: ButtonState | None = None,
        abort: ButtonState | None = None,
    ) -> None:
        if text is not None:
            self._tempo_progress.configure(text=text)
        for widget, state in (
            (self._tempo_start, start),
            (self._tempo_pause, pause),
            (self._tempo_resume, resume),
            (self._tempo_abort, abort),
        ):
            if state is not None:
                widget.configure(state=state)

    def read_filter_values(self) -> CatalogFilterValues:
        return CatalogFilterValues(
            self._queue.get(),
            self._search.get(),
            self._filter_field.get(),
            self._filter_source.get(),
            self._filter_status.get(),
            self._filter_value.get(),
            self._filter_confirmed.get(),
            self._filter_conflict.get(),
            self._filter_suggestion.get(),
            self._filter_confidence.get(),
            self._filter_changed_from.get(),
            self._filter_changed_to.get(),
            self._filter_bpm_from.get(),
            self._filter_bpm_to.get(),
        )

    def reset_filter_inputs(self) -> None:
        defaults = (
            (self._queue, "Alle Arbeitsvorräte"),
            (self._filter_field, "Alle Felder"),
            (self._filter_source, "Alle Quellen"),
            (self._filter_status, "Alle Prüfstatus"),
            (self._filter_value, "Wert egal"),
            (self._filter_confirmed, "Bestätigung egal"),
            (self._filter_conflict, "Konflikt egal"),
            (self._filter_suggestion, "Vorschlag egal"),
        )
        for widget, value in defaults:
            widget.set(value)
        for entry in (
            self._search,
            self._filter_confidence,
            self._filter_changed_from,
            self._filter_changed_to,
            self._filter_bpm_from,
            self._filter_bpm_to,
        ):
            entry.delete(0, "end")

    def toggle_filter_details(self, results_panel: Any | None) -> None:
        self._filter_details_visible = not self._filter_details_visible
        if self._filter_details_visible:
            self._filter_wrapper.grid()
            self._filter_toggle.configure(text="Weitere Filter ausblenden ▴")
        else:
            self._filter_wrapper.grid_remove()
            self._filter_toggle.configure(text="Weitere Filter anzeigen ▾")
        if self._compact:
            self.configure(height=275 if self._filter_details_visible else 105)
            self._filter_wrapper.configure(height=170)
            if results_panel is not None:
                results_panel.configure(height=300)
        self.refresh_layout(results_panel)

    def set_compact_layout(self, compact: bool, results_panel: Any) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        if compact:
            self.grid_propagate(False)
            self.configure(height=275 if self._filter_details_visible else 105)
            self._filter_wrapper.configure(height=170)
            results_panel.configure(height=300)
            self._filter_actions.grid_configure(row=2, column=0, columnspan=2, sticky="w")
            self._counts.grid_configure(row=3, column=0, columnspan=4)
            self._filter_toggle.grid_configure(row=4)
            self._filter_wrapper.grid_configure(row=5)
        else:
            self.grid_propagate(True)
            self._filter_wrapper.grid_propagate(False)
            self._filter_wrapper.configure(height=240)
            results_panel.grid_propagate(True)
            self._filter_actions.grid_configure(row=1, column=2, columnspan=2, sticky="e")
            self._counts.grid_configure(row=2, column=0, columnspan=4)
            self._filter_toggle.grid_configure(row=3)
            self._filter_wrapper.grid_configure(row=4)
        self.refresh_layout(results_panel)

    def refresh_layout(self, results_panel: Any | None) -> None:
        self.update_idletasks()
        self._filter_details.update_idletasks()
        if self._compact and results_panel is not None:
            LOGGER.info(
                "Katalogpflege compact layout heights filter_details=%s filter_wrapper=%s "
                "results=%s selection_area=%s",
                self._filter_details.winfo_height(),
                self._filter_wrapper.winfo_height(),
                results_panel.winfo_height(),
                results_panel.winfo_reqheight(),
            )

    queue = property(lambda self: self._queue)
    search = property(lambda self: self._search)
    filter_field = property(lambda self: self._filter_field)
    filter_source = property(lambda self: self._filter_source)
    filter_status = property(lambda self: self._filter_status)
    filter_value = property(lambda self: self._filter_value)
    filter_confirmed = property(lambda self: self._filter_confirmed)
    filter_conflict = property(lambda self: self._filter_conflict)
    filter_suggestion = property(lambda self: self._filter_suggestion)
    filter_confidence = property(lambda self: self._filter_confidence)
    filter_changed_from = property(lambda self: self._filter_changed_from)
    filter_changed_to = property(lambda self: self._filter_changed_to)
    filter_bpm_from = property(lambda self: self._filter_bpm_from)
    filter_bpm_to = property(lambda self: self._filter_bpm_to)
    filter_toggle = property(lambda self: self._filter_toggle)
    filter_wrapper = property(lambda self: self._filter_wrapper)
    filter_details = property(lambda self: self._filter_details)
    filter_actions = property(lambda self: self._filter_actions)
    counts = property(lambda self: self._counts)
    analysis_toggle = property(lambda self: self._analysis_toggle)
    analysis_panel = property(lambda self: self._analysis_panel)
    tempo_profile = property(lambda self: self._tempo_profile)
    tempo_scope = property(lambda self: self._tempo_scope)
    tempo_skip_current = property(lambda self: self._tempo_skip_current)
    tempo_start = property(lambda self: self._tempo_start)
    tempo_pause = property(lambda self: self._tempo_pause)
    tempo_resume = property(lambda self: self._tempo_resume)
    tempo_abort = property(lambda self: self._tempo_abort)
    tempo_pending_resume = property(lambda self: self._tempo_pending_resume)
    tempo_pending_discard = property(lambda self: self._tempo_pending_discard)
    tempo_progress = property(lambda self: self._tempo_progress)
