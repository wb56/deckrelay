"""Presentation-only filter and analysis controls for catalog maintenance."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]


LOGGER = logging.getLogger(__name__)


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
        self._analysis_host.grid(row=2, column=0, sticky="ew")
        self._filter_actions = ctk.CTkFrame(self, fg_color="transparent")
        self._filter_actions.grid(row=1, column=2, columnspan=2, padx=2, pady=0, sticky="e")
        ctk.CTkButton(self._filter_actions, text="Filtern", command=apply_filter).pack(
            side="left", padx=3
        )
        ctk.CTkButton(self._filter_actions, text="Filter zurücksetzen", command=reset_filters).pack(
            side="left", padx=3
        )

    @property
    def analysis_host(self) -> Any:
        return self._analysis_host

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
        else:
            self.grid_propagate(True)
            self._filter_wrapper.grid_propagate(False)
            self._filter_wrapper.configure(height=240)
            results_panel.grid_propagate(True)
            self._filter_actions.grid_configure(row=1, column=2, columnspan=2, sticky="e")
            self._counts.grid_configure(row=2, column=0, columnspan=4)
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
