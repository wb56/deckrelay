"""Responsive service-backed editor for persistent automatic-selection rules."""

from dataclasses import dataclass
from enum import Enum
import math
from tkinter import messagebox
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.controllers.selection_rule_settings_controller import (
    SelectionRuleSettingsController,
)
from party_player.selection_rule_settings import (
    DEFAULT_SELECTION_SCORING_SETTINGS,
    EffectiveSelectionRuleConfiguration,
    PLAY_COUNT_RULE_ID,
    RATING_RULE_ID,
    SelectionScoringSettings,
    SoftRuleSetting,
    selection_rule_weight_limits,
)
from party_player.selection_continuity import (
    BPM_CONTINUITY_RULE_ID,
    ENERGY_CONTINUITY_RULE_ID,
    GENRE_DIVERSITY_RULE_ID,
    MOOD_CONTINUITY_RULE_ID,
)
from party_player.ui import theme
from party_player.ui.responsive_dialog import (
    apply_responsive_dialog_geometry,
    bind_dialog_escape,
    release_dialog,
)


@dataclass(frozen=True, slots=True)
class SelectionRuleFormValues:
    play_count_enabled: bool
    play_count_weight: str
    rating_enabled: bool
    rating_weight: str
    genre_enabled: bool = False
    genre_strength: str = "Normal"
    bpm_enabled: bool = False
    bpm_strength: str = "Normal"
    energy_enabled: bool = False
    energy_strength: str = "Normal"
    mood_enabled: bool = False
    mood_strength: str = "Normal"


@dataclass(frozen=True, slots=True)
class SelectionRuleFormResult:
    settings: SelectionScoringSettings | None
    play_count_error: str = ""
    rating_error: str = ""
    transition_error: str = ""


STRENGTH_WEIGHTS = {"Niedrig": 0.5, "Normal": 1.0, "Hoch": 2.0}


class SelectionRuleMessageKind(Enum):
    INFORMATION = "information"
    SUCCESS = "success"
    ERROR = "error"


def selection_rule_message_color(kind: SelectionRuleMessageKind) -> str:
    return {
        SelectionRuleMessageKind.INFORMATION: theme.TEXT_MUTED,
        SelectionRuleMessageKind.SUCCESS: theme.SUCCESS,
        SelectionRuleMessageKind.ERROR: theme.ERROR,
    }[kind]


def strength_for_weight(weight: float) -> str:
    for label, value in STRENGTH_WEIGHTS.items():
        if weight == value:
            return label
    if math.isfinite(weight) and 0.0 <= weight <= 2.0:
        return f"{weight:g}"
    raise ValueError("Ungültige Stärke für Übergangsregel")


def weight_for_strength(value: str) -> float:
    if value in STRENGTH_WEIGHTS:
        return STRENGTH_WEIGHTS[value]
    weight, error = _parse_weight(value, *selection_rule_weight_limits(GENRE_DIVERSITY_RULE_ID))
    if error or weight is None:
        raise ValueError("Ungültige Stärke für Übergangsregel")
    return weight


def form_values(settings: SelectionScoringSettings) -> SelectionRuleFormValues:
    return SelectionRuleFormValues(
        settings.play_count.enabled,
        f"{settings.play_count.weight:g}",
        settings.rating.enabled,
        f"{settings.rating.weight:g}",
        settings.genre_diversity.enabled,
        strength_for_weight(settings.genre_diversity.weight),
        settings.bpm_continuity.enabled,
        strength_for_weight(settings.bpm_continuity.weight),
        settings.energy_continuity.enabled,
        strength_for_weight(settings.energy_continuity.weight),
        settings.mood_continuity.enabled,
        strength_for_weight(settings.mood_continuity.weight),
    )


def form_values_from_snapshot(
    snapshot: EffectiveSelectionRuleConfiguration,
) -> SelectionRuleFormValues:
    values = {
        rule.rule_id: SoftRuleSetting(rule.rule_id, rule.enabled, float(rule.weight))
        for rule in snapshot.rules
        if rule.configurable and rule.weight is not None
    }
    return form_values(
        SelectionScoringSettings(
            *(
                values[rule_id]
                for rule_id in (
                    PLAY_COUNT_RULE_ID,
                    RATING_RULE_ID,
                    GENRE_DIVERSITY_RULE_ID,
                    BPM_CONTINUITY_RULE_ID,
                    ENERGY_CONTINUITY_RULE_ID,
                    MOOD_CONTINUITY_RULE_ID,
                )
            )
        )
    )


