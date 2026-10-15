"""Controller boundary for automatic-selection rule settings."""

from party_player.selection_rule_settings import (
    DEFAULT_SELECTION_RULE_CONFIGURATION,
    EffectiveSelectionRuleConfiguration,
    SelectionScoringSettings,
    SelectionRuleSettingsService,
)


class SelectionRuleSettingsController:
    def __init__(self, service: SelectionRuleSettingsService) -> None:
        self._service = service

    def load(self) -> EffectiveSelectionRuleConfiguration:
        return self._service.current()

    def save(self, settings: SelectionScoringSettings) -> EffectiveSelectionRuleConfiguration:
        return self._service.save(settings)

    def defaults(self) -> EffectiveSelectionRuleConfiguration:
        return DEFAULT_SELECTION_RULE_CONFIGURATION
