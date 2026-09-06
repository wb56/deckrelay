"""Validated persistent settings for the existing automatic soft rules."""

from dataclasses import dataclass
import math

from party_player.database.connection import Database

PLAY_COUNT_RULE_ID = "selection.play_count"
RATING_RULE_ID = "selection.rating"
CONFIG_VERSION = 1


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


DEFAULT_SELECTION_SCORING_SETTINGS = SelectionScoringSettings()


class SelectionRuleSettingsRepository:
    """Load one safe immutable scoring snapshot per selection operation."""

    _LIMITS = {
        PLAY_COUNT_RULE_ID: (5.0, 100.0),
        RATING_RULE_ID: (0.0, 1.0),
    }

    def __init__(self, database: Database) -> None:
        self._database = database

    def load(self) -> SelectionScoringSettings:
        defaults = DEFAULT_SELECTION_SCORING_SETTINGS
        known = {PLAY_COUNT_RULE_ID: defaults.play_count, RATING_RULE_ID: defaults.rating}
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT rule_id, config_version, enabled, weight
                   FROM selection_rule_settings"""
            ).fetchall()
        parsed: dict[str, SoftRuleSetting] = {}
        for row in rows:
            rule_id = str(row["rule_id"])
            if rule_id not in known:
                continue
            try:
                version = int(row["config_version"])
                enabled_raw = int(row["enabled"])
                weight = float(row["weight"])
            except (TypeError, ValueError, OverflowError):
                continue
            if version != CONFIG_VERSION or enabled_raw not in (0, 1):
                continue
            minimum, maximum = self._LIMITS[rule_id]
            if not math.isfinite(weight) or not minimum <= weight <= maximum:
                continue
            parsed[rule_id] = SoftRuleSetting(rule_id, bool(enabled_raw), weight, version)
        return SelectionScoringSettings(
            parsed.get(PLAY_COUNT_RULE_ID, defaults.play_count),
            parsed.get(RATING_RULE_ID, defaults.rating),
        )

    def set(self, setting: SoftRuleSetting) -> None:
        if setting.rule_id not in self._LIMITS:
            raise ValueError("Unbekannte Auswahlregel")
        if setting.config_version != CONFIG_VERSION:
            raise ValueError("Nicht unterstützte Regelkonfigurationsversion")
        if type(setting.enabled) is not bool:
            raise ValueError("Aktivstatus muss boolesch sein")
        minimum, maximum = self._LIMITS[setting.rule_id]
        if not math.isfinite(setting.weight) or not minimum <= setting.weight <= maximum:
            raise ValueError(f"Gewichtung muss zwischen {minimum:g} und {maximum:g} liegen")
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO selection_rule_settings
                   (rule_id, config_version, enabled, weight)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(rule_id) DO UPDATE SET
                       config_version=excluded.config_version,
                       enabled=excluded.enabled,
                       weight=excluded.weight,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    setting.rule_id,
                    setting.config_version,
                    int(setting.enabled),
                    setting.weight,
                ),
            )
