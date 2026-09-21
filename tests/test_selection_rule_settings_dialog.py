"""Display-independent tests for automatic-selection settings UI behavior."""

from typing import Any, cast

import pytest

from party_player.selection_rule_settings import (
    PLAY_COUNT_RULE_ID,
    RATING_RULE_ID,
    SelectionScoringSettings,
    SoftRuleSetting,
)
from party_player.selection_continuity import (
    BPM_CONTINUITY_RULE_ID,
    ENERGY_CONTINUITY_RULE_ID,
    GENRE_DIVERSITY_RULE_ID,
    MOOD_CONTINUITY_RULE_ID,
)
from party_player.ui.selection_rule_settings_dialog import (
    STRENGTH_WEIGHTS,
    SelectionRuleFormValues,
    SelectionRuleSettingsDialog,
    form_values,
    selection_rule_dialog_dimensions,
    strength_for_weight,
    validate_form,
    weight_for_strength,
)
from party_player.ui import main_window
from party_player.ui.main_window import MainWindow


class _Value:
    def __init__(self, value: object) -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class _Widget:
    def __init__(self) -> None:
        self.text = ""
        self.state = "normal"
        self.focused = False

    def configure(self, **values: object) -> None:
        self.text = str(values.get("text", self.text))
        self.state = str(values.get("state", self.state))

    def focus_set(self) -> None:
        self.focused = True


class _Controller:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.saved: list[object] = []

    def save(self, settings: object) -> None:
        if self.error is not None:
            raise self.error
        self.saved.append(settings)


def test_visible_program_options_build_selection_settings_button(monkeypatch: Any) -> None:
    created: list[Any] = []
    opened: list[bool] = []

    class Button:
        def __init__(self, parent: object, *, text: str, command: Any) -> None:
            self.parent = parent
            self.text = text
            self.command = command
            self.grid_options: dict[str, object] = {}
            created.append(self)

        def grid(self, **options: object) -> None:
            self.grid_options = options

    monkeypatch.setattr(main_window.ctk, "CTkButton", Button)
    window = object.__new__(MainWindow)
    window._show_selection_rule_settings = lambda: opened.append(True)
    window._show_automatic_preview = lambda: None
    window._show_external_program_settings = lambda: None
    playback_group = object()
    system_group = object()

    window._build_program_option_action_buttons(playback_group, system_group)

    selection_button = window._selection_rule_settings_button
    assert selection_button in created
    assert selection_button.parent is playback_group
    assert selection_button.text == "Automatikregeln…"
    assert selection_button.grid_options["row"] == 5
    assert window._automatic_queue_planning_button.text == ("Queue automatisch zusammenstellen…")
    assert window._automatic_queue_planning_button.grid_options["row"] == 5
    assert window._external_program_settings_button.parent is system_group
    assert window._external_program_settings_button.grid_options["row"] == 1
    selection_button.command()
    assert opened == [True]


