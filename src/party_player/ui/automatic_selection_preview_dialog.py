"""Responsive, state-neutral presentation of automatic-selection previews."""

from dataclasses import dataclass
from typing import Any, Callable, Protocol

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
from party_player.ui import theme
from party_player.ui.responsive_dialog import (
    apply_responsive_dialog_geometry,
    bind_dialog_escape,
    release_dialog,
)


class PreviewRequester(Protocol):
    def request_automatic_selection_preview(
        self,
        count: int,
        completed: Callable[[SelectionPreview], None],
        failed: Callable[[str], None],
    ) -> bool: ...


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
    relaxation = (
        f" · Lockerung: {_relaxation_text(step.relaxation_stage)}"
        if step.relaxation_stage != "STRICT"
        else ""
    )
    summary = f"{step.position}. {artist} – {title}\n{reason}{relaxation}"
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
        compact = bool(getattr(parent, "_compact_layout_active", False))
        self._compact = compact
        self.title("Automatik-Vorschau")
        preferred, minimum = preview_dialog_dimensions(compact)
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=preferred, minimum_size=minimum
        )
        self.transient(parent)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self, text="Automatik-Vorschau", font=ctk.CTkFont(size=22, weight="bold")
        ).grid(row=0, column=0, padx=20, pady=(20, 4), sticky="w")
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=1, column=0, padx=20, pady=8, sticky="ew")
        ctk.CTkLabel(controls, text="Vorschautiefe:").pack(side="left")
        self._depth = ctk.StringVar(value="5")
        self._depth_menu = ctk.CTkOptionMenu(
            controls, values=[str(value) for value in range(1, 11)], variable=self._depth, width=80
        )
        self._depth_menu.pack(side="left", padx=8)
        self._calculate = ctk.CTkButton(controls, text="Vorschau berechnen", command=self._start)
        self._calculate.pack(side="left", padx=8)
        self._created = ctk.CTkLabel(controls, text="Noch nicht berechnet", anchor="w")
        self._created.pack(side="bottom", fill="x", pady=(8, 0))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, padx=20, pady=4, sticky="nsew")
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
        self._list = ctk.CTkScrollableFrame(body, label_text="Voraussichtliche Titel")
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
        self._warning.grid(row=3, column=0, padx=20, pady=(6, 2), sticky="ew")
        self._status = ctk.CTkLabel(self, text="", anchor="w", wraplength=900)
        self._status.grid(row=4, column=0, padx=20, pady=(2, 6), sticky="ew")
        ctk.CTkButton(self, text="Schließen", command=self._close).grid(
            row=5, column=0, padx=20, pady=(4, 20), sticky="e"
        )
        self.protocol("WM_DELETE_WINDOW", self._close)
        bind_dialog_escape(self, self._close)
        self.bind("<Return>", lambda _event: self._start())
        self._depth_menu.focus_set()

    def _start(self) -> None:
        generation = self._request_state.begin()
        if generation is None:
            return
        try:
            count = preview_depth(self._depth.get())
        except ValueError as exc:
            self._request_state.finish(generation)
            self._status.configure(text=str(exc), text_color=theme.ERROR)
            return
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

    def _accept_preview(self, generation: int, preview: SelectionPreview) -> None:
        if not self._request_state.finish(generation):
            return
        self._calculate.configure(state="normal", text="Aktualisieren")
        self._created.configure(text=f"Erstellt: {preview.created_at:%d.%m.%Y %H:%M:%S}")
        self._status.configure(text=preview_completion_text(preview), text_color=theme.TEXT_MUTED)
        for row, step in enumerate(preview.steps):
            presentation = present_preview_step(step)
            button = ctk.CTkButton(
                self._list,
                text=presentation.summary,
                anchor="w",
                command=lambda text=presentation.detail: self._detail_text.configure(text=text),
            )
            button.grid(row=row, column=0, padx=6, pady=4, sticky="ew")
            self._step_buttons.append(button)
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
        for button in self._step_buttons:
            button.destroy()
        self._step_buttons.clear()
        self._detail_text.configure(text="Keine aktuelle Vorschau.")
        self._created.configure(text="Wird berechnet…")

    def _close(self) -> None:
        self._request_state.close()
        release_dialog(self)
        self.destroy()
