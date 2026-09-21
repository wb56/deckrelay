"""Responsive, state-neutral presentation of automatic-selection previews."""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Callable, Protocol
from tkinter import messagebox

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.selection_decision import (
    CandidateDecisionCategory,
    CandidateEvaluation,
    RuleOutcome,
)
from party_player.selection_preview import (
    SelectionPreview,
    SelectionPreviewCompletion,
    SelectionPreviewStep,
)
from party_player.selection_catalog_filter import SelectionCatalogFilter
from party_player.ui import theme
from party_player.automatic_selection_plan_ui import PreviewAdoptionCode, PreviewAdoptionResult
from party_player.ui.responsive_dialog import (
    apply_responsive_dialog_geometry,
    bind_dialog_escape,
    release_dialog,
)


class PreviewRequester(Protocol):
    def automatic_preview_defaults(self) -> tuple[int, bool]: ...

    def selection_catalog_filter(self) -> SelectionCatalogFilter: ...

    def save_selection_catalog_filter(self, selected: SelectionCatalogFilter) -> None: ...
    def request_automatic_selection_preview(
        self,
        count: int,
        completed: Callable[[SelectionPreview], None],
        failed: Callable[[str], None],
    ) -> bool: ...

    def request_preview_adoption(
        self,
        preview: SelectionPreview,
        completed: Callable[[PreviewAdoptionResult], None],
        failed: Callable[[str], None],
        *,
        replace_plan_id: str | None = None,
        replace_revision: int | None = None,
        activate: bool = False,
    ) -> bool: ...

    def request_preview_queue_adoption(
        self,
        preview: SelectionPreview,
        completed: Callable[[int, int], None],
        failed: Callable[[str], None],
        *,
        replace_queue: bool = True,
    ) -> bool: ...


class PreviewUse(StrEnum):
    REPLACE_QUEUE = "replace_queue"
    APPEND_QUEUE = "append_queue"
    START_AUTOMATIC = "start_automatic"


_USE_LABELS = {
    PreviewUse.REPLACE_QUEUE: "Neue Queue erstellen",
    PreviewUse.APPEND_QUEUE: "An bestehende Queue anhängen",
    PreviewUse.START_AUTOMATIC: "Fortlaufende Automatik starten",
}


def selected_preview(preview: SelectionPreview, track_ids: set[int]) -> SelectionPreview:
    """Return the selected preview steps in display order with valid plan positions."""
    steps = tuple(
        replace(step, position=position)
        for position, step in enumerate(
            (step for step in preview.steps if step.track_id in track_ids), start=1
        )
    )
    return replace(
        preview,
        requested_depth=len(steps),
        achieved_depth=len(steps),
        steps=steps,
    )


@dataclass(slots=True)
class PreviewRequestState:
    generation: int = 0
    running: bool = False
    closed: bool = False

    def begin(self) -> int | None:
        if self.running or self.closed:
            return None
        self.generation += 1
        self.running = True
        return self.generation

    def accepts(self, generation: int) -> bool:
        return not self.closed and self.running and generation == self.generation

    def finish(self, generation: int) -> bool:
        if not self.accepts(generation):
            return False
        self.running = False
        return True

    def close(self) -> None:
        self.generation += 1
        self.running = False
        self.closed = True


@dataclass(frozen=True, slots=True)
class PreviewStepPresentation:
    summary: str
    detail: str


_DECISION_TEXT = {
    "SELECTED_HIGHEST_SCORE": "Höchste Bewertung der verfügbaren Kandidaten",
    "SELECTED_STABLE_TIE_BREAK": "Stabile Reihenfolge bei gleichem Ergebnis",
    "SELECTED_RNG_TIE_BREAK": "Zufallsentscheidung zwischen vollständig gleichwertigen Titeln",
    "SELECTED_EMERGENCY_ORDER": "Reihenfolge der Notfallauswahl",
    "SELECTED_QUEUE_PRIORITY": "Priorität der Warteschlange",
    "SELECTED_HIGHEST_SECONDARY_SCORE": "Höchste sekundäre Bewertung in der Primärgruppe",
}

