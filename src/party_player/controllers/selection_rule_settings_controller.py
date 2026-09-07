"""Controller boundary for automatic-selection rule settings."""

from party_player.selection_rule_settings import (
    SelectionRuleSettingsRepository,
    SelectionScoringSettings,
)


class SelectionRuleSettingsController:
    def __init__(self, repository: SelectionRuleSettingsRepository) -> None:
        self._repository = repository

    def load(self) -> SelectionScoringSettings:
        return self._repository.load()

    def save(self, settings: SelectionScoringSettings) -> None:
        self._repository.save(settings)
