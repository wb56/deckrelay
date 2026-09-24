"""Persistence and integration tests for bounded soft-rule settings."""

from datetime import datetime, timedelta
from pathlib import Path
import random
import sqlite3
from typing import Any

import pytest

from party_player.automatic_selection import AutomaticSelectionHistory, AutomaticSelectionService
from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.database import migrations
from party_player.enums import CompletionStatus
from party_player.models import Track
from party_player.repository import PartyPlayerRepository
from party_player.repositories.track_repository import TrackRepository
from party_player.selection_rule_settings import (
    DEFAULT_SELECTION_RULE_CONFIGURATION,
    DEFAULT_SELECTION_SCORING_SETTINGS,
    PLAY_COUNT_RULE_ID,
    RATING_RULE_ID,
    SelectionScoringSettings,
    SelectionRuleConfigurationError,
    SelectionRuleSettingsRepository,
    SelectionRuleSettingsService,
    SoftRuleSetting,
)
from party_player.selection_continuity import (
    BPM_CONTINUITY_RULE_ID,
    ENERGY_CONTINUITY_RULE_ID,
    GENRE_DIVERSITY_RULE_ID,
    MOOD_CONTINUITY_RULE_ID,
)
from party_player.track_selection import SelectionDecision, TrackSelectionService
from party_player.selection_decision import RuleKind, RuleOutcome


def _database(path: Path) -> tuple[Database, int]:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO tracks (id, file_path, title, artist, rating)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (1, "one.mp3", "One", "A", 1),
                (2, "two.mp3", "Two", "B", 5),
            ],
        )
    session = PartyPlayerRepository(database).create_session("Settings")
    return database, session.session_id


def _record_play(database: Database, session_id: int, track_id: int) -> None:
    PartyPlayerRepository(database).add_history(
        session_id,
        track_id,
        "A",
        datetime(2026, 9, 6, 12, 0) - timedelta(minutes=3),
        CompletionStatus.COMPLETED,
        180,
        completed_at=datetime(2026, 9, 6, 12, 0),
    )


def test_v43_migration_adds_disabled_metadata_rules_without_overwriting(tmp_path: Path) -> None:
    database = Database(tmp_path / "upgrade.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute("UPDATE schema_version SET version = 42")
        connection.execute(
            "UPDATE selection_rule_settings SET enabled=0, weight=25 WHERE rule_id=?",
            (PLAY_COUNT_RULE_ID,),
        )
        connection.execute(
            """INSERT OR REPLACE INTO selection_rule_settings
               (rule_id, rule_kind, config_version, enabled, configurable, weight,
                default_enabled, default_weight, scope)
               VALUES (?, 'SOFT_WEIGHT', 1, 1, 1, 2, 0, 1, 'AUTOMATIC_SELECTION')""",
            (GENRE_DIVERSITY_RULE_ID,),
        )

    migrate(database)
    migrate(database)

    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        rows = connection.execute(
            """SELECT rule_id, config_version, enabled, weight
               FROM selection_rule_settings WHERE rule_kind='SOFT_WEIGHT' ORDER BY rule_id"""
        ).fetchall()
    assert version == LATEST_SCHEMA_VERSION == 45
    assert [tuple(row) for row in rows] == [
        (BPM_CONTINUITY_RULE_ID, 1, 0, 1.0),
        (ENERGY_CONTINUITY_RULE_ID, 1, 0, 1.0),
        (GENRE_DIVERSITY_RULE_ID, 1, 1, 2.0),
        (MOOD_CONTINUITY_RULE_ID, 1, 0, 1.0),
        (PLAY_COUNT_RULE_ID, 1, 0, 25.0),
        (RATING_RULE_ID, 1, 1, 1.0),
    ]


def test_v43_migration_rolls_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "rollback.db")
    migrate(database)
    new_ids = (
        GENRE_DIVERSITY_RULE_ID,
        BPM_CONTINUITY_RULE_ID,
        ENERGY_CONTINUITY_RULE_ID,
        MOOD_CONTINUITY_RULE_ID,
    )
    with database.connect() as connection:
        connection.execute("UPDATE schema_version SET version = 42")
        connection.executemany(
            "DELETE FROM selection_rule_settings WHERE rule_id=?",
            ((rule_id,) for rule_id in new_ids),
        )

    def fail(connection: Any) -> None:
        connection.execute(
            """INSERT INTO selection_rule_settings
               (rule_id, rule_kind, config_version, enabled, configurable, weight,
                default_enabled, default_weight, scope)
               VALUES (?, 'SOFT_WEIGHT', 1, 0, 1, 1, 0, 1, 'AUTOMATIC_SELECTION')""",
            (GENRE_DIVERSITY_RULE_ID,),
        )
        raise sqlite3.OperationalError("simulated")

    monkeypatch.setattr(migrations, "_migrate_to_v43", fail)
    with pytest.raises(sqlite3.OperationalError):
        migrate(database)
    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        count = connection.execute(
            "SELECT COUNT(*) FROM selection_rule_settings WHERE rule_id IN (?, ?, ?, ?)",
            new_ids,
        ).fetchone()[0]
    assert version == 42
    assert count == 0