_RULE_LABELS = {
    "selection.play_count": "Selten gespielt",
    "selection.rating": "Titelbewertung",
    "selection.genre_diversity": "Genre-Abwechslung",
    "selection.bpm_continuity": "BPM-Kontinuität",
    "selection.energy_continuity": "Energie-Kontinuität",
    "selection.mood_continuity": "Stimmungsanschluss",
}

_SOURCE_TEXT = {
    "AUTOMATIC": "Automatische Katalogauswahl",
    "EMERGENCY": "Notfallauswahl",
    "MANUAL": "Manuelle Queue",
    "GUEST_REQUEST": "Musikwunsch",
    "PLAYLIST": "Playlist oder Verzeichnis",
}

_EXCLUSION_TEXT = {
    "SUITABILITY_APPROVAL_REQUIRED": "sind noch nicht für die automatische Auswahl freigegeben",
    "UNSUITABLE_TRACK": "sind als ungeeignet markiert",
    "BLOCKED_TRACK": "sind ausdrücklich gesperrt",
    "RESTRICTED_TRACK": "benötigen eine ausdrückliche Operatorfreigabe",
    "BLOCKED_ARTIST": "haben einen gesperrten Interpreten",
    "SHORT_TRACK_MANUAL_ONLY": "sind nur für die manuelle Auswahl geeignet",
    "OTHER_EXCLUSIONS": "wurden aus weiteren Sicherheitsgründen ausgeschlossen",
}


def preview_dialog_dimensions(
    compact: bool,
) -> tuple[tuple[int, int], tuple[int, int]]:
    return ((760, 650), (500, 430)) if compact else ((980, 720), (720, 500))


