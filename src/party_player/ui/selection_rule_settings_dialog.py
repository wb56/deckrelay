"""Responsive editor for the two persisted automatic soft-scoring rules."""

from dataclasses import dataclass
import math
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.controllers.selection_rule_settings_controller import (
    SelectionRuleSettingsController,
)
from party_player.selection_rule_settings import (
    DEFAULT_SELECTION_SCORING_SETTINGS,
    PLAY_COUNT_RULE_ID,
    RATING_RULE_ID,
    SelectionScoringSettings,
    SoftRuleSetting,
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


@dataclass(frozen=True, slots=True)
class SelectionRuleFormResult:
    settings: SelectionScoringSettings | None
    play_count_error: str = ""
    rating_error: str = ""


def form_values(settings: SelectionScoringSettings) -> SelectionRuleFormValues:
    return SelectionRuleFormValues(
        settings.play_count.enabled,
        f"{settings.play_count.weight:g}",
        settings.rating.enabled,
        f"{settings.rating.weight:g}",
    )


def selection_rule_dialog_dimensions(
    compact: bool,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Keep the form usable in both established presentation classes."""
    return ((620, 620), (480, 420)) if compact else ((720, 690), (500, 430))


def validate_form(values: SelectionRuleFormValues) -> SelectionRuleFormResult:
    play_count, play_error = _parse_weight(values.play_count_weight, 5.0, 100.0)
    rating, rating_error = _parse_weight(values.rating_weight, 0.0, 1.0)
    if play_error or rating_error:
        return SelectionRuleFormResult(None, play_error, rating_error)
    assert play_count is not None and rating is not None
    return SelectionRuleFormResult(
        SelectionScoringSettings(
            SoftRuleSetting(PLAY_COUNT_RULE_ID, values.play_count_enabled, play_count),
            SoftRuleSetting(RATING_RULE_ID, values.rating_enabled, rating),
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
        self.title("Einstellungen – Automatische Titelauswahl")
        preferred, minimum = selection_rule_dialog_dimensions(
            bool(getattr(parent, "_compact_layout_active", False))
        )
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
                "Selten gespielte Titel werden bevorzugt. Besser bewertete Titel werden bei "
                "ansonsten vergleichbaren Kandidaten bevorzugt.\n\nDiese Regeln wirken nur auf die "
                "automatische Katalogauswahl. Manuelle Queue, Wünsche, Playlist, "
                "Notfallauswahl und harte Sperren bleiben unberührt. Änderungen gelten ab "
                "der nächsten automatischen Auswahl beziehungsweise Vorschau."
            ),
            justify="left",
            anchor="w",
            wraplength=650,
        ).grid(row=0, column=0, padx=20, pady=(4, 12), sticky="ew")
        self._play_enabled = ctk.BooleanVar()
        self._rating_enabled = ctk.BooleanVar()
        self._play_weight = ctk.StringVar()
        self._rating_weight = ctk.StringVar()
        self._play_entry, self._play_effect, self._play_error = self._rule_group(
            content,
            row=1,
            title="Abspielhäufigkeit berücksichtigen",
            variable=self._play_enabled,
            weight=self._play_weight,
            label="Punktabzug je abgeschlossener Wiedergabe",
            toggle=self._refresh_enabled_state,
        )
        self._rating_entry, self._rating_effect, self._rating_error = self._rule_group(
            content,
            row=2,
            title="Titelbewertung berücksichtigen",
            variable=self._rating_enabled,
            weight=self._rating_weight,
            label="Gewichtung der Bewertung",
            toggle=self._refresh_enabled_state,
        )
        self._message = ctk.CTkLabel(content, text="", text_color=theme.ERROR, anchor="w")
        self._message.grid(row=3, column=0, padx=20, pady=6, sticky="ew")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, padx=20, pady=(8, 20), sticky="ew")
        ctk.CTkButton(actions, text="Standardwerte", command=self._restore_defaults).pack(
            side="left"
        )
        ctk.CTkButton(actions, text="Abbrechen", command=self._close).pack(side="right")
        ctk.CTkButton(actions, text="Speichern", command=self._save).pack(side="right", padx=8)
        self.protocol("WM_DELETE_WINDOW", self._close)
        bind_dialog_escape(self, self._close)
        self.bind("<Return>", lambda _event: self._save())
        try:
            self._set_values(form_values(controller.load()))
        except Exception as exc:
            self._message.configure(text=f"Einstellungen konnten nicht geladen werden: {exc}")
            self._set_values(form_values(DEFAULT_SELECTION_SCORING_SETTINGS))
        self._play_entry.focus_set()

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

    def _current_values(self) -> SelectionRuleFormValues:
        return SelectionRuleFormValues(
            bool(self._play_enabled.get()),
            self._play_weight.get(),
            bool(self._rating_enabled.get()),
            self._rating_weight.get(),
        )

    def _set_values(self, values: SelectionRuleFormValues) -> None:
        self._play_enabled.set(values.play_count_enabled)
        self._play_weight.set(values.play_count_weight)
        self._rating_enabled.set(values.rating_enabled)
        self._rating_weight.set(values.rating_weight)
        self._refresh_enabled_state()

    def _refresh_enabled_state(self) -> None:
        self._play_entry.configure(state="normal" if self._play_enabled.get() else "disabled")
        self._rating_entry.configure(state="normal" if self._rating_enabled.get() else "disabled")
        self._refresh_form()

    def _refresh_form(self) -> None:
        self._refresh_effects()
        result = validate_form(self._current_values())
        self._play_error.configure(text=result.play_count_error)
        self._rating_error.configure(text=result.rating_error)

    def _refresh_effects(self) -> None:
        values = self._current_values()
        play, _ = _parse_weight(values.play_count_weight, 5.0, 100.0)
        rating, _ = _parse_weight(values.rating_weight, 0.0, 1.0)
        self._play_effect.configure(
            text=(f"Eine vollständige Wiedergabe: −{play:g} Punkte" if play is not None else "")
        )
        self._rating_effect.configure(
            text=f"Bewertung 5: +{_points(2 * rating)}" if rating is not None else ""
        )

    def _restore_defaults(self) -> None:
        self._set_values(form_values(DEFAULT_SELECTION_SCORING_SETTINGS))
        self._message.configure(text="Standardwerte im Formular – zum Übernehmen speichern.")

    def _save(self) -> None:
        result = validate_form(self._current_values())
        self._play_error.configure(text=result.play_count_error)
        self._rating_error.configure(text=result.rating_error)
        self._message.configure(text="")
        if result.settings is None:
            (self._play_entry if result.play_count_error else self._rating_entry).focus_set()
            return
        try:
            self._controller.save(result.settings)
        except Exception as exc:
            self._message.configure(
                text=f"Speichern fehlgeschlagen. Die bisherigen Einstellungen bleiben erhalten: {exc}"
            )
            return
        self._close()

    def _close(self) -> None:
        release_dialog(self)
        self.destroy()