def test_v45_migration_is_transactional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "v45-rollback.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute("UPDATE schema_version SET version = 44")

    def fail(connection: Any) -> None:
        connection.execute("CREATE TABLE v45_partial_write (value INTEGER NOT NULL)")
        raise sqlite3.OperationalError("simulated")

    monkeypatch.setattr(migrations, "_migrate_to_v45", fail)
    with pytest.raises(sqlite3.OperationalError):
        migrate(database)

    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        partial = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v45_partial_write'"
        ).fetchone()
    assert version == 44
    assert partial is None


def test_complete_typed_registry_contains_soft_defaults_and_immutable_hard_rules(
    tmp_path: Path,
) -> None:
    database, _ = _database(tmp_path / "registry.db")
    configuration = SelectionRuleSettingsRepository(database).load_configuration()

    assert len(configuration.rules) == 14
    hard_rules = tuple(
        rule for rule in configuration.rules if rule.rule_kind is RuleKind.HARD_EXCLUSION
    )
    assert len(hard_rules) == 8
    assert all(rule.enabled and not rule.configurable for rule in hard_rules)
    assert all(rule.weight is None and rule.default_weight is None for rule in hard_rules)
    assert all(rule.created_at and rule.updated_at for rule in configuration.rules)
    assert {
        (rule.rule_id, rule.enabled, rule.weight)
        for rule in configuration.rules
        if rule.rule_kind is RuleKind.SOFT_WEIGHT
    } == {
        (rule.rule_id, rule.enabled, rule.weight)
        for rule in DEFAULT_SELECTION_RULE_CONFIGURATION.rules
        if rule.rule_kind is RuleKind.SOFT_WEIGHT
    }


def test_service_updates_soft_rules_protects_hard_rules_and_restores_defaults(
    tmp_path: Path,
) -> None:
    database, _ = _database(tmp_path / "service.db")
    repository = SelectionRuleSettingsRepository(database)
    service = SelectionRuleSettingsService(repository)

    service.update(RATING_RULE_ID, enabled=False, weight=0.5)
    assert repository.load().rating == SoftRuleSetting(RATING_RULE_ID, False, 0.5)
    with pytest.raises(ValueError, match="Schutzregel"):
        service.update("core.track_exists", enabled=False, weight=None)

    service.restore_defaults()
    assert repository.load() == DEFAULT_SELECTION_SCORING_SETTINGS


