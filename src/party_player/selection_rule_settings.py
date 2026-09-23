"""Validated persistent configuration for the bounded selection-rule registry."""

from dataclasses import dataclass
import math
import sqlite3

from party_player.database.connection import Database
from party_player.selection_continuity import (
    BPM_CONTINUITY_RULE_ID,
    ENERGY_CONTINUITY_RULE_ID,
    GENRE_DIVERSITY_RULE_ID,
    MOOD_CONTINUITY_RULE_ID,
)
from party_player.selection_decision import RuleKind

PLAY_COUNT_RULE_ID = "selection.play_count"
RATING_RULE_ID = "selection.rating"
CONFIG_VERSION = 1
ALL_SELECTIONS_SCOPE = "ALL_SELECTIONS"
AUTOMATIC_SELECTION_SCOPE = "AUTOMATIC_SELECTION"


@dataclass(frozen=True, slots=True)
class SoftRuleSetting:
    rule_id: str
    enabled: bool
    weight: float
    config_version: int = CONFIG_VERSION


@dataclass(frozen=True, slots=True)
class SelectionScoringSettings:
    play_count: SoftRuleSetting = SoftRuleSetting(PLAY_COUNT_RULE_ID, True, 10.0)
    rating: SoftRuleSetting = SoftRuleSetting(RATING_RULE_ID, True, 1.0)
    genre_diversity: SoftRuleSetting = SoftRuleSetting(GENRE_DIVERSITY_RULE_ID, False, 1.0)
    bpm_continuity: SoftRuleSetting = SoftRuleSetting(BPM_CONTINUITY_RULE_ID, False, 1.0)
    energy_continuity: SoftRuleSetting = SoftRuleSetting(ENERGY_CONTINUITY_RULE_ID, False, 1.0)
    mood_continuity: SoftRuleSetting = SoftRuleSetting(MOOD_CONTINUITY_RULE_ID, False, 1.0)


DEFAULT_SELECTION_SCORING_SETTINGS = SelectionScoringSettings()


@dataclass(frozen=True, slots=True)
class SelectionRuleConfiguration:
    rule_id: str
    rule_kind: RuleKind
    enabled: bool
    configurable: bool
    weight: float | None
    default_enabled: bool
    default_weight: float | None
    scope: str
    config_version: int = CONFIG_VERSION
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class EffectiveSelectionRuleConfiguration:
    rules: tuple[SelectionRuleConfiguration, ...]

    def rule(self, rule_id: str) -> SelectionRuleConfiguration:
        try:
            return next(rule for rule in self.rules if rule.rule_id == rule_id)
        except StopIteration as error:
            raise ValueError("Unbekannte Auswahlregel") from error


class SelectionRuleConfigurationError(RuntimeError):
    """Stable service-level error that never exposes a raw database failure."""


_SOFT_LIMITS = {
    PLAY_COUNT_RULE_ID: (5.0, 100.0),
    RATING_RULE_ID: (0.0, 1.0),
    GENRE_DIVERSITY_RULE_ID: (0.0, 2.0),
    BPM_CONTINUITY_RULE_ID: (0.0, 2.0),
    ENERGY_CONTINUITY_RULE_ID: (0.0, 2.0),
    MOOD_CONTINUITY_RULE_ID: (0.0, 2.0),
}

_HARD_RULES = (
    ("core.track_exists", ALL_SELECTIONS_SCOPE),
    ("core.required_metadata", ALL_SELECTIONS_SCOPE),
    ("selection.track_policy", ALL_SELECTIONS_SCOPE),
    ("selection.artist_policy", ALL_SELECTIONS_SCOPE),
    ("selection.track_suitability", ALL_SELECTIONS_SCOPE),
    ("selection.repetition", ALL_SELECTIONS_SCOPE),
    ("selection.short_track", ALL_SELECTIONS_SCOPE),
    ("selection.automatic_recent_track", AUTOMATIC_SELECTION_SCOPE),
)