def preview_depth(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("Bitte eine Vorschautiefe von 1 bis 10 wählen.") from exc
    if not 1 <= value <= 10:
        raise ValueError("Die Vorschautiefe muss zwischen 1 und 10 liegen.")
    return value


def preview_completion_text(preview: SelectionPreview) -> str:
    if preview.completion_reason is SelectionPreviewCompletion.NO_SAFE_CANDIDATE:
        return (
            f"Vorschau endet nach {preview.achieved_depth} von {preview.requested_depth} Titeln: "
            "Kein sicher geeigneter weiterer Kandidat verfügbar."
        )
    return f"{preview.achieved_depth} Titel wie angefordert vorausberechnet."


def preview_exclusion_text(preview: SelectionPreview) -> str:
    rationale = preview.completion_rationale
    if rationale is None or rationale.excluded_candidate_count == 0:
        return "Keine geeigneten Titel gefunden."
    lines = [
        "Keine geeigneten Titel gefunden.",
        f"{rationale.excluded_candidate_count} Titel geprüft.",
    ]
    for item in rationale.exclusion_summary:
        explanation = _EXCLUSION_TEXT.get(
            item.reason_code, "wurden aus einem Sicherheitsgrund ausgeschlossen"
        )
        lines.append(f"{item.count} Titel {explanation}.")
    return "\n".join(lines)


def _shorten(text: str, maximum: int) -> str:
    return text if len(text) <= maximum else f"{text[: maximum - 1]}…"


def present_preview_step(step: SelectionPreviewStep) -> PreviewStepPresentation:
    rationale = step.rationale
    selected = next(
        (
            candidate
            for candidate in rationale.evaluated_candidates
            if candidate.decision_category is CandidateDecisionCategory.SELECTED
        ),
        None,
    )
    reason_code = (
        selected.decision_reason_code if selected is not None else rationale.decision_reason_code
    )
    reason = _DECISION_TEXT.get(reason_code, "Geeignetster verfügbarer Titel")
    artist = _shorten(step.artist.strip() or "Unbekannter Interpret", 45)
    title = _shorten(step.title, 65)
    relaxation = " · Regeln gelockert" if step.relaxation_stage != "STRICT" else ""
    summary = f"{step.position}. {artist} – {title}\n{_shorten(reason, 52)}{relaxation}"
    detail = _detail_text(step, selected, reason)
    return PreviewStepPresentation(summary, detail)


def _detail_text(
    step: SelectionPreviewStep,
    selected: CandidateEvaluation | None,
    reason: str,
) -> str:
    rationale = step.rationale
    source = rationale.source_resolution
    source_key = source.selected_source.value if source and source.selected_source else "AUTOMATIC"
    lines = [
        f"Titel: {step.artist.strip() or 'Unbekannter Interpret'} – {step.title}",
        f"Quelle: {_SOURCE_TEXT.get(source_key, 'Automatische Auswahl')}",
        f"Hauptgrund: {reason}",
    ]
    if step.relaxation_stage != "STRICT":
        lines.append(f"Regellockerung: {_relaxation_text(step.relaxation_stage)}")
    if selected is not None:
        lines.extend(_selection_rank_text(rationale, selected))
        by_rule = {rule.rule_id: rule for rule in selected.rules if rule.rule_id in _RULE_LABELS}
        soft_rules = list(by_rule.values())
        effective = [rule for rule in soft_rules if rule.score_delta != 0.0]
        neutral = [rule for rule in soft_rules if rule.score_delta == 0.0]
        if effective:
            lines.append("\nWirksame Bewertungen:")
            lines.extend(f"• {_rule_text(rule)}" for rule in effective)
        if neutral:
            lines.append("\nRegeln ohne Auswirkung:")
            lines.extend(f"• {_rule_text(rule)}" for rule in neutral)
        relaxed = [
            (
                f"Bedienerausnahme: {rule.reason}"
                if rule.result_code is RuleOutcome.OVERRIDDEN
                else f"Gelockert: {rule.reason}"
            )
            for rule in selected.rules
            if rule.result_code in {RuleOutcome.RELAXED, RuleOutcome.OVERRIDDEN}
        ]
        if relaxed:
            lines.append("\nGelockerte oder übersteuerte Regeln:")
            lines.extend(f"• {text}" for text in relaxed)
    alternatives = [
        candidate.reason
        for candidate in rationale.evaluated_candidates
        if candidate.decision_category is CandidateDecisionCategory.EXCLUDED and candidate.reason
    ][:5]
    if alternatives:
        lines.append("\nBegrenzte Alternativen – Ausschlussgründe:")
        lines.extend(f"• {text}" for text in alternatives)
    if rationale.omitted_candidate_count:
        lines.append(
            f"\nWeitere {rationale.omitted_candidate_count} Kandidatenauswertungen wurden ausgeblendet."
        )
    return "\n".join(lines)


def _selection_rank_text(rationale: Any, selected: CandidateEvaluation) -> list[str]:
    play_count = selected.play_count
    primary = selected.primary_play_count
    lines = [f"Abspielzahl: {play_count if play_count is not None else 'nicht verfügbar'}"]
    if primary is None:
        lines.append("Primäre Abspielgruppe: Abspielregel nicht aktiv")
    else:
        membership = "ja" if selected.in_primary_play_group else "nein"
        lines.append(f"Primäre Abspielgruppe: mindestens {primary} · Zugehörigkeit: {membership}")
    lines.append(f"Sekundärer Bewertungsscore: {selected.secondary_score:+g} Punkte")
    if rationale.final_tie_candidate_count > 1:
        lines.append(
            f"Gleichstandsentscheidung zwischen {rationale.final_tie_candidate_count} "
            "gleichrangigen Finalisten"
        )
    return lines


def _fact(rule: Any, name: str) -> str | int | float | bool | None:
    return dict(rule.facts).get(name)


def _number_fact(rule: Any, name: str) -> str:
    value = _fact(rule, name)
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def _safe_term_fact(rule: Any, name: str) -> str:
    value = _fact(rule, name)
    if (
        isinstance(value, str)
        and value
        and len(value) <= 80
        and value.isprintable()
        and not any(marker in value for marker in ("\\", "/", ":"))
    ):
        return value
    return "bestätigte Stimmung"


def _rule_text(rule: Any) -> str:
    label = _RULE_LABELS[rule.rule_id]
    if rule.reason_code == "PREDECESSOR_MISSING":
        return f"{label}: Kein vorheriger Titel – Regel nicht anwendbar"
    if rule.reason_code in {"METADATA_UNKNOWN", "RATING_UNKNOWN", "RATING_INVALID"}:
        return f"{label}: Metadaten unbekannt – keine Auswirkung"
    if rule.reason_code == "ZERO_WEIGHT":
        return f"{label}: Gewichtung 0 – keine Auswirkung"
    if rule.reason_code == "MAIN_GENRE_REPEATED":
        detail = "Gleiches Hauptgenre wie der vorherige Titel"
    elif rule.reason_code == "BPM_DISTANCE":
        detail = f"BPM-Abstand {_number_fact(rule, 'distance')} BPM"
    elif rule.reason_code == "ENERGY_DISTANCE":
        detail = f"Energieabstand {_number_fact(rule, 'distance')}"
    elif rule.reason_code == "MOOD_OVERLAP":
        detail = f"Gemeinsame Stimmung: {_safe_term_fact(rule, 'common_terms')}"
    elif rule.reason_code == "ADDITIONAL_GENRE_OVERLAP":
        detail = "Überschneidung zusätzlicher Genres"
    elif rule.reason_code == "NO_GENRE_OVERLAP":
        detail = "Keine gemeinsame Genrebezeichnung"
    elif rule.reason_code == "NO_MOOD_OVERLAP":
        detail = "Keine gemeinsame Stimmung"
    elif rule.rule_id == "selection.play_count":
        detail = "Abspielhäufigkeit"
    elif rule.rule_id == "selection.rating":
        detail = "Titelbewertung"
    else:
        detail = "Keine Auswirkung"
    return f"{label}: {detail}: {rule.score_delta:+g} Punkte"


def _relaxation_text(stage: str) -> str:
    return {
        "ARTIST_DISTANCE": "Interpretensperre erweitert",
        "TRACK_DISTANCE": "Titel- und Interpretabstand erweitert",
        "EMERGENCY_PLAYLIST": "Notfallreihenfolge",
    }.get(stage, "Erweiterte sichere Suche")


class AutomaticSelectionPreviewDialog(ctk.CTkToplevel):  # type: ignore[misc]
    def __init__(self, parent: Any, requester: PreviewRequester) -> None:
        super().__init__(parent)
        self._requester = requester
        self._request_state = PreviewRequestState()
        self._step_buttons: list[Any] = []
        self._current_preview: SelectionPreview | None = None
        self._selected_track_ids: set[int] = set()
        self._adoption_running = False
        defaults = getattr(requester, "automatic_preview_defaults", None)
        default_depth, adjust_automatic = defaults() if callable(defaults) else (5, False)
        compact = bool(getattr(parent, "_compact_layout_active", False))
        self._compact = compact
        self.title("Queue automatisch zusammenstellen")
        preferred, minimum = preview_dialog_dimensions(compact)
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=preferred, minimum_size=minimum
        )
        self.transient(parent)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(
            self,
            text="Queue automatisch zusammenstellen",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(20, 4), sticky="w")
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=1, column=0, padx=20, pady=8, sticky="ew")
        ctk.CTkLabel(controls, text="Anzahl / Mindestvorrat:").pack(side="left")
        self._depth = ctk.StringVar(value=str(default_depth))
        self._depth_menu = ctk.CTkOptionMenu(
            controls, values=[str(value) for value in range(1, 11)], variable=self._depth, width=80
        )
        self._depth_menu.pack(side="left", padx=8)
        self._calculate = ctk.CTkButton(controls, text="Vorschau berechnen", command=self._start)
        self._calculate.pack(side="left", padx=8)
        ctk.CTkButton(
            controls,
            text="Auswahlregeln…",
            command=self._show_selection_rules,
        ).pack(side="left", padx=8)
        self._created = ctk.CTkLabel(controls, text="Noch nicht berechnet", anchor="w")
        self._created.pack(side="bottom", fill="x", pady=(8, 0))

        selected_filter = requester.selection_catalog_filter()
        self._active_filter = selected_filter
        filters = ctk.CTkFrame(self)
        filters.grid(row=2, column=0, padx=20, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(
            filters,
            text="1. Auswahl festlegen · Katalogfilter",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, columnspan=6, padx=8, pady=6, sticky="w")
        self._filter_values: dict[str, Any] = {}
        specifications = (
            ("genres", "Genres (Komma)", ", ".join(selected_filter.genres), 1, 0, 2),
            ("moods", "Stimmungen (Komma)", ", ".join(selected_filter.moods), 1, 2, 2),
            ("minimum_rating", "Bewertung ab", selected_filter.minimum_rating, 1, 4, 1),
            ("year_from", "Jahr von", selected_filter.year_from, 3, 0, 1),
            ("year_to", "Jahr bis", selected_filter.year_to, 3, 1, 1),
            ("bpm_from", "BPM von", selected_filter.bpm_from, 3, 2, 1),
            ("bpm_to", "BPM bis", selected_filter.bpm_to, 3, 3, 1),
            ("energy_from", "Energie von", selected_filter.energy_from, 3, 4, 1),
            ("energy_to", "Energie bis", selected_filter.energy_to, 3, 5, 1),
        )
        for column in range(6):
            filters.grid_columnconfigure(column, weight=1)
        for key, label, value, row, column, columnspan in specifications:
            ctk.CTkLabel(filters, text=label).grid(row=row, column=column, padx=4, sticky="w")
            entry = ctk.CTkEntry(filters)
            entry.insert(0, "" if value is None else str(value))
            entry.grid(
                row=row + 1,
                column=column,
                columnspan=columnspan,
                padx=4,
                pady=(0, 6),
                sticky="ew",
            )
            self._filter_values[key] = entry
        self._include_missing = ctk.BooleanVar(value=selected_filter.include_missing)
        ctk.CTkCheckBox(
            filters,
            text="Fehlende Werte",
            variable=self._include_missing,
        ).grid(row=2, column=5, padx=4, pady=(0, 6), sticky="w")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=3, column=0, padx=20, pady=4, sticky="nsew")
        if compact:
            body.grid_columnconfigure(0, weight=1)
            body.grid_rowconfigure(0, weight=1)
            body.grid_rowconfigure(1, weight=1)
            list_grid = {"row": 0, "column": 0, "sticky": "nsew", "pady": (0, 6)}
            detail_grid = {"row": 1, "column": 0, "sticky": "nsew", "pady": (6, 0)}
        else:
            body.grid_columnconfigure(0, weight=1)
            body.grid_columnconfigure(1, weight=1)
            body.grid_rowconfigure(0, weight=1)
            list_grid = {"row": 0, "column": 0, "sticky": "nsew", "padx": (0, 6)}
            detail_grid = {"row": 0, "column": 1, "sticky": "nsew", "padx": (6, 0)}
        self._list = ctk.CTkScrollableFrame(body, label_text="2. Vorschau prüfen")
        self._list.grid(**list_grid)
        self._list.grid_columnconfigure(0, weight=1)
        self._detail = ctk.CTkScrollableFrame(body, label_text="Auswahlgründe")
        self._detail.grid(**detail_grid)
        self._detail.grid_columnconfigure(0, weight=1)
        self._detail_text = ctk.CTkLabel(
            self._detail,
            text="Einen Vorschauschritt auswählen.",
            justify="left",
            anchor="nw",
            wraplength=430 if compact else 400,
        )
        self._detail_text.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        self._warning = ctk.CTkLabel(
            self,
            text="Momentaufnahme – tatsächliche Reihenfolge kann sich ändern.",
            text_color=theme.WARNING,
            anchor="w",
        )
        self._warning.grid(row=4, column=0, padx=20, pady=(6, 2), sticky="ew")
        self._status = ctk.CTkLabel(self, text="", anchor="w", wraplength=900)
        self._status.grid(row=5, column=0, padx=20, pady=(2, 6), sticky="ew")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=6, column=0, padx=20, pady=(4, 20), sticky="ew")
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)
        actions.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            actions,
            text="3. Verwendung wählen",
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, pady=(0, 8), sticky="ew")
        self._use = ctk.StringVar(
            value=(
                PreviewUse.START_AUTOMATIC.value
                if adjust_automatic
                else PreviewUse.REPLACE_QUEUE.value
            )
        )
        for column, use in enumerate(PreviewUse):
            ctk.CTkRadioButton(
                actions,
                text=_USE_LABELS[use],
                variable=self._use,
                value=use.value,
                command=self._update_primary_action,
            ).grid(row=1, column=column, padx=4, pady=(0, 8), sticky="w")
        self._confirmation = ctk.CTkLabel(
            actions,
            text="4. Bestätigen und starten · Noch keine Vorschau ausgewählt",
            anchor="w",
            justify="left",
            wraplength=900,
        )
        self._confirmation.grid(row=2, column=0, columnspan=3, pady=(2, 8), sticky="ew")
        self._queue_adopt = ctk.CTkButton(
            actions,
            text="Neue Queue erstellen",
            command=self._apply_selection,
            state="disabled",
        )
        self._queue_adopt.grid(row=3, column=0, columnspan=3, sticky="ew")
        self._adopt = self._queue_adopt
        ctk.CTkButton(actions, text="Schließen", command=self._close).grid(
            row=4, column=2, pady=(8, 0), sticky="e"
        )
        self.protocol("WM_DELETE_WINDOW", self._close)
        bind_dialog_escape(self, self._close)
        self.bind("<Return>", lambda _event: self._start())
        self._update_primary_action()
        self._depth_menu.focus_set()

    def _start(self) -> None:
        generation = self._request_state.begin()
        if generation is None:
            return
        try:
            count = preview_depth(self._depth.get())
            selected_filter = (
                self._catalog_filter()
                if hasattr(self, "_filter_values")
                else SelectionCatalogFilter()
            )
        except ValueError as exc:
            self._request_state.finish(generation)
            self._status.configure(text=str(exc), text_color=theme.ERROR)
            return
        save_filter = getattr(self._requester, "save_selection_catalog_filter", None)
        if callable(save_filter):
            save_filter(selected_filter)
        self._active_filter = selected_filter
        self._clear_result()
        self._calculate.configure(state="disabled", text="Vorschau läuft…")
        self._status.configure(text="Vorschau wird berechnet…", text_color=theme.TEXT_MUTED)
        started = self._requester.request_automatic_selection_preview(
            count,
            lambda preview: self._accept_preview(generation, preview),
            lambda message: self._accept_error(generation, message),
        )
        if not started:
            self._accept_error(generation, "Die Vorschau konnte nicht gestartet werden.")

    def _catalog_filter(self) -> SelectionCatalogFilter:
        def terms(key: str) -> tuple[str, ...]:
            return tuple(
                dict.fromkeys(
                    value.strip()
                    for value in self._filter_values[key].get().split(",")
                    if value.strip()
                )
            )

        def number(key: str, cast: Any) -> Any:
            raw = self._filter_values[key].get().strip()
            return None if not raw else cast(raw.replace(",", "."))

        selected = SelectionCatalogFilter(
            genres=terms("genres"),
            moods=terms("moods"),
            year_from=number("year_from", int),
            year_to=number("year_to", int),
            bpm_from=number("bpm_from", float),
            bpm_to=number("bpm_to", float),
            energy_from=number("energy_from", int),
            energy_to=number("energy_to", int),
            minimum_rating=number("minimum_rating", int),
            include_missing=bool(self._include_missing.get()),
        )
        ranges = (
            (selected.year_from, selected.year_to, 1000, 3000, "Jahr"),
            (selected.bpm_from, selected.bpm_to, 20, 300, "BPM"),
            (selected.energy_from, selected.energy_to, 0, 100, "Energie"),
            (selected.minimum_rating, selected.minimum_rating, 1, 5, "Bewertung"),
        )
        for minimum, maximum, lower, upper, label in ranges:
            if minimum is not None and not lower <= minimum <= upper:
                raise ValueError(f"{label}: Wert außerhalb des gültigen Bereichs")
            if maximum is not None and not lower <= maximum <= upper:
                raise ValueError(f"{label}: Wert außerhalb des gültigen Bereichs")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"{label}: Von-Wert darf nicht größer als Bis-Wert sein")
        return selected

    def _accept_preview(self, generation: int, preview: SelectionPreview) -> None:
        if not self._request_state.finish(generation):
            return
        self._calculate.configure(state="normal", text="Aktualisieren")
        self._current_preview = preview
        self._selected_track_ids = {step.track_id for step in preview.steps}
        queue_adopt = getattr(self, "_queue_adopt", None)
        if queue_adopt is not None:
            queue_adopt.configure(state="normal" if preview.steps else "disabled")
        self._created.configure(text=f"Erstellt: {preview.created_at:%d.%m.%Y %H:%M:%S}")
        self._status.configure(text=preview_completion_text(preview), text_color=theme.TEXT_MUTED)
        for row, step in enumerate(preview.steps):
            presentation = present_preview_step(step)
            selected = ctk.BooleanVar(value=True)
            checkbox = ctk.CTkCheckBox(
                self._list,
                text=presentation.summary,
                variable=selected,
                command=lambda track_id=step.track_id, value=selected, detail=presentation.detail: self._select_track(
                    track_id, bool(value.get()), detail
                ),
            )
            checkbox.grid(row=row, column=0, padx=6, pady=5, sticky="ew")
            self._step_buttons.append(checkbox)
        preview_list = getattr(self, "_list", None)
        if preview_list is not None:
            preview_list.grid_columnconfigure(0, weight=1)
        if hasattr(self, "_use"):
            self._update_primary_action()
        if preview.steps:
            first = present_preview_step(preview.steps[0])
            self._detail_text.configure(text=first.detail)
        else:
            self._detail_text.configure(text=preview_exclusion_text(preview))

    def _accept_error(self, generation: int, message: str) -> None:
        if not self._request_state.finish(generation):
            return
        self._clear_result()
        self._calculate.configure(state="normal", text="Erneut versuchen")
        self._created.configure(text="Keine aktuelle Vorschau")
        self._status.configure(text=f"Vorschau fehlgeschlagen: {message}", text_color=theme.ERROR)

    def _clear_result(self) -> None:
        self._current_preview = None
        self._selected_track_ids.clear()
        adopt = getattr(self, "_adopt", None)
        if adopt is not None:
            adopt.configure(state="disabled")
        queue_adopt = getattr(self, "_queue_adopt", None)
        if queue_adopt is not None:
            queue_adopt.configure(state="disabled")
        for button in self._step_buttons:
            button.destroy()
        self._step_buttons.clear()
        self._detail_text.configure(text="Keine aktuelle Vorschau.")
        self._created.configure(text="Wird berechnet…")

    def _select_track(self, track_id: int, selected: bool, detail: str = "") -> None:
        if selected:
            self._selected_track_ids.add(track_id)
        else:
            self._selected_track_ids.discard(track_id)
        if detail:
            self._detail_text.configure(text=detail)
        self._update_primary_action()

    def _selected_preview(self) -> SelectionPreview | None:
        preview = self._current_preview
        if preview is None:
            return None
        result = selected_preview(preview, self._selected_track_ids)
        return result if result.steps else None

    def _update_primary_action(self) -> None:
        button = getattr(self, "_queue_adopt", None)
        if button is None or self._adoption_running:
            return
        count = len(self._selected_track_ids)
        use = PreviewUse(self._use.get())
        if not count:
            text = _USE_LABELS[use]
        elif use is PreviewUse.REPLACE_QUEUE:
            text = f"Neue Queue mit {count} Titeln erstellen"
        elif use is PreviewUse.APPEND_QUEUE:
            text = f"{count} Titel an bestehende Queue anhängen"
        else:
            text = f"Fortlaufende Automatik mit {count} Titeln starten"
        button.configure(text=text, state="normal" if count else "disabled")
        confirmation = getattr(self, "_confirmation", None)
        if confirmation is not None and count:
            selected_filter = getattr(self, "_active_filter", SelectionCatalogFilter())
            confirmation.configure(
                text=(
                    "4. Bestätigen und starten\n"
                    f"{count} Titel · Filter: {selected_filter.summary()} · "
                    "Auswahlregeln: aktuelle Konfiguration"
                )
            )

    def _show_selection_rules(self) -> None:
        callback = getattr(self.master, "_show_selection_rule_settings", None)
        if callable(callback):
            callback()

    def _apply_selection(self) -> None:
        use = PreviewUse(self._use.get())
        if use is PreviewUse.START_AUTOMATIC:
            self._adopt_preview()
        else:
            self._adopt_queue(replace_queue=use is PreviewUse.REPLACE_QUEUE)

    def _adopt_queue(self, *, replace_queue: bool = True) -> None:
        preview = self._selected_preview()
        if preview is None or self._adoption_running:
            return
        self._adoption_running = True
        self._queue_adopt.configure(state="disabled", text="Wird übernommen …")
        self._adopt.configure(state="disabled")
        self._status.configure(
            text="Die angezeigte Reihenfolge wird in die bearbeitbare Queue übernommen …",
            text_color=theme.TEXT_MUTED,
        )
        started = self._requester.request_preview_queue_adoption(
            preview,
            self._accept_queue_adoption,
            self._queue_adoption_error,
            replace_queue=replace_queue,
        )
        if not started:
            self._queue_adoption_error("Die Queue-Übernahme konnte nicht gestartet werden.")

    def _accept_queue_adoption(self, added: int, skipped: int) -> None:
        if self._request_state.closed:
            return
        self._adoption_running = False
        self._queue_adopt.configure(state="normal")
        skipped_text = f" {skipped} bereits vorhandene Titel wurden ausgelassen." if skipped else ""
        self._status.configure(
            text=(
                f"{added} Titel wurden in die Queue übernommen.{skipped_text} "
                "Sie können weitere Titel auswählen oder den Dialog schließen."
            ),
            text_color=theme.TEXT_MUTED,
        )
        self._update_primary_action()

    def _queue_adoption_error(self, message: str) -> None:
        if self._request_state.closed:
            return
        self._adoption_running = False
        self._queue_adopt.configure(state="normal")
        self._update_primary_action()
        self._status.configure(text=message, text_color=theme.ERROR)

    def _adopt_preview(self) -> None:
        preview = self._selected_preview()
        if preview is None or self._adoption_running:
            return
        self._adoption_running = True
        self._adopt.configure(state="disabled", text="Wird übernommen …")
        self._status.configure(
            text="Vorschau wird sicher übernommen …", text_color=theme.TEXT_MUTED
        )
        started = self._requester.request_preview_adoption(
            preview,
            self._accept_adoption,
            self._adoption_error,
            activate=True,
        )
        if not started:
            self._adoption_error("Die Übernahme konnte nicht gestartet werden.")

    def _accept_adoption(self, result: PreviewAdoptionResult) -> None:
        if self._request_state.closed:
            return
        if result.code is PreviewAdoptionCode.CONFLICT and result.plan is not None:
            replace = messagebox.askyesno(
                "Vorhandener Automatikplan",
                (
                    "Es besteht bereits ein aktiver oder pausierter Automatikplan. Soll dessen "
                    "verbleibender Teil verworfen und die angezeigte Vorschau übernommen werden? "
                    "Bereits gespielte Titel, Historie und fremde Queue-Einträge bleiben erhalten."
                ),
                parent=self,
            )
            if replace:
                preview = self._current_preview
                if preview is not None:
                    self._requester.request_preview_adoption(
                        preview,
                        self._accept_adoption,
                        self._adoption_error,
                        replace_plan_id=result.plan.plan.plan_id,
                        replace_revision=result.plan.plan.revision,
                        activate=True,
                    )
                    return
        self._adoption_running = False
        self._adopt.configure(state="normal")
        update_primary = getattr(self, "_update_primary_action", None)
        if callable(update_primary):
            update_primary()
        if result.code in {PreviewAdoptionCode.CREATED, PreviewAdoptionCode.REPLACED}:
            self._status.configure(
                text="Die fortlaufende Automatik wurde gestartet.",
                text_color=theme.TEXT_MUTED,
            )
            callback = getattr(self.master, "show_automatic_plan_status", None)
            if callable(callback):
                callback("Aktiv")
            scheduler = getattr(self, "after_idle", None) or getattr(
                self.master, "after_idle", None
            )
            if callable(scheduler):
                scheduler(self._close)
        elif result.code is PreviewAdoptionCode.STALE:
            self._status.configure(
                text="Die Vorschau ist nicht mehr aktuell. Bitte aktualisieren Sie sie.",
                text_color=theme.ERROR,
            )
        elif result.code is PreviewAdoptionCode.CONFLICT:
            self._status.configure(
                text="Der vorhandene Plan blieb unverändert.", text_color=theme.WARNING
            )
        else:
            self._status.configure(
                text="Die Vorschau konnte nicht sicher übernommen werden.", text_color=theme.ERROR
            )

    def _adoption_error(self, message: str) -> None:
        if self._request_state.closed:
            return
        self._adoption_running = False
        self._adopt.configure(state="normal")
        self._update_primary_action()
        self._status.configure(text=message, text_color=theme.ERROR)

    def _close(self) -> None:
        self._request_state.close()
        release_dialog(self)
        self.destroy()