def test_service_translates_atomic_database_failure(tmp_path: Path) -> None:
    database, _ = _database(tmp_path / "service-failure.db")
    service = SelectionRuleSettingsService(SelectionRuleSettingsRepository(database))
    with database.connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_rating BEFORE UPDATE ON selection_rule_settings
               WHEN NEW.rule_id = 'selection.rating' BEGIN
               SELECT RAISE(ABORT, 'internal detail'); END"""
        )

    with pytest.raises(SelectionRuleConfigurationError, match="konnte nicht gespeichert"):
        service.update(RATING_RULE_ID, enabled=False, weight=0.5)
    assert SelectionRuleSettingsRepository(database).load() == DEFAULT_SELECTION_SCORING_SETTINGS


def test_service_saves_complete_form_atomically_and_returns_effective_snapshot(
    tmp_path: Path,
) -> None:
    database, _ = _database(tmp_path / "service-complete-save.db")
    repository = SelectionRuleSettingsRepository(database)
    service = SelectionRuleSettingsService(repository)
    changed = SelectionScoringSettings(
        SoftRuleSetting(PLAY_COUNT_RULE_ID, False, 20.0),
        SoftRuleSetting(RATING_RULE_ID, True, 0.5),
        SoftRuleSetting(GENRE_DIVERSITY_RULE_ID, True, 0.5),
        SoftRuleSetting(BPM_CONTINUITY_RULE_ID, True, 1.0),
        SoftRuleSetting(ENERGY_CONTINUITY_RULE_ID, True, 2.0),
        SoftRuleSetting(MOOD_CONTINUITY_RULE_ID, True, 1.0),
    )

    snapshot = service.save(changed)

    assert repository.load() == changed
    assert snapshot.rule(PLAY_COUNT_RULE_ID).enabled is False
    assert snapshot.rule(RATING_RULE_ID).weight == 0.5
    assert snapshot.rule("core.track_exists").enabled is True
    assert snapshot.rule("core.track_exists").configurable is False


def test_repository_validates_writes_and_falls_back_per_damaged_row(tmp_path: Path) -> None:
    database, _ = _database(tmp_path / "validation.db")
    repository = SelectionRuleSettingsRepository(database)
    assert repository.load() == DEFAULT_SELECTION_SCORING_SETTINGS

    repository.set(SoftRuleSetting(PLAY_COUNT_RULE_ID, False, 5.0))
    repository.set(SoftRuleSetting(RATING_RULE_ID, False, 0.5))
    loaded = repository.load()
    assert not loaded.play_count.enabled and loaded.play_count.weight == 5.0
    assert not loaded.rating.enabled and loaded.rating.weight == 0.5

    for setting in (
        SoftRuleSetting(PLAY_COUNT_RULE_ID, True, 4.99),
        SoftRuleSetting(RATING_RULE_ID, True, 1.01),
        SoftRuleSetting(RATING_RULE_ID, True, float("nan")),
        SoftRuleSetting(PLAY_COUNT_RULE_ID, True, float("inf")),
        SoftRuleSetting("selection.future", True, 1.0),
        SoftRuleSetting(PLAY_COUNT_RULE_ID, "yes", 10.0),  # type: ignore[arg-type]
    ):
        with pytest.raises(ValueError):
            repository.set(setting)

    with database.connect() as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """UPDATE selection_rule_settings
               SET config_version=99, weight='damaged' WHERE rule_id=?""",
            (RATING_RULE_ID,),
        )
        connection.execute(
            """INSERT INTO selection_rule_settings
               (rule_id, rule_kind, config_version, enabled, configurable, weight,
                default_enabled, default_weight, scope)
               VALUES (?, 'SOFT_WEIGHT', 1, 1, 1, 1, 1, 1, 'AUTOMATIC_SELECTION')""",
            ("selection.future",),
        )
        connection.execute(
            "DELETE FROM selection_rule_settings WHERE rule_id=?", (PLAY_COUNT_RULE_ID,)
        )
    assert repository.load() == DEFAULT_SELECTION_SCORING_SETTINGS


def test_repository_saves_all_six_rules_atomically(tmp_path: Path) -> None:
    database, _ = _database(tmp_path / "atomic.db")
    repository = SelectionRuleSettingsRepository(database)
    changed = SelectionScoringSettings(
        SoftRuleSetting(PLAY_COUNT_RULE_ID, False, 20.0),
        SoftRuleSetting(RATING_RULE_ID, False, 0.5),
        SoftRuleSetting(GENRE_DIVERSITY_RULE_ID, True, 0.5),
        SoftRuleSetting(BPM_CONTINUITY_RULE_ID, True, 1.0),
        SoftRuleSetting(ENERGY_CONTINUITY_RULE_ID, True, 2.0),
        SoftRuleSetting(MOOD_CONTINUITY_RULE_ID, True, 1.0),
    )

    repository.save(changed)

    assert repository.load() == changed
    with database.connect() as connection:
        connection.execute(
            f"""CREATE TRIGGER reject_energy BEFORE UPDATE ON selection_rule_settings
                WHEN NEW.rule_id = '{ENERGY_CONTINUITY_RULE_ID}' BEGIN
                SELECT RAISE(ABORT, 'stop'); END"""
        )
    attempted = SelectionScoringSettings(
        SoftRuleSetting(PLAY_COUNT_RULE_ID, True, 20.0),
        SoftRuleSetting(RATING_RULE_ID, True, 0.5),
        SoftRuleSetting(GENRE_DIVERSITY_RULE_ID, False, 0.5),
        SoftRuleSetting(BPM_CONTINUITY_RULE_ID, False, 1.0),
        SoftRuleSetting(ENERGY_CONTINUITY_RULE_ID, False, 2.0),
        SoftRuleSetting(MOOD_CONTINUITY_RULE_ID, False, 1.0),
    )
    with pytest.raises(sqlite3.IntegrityError):
        repository.save(attempted)
    assert repository.load() == changed


def test_repository_loads_all_rules_with_one_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = _database(tmp_path / "one-select.db")
    statements: list[str] = []
    open_connection = database._open_connection

    def traced_connection() -> sqlite3.Connection:
        connection = open_connection()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_open_connection", traced_connection)

    assert SelectionRuleSettingsRepository(database).load() == DEFAULT_SELECTION_SCORING_SETTINGS
    selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "selection_rule_settings" in statement
    ]
    assert len(selects) == 1


def test_defaults_preserve_play_count_dominance_and_rules_can_be_disabled(
    tmp_path: Path,
) -> None:
    database, session_id = _database(tmp_path / "selection.db")
    _record_play(database, session_id, 2)
    settings = SelectionRuleSettingsRepository(database)

    selected = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(1),
        rule_settings=settings,
    ).select(TrackSelectionService())
    assert selected is not None and selected.id == 1

    settings.set(SoftRuleSetting(PLAY_COUNT_RULE_ID, False, 10.0))
    selected = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(1),
        rule_settings=settings,
    ).select(TrackSelectionService())
    assert selected is not None and selected.id == 2

    settings.set(SoftRuleSetting(RATING_RULE_ID, False, 1.0))
    first = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(7),
        rule_settings=settings,
    ).select(TrackSelectionService())
    second = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(7),
        rule_settings=settings,
    ).select(TrackSelectionService())
    assert first is not None and second is not None and first.id == second.id


def test_settings_load_once_for_real_selection_and_once_for_entire_preview(
    tmp_path: Path,
) -> None:
    database, _ = _database(tmp_path / "snapshot.db")

    class CountingSettings(SelectionRuleSettingsRepository):
        calls = 0

        def load(self):
            self.calls += 1
            return super().load()

    settings = CountingSettings(database)
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(3),
        rule_settings=settings,
    )

    assert selector.select(TrackSelectionService()) is not None
    assert settings.calls == 1
    preview = selector.preview(TrackSelectionService(), 2)
    assert preview.achieved_depth == 2
    assert settings.calls == 2


def test_persisted_continuity_setting_is_used_without_extra_settings_load(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "persisted-continuity.db")
    _record_play(database, session_id, 2)
    repository = SelectionRuleSettingsRepository(database)
    repository.set(SoftRuleSetting(GENRE_DIVERSITY_RULE_ID, True, 2.0))
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(1),
        rule_settings=repository,
    )

    assert selector.select(TrackSelectionService()) is not None
    assert selector.last_rationale is not None
    results = [
        result
        for result in selector.last_rationale.rule_evaluations
        if result.rule_id == GENRE_DIVERSITY_RULE_ID
    ]
    assert results
    assert all(dict(result.facts)["weight"] == 2.0 for result in results)


def test_soft_settings_cannot_override_a_hard_exclusion(tmp_path: Path) -> None:
    class RejectAll:
        def evaluate(self, _entry, _track: Track):
            return SelectionDecision.reject("BLOCKED_TRACK")

    database, _ = _database(tmp_path / "hard-rule.db")
    repository = SelectionRuleSettingsRepository(database)
    repository.set(SoftRuleSetting(PLAY_COUNT_RULE_ID, False, 10.0))
    repository.set(SoftRuleSetting(RATING_RULE_ID, False, 1.0))
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        rule_settings=repository,
    )

    assert selector.select(TrackSelectionService((RejectAll(),))) is None


def test_zero_rating_weight_is_neutral_and_disabled_rules_keep_rng_ties(
    tmp_path: Path,
) -> None:
    database, _ = _database(tmp_path / "neutral-and-disabled.db")
    repository = SelectionRuleSettingsRepository(database)
    repository.set(SoftRuleSetting(PLAY_COUNT_RULE_ID, False, 10.0))
    repository.set(SoftRuleSetting(RATING_RULE_ID, True, 0.0))
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(13),
        rule_settings=repository,
    )

    assert selector.select(TrackSelectionService()) is not None
    assert selector.last_rationale is not None
    rating_results = [
        result
        for result in selector.last_rationale.rule_evaluations
        if result.rule_id == RATING_RULE_ID
    ]
    assert rating_results
    assert all(result.result_code is RuleOutcome.PASS for result in rating_results)
    assert all(result.score_delta == 0 for result in rating_results)
    assert selector.last_rationale.decision_reason_code == "SELECTED_RNG_TIE_BREAK"

    repository.set(SoftRuleSetting(RATING_RULE_ID, False, 0.0))
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(13),
        rule_settings=repository,
    )
    assert selector.select(TrackSelectionService()) is not None
    assert selector.last_rationale is not None
    assert selector.last_rationale.decision_reason_code == "SELECTED_RNG_TIE_BREAK"
    assert not {result.rule_id for result in selector.last_rationale.rule_evaluations} & {
        PLAY_COUNT_RULE_ID,
        RATING_RULE_ID,
    }