def _defaults() -> tuple[SelectionRuleConfiguration, ...]:
    scoring = DEFAULT_SELECTION_SCORING_SETTINGS
    soft = (
        scoring.play_count,
        scoring.rating,
        scoring.genre_diversity,
        scoring.bpm_continuity,
        scoring.energy_continuity,
        scoring.mood_continuity,
    )
    hard = tuple(
        SelectionRuleConfiguration(
            rule_id, RuleKind.HARD_EXCLUSION, True, False, None, True, None, scope
        )
        for rule_id, scope in _HARD_RULES
    )
    weighted = tuple(
        SelectionRuleConfiguration(
            setting.rule_id,
            RuleKind.SOFT_WEIGHT,
            setting.enabled,
            True,
            setting.weight,
            setting.enabled,
            setting.weight,
            AUTOMATIC_SELECTION_SCOPE,
        )
        for setting in soft
    )
    return (*hard, *weighted)


DEFAULT_SELECTION_RULE_CONFIGURATION = EffectiveSelectionRuleConfiguration(_defaults())


class SelectionRuleSettingsRepository:
    """Load one validated immutable configuration snapshot per selection operation."""

    _LIMITS = _SOFT_LIMITS

    def __init__(self, database: Database) -> None:
        self._database = database

    def load_configuration(self) -> EffectiveSelectionRuleConfiguration:
        defaults = {rule.rule_id: rule for rule in DEFAULT_SELECTION_RULE_CONFIGURATION.rules}
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT rule_id, rule_kind, config_version, enabled, configurable, weight,
                          default_enabled, default_weight, scope, created_at, updated_at
                   FROM selection_rule_settings"""
            ).fetchall()
        parsed: dict[str, SelectionRuleConfiguration] = {}
        invalid = False
        for row in rows:
            rule_id = str(row["rule_id"])
            expected = defaults.get(rule_id)
            if expected is None:
                continue
            try:
                rule = SelectionRuleConfiguration(
                    rule_id=rule_id,
                    rule_kind=RuleKind(str(row["rule_kind"])),
                    enabled=bool(self._boolean(row["enabled"])),
                    configurable=bool(self._boolean(row["configurable"])),
                    weight=None if row["weight"] is None else float(row["weight"]),
                    default_enabled=bool(self._boolean(row["default_enabled"])),
                    default_weight=(
                        None if row["default_weight"] is None else float(row["default_weight"])
                    ),
                    scope=str(row["scope"]),
                    config_version=int(row["config_version"]),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                self._validate_configuration(rule, expected)
            except (TypeError, ValueError, OverflowError):
                invalid = True
                continue
            parsed[rule_id] = rule
        if invalid:
            return DEFAULT_SELECTION_RULE_CONFIGURATION
        return EffectiveSelectionRuleConfiguration(
            tuple(parsed.get(rule.rule_id, rule) for rule in defaults.values())
        )

    def load(self) -> SelectionScoringSettings:
        configuration = self.load_configuration()
        values = {
            rule.rule_id: SoftRuleSetting(rule.rule_id, rule.enabled, float(rule.weight))
            for rule in configuration.rules
            if rule.rule_kind is RuleKind.SOFT_WEIGHT and rule.weight is not None
        }
        return SelectionScoringSettings(*(values[rule_id] for rule_id in self._LIMITS))

    def set(self, setting: SoftRuleSetting) -> None:
        self._validate(setting)
        current = self.load()
        values = {value.rule_id: value for value in self._values(current)}
        values[setting.rule_id] = setting
        self.save(SelectionScoringSettings(*(values[rule_id] for rule_id in self._LIMITS)))

    def save(self, settings: SelectionScoringSettings) -> None:
        """Validate and persist one complete settings form atomically."""
        values = self._values(settings)
        if tuple(setting.rule_id for setting in values) != tuple(self._LIMITS):
            raise ValueError("Auswahlregeln sind unvollständig oder doppelt")
        for setting in values:
            self._validate(setting)
        with self._database.connect() as connection:
            for setting in values:
                default = DEFAULT_SELECTION_RULE_CONFIGURATION.rule(setting.rule_id)
                connection.execute(
                    """INSERT INTO selection_rule_settings
                       (rule_id, rule_kind, config_version, enabled, configurable, weight,
                        default_enabled, default_weight, scope)
                       VALUES (?, 'SOFT_WEIGHT', ?, ?, 1, ?, ?, ?, ?)
                       ON CONFLICT(rule_id) DO UPDATE SET
                           config_version=excluded.config_version,
                           enabled=excluded.enabled,
                           weight=excluded.weight,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE selection_rule_settings.configurable=1
                         AND selection_rule_settings.rule_kind='SOFT_WEIGHT'""",
                    (
                        setting.rule_id,
                        setting.config_version,
                        int(setting.enabled),
                        setting.weight,
                        int(default.default_enabled),
                        default.default_weight,
                        default.scope,
                    ),
                )

    def restore_defaults(self) -> None:
        self.save(DEFAULT_SELECTION_SCORING_SETTINGS)

    @staticmethod
    def _values(settings: SelectionScoringSettings) -> tuple[SoftRuleSetting, ...]:
        return (
            settings.play_count,
            settings.rating,
            settings.genre_diversity,
            settings.bpm_continuity,
            settings.energy_continuity,
            settings.mood_continuity,
        )

    @staticmethod
    def _boolean(value: object) -> int:
        if type(value) is not int or value not in (0, 1):
            raise ValueError("Aktivstatus muss boolesch sein")
        return value

    def _validate(self, setting: SoftRuleSetting) -> None:
        if setting.rule_id not in self._LIMITS:
            raise ValueError("Unbekannte oder unveränderliche Auswahlregel")
        if setting.config_version != CONFIG_VERSION:
            raise ValueError("Nicht unterstützte Regelkonfigurationsversion")
        if type(setting.enabled) is not bool:
            raise ValueError("Aktivstatus muss boolesch sein")
        minimum, maximum = self._LIMITS[setting.rule_id]
        if not math.isfinite(setting.weight) or not minimum <= setting.weight <= maximum:
            raise ValueError(f"Gewichtung muss zwischen {minimum:g} und {maximum:g} liegen")

    def _validate_configuration(
        self,
        rule: SelectionRuleConfiguration,
        expected: SelectionRuleConfiguration,
    ) -> None:
        if (
            rule.config_version != CONFIG_VERSION
            or rule.scope != expected.scope
            or rule.default_enabled is not expected.default_enabled
            or rule.default_weight != expected.default_weight
            or not rule.created_at
            or not rule.updated_at
        ):
            raise ValueError("Ungültige Auswahlregelkonfiguration")
        if rule.rule_kind is not expected.rule_kind:
            raise ValueError("Ungültige Auswahlregelart")
        if rule.rule_kind is RuleKind.HARD_EXCLUSION:
            if not rule.enabled or rule.configurable or rule.weight is not None:
                raise ValueError("Schutzregeln müssen unveränderlich aktiv bleiben")
            return
        if not rule.configurable or rule.weight is None:
            raise ValueError("Weiche Auswahlregel ist unvollständig")
        self._validate(SoftRuleSetting(rule.rule_id, rule.enabled, rule.weight))


class SelectionRuleSettingsService:
    """Application boundary used by later presentation layers."""

    def __init__(self, repository: SelectionRuleSettingsRepository) -> None:
        self._repository = repository

    def current(self) -> EffectiveSelectionRuleConfiguration:
        return self._repository.load_configuration()

    def update(self, rule_id: str, *, enabled: bool, weight: float | None) -> None:
        current = self.current().rule(rule_id)
        if not current.configurable:
            raise ValueError("Diese Schutzregel muss aktiv bleiben")
        if weight is None:
            raise ValueError("Für diese Bewertungsregel ist eine Gewichtung erforderlich")
        try:
            self._repository.set(SoftRuleSetting(rule_id, enabled, weight))
        except sqlite3.Error as error:
            raise SelectionRuleConfigurationError(
                "Die Auswahlregel-Konfiguration konnte nicht gespeichert werden"
            ) from error

    def restore_defaults(self) -> None:
        try:
            self._repository.restore_defaults()
        except sqlite3.Error as error:
            raise SelectionRuleConfigurationError(
                "Die Standardkonfiguration konnte nicht wiederhergestellt werden"
            ) from error