def selection_rule_dialog_dimensions(
    compact: bool,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Keep the form usable in both established presentation classes."""
    return ((680, 660), (500, 430)) if compact else ((940, 740), (700, 500))


def validate_form(values: SelectionRuleFormValues) -> SelectionRuleFormResult:
    play_count, play_error = _parse_weight(
        values.play_count_weight, *selection_rule_weight_limits(PLAY_COUNT_RULE_ID)
    )
    rating, rating_error = _parse_weight(
        values.rating_weight, *selection_rule_weight_limits(RATING_RULE_ID)
    )
    strengths = (
        values.genre_strength,
        values.bpm_strength,
        values.energy_strength,
        values.mood_strength,
    )
    try:
        weights = tuple(weight_for_strength(strength) for strength in strengths)
    except ValueError:
        return SelectionRuleFormResult(None, play_error, rating_error, "Ungültige Stärke.")
    if play_error or rating_error:
        return SelectionRuleFormResult(None, play_error, rating_error)
    assert play_count is not None and rating is not None
    return SelectionRuleFormResult(
        SelectionScoringSettings(
            SoftRuleSetting(PLAY_COUNT_RULE_ID, values.play_count_enabled, play_count),
            SoftRuleSetting(RATING_RULE_ID, values.rating_enabled, rating),
            SoftRuleSetting(
                GENRE_DIVERSITY_RULE_ID,
                values.genre_enabled,
                weights[0],
            ),
            SoftRuleSetting(
                BPM_CONTINUITY_RULE_ID,
                values.bpm_enabled,
                weights[1],
            ),
            SoftRuleSetting(
                ENERGY_CONTINUITY_RULE_ID,
                values.energy_enabled,
                weights[2],
            ),
            SoftRuleSetting(
                MOOD_CONTINUITY_RULE_ID,
                values.mood_enabled,
                weights[3],
            ),
        )
    )


def _parse_weight(raw: str, minimum: float, maximum: float) -> tuple[float | None, str]:
    try:
        value = float(raw.strip().replace(",", "."))
    except ValueError:
        return None, "Bitte eine Zahl eingeben."
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return None, f"Zulässig sind Werte von {minimum:g} bis {maximum:g}."
    return value, ""


def _points(value: float) -> str:
    return f"{value:g} Punkt" if value == 1 else f"{value:g} Punkte"


class SelectionRuleSettingsDialog(ctk.CTkToplevel):  # type: ignore[misc]
    def __init__(self, parent: Any, controller: SelectionRuleSettingsController) -> None:
        super().__init__(parent)
        self._controller = controller
        self._loaded = False
        self.title("Einstellungen – Automatische Titelauswahl")
        compact = bool(getattr(parent, "_compact_layout_active", False))
        self._compact = compact
        preferred, minimum = selection_rule_dialog_dimensions(compact)
        apply_responsive_dialog_geometry(
            self, parent, preferred_size=preferred, minimum_size=minimum
        )
        self.transient(parent)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            self,
            text="Automatische Titelauswahl",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(20, 8), sticky="w")
        content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        content.grid(row=1, column=0, padx=4, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            content,
            text=(
                "Die Grundpriorität bestimmt die beste Abspielgruppe. Übergangsregeln "
                "bewerten nur Titel innerhalb dieser Gruppe.\n\nDiese Regeln wirken nur auf die "
                "automatische Katalogauswahl. Manuelle Queue, Wünsche, Playlist, "
                "Notfallauswahl und harte Sperren bleiben unberührt. Änderungen gelten ab "
                "der nächsten automatischen Auswahl beziehungsweise Vorschau."
            ),
            justify="left",
            anchor="w",
            wraplength=850 if not compact else 580,
        ).grid(row=0, column=0, padx=20, pady=(4, 12), sticky="ew")
        self._play_enabled = ctk.BooleanVar()
        self._rating_enabled = ctk.BooleanVar()
        self._play_weight = ctk.StringVar()
        self._rating_weight = ctk.StringVar()
        self._genre_enabled = ctk.BooleanVar()
        self._bpm_enabled = ctk.BooleanVar()
        self._energy_enabled = ctk.BooleanVar()
        self._mood_enabled = ctk.BooleanVar()
        self._genre_strength = ctk.StringVar(value="Normal")
        self._bpm_strength = ctk.StringVar(value="Normal")
        self._energy_strength = ctk.StringVar(value="Normal")
        self._mood_strength = ctk.StringVar(value="Normal")
        groups = ctk.CTkFrame(content, fg_color="transparent")
        groups.grid(row=1, column=0, padx=14, pady=4, sticky="nsew")
        groups.grid_columnconfigure(0, weight=1)
        if not compact:
            groups.grid_columnconfigure(1, weight=1)
        priority = ctk.CTkFrame(groups)
        priority.grid(
            row=0,
            column=0,
            padx=(0, 6) if not compact else 0,
            pady=6,
            sticky="nsew",
        )
        priority.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(priority, text="Grundpriorität", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=14, pady=(12, 4), sticky="w"
        )
        self._play_entry, self._play_effect, self._play_error = self._rule_group(
            priority,
            row=1,
            title="Selten gespielte Titel bevorzugen",
            variable=self._play_enabled,
            weight=self._play_weight,
            label="Punktabzug je abgeschlossener Wiedergabe",
            toggle=self._refresh_enabled_state,
        )
        self._rating_entry, self._rating_effect, self._rating_error = self._rule_group(
            priority,
            row=2,
            title="Bewertung berücksichtigen",
            variable=self._rating_enabled,
            weight=self._rating_weight,
            label="Gewichtung der Bewertung",
            toggle=self._refresh_enabled_state,
        )
        transition = ctk.CTkFrame(groups)
        transition.grid(
            row=0 if not compact else 1,
            column=1 if not compact else 0,
            padx=(6, 0) if not compact else 0,
            pady=6,
            sticky="nsew",
        )
        transition.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            transition,
            text="Übergang zum vorherigen Titel",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(12, 4), sticky="w")
        self._transition_menus = (
            self._transition_rule(
                transition,
                1,
                "Genre-Abwechslung",
                self._genre_enabled,
                self._genre_strength,
                "Wertet eine Wiederholung desselben Genres gegenüber dem vorherigen Titel ab.",
            ),
            self._transition_rule(
                transition,
                2,
                "BPM-Kontinuität",
                self._bpm_enabled,
                self._bpm_strength,
                "Bevorzugt kleinere Tempoabstände zum vorherigen Titel.",
            ),
            self._transition_rule(
                transition,
                3,
                "Energie-Kontinuität",
                self._energy_enabled,
                self._energy_strength,
                "Bevorzugt kleinere Energieunterschiede zum vorherigen Titel.",
            ),
            self._transition_rule(
                transition,
                4,
                "Stimmungsanschluss",
                self._mood_enabled,
                self._mood_strength,
                "Bevorzugt Titel mit gemeinsamen bestätigten Stimmungen.",
            ),
        )
        ctk.CTkLabel(
            transition,
            text=(
                "Fehlende, ungeprüfte oder zu unsichere Metadaten führen weder zu einem "
                "Vorteil noch zu einem Nachteil."
            ),
            justify="left",
            anchor="w",
            wraplength=390 if not compact else 540,
            text_color=theme.TEXT_MUTED,
        ).grid(row=5, column=0, padx=14, pady=(8, 14), sticky="ew")
        self._transition_error = ctk.CTkLabel(content, text="", text_color=theme.ERROR, anchor="w")
        self._transition_error.grid(row=2, column=0, padx=20, pady=2, sticky="ew")
        ctk.CTkLabel(
            content,
            text=(
                "Immer aktive Schutzregeln prüfen unter anderem Dateiverfügbarkeit, "
                "Pflichtmetadaten, Sperren und Wiederholungen. Sie können hier nicht "
                "deaktiviert werden."
            ),
            justify="left",
            anchor="w",
            wraplength=850 if not compact else 580,
            text_color=theme.TEXT_MUTED,
        ).grid(row=3, column=0, padx=20, pady=(4, 2), sticky="ew")
        self._message = ctk.CTkLabel(content, text="", text_color=theme.TEXT_MUTED, anchor="w")
        self._message_kind = SelectionRuleMessageKind.INFORMATION
        self._message.grid(row=4, column=0, padx=20, pady=6, sticky="ew")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, padx=20, pady=(8, 20), sticky="ew")
        ctk.CTkButton(
            actions, text="Standardwerte wiederherstellen", command=self._restore_defaults
        ).pack(side="left")
        ctk.CTkButton(actions, text="Abbrechen", command=self._close).pack(side="right")
        self._save_button = ctk.CTkButton(actions, text="Speichern", command=self._save)
        self._save_button.pack(side="right", padx=8)
        self.protocol("WM_DELETE_WINDOW", self._close)
        bind_dialog_escape(self, self._close)
        self.bind("<Return>", lambda _event: self._save())
        self._load_settings()
        self._play_entry.focus_set()

    def _load_settings(self) -> None:
        try:
            self._set_values(form_values_from_snapshot(self._controller.load()))
            self._loaded = True
        except Exception:
            self._show_message(
                "Die Auswahlregel-Einstellungen konnten nicht geladen werden. "
                "Es können keine Änderungen gespeichert werden.",
                SelectionRuleMessageKind.ERROR,
            )
            self._set_values(form_values(DEFAULT_SELECTION_SCORING_SETTINGS))
            self._save_button.configure(state="disabled")

    def _show_message(self, text: str, kind: SelectionRuleMessageKind) -> None:
        self._message_kind = kind
        self._message.configure(text=text, text_color=selection_rule_message_color(kind))

    def _rule_group(
        self,
        parent: Any,
        *,
        row: int,
        title: str,
        variable: Any,
        weight: Any,
        label: str,
        toggle: Any,
    ) -> tuple[Any, Any, Any]:
        frame = ctk.CTkFrame(parent)
        frame.grid(row=row, column=0, padx=20, pady=8, sticky="ew")
        frame.grid_columnconfigure(1, weight=1)
        ctk.CTkSwitch(frame, text=title, variable=variable, command=toggle).grid(
            row=0, column=0, columnspan=2, padx=14, pady=(12, 8), sticky="w"
        )
        ctk.CTkLabel(frame, text=label, anchor="w").grid(
            row=1, column=0, padx=14, pady=4, sticky="w"
        )
        entry = ctk.CTkEntry(frame, textvariable=weight, width=120)
        entry.grid(row=1, column=1, padx=14, pady=4, sticky="e")
        effect = ctk.CTkLabel(frame, text="", text_color=theme.TEXT_MUTED, anchor="w")
        effect.grid(row=2, column=0, columnspan=2, padx=14, pady=(2, 4), sticky="w")
        error = ctk.CTkLabel(frame, text="", text_color=theme.ERROR, anchor="w")
        error.grid(row=3, column=0, columnspan=2, padx=14, pady=(0, 10), sticky="w")
        weight.trace_add("write", lambda *_args: self._refresh_form())
        return entry, effect, error

    def _transition_rule(
        self,
        parent: Any,
        row: int,
        title: str,
        enabled: Any,
        strength: Any,
        description: str,
    ) -> Any:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, padx=10, pady=5, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkSwitch(
            frame,
            text=title,
            variable=enabled,
            command=self._refresh_enabled_state,
        ).grid(row=0, column=0, padx=4, sticky="w")
        menu = ctk.CTkOptionMenu(
            frame,
            variable=strength,
            values=list(STRENGTH_WEIGHTS),
            width=110,
        )
        menu.grid(row=0, column=1, padx=4, sticky="e")
        ctk.CTkLabel(
            frame,
            text=description,
            justify="left",
            anchor="w",
            wraplength=360 if not self._compact else 500,
            text_color=theme.TEXT_MUTED,
        ).grid(row=1, column=0, columnspan=2, padx=4, pady=(2, 0), sticky="ew")
        return menu

    def _current_values(self) -> SelectionRuleFormValues:
        return SelectionRuleFormValues(
            bool(self._play_enabled.get()),
            self._play_weight.get(),
            bool(self._rating_enabled.get()),
            self._rating_weight.get(),
            bool(self._genre_enabled.get()),
            self._genre_strength.get(),
            bool(self._bpm_enabled.get()),
            self._bpm_strength.get(),
            bool(self._energy_enabled.get()),
            self._energy_strength.get(),
            bool(self._mood_enabled.get()),
            self._mood_strength.get(),
        )

    def _set_values(self, values: SelectionRuleFormValues) -> None:
        self._play_enabled.set(values.play_count_enabled)
        self._play_weight.set(values.play_count_weight)
        self._rating_enabled.set(values.rating_enabled)
        self._rating_weight.set(values.rating_weight)
        self._genre_enabled.set(values.genre_enabled)
        self._genre_strength.set(values.genre_strength)
        self._bpm_enabled.set(values.bpm_enabled)
        self._bpm_strength.set(values.bpm_strength)
        self._energy_enabled.set(values.energy_enabled)
        self._energy_strength.set(values.energy_strength)
        self._mood_enabled.set(values.mood_enabled)
        self._mood_strength.set(values.mood_strength)
        self._refresh_enabled_state()

    def _refresh_enabled_state(self) -> None:
        self._play_entry.configure(state="normal" if self._play_enabled.get() else "disabled")
        self._rating_entry.configure(state="normal" if self._rating_enabled.get() else "disabled")
        enabled = (
            self._genre_enabled.get(),
            self._bpm_enabled.get(),
            self._energy_enabled.get(),
            self._mood_enabled.get(),
        )
        for menu, active in zip(self._transition_menus, enabled, strict=True):
            menu.configure(state="normal" if active else "disabled")
        self._refresh_form()

    def _refresh_form(self) -> None:
        self._refresh_effects()
        result = validate_form(self._current_values())
        self._play_error.configure(text=result.play_count_error, text_color=theme.ERROR)
        self._rating_error.configure(text=result.rating_error, text_color=theme.ERROR)
        self._transition_error.configure(text=result.transition_error, text_color=theme.ERROR)

    def _refresh_effects(self) -> None:
        values = self._current_values()
        play, _ = _parse_weight(
            values.play_count_weight, *selection_rule_weight_limits(PLAY_COUNT_RULE_ID)
        )
        rating, _ = _parse_weight(
            values.rating_weight, *selection_rule_weight_limits(RATING_RULE_ID)
        )
        self._play_effect.configure(
            text=(f"Eine vollständige Wiedergabe: −{play:g} Punkte" if play is not None else "")
        )
        self._rating_effect.configure(
            text=f"Bewertung 5: +{_points(2 * rating)}" if rating is not None else ""
        )

    def _restore_defaults(self) -> None:
        if not messagebox.askyesno(
            "Standardwerte wiederherstellen",
            "Alle Eingaben im Dialog auf die sicheren Standardwerte zurücksetzen? "
            "Gespeichert wird erst mit „Speichern“.",
            parent=self,
        ):
            return
        self._set_values(form_values_from_snapshot(self._controller.defaults()))
        self._show_message(
            "Standardwerte im Formular – zum Übernehmen speichern.",
            SelectionRuleMessageKind.INFORMATION,
        )

    def _save(self) -> None:
        if not self._loaded:
            return
        result = validate_form(self._current_values())
        self._play_error.configure(text=result.play_count_error, text_color=theme.ERROR)
        self._rating_error.configure(text=result.rating_error, text_color=theme.ERROR)
        self._transition_error.configure(text=result.transition_error, text_color=theme.ERROR)
        self._show_message("", SelectionRuleMessageKind.INFORMATION)
        if result.settings is None:
            if result.play_count_error:
                self._play_entry.focus_set()
            elif result.rating_error:
                self._rating_entry.focus_set()
            return
        try:
            saved = self._controller.save(result.settings)
        except (ValueError, RuntimeError):
            self._show_message(
                "Speichern fehlgeschlagen. Die bisherigen Einstellungen bleiben "
                "vollständig erhalten. Bitte Eingaben prüfen oder später erneut versuchen.",
                SelectionRuleMessageKind.ERROR,
            )
            return
        self._set_values(form_values_from_snapshot(saved))
        self._show_message(
            "Auswahlregel-Einstellungen wurden gespeichert.",
            SelectionRuleMessageKind.SUCCESS,
        )

    def _close(self) -> None:
        release_dialog(self)
        self.destroy()
