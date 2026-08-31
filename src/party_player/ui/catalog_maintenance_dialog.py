"""Responsive, worker-backed catalog-maintenance workspace."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any, cast

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.catalog_maintenance import (
    BatchAction,
    BatchPreview,
    BatchResult,
    CatalogMaintenanceService,
    MaintenanceFilter,
    MaintenancePage,
    MetadataBatchRequest,
    SelectionDescription,
    UndoPreview,
    WorkQueue,
    WorkQueueCount,
    format_metadata_value,
)
from party_player.metadata_rules import (
    MetadataFieldKey,
    MetadataReviewStatus,
    RecordingClassification,
    RecordingKind,
    RecordingTrait,
    normalize_metadata_value,
)
from party_player.metadata_editor import FIELD_LABELS, SOURCE_LABELS, STATUS_LABELS
from party_player.metadata_analysis_profiles import MetadataAnalysisProfile
from party_player.metadata_analysis_contracts import TempoAnalysisScope
from party_player.metadata_analysis_service import (
    MetadataAnalysisService,
    TempoBatchPreview,
    TempoBatchProgress,
)
from party_player.ui.dialogs import ask_silent_yes_no, ask_silent_yes_no_cancel
from party_player.ui.responsive_dialog import (
    apply_responsive_dialog_geometry,
    bind_dialog_escape,
    release_dialog,
)
from party_player.ui.tooltip import Tooltip


Submit = Callable[
    [Callable[[], object], Callable[[object], None], Callable[[Exception], None]], bool
]


@dataclass(frozen=True, slots=True)
class CatalogAnalysisActions:
    analyze_cues_outdated: Callable[[], None]
    analyze_cues_all: Callable[[], None]
    cancel_cues: Callable[[], None]
    analyze_loudness_outdated: Callable[[], None]
    analyze_loudness_all: Callable[[], None]
    cancel_loudness: Callable[[], None]


WORK_QUEUE_LABELS = dict(
    zip(
        WorkQueue,
        (
            "Fehlende ursprüngliche Erscheinungsjahre",
            "Unbestätigte ursprüngliche Erscheinungsjahre",
            "Bestätigte Leerwerte",
            "Offene Vorschläge",
            "Importkonflikte",
            "Widersprüchliche Metadaten",
            "Fehlende BPM-Werte",
            "Unsichere BPM-Werte",
            "Mögliche Halb-/Doppeltempo-Fälle",
            "Fehlende Hauptgenres",
            "Fehlende musikalische Dekaden",
            "Fehlende Energie",
            "Fehlende Tanzbarkeit",
            "Fehlende Bewertung",
            "Unvollständige Metadaten",
            "Veraltete Ergebnisse",
            "Fehlgeschlagene Analyseläufe",
            "Zuletzt manuell geändert",
        ),
        strict=True,
    )
)
BATCH_ACTION_LABELS = {
    BatchAction.SET: "Wert setzen",
    BatchAction.CONFIRM: "Vorhandenen Wert bestätigen",
    BatchAction.CONFIRM_EMPTY: "Bewusst ohne Wert bestätigen",
    BatchAction.REMOVE_MISSING: "Wert entfernen und als fehlend behandeln",
    BatchAction.MULTI_ADD: "Mehrfachwert hinzufügen",
    BatchAction.MULTI_REMOVE: "Mehrfachwert entfernen",
    BatchAction.MULTI_REPLACE: "Mehrfachwerte vollständig ersetzen",
    BatchAction.SUGGESTION_ACCEPT: "Vorhandene Vorschläge als Werte übernehmen",
    BatchAction.SUGGESTION_ACCEPT_CONFIRM: (
        "Vorhandene Vorschläge als Werte übernehmen und bestätigen"
    ),
    BatchAction.SUGGESTION_REJECT: "Vorschläge ablehnen",
    BatchAction.SUGGESTION_DEFER: "Vorschläge später prüfen",
}
HIGH_RISK_IDENTITY_FIELDS = frozenset(
    {MetadataFieldKey.TITLE, MetadataFieldKey.ARTIST, MetadataFieldKey.ALBUM}
)
SUGGESTION_STATUS_LABELS = {
    "PENDING": "Offen",
    "ACCEPTED": "Übernommen",
    "REJECTED": "Abgelehnt",
    "SUPERSEDED": "Abgelöst",
}


def high_risk_confirmation_text(preview: BatchPreview) -> str | None:
    if preview.request.action is not BatchAction.SET or preview.changeable <= 1:
        return None
    key = next(iter(preview.request.field_mask))
    if key not in HIGH_RISK_IDENTITY_FIELDS:
        return None
    target = dict(preview.request.values)[key]
    examples = "\n".join(
        f"• Titel-ID {item.track_id}: {format_metadata_value(item.field, item.before)}"
        for item in preview.examples[:5]
    )
    return (
        f"Du änderst {FIELD_LABELS[key]} bei {preview.selected} ausgewählten "
        f"Katalogeinträgen auf denselben Wert:\n\n{format_metadata_value(key, target)}\n\n"
        f"Tatsächlich änderbar: {preview.changeable}\n{examples}\n\n"
        "Alle betroffenen Katalogeinträge erhalten denselben Wert.\n"
        "Musikdateien und Tags werden nicht verändert.\n\n"
        "Ich möchte diesen Wert wirklich für alle angezeigten Titel setzen."
    )


def ask_filter_selection_strategy(parent: Any) -> str | None:
    keep = ask_silent_yes_no_cancel(
        parent,
        "Auswahl beim Filterwechsel",
        "Ja: Auswahl erhalten\nNein: Auswahl anpassen\nAbbrechen: Filterwechsel abbrechen",
    )
    if keep is None:
        return None
    if keep:
        return "KEEP"
    restrict = ask_silent_yes_no_cancel(
        parent,
        "Auswahl anpassen",
        "Ja: auf die neue Treffermenge beschränken\nNein: Auswahl verwerfen\nAbbrechen: Filterwechsel abbrechen",
    )
    if restrict is None:
        return None
    return "RESTRICT" if restrict else "DISCARD"


_VALUE_ACTIONS = frozenset(
    {
        BatchAction.SET,
        BatchAction.MULTI_ADD,
        BatchAction.MULTI_REMOVE,
        BatchAction.MULTI_REPLACE,
    }
)
_INTEGER_INPUT_HINTS = {
    MetadataFieldKey.YEAR: "Ausgabejahr muss eine ganze Zahl von 1877 bis 2100 sein.",
    MetadataFieldKey.ORIGINAL_RELEASE_YEAR: (
        "Ursprüngliches Erscheinungsjahr muss eine ganze Zahl von 1877 bis 2100 sein."
    ),
    MetadataFieldKey.ENERGY: "Energie muss eine ganze Zahl von 0 bis 100 sein.",
    MetadataFieldKey.DANCEABILITY: "Tanzbarkeit muss eine ganze Zahl von 0 bis 100 sein.",
    MetadataFieldKey.RATING: "Bewertung muss eine ganze Zahl von 1 bis 5 sein.",
}


def parse_batch_input(key: MetadataFieldKey, action: BatchAction, raw: str) -> object:
    """Parse one user-facing batch value and raise only German validation messages."""
    if action not in _VALUE_ACTIONS:
        return raw
    text = raw.strip()
    if not text:
        raise ValueError("Bitte einen Zielwert eingeben.")
    if key in _INTEGER_INPUT_HINTS:
        try:
            value: object = int(text)
        except ValueError as error:
            raise ValueError(_INTEGER_INPUT_HINTS[key]) from error
    elif key in {MetadataFieldKey.BPM, MetadataFieldKey.ALTERNATIVE_BPM}:
        try:
            value = float(text.replace(",", "."))
        except ValueError as error:
            raise ValueError("BPM muss eine Zahl von 20 bis 300 sein.") from error
    elif key is MetadataFieldKey.RECORDING_CLASSIFICATION:
        normalized = text.casefold()
        kinds = {
            "original": RecordingKind.ORIGINAL,
            "originalaufnahme": RecordingKind.ORIGINAL,
            "neuaufnahme": RecordingKind.RE_RECORDING,
            "live": RecordingKind.LIVE,
            "liveaufnahme": RecordingKind.LIVE,
            "remix": RecordingKind.REMIX,
            "radio edit": RecordingKind.RADIO_EDIT,
            "unbekannt": RecordingKind.UNKNOWN,
        }
        base = normalized.replace("remastert", "").replace("·", "").strip()
        if base not in kinds:
            raise ValueError(
                "Aufnahmeart muss Original, Neuaufnahme, Liveaufnahme, Remix, "
                "Radio Edit oder Unbekannt sein."
            )
        traits = (
            frozenset({RecordingTrait.REMASTERED}) if "remastert" in normalized else frozenset()
        )
        value = RecordingClassification(kinds[base], traits)
    elif action in {
        BatchAction.MULTI_ADD,
        BatchAction.MULTI_REMOVE,
        BatchAction.MULTI_REPLACE,
    }:
        parts = tuple(part.strip() for part in text.replace("\n", ",").split(",") if part.strip())
        if key is MetadataFieldKey.MUSICAL_DECADES:
            try:
                value = tuple(int(part) for part in parts)
            except ValueError as error:
                raise ValueError(
                    "Musikalische Dekaden müssen als Jahreszahlen angegeben werden, "
                    "zum Beispiel 1970, 1980."
                ) from error
        else:
            value = parts
    else:
        value = text
    try:
        return normalize_metadata_value(key, value)
    except (TypeError, ValueError) as error:
        if key in _INTEGER_INPUT_HINTS:
            raise ValueError(_INTEGER_INPUT_HINTS[key]) from error
        if key in {MetadataFieldKey.BPM, MetadataFieldKey.ALTERNATIVE_BPM}:
            raise ValueError("BPM muss eine Zahl von 20 bis 300 sein.") from error
        raise ValueError(f"Der Zielwert für {FIELD_LABELS[key]} ist ungültig.") from error


def parse_bpm_filter(minimum: str, maximum: str) -> tuple[float | None, float | None]:
    """Parse an optional inclusive BPM range for catalog filtering."""
    try:
        lower = float(minimum.strip().replace(",", ".")) if minimum.strip() else None
        upper = float(maximum.strip().replace(",", ".")) if maximum.strip() else None
    except ValueError as error:
        raise ValueError("BPM von/bis muss eine Zahl sein.") from error
    if any(value is not None and not 20.0 <= value <= 300.0 for value in (lower, upper)):
        raise ValueError("BPM von/bis muss zwischen 20 und 300 liegen.")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("BPM von darf nicht größer als BPM bis sein.")
    return lower, upper


def _labeled_filter_entry(
    parent: Any, label: str, *, placeholder: str = ""
) -> tuple[ctk.CTkFrame, ctk.CTkEntry]:
    """Build a filter input whose meaning never depends on its placeholder."""
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(frame, text=label, anchor="w").grid(
        row=0, column=0, padx=2, pady=(0, 1), sticky="ew"
    )
    entry = ctk.CTkEntry(frame, placeholder_text=placeholder)
    entry.grid(row=1, column=0, sticky="ew")
    return frame, entry


class CatalogMaintenanceDialog(ctk.CTkToplevel):  # type: ignore[misc]
    """Keep catalog-wide reads and writes outside Tk callbacks."""

    def __init__(
        self,
        parent: Any,
        service: CatalogMaintenanceService,
        submit: Submit,
        open_track: Callable[[int], None],
        analysis_actions: CatalogAnalysisActions | None = None,
        metadata_analysis: MetadataAnalysisService | None = None,
    ) -> None:
        super().__init__(parent)
        self._service, self._submit, self._open_track = service, submit, open_track
        self._analysis_actions = analysis_actions
        self._metadata_analysis = metadata_analysis
        self._closed = False
        self._page = 1
        self._filter = MaintenanceFilter()
        self._selection = SelectionDescription.for_filter(self._filter)
        self._current: MaintenancePage | None = None
        self._preview: BatchPreview | None = None
        self._cancel_event = Event()
        self._load_generation = 0
        self._running = False
        self._progress_state = (0, 0)
        self._progress_after: str | None = None
        self._tempo_run_ids: tuple[int, ...] = ()
        self._tempo_poll_after: str | None = None
        self._tempo_skipped = 0
        self.title("Katalogpflege")
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=(1180, 760), minimum_size=(760, 560)
        )
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._close)
        bind_dialog_escape(self, self._close)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build()
        self.grab_set()
        self._load_counts_and_page()

    def _build(self) -> None:
        filters = ctk.CTkFrame(self)
        filters.grid(row=0, column=0, padx=12, pady=10, sticky="ew")
        filters.grid_columnconfigure(1, weight=1)
        self._queue = ctk.CTkOptionMenu(
            filters,
            values=["Alle Arbeitsvorräte", *WORK_QUEUE_LABELS.values()],
            command=lambda _v: self._apply_filter(),
        )
        self._queue.grid(row=0, column=0, padx=5, pady=5)
        self._search = ctk.CTkEntry(filters, placeholder_text="Titel oder Interpret")
        self._search.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        ctk.CTkButton(filters, text="Filtern", command=self._apply_filter).grid(
            row=0, column=2, padx=5, sticky="ew"
        )
        ctk.CTkButton(filters, text="Filter zurücksetzen", command=self._reset_filters).grid(
            row=0, column=3, padx=5, sticky="ew"
        )
        self._counts = ctk.CTkLabel(filters, text="Arbeitsvorräte werden gezählt …", anchor="w")
        self._counts.grid(row=1, column=0, columnspan=4, padx=5, sticky="ew")
        advanced = ctk.CTkFrame(filters, fg_color="transparent")
        advanced.grid(row=2, column=0, columnspan=4, sticky="ew")
        for column in range(4):
            advanced.grid_columnconfigure(column, weight=1)
        self._filter_field = ctk.CTkOptionMenu(
            advanced, values=["Alle Felder", *FIELD_LABELS.values()]
        )
        self._filter_field.grid(row=0, column=0, padx=3, pady=2, sticky="ew")
        self._filter_source = ctk.CTkOptionMenu(
            advanced, values=["Alle Quellen", *SOURCE_LABELS.values()]
        )
        self._filter_source.grid(row=0, column=1, padx=3, pady=2, sticky="ew")
        self._filter_status = ctk.CTkOptionMenu(
            advanced, values=["Alle Prüfstatus", *STATUS_LABELS.values()]
        )
        self._filter_status.grid(row=0, column=2, padx=3, pady=2, sticky="ew")
        self._filter_value = ctk.CTkOptionMenu(
            advanced, values=["Wert egal", "Mit Wert", "Ohne Wert"]
        )
        self._filter_value.grid(row=0, column=3, padx=3, pady=2, sticky="ew")
        self._filter_confirmed = ctk.CTkOptionMenu(
            advanced, values=["Bestätigung egal", "Bestätigt", "Nicht bestätigt"]
        )
        self._filter_confirmed.grid(row=1, column=0, padx=3, pady=2, sticky="ew")
        self._filter_conflict = ctk.CTkOptionMenu(
            advanced, values=["Konflikt egal", "Mit Konflikt", "Ohne Konflikt"]
        )
        self._filter_conflict.grid(row=1, column=1, padx=3, pady=2, sticky="ew")
        self._filter_suggestion = ctk.CTkOptionMenu(
            advanced, values=["Vorschlag egal", *SUGGESTION_STATUS_LABELS.values()]
        )
        self._filter_suggestion.grid(row=1, column=2, padx=3, pady=2, sticky="ew")
        confidence_frame, self._filter_confidence = _labeled_filter_entry(
            advanced, "Min. Konfidenz", placeholder="0–1"
        )
        confidence_frame.grid(row=1, column=3, padx=3, pady=2, sticky="ew")
        changed_from_frame, self._filter_changed_from = _labeled_filter_entry(
            advanced, "Geändert von", placeholder="JJJJ-MM-TT"
        )
        changed_from_frame.grid(row=2, column=0, columnspan=2, padx=3, pady=2, sticky="ew")
        changed_to_frame, self._filter_changed_to = _labeled_filter_entry(
            advanced, "Geändert bis", placeholder="JJJJ-MM-TT"
        )
        changed_to_frame.grid(row=2, column=2, columnspan=2, padx=3, pady=2, sticky="ew")
        bpm_from_frame, self._filter_bpm_from = _labeled_filter_entry(
            advanced, "BPM von", placeholder="20–300"
        )
        bpm_from_frame.grid(row=3, column=0, columnspan=2, padx=3, pady=2, sticky="ew")
        bpm_to_frame, self._filter_bpm_to = _labeled_filter_entry(
            advanced, "BPM bis", placeholder="20–300"
        )
        bpm_to_frame.grid(row=3, column=2, columnspan=2, padx=3, pady=2, sticky="ew")
        self._analysis_toggle = ctk.CTkButton(
            filters,
            text="Audioanalyse anzeigen ▾",
            command=self._toggle_analysis_panel,
            anchor="w",
        )
        self._analysis_toggle.grid(row=3, column=0, columnspan=4, padx=3, pady=(5, 2), sticky="ew")
        self._analysis_panel = ctk.CTkFrame(filters)
        self._analysis_panel.grid(row=4, column=0, columnspan=4, padx=3, pady=3, sticky="ew")
        self._analysis_panel.grid_columnconfigure(1, weight=1)
        self._build_analysis_panel(self._analysis_panel)
        self._analysis_panel.grid_remove()

        body = ctk.CTkScrollableFrame(self)
        body.grid(row=1, column=0, padx=12, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        self._rows = ctk.CTkFrame(body)
        self._rows.grid(row=0, column=0, sticky="ew")
        self._rows.grid_columnconfigure(1, weight=1)
        self._row_widgets: list[tuple[Any, Any, Tooltip]] = []
        for index in range(12):
            chosen = ctk.CTkCheckBox(
                self._rows, text="", width=24, command=lambda i=index: self._toggle(i)
            )
            chosen.grid(row=index, column=0, padx=4, pady=2)
            label = ctk.CTkButton(
                self._rows,
                text="",
                anchor="w",
                fg_color="transparent",
                command=lambda i=index: self._open(i),
            )
            label.grid(row=index, column=1, padx=4, pady=2, sticky="ew")
            self._row_widgets.append((chosen, label, Tooltip(label, "")))
        nav = ctk.CTkFrame(body, fg_color="transparent")
        nav.grid(row=1, column=0, pady=6)
        ctk.CTkButton(nav, text="◀", width=36, command=lambda: self._change_page(-1)).pack(
            side="left"
        )
        self._page_label = ctk.CTkLabel(nav, text="Seite 1")
        self._page_label.pack(side="left", padx=10)
        ctk.CTkButton(nav, text="▶", width=36, command=lambda: self._change_page(1)).pack(
            side="left"
        )
        selection = ctk.CTkFrame(body)
        selection.grid(row=2, column=0, pady=6, sticky="ew")
        ctk.CTkButton(selection, text="Seite auswählen", command=self._select_page).pack(
            side="left", padx=3
        )
        ctk.CTkButton(selection, text="Seite abwählen", command=self._deselect_page).pack(
            side="left", padx=3
        )
        ctk.CTkButton(selection, text="Alle Treffer auswählen", command=self._select_all).pack(
            side="left", padx=3
        )
        self._selection_label = ctk.CTkLabel(selection, text="0 ausgewählt")
        self._selection_label.pack(side="left", padx=10)
        action = ctk.CTkFrame(body)
        action.grid(row=3, column=0, pady=6, sticky="ew")
        action.grid_columnconfigure(2, weight=1)
        action.grid_columnconfigure(3, weight=1)
        self._field = ctk.CTkOptionMenu(
            action,
            values=[
                FIELD_LABELS[key]
                for key in MetadataFieldKey
                if key is not MetadataFieldKey.BPM_CONFIDENCE
            ],
        )
        self._field.grid(row=0, column=0, padx=3, pady=3, sticky="ew")
        self._action = ctk.CTkOptionMenu(action, values=list(BATCH_ACTION_LABELS.values()))
        self._action.grid(row=0, column=1, padx=3, pady=3, sticky="ew")
        self._value = ctk.CTkEntry(action, placeholder_text="Zielwert")
        self._value.grid(row=0, column=2, padx=3, pady=3, sticky="ew")
        ctk.CTkButton(action, text="Vorschau", command=self._make_preview).grid(
            row=1, column=0, padx=3, pady=3, sticky="ew"
        )
        self._execute_button = ctk.CTkButton(action, text="Ausführen", command=self._execute)
        self._execute_button.grid(row=1, column=1, padx=3, pady=3, sticky="ew")
        self._cancel_button = ctk.CTkButton(
            action,
            text="Abbrechen",
            state="disabled",
            fg_color="#7d3030",
            command=self._cancel_batch,
        )
        self._cancel_button.grid(row=1, column=2, padx=3, pady=3, sticky="w")
        ctk.CTkButton(action, text="Letzte Aktion rückgängig", command=self._undo).grid(
            row=1, column=3, padx=3, pady=3, sticky="ew"
        )
        self._result = ctk.CTkLabel(body, text="", justify="left", anchor="w", wraplength=900)
        self._result.grid(row=4, column=0, padx=6, pady=8, sticky="ew")
        ctk.CTkButton(self, text="Schließen", command=self._close).grid(
            row=2, column=0, padx=14, pady=10, sticky="e"
        )

    def _build_analysis_panel(self, panel: Any) -> None:
        actions = self._analysis_actions
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
                ctk.CTkButton(
                    controls,
                    text=text,
                    command=(
                        (lambda selected=command: self._run_analysis_action(selected))
                        if command is not None
                        else None
                    ),
                    state="normal" if command is not None else "disabled",
                    fg_color="#7d3030" if danger else None,
                ).pack(side="left", padx=3)
        ctk.CTkLabel(panel, text="Tempo", width=75, anchor="w").grid(
            row=2, column=0, padx=6, pady=3, sticky="w"
        )
        bpm = ctk.CTkFrame(panel, fg_color="transparent")
        bpm.grid(row=2, column=1, padx=3, pady=3, sticky="ew")
        self._tempo_profile = ctk.CTkOptionMenu(
            bpm, values=("Tempo", "Tempo und experimentelle Energie"), width=230
        )
        self._tempo_profile.pack(side="left", padx=3)
        self._tempo_scope = ctk.CTkOptionMenu(
            bpm,
            values=("Vollständige Aufnahme", "Wirksame Katalog-Cue-Bereiche"),
            width=245,
        )
        self._tempo_scope.pack(side="left", padx=3)
        self._tempo_skip_current = ctk.CTkSwitch(bpm, text="Aktuelle Ergebnisse überspringen")
        self._tempo_skip_current.select()
        self._tempo_skip_current.pack(side="left", padx=6)
        self._tempo_start = ctk.CTkButton(
            bpm,
            text="Auswahl analysieren …",
            command=self._prepare_tempo_batch,
            state="normal" if self._metadata_analysis is not None else "disabled",
        )
        self._tempo_start.pack(side="left", padx=3)
        tempo_controls = ctk.CTkFrame(panel, fg_color="transparent")
        tempo_controls.grid(row=3, column=1, padx=3, pady=3, sticky="w")
        self._tempo_pause = ctk.CTkButton(
            tempo_controls,
            text="Pause",
            state="disabled",
            command=self._pause_tempo_batch,
        )
        self._tempo_pause.pack(side="left", padx=3)
        self._tempo_resume = ctk.CTkButton(
            tempo_controls,
            text="Fortsetzen",
            state="disabled",
            command=self._resume_tempo_batch,
        )
        self._tempo_resume.pack(side="left", padx=3)
        self._tempo_abort = ctk.CTkButton(
            tempo_controls,
            text="Gesamten Auftrag abbrechen",
            state="disabled",
            fg_color="#7d3030",
            command=self._abort_tempo_batch,
        )
        self._tempo_abort.pack(side="left", padx=3)
        self._tempo_pending_resume = ctk.CTkButton(
            tempo_controls,
            text="Wartende Runs fortsetzen",
            command=self._resume_persistent_tempo,
            state="normal" if self._metadata_analysis is not None else "disabled",
        )
        self._tempo_pending_resume.pack(side="left", padx=3)
        self._tempo_pending_discard = ctk.CTkButton(
            tempo_controls,
            text="Wartende Runs verwerfen",
            fg_color="#6b5b2a",
            command=self._discard_persistent_tempo,
            state="normal" if self._metadata_analysis is not None else "disabled",
        )
        self._tempo_pending_discard.pack(side="left", padx=3)
        self._tempo_progress = ctk.CTkLabel(
            panel,
            text="Tempoanalyse startet nur nach ausdrücklicher Bedienung. Ergebnisse bleiben Vorschläge.",
            text_color="#9fb3c8",
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self._tempo_progress.grid(row=4, column=0, columnspan=2, padx=6, pady=(2, 6), sticky="ew")

    def _toggle_analysis_panel(self) -> None:
        if self._analysis_panel.winfo_ismapped():
            self._analysis_panel.grid_remove()
            self._analysis_toggle.configure(text="Audioanalyse anzeigen ▾")
        else:
            self._analysis_panel.grid()
            self._analysis_toggle.configure(text="Audioanalyse ausblenden ▴")

    def _run_analysis_action(self, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as error:
            self._failed(error)

    def _prepare_tempo_batch(self) -> None:
        if self._metadata_analysis is None:
            return
        self._tempo_start.configure(state="disabled")
        self._tempo_progress.configure(text="Auswahl und Analysestand werden geprüft …")
        self._task(
            lambda: self._service.repository.resolve_selection(self._selection),
            self._tempo_selection_resolved,
        )

    def _tempo_selection_resolved(self, value: object) -> None:
        if not self._active() or self._metadata_analysis is None:
            return
        analysis = self._metadata_analysis
        rows = cast(tuple[tuple[int, int], ...], value)
        ids = tuple(track_id for track_id, _revision in rows)
        if not ids:
            self._tempo_start.configure(state="normal")
            self._tempo_progress.configure(text="Bitte mindestens einen Titel auswählen.")
            return
        skip = bool(self._tempo_skip_current.get())
        scope = self._selected_tempo_scope()
        self._task(
            lambda: analysis.preview_tracks(ids, skip_current=skip, scope=scope),
            self._tempo_previewed,
        )

    def _selected_tempo_scope(self) -> TempoAnalysisScope:
        return (
            TempoAnalysisScope.TRACK_DEFAULT_CUES
            if self._tempo_scope.get() == "Wirksame Katalog-Cue-Bereiche"
            else TempoAnalysisScope.TRACK_FULL
        )

    def _tempo_previewed(self, value: object) -> None:
        if not self._active() or self._metadata_analysis is None:
            return
        analysis = self._metadata_analysis
        preview = cast(TempoBatchPreview, value)
        estimate = (
            f"ca. {preview.estimated_seconds / 60:.1f} Minuten"
            if preview.estimated_seconds is not None
            else "noch nicht belastbar"
        )
        text = (
            f"Ausgewählt: {preview.selected}\n"
            f"Bereits aktuell analysiert: {preview.current}\n"
            f"Veraltete Ergebnisse: {preview.outdated}\n"
            f"Tatsächlich geplante Runs: {preview.planned}\n"
            f"Fehlende oder nicht erreichbare Dateien: {preview.missing_files}\n"
            f"Fehlende oder ungültige Cue-Bereiche: {preview.invalid_cues}\n"
            f"Vorhandene offene Vorschläge: {preview.open_suggestions}\n"
            f"Erwartete Dauer: {estimate}\n\n"
            "BPM und experimentelle Energie werden nur als Vorschläge gespeichert."
        )
        block_reason = analysis.block_reason(batch=True)
        if block_reason:
            text += f"\n\nStart derzeit gesperrt: {block_reason}"
        self._tempo_progress.configure(text=text)
        if (
            preview.planned == 0
            or bool(block_reason)
            or not ask_silent_yes_no(self, "Tempoanalyse starten?", text)
        ):
            self._tempo_start.configure(state="normal")
            return
        profile = (
            MetadataAnalysisProfile.TEMPO_AND_ENERGY_EXPERIMENTAL
            if self._tempo_profile.get() == "Tempo und experimentelle Energie"
            else MetadataAnalysisProfile.TEMPO
        )
        self._tempo_skipped = preview.selected - preview.planned
        self._task(
            lambda: analysis.analyze_selected(
                preview.track_ids, profile, scope=self._selected_tempo_scope()
            ),
            self._tempo_batch_started,
        )

    def _tempo_batch_started(self, value: object) -> None:
        if not self._active():
            return
        jobs = cast(tuple[Any, ...], value)
        self._tempo_run_ids = tuple(int(job.run_id) for job in jobs)
        self._tempo_pause.configure(state="normal")
        self._tempo_abort.configure(state="normal")
        self._tempo_progress.configure(text="Serielle Tempoanalyse wurde gestartet …")
        self._schedule_tempo_progress()

    def _schedule_tempo_progress(self) -> None:
        if not self._active() or self._tempo_poll_after is not None:
            return
        self._tempo_poll_after = self.after(400, self._poll_tempo_progress)

    def _poll_tempo_progress(self) -> None:
        self._tempo_poll_after = None
        if not self._active() or self._metadata_analysis is None or not self._tempo_run_ids:
            return
        analysis = self._metadata_analysis
        self._task(
            lambda: analysis.batch_progress(self._tempo_run_ids),
            self._tempo_progressed,
        )

    def _tempo_progressed(self, value: object) -> None:
        if not self._active():
            return
        progress = cast(TempoBatchProgress, value)
        remaining = (
            f" · Rest ca. {progress.estimated_remaining_seconds / 60:.1f} min"
            if progress.estimated_remaining_seconds is not None
            else ""
        )
        reason = f"\nPausen-/Sperrgrund: {progress.reason}" if progress.reason else ""
        current = f"\nAktuell: {progress.current_title}" if progress.current_title else ""
        self._tempo_progress.configure(
            text=(
                f"{progress.completed} / {progress.total} abgeschlossen · "
                f"erfolgreich {progress.successful} · ohne BPM {progress.without_bpm} · "
                f"Prüfung erforderlich {progress.review_required} · "
                f"fehlgeschlagen {progress.failed} · übersprungen {self._tempo_skipped} · "
                f"abgebrochen {progress.cancelled}{remaining}{current}{reason}"
            )
        )
        if progress.completed < progress.total:
            self._schedule_tempo_progress()
            return
        self._tempo_start.configure(state="normal")
        self._tempo_pause.configure(state="disabled")
        self._tempo_resume.configure(state="disabled")
        self._tempo_abort.configure(state="disabled")
        self._load_counts_and_page()

    def _pause_tempo_batch(self) -> None:
        if self._metadata_analysis is None:
            return
        self._metadata_analysis.pause()
        self._tempo_pause.configure(state="disabled")
        self._tempo_resume.configure(state="normal")
        self._tempo_progress.configure(text="PAUSIERT – der laufende Titel darf noch enden.")

    def _resume_tempo_batch(self) -> None:
        if self._metadata_analysis is None:
            return
        self._metadata_analysis.resume()
        self._tempo_pause.configure(state="normal")
        self._tempo_resume.configure(state="disabled")
        self._schedule_tempo_progress()

    def _abort_tempo_batch(self) -> None:
        if self._metadata_analysis is None:
            return
        self._metadata_analysis.cancel_all()
        self._tempo_abort.configure(state="disabled")
        self._tempo_progress.configure(text="Tempoanalyse wurde vollständig abgebrochen.")
        self._schedule_tempo_progress()

    def _resume_persistent_tempo(self) -> None:
        if self._metadata_analysis is None:
            return
        count = self._metadata_analysis.resume_persistent_pending()
        self._tempo_progress.configure(
            text=f"{count} wartende Runs wurden eingereiht; Start erfolgt kontrolliert."
        )

    def _discard_persistent_tempo(self) -> None:
        if self._metadata_analysis is None:
            return
        try:
            count = self._metadata_analysis.discard_persistent_pending()
        except RuntimeError as error:
            self._tempo_progress.configure(text=str(error))
            return
        self._tempo_progress.configure(text=f"{count} wartende Runs wurden verworfen.")

    def _task(self, work: Callable[[], object], done: Callable[[object], None]) -> None:
        self._submit(work, done, self._failed)

    def _load_counts_and_page(self) -> None:
        self._load_generation += 1
        generation = self._load_generation
        self._counts.configure(text="Arbeitsvorräte werden gezählt …")
        self._page_label.configure(text="Treffer werden geladen …")
        self._task(
            lambda: (
                self._service.repository.counts(),
                self._service.repository.page(self._filter, self._page, 12),
            ),
            lambda value: self._loaded(value, generation),
        )

    def _loaded(self, value: object, generation: int | None = None) -> None:
        if not self._active() or (generation is not None and generation != self._load_generation):
            return
        counts, page = cast(tuple[tuple[WorkQueueCount, ...], MaintenancePage], value)
        self._counts.configure(
            text=" · ".join(
                f"{WORK_QUEUE_LABELS[item.queue]}: {item.count}" for item in counts if item.count
            )
        )
        self._show_page(page)

    def _show_page(self, page: MaintenancePage) -> None:
        self._current = page
        self._page_label.configure(text=f"Seite {page.page} · {page.total} Treffer")
        for index, (check, label, tooltip) in enumerate(self._row_widgets):
            if index >= len(page.rows):
                check.grid_remove()
                label.grid_remove()
                continue
            row = page.rows[index]
            check.grid()
            label.grid()
            selected = (
                row.track_id in self._selection.included_ids
                or self._selection.all_matches
                and row.track_id not in self._selection.excluded_ids
            )
            check.select() if selected else check.deselect()
            label.configure(
                text=(
                    f"{row.artist} — {row.title} · "
                    f"{FIELD_LABELS.get(MetadataFieldKey(row.field), 'Metadaten') if row.field else 'Metadaten'} · "
                    f"{STATUS_LABELS.get(MetadataReviewStatus(row.review_status), row.review_status)} · "
                    f"{row.current_value}"
                    f"{' → Vorschlag: ' + row.suggestion if row.suggestion else ''}"
                    f"{' · ' + row.warning if row.warning else ''}"
                )
            )
            tooltip.set_text(
                f"{row.artist} — {row.title}\nAktuell: {row.current_value_full}"
                f"{chr(10) + 'Vorschlag: ' + row.suggestion_full if row.suggestion_full else ''}"
            )

    def _apply_filter(self) -> None:
        queue_label = self._queue.get()
        queue = next(
            (item for item, label in WORK_QUEUE_LABELS.items() if label == queue_label),
            None,
        )
        confidence_text = self._filter_confidence.get().strip().replace(",", ".")
        try:
            minimum_bpm, maximum_bpm = parse_bpm_filter(
                self._filter_bpm_from.get(), self._filter_bpm_to.get()
            )
        except ValueError as error:
            self._result.configure(text=f"Fehler: {error}")
            return
        new_filter = MaintenanceFilter(
            work_queue=queue,
            field=(
                None
                if self._filter_field.get() == "Alle Felder"
                else next(
                    key for key, label in FIELD_LABELS.items() if label == self._filter_field.get()
                )
            ),
            source=(
                None
                if self._filter_source.get() == "Alle Quellen"
                else next(
                    key
                    for key, label in SOURCE_LABELS.items()
                    if label == self._filter_source.get()
                )
            ),
            review_status=(
                None
                if self._filter_status.get() == "Alle Prüfstatus"
                else next(
                    key
                    for key, label in STATUS_LABELS.items()
                    if label == self._filter_status.get()
                )
            ),
            suggestion_status=(
                None
                if self._filter_suggestion.get() == "Vorschlag egal"
                else next(
                    key
                    for key, label in SUGGESTION_STATUS_LABELS.items()
                    if label == self._filter_suggestion.get()
                )
            ),
            minimum_confidence=float(confidence_text) if confidence_text else None,
            has_value={"Mit Wert": True, "Ohne Wert": False}.get(self._filter_value.get()),
            confirmed={"Bestätigt": True, "Nicht bestätigt": False}.get(
                self._filter_confirmed.get()
            ),
            conflict={"Mit Konflikt": True, "Ohne Konflikt": False}.get(
                self._filter_conflict.get()
            ),
            text=self._search.get(),
            changed_from=self._filter_changed_from.get().strip() or None,
            changed_to=self._filter_changed_to.get().strip() or None,
            minimum_bpm=minimum_bpm,
            maximum_bpm=maximum_bpm,
        )
        has_selection = self._selection.all_matches or bool(self._selection.included_ids)
        if has_selection:
            strategy = ask_filter_selection_strategy(self)
            if strategy is None:
                return
            if strategy != "KEEP":
                if strategy == "RESTRICT":
                    self._task(
                        lambda: self._service.repository.restrict_selection(
                            self._selection, new_filter
                        ),
                        lambda value: self._filter_restricted(new_filter, value),
                    )
                    return
                self._selection = SelectionDescription.for_filter(new_filter)
            else:
                self._task(
                    lambda: self._service.repository.resolve_selection(self._selection),
                    lambda value: self._filter_preserved(new_filter, value),
                )
                return
        else:
            self._selection = SelectionDescription.for_filter(new_filter)
        self._filter = new_filter
        self._page = 1
        self._load_counts_and_page()

    def _reset_filters(self) -> None:
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
        self._filter = MaintenanceFilter()
        self._selection = SelectionDescription.for_filter(self._filter)
        self._preview = None
        self._page = 1
        self._update_selection()
        self._result.configure(text="Filter und Auswahl wurden zurückgesetzt.")
        self._load_counts_and_page()

    def _filter_restricted(self, filter_: MaintenanceFilter, value: object) -> None:
        if not self._active():
            return
        rows = cast(tuple[tuple[int, int], ...], value)
        base = SelectionDescription.for_filter(filter_)
        self._selection = SelectionDescription(
            filter_,
            False,
            frozenset(track_id for track_id, _revision in rows),
            frozenset(),
            base.query_snapshot,
        )
        self._filter = filter_
        self._page = 1
        self._update_selection()
        self._load_counts_and_page()

    def _filter_preserved(self, filter_: MaintenanceFilter, value: object) -> None:
        if not self._active():
            return
        rows = cast(tuple[tuple[int, int], ...], value)
        original = self._selection
        self._selection = SelectionDescription(
            original.filter,
            False,
            frozenset(track_id for track_id, _revision in rows),
            frozenset(),
            original.query_snapshot,
        )
        self._filter = filter_
        self._page = 1
        self._update_selection()
        self._load_counts_and_page()

    def _toggle(self, index: int) -> None:
        if self._current is None or index >= len(self._current.rows):
            return
        track_id = self._current.rows[index].track_id
        selected = (
            track_id in self._selection.included_ids
            or self._selection.all_matches
            and track_id not in self._selection.excluded_ids
        )
        self._selection = (
            self._selection.deselect(track_id) if selected else self._selection.select(track_id)
        )
        self._update_selection()

    def _select_page(self) -> None:
        if self._current:
            for row in self._current.rows:
                self._selection = self._selection.select(row.track_id)
            self._show_page(self._current)
            self._update_selection()

    def _deselect_page(self) -> None:
        if self._current:
            for row in self._current.rows:
                self._selection = self._selection.deselect(row.track_id)
            self._show_page(self._current)
            self._update_selection()

    def _select_all(self) -> None:
        self._selection = self._selection.select_all_matches()
        self._update_selection()
        if self._current:
            self._show_page(self._current)

    def _update_selection(self) -> None:
        text = (
            "Alle Treffer"
            if self._selection.all_matches
            else str(len(self._selection.included_ids))
        )
        self._selection_label.configure(
            text=f"{text} ausgewählt · {len(self._selection.excluded_ids)} ausgeschlossen"
        )

    def _change_page(self, delta: int) -> None:
        self._page = max(1, self._page + delta)
        self._load_counts_and_page()

    def _open(self, index: int) -> None:
        if self._current and index < len(self._current.rows):
            self._open_track(self._current.rows[index].track_id)

    def _request(self) -> MetadataBatchRequest:
        key = next(key for key, label in FIELD_LABELS.items() if label == self._field.get())
        action = next(
            item for item, label in BATCH_ACTION_LABELS.items() if label == self._action.get()
        )
        value = parse_batch_input(key, action, self._value.get())
        values = () if action not in _VALUE_ACTIONS else ((key, value),)
        return MetadataBatchRequest(self._selection, frozenset({key}), action, values)

    def _make_preview(self) -> None:
        self._task(lambda: self._service.preview(self._request()), self._previewed)

    def _previewed(self, value: object) -> None:
        if not self._active():
            return
        self._preview = cast(BatchPreview, value)
        p = self._preview
        self._result.configure(
            text=f"Vorschau {p.token[:8]} · ausgewählt {p.selected} · änderbar {p.changeable} · unverändert {p.unchanged} · geschützt {p.protected} · ungültig {p.invalid}"
        )

    def _execute(self) -> None:
        preview = self._preview
        if self._running or preview is None:
            return
        general_confirmed = ask_silent_yes_no(
            self,
            "Sammeländerung ausführen?",
            f"{preview.changeable} Titel werden entsprechend der Vorschau geändert.",
        )
        if not general_confirmed:
            return
        high_risk_text = high_risk_confirmation_text(preview)
        high_risk_confirmed = high_risk_text is None or ask_silent_yes_no(
            self,
            "Besonders gefährliche Sammeländerung ausdrücklich bestätigen",
            high_risk_text,
        )
        if high_risk_confirmed:
            self._cancel_event.clear()
            self._running = True
            self._progress_state = (0, preview.selected)
            self._execute_button.configure(state="disabled")
            self._cancel_button.configure(state="normal")
            self._poll_progress()
            self._task(
                lambda: self._service.execute(
                    preview.request,
                    cancel_requested=self._cancel_event.is_set,
                    progress=self._set_worker_progress,
                ),
                self._finished,
            )

    def _set_worker_progress(self, done: int, total: int) -> None:
        self._progress_state = (done, total)

    def _poll_progress(self) -> None:
        if not self._active() or not self._running:
            self._progress_after = None
            return
        done, total = self._progress_state
        self._result.configure(text=f"Sammeländerung läuft: {done} von {total} geprüft …")
        self._progress_after = self.after(100, self._poll_progress)

    def _cancel_batch(self) -> None:
        cancel_event = getattr(self, "_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        progress_after = getattr(self, "_progress_after", None)
        if progress_after is not None:
            try:
                self.after_cancel(progress_after)
            except Exception:
                pass
        self._cancel_button.configure(state="disabled")
        self._result.configure(text="Abbruch vorgemerkt; die laufende Teiltransaktion endet noch.")

    def _finished(self, value: object) -> None:
        if not self._active():
            return
        result = cast(BatchResult, value)
        self._running = False
        self._preview = None
        self._execute_button.configure(state="normal")
        self._cancel_button.configure(state="disabled")
        self._result.configure(
            text=f"{result.status}: {result.changed} geändert, {result.unchanged} unverändert, {result.protected} geschützt, {result.revision_conflicts} Konflikte, {result.failed} fehlgeschlagen"
        )
        self._load_counts_and_page()

    def _undo(self) -> None:
        self._task(self._service.preview_undo, self._undo_previewed)

    def _undo_previewed(self, value: object) -> None:
        preview = cast(UndoPreview | None, value)
        if preview is None:
            self._result.configure(text="Keine rückgängig machbare Sammelaktion.")
            return
        self._result.configure(
            text=f"Undo-Vorschau: {preview.changeable_tracks} änderbar, {preview.conflict_tracks} Konflikte"
        )
        if ask_silent_yes_no(
            self,
            "Letzte Sammelaktion rückgängig machen?",
            f"{preview.changeable_tracks} Titel können sicher zurückgenommen werden; {preview.conflict_tracks} Konflikte werden übersprungen.",
        ):
            self._task(lambda: self._service.undo(preview), self._finished)

    def _failed(self, error: Exception) -> None:
        if self._active():
            self._running = False
            self._execute_button.configure(state="normal")
            self._cancel_button.configure(state="disabled")
            if hasattr(self, "_tempo_start"):
                self._tempo_start.configure(state="normal")
            self._result.configure(text=f"Fehler: {error}")

    def _active(self) -> bool:
        try:
            return not self._closed and bool(self.winfo_exists())
        except Exception:
            return False

    def _close(self) -> None:
        if self._closed:
            return
        cancel_event = getattr(self, "_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        for _check, _label, tooltip in getattr(self, "_row_widgets", ()):
            tooltip.close()
        tempo_after = getattr(self, "_tempo_poll_after", None)
        if tempo_after is not None:
            try:
                self.after_cancel(tempo_after)
            except Exception:
                pass
            self._tempo_poll_after = None
        self._closed = True
        release_dialog(self)
        self.destroy()