class _Dialog:
    def __init__(self, controller: _Controller | None = None) -> None:
        self._controller = controller or _Controller()
        self._play_enabled = _Value(True)
        self._play_weight = _Value("10")
        self._rating_enabled = _Value(True)
        self._rating_weight = _Value("1")
        self._genre_enabled = _Value(False)
        self._bpm_enabled = _Value(False)
        self._energy_enabled = _Value(False)
        self._mood_enabled = _Value(False)
        self._genre_strength = _Value("Normal")
        self._bpm_strength = _Value("Normal")
        self._energy_strength = _Value("Normal")
        self._mood_strength = _Value("Normal")
        self._play_entry = _Widget()
        self._rating_entry = _Widget()
        self._play_effect = _Widget()
        self._rating_effect = _Widget()
        self._play_error = _Widget()
        self._rating_error = _Widget()
        self._message = _Widget()
        self._transition_error = _Widget()
        self._transition_menus = (_Widget(), _Widget(), _Widget(), _Widget())
        self.closed = False

    def _current_values(self) -> SelectionRuleFormValues:
        return SelectionRuleFormValues(
            bool(self._play_enabled.get()),
            str(self._play_weight.get()),
            bool(self._rating_enabled.get()),
            str(self._rating_weight.get()),
            bool(self._genre_enabled.get()),
            str(self._genre_strength.get()),
            bool(self._bpm_enabled.get()),
            str(self._bpm_strength.get()),
            bool(self._energy_enabled.get()),
            str(self._energy_strength.get()),
            bool(self._mood_enabled.get()),
            str(self._mood_strength.get()),
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
        SelectionRuleSettingsDialog._refresh_enabled_state(cast(Any, self))

    def _refresh_effects(self) -> None:
        SelectionRuleSettingsDialog._refresh_effects(cast(Any, self))

    def _refresh_form(self) -> None:
        SelectionRuleSettingsDialog._refresh_form(cast(Any, self))

    def _close(self) -> None:
        self.closed = True


def test_form_loads_persisted_values_and_explains_effects() -> None:
    values = form_values(
        SelectionScoringSettings(
            SoftRuleSetting(PLAY_COUNT_RULE_ID, False, 20.0),
            SoftRuleSetting(RATING_RULE_ID, True, 0.5),
        )
    )
    dialog = _Dialog()

    dialog._set_values(values)

    assert dialog._play_enabled.get() is False
    assert dialog._play_weight.get() == "20"
    assert dialog._rating_weight.get() == "0.5"
    assert dialog._play_effect.text == "Eine vollständige Wiedergabe: −20 Punkte"
    assert dialog._rating_effect.text == "Bewertung 5: +1 Punkt"


def test_disabled_rules_make_only_their_weight_fields_inactive() -> None:
    dialog = _Dialog()
    dialog._play_enabled.set(False)

    SelectionRuleSettingsDialog._refresh_enabled_state(cast(Any, dialog))

    assert dialog._play_entry.state == "disabled"
    assert dialog._rating_entry.state == "normal"


def test_large_and_compact_dialog_sizes_remain_locally_scrollable_targets() -> None:
    assert selection_rule_dialog_dimensions(False) == ((940, 740), (700, 500))
    assert selection_rule_dialog_dimensions(True) == ((680, 660), (500, 430))


@pytest.mark.parametrize(
    ("play", "rating", "field"),
    [("text", "1", "play"), ("4", "1", "play"), ("10", "1.1", "rating")],
)
def test_invalid_input_is_explained_at_the_affected_field(
    play: str, rating: str, field: str
) -> None:
    result = validate_form(SelectionRuleFormValues(True, play, True, rating))

    assert result.settings is None
    assert bool(result.play_count_error) is (field == "play")
    assert bool(result.rating_error) is (field == "rating")


def test_save_persists_all_values_together_and_cancel_does_not_save() -> None:
    controller = _Controller()
    saved = _Dialog(controller)
    cancelled = _Dialog(controller)
    saved._play_enabled.set(False)
    saved._rating_weight.set("0,5")
    saved._genre_enabled.set(True)
    saved._genre_strength.set("Niedrig")
    saved._bpm_enabled.set(True)
    saved._bpm_strength.set("Normal")
    saved._energy_enabled.set(True)
    saved._energy_strength.set("Hoch")

    SelectionRuleSettingsDialog._save(cast(Any, saved))
    cancelled._close()

    assert len(controller.saved) == 1
    settings = controller.saved[0]
    assert not settings.play_count.enabled
    assert settings.rating.weight == 0.5
    assert settings.genre_diversity == SoftRuleSetting(GENRE_DIVERSITY_RULE_ID, True, 0.5)
    assert settings.bpm_continuity == SoftRuleSetting(BPM_CONTINUITY_RULE_ID, True, 1.0)
    assert settings.energy_continuity == SoftRuleSetting(ENERGY_CONTINUITY_RULE_ID, True, 2.0)
    assert settings.mood_continuity == SoftRuleSetting(MOOD_CONTINUITY_RULE_ID, False, 1.0)
    assert saved.closed and cancelled.closed


def test_defaults_only_change_form_until_save() -> None:
    controller = _Controller()
    dialog = _Dialog(controller)
    dialog._play_weight.set("20")

    SelectionRuleSettingsDialog._restore_defaults(cast(Any, dialog))

    assert dialog._play_weight.get() == "10"
    assert controller.saved == []
    assert "zum Übernehmen speichern" in dialog._message.text


def test_storage_error_keeps_dialog_open_and_previous_values_untouched() -> None:
    controller = _Controller(RuntimeError("Datenbank nicht erreichbar"))
    dialog = _Dialog(controller)

    SelectionRuleSettingsDialog._save(cast(Any, dialog))

    assert not dialog.closed
    assert controller.saved == []
    assert "bisherigen Einstellungen bleiben erhalten" in dialog._message.text


def test_transition_strengths_are_discrete_and_disabled_by_default() -> None:
    values = form_values(SelectionScoringSettings())

    assert STRENGTH_WEIGHTS == {"Niedrig": 0.5, "Normal": 1.0, "Hoch": 2.0}
    assert not values.genre_enabled and values.genre_strength == "Normal"
    assert not values.bpm_enabled and values.bpm_strength == "Normal"
    assert not values.energy_enabled and values.energy_strength == "Normal"
    assert not values.mood_enabled and values.mood_strength == "Normal"


def test_valid_custom_persisted_weight_is_preserved_without_free_form_editor() -> None:
    assert strength_for_weight(0.75) == "0.75"
    assert weight_for_strength("0,75") == 0.75
    values = form_values(
        SelectionScoringSettings(
            genre_diversity=SoftRuleSetting(GENRE_DIVERSITY_RULE_ID, True, 0.75)
        )
    )

    result = validate_form(values)

    assert result.settings is not None
    assert result.settings.genre_diversity.weight == 0.75
