"""Persistence and integration tests for bounded soft-rule settings."""

from datetime import datetime, timedelta
from pathlib import Path
import random

import pytest

from party_player.automatic_selection import AutomaticSelectionHistory, AutomaticSelectionService
from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.enums import CompletionStatus
from party_player.models import Track
from party_player.repository import PartyPlayerRepository
from party_player.repositories.track_repository import TrackRepository
from party_player.selection_rule_settings import (
    DEFAULT_SELECTION_SCORING_SETTINGS,
    PLAY_COUNT_RULE_ID,
    RATING_RULE_ID,
    SelectionRuleSettingsRepository,
    SoftRuleSetting,
)
from party_player.track_selection import SelectionDecision, TrackSelectionService
from party_player.selection_decision import RuleOutcome


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


def test_v42_migration_seeds_compatible_defaults_and_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "upgrade.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute("UPDATE schema_version SET version = 41")
        connection.execute("DROP TABLE selection_rule_settings")

    migrate(database)
    migrate(database)

    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        rows = connection.execute(
            """SELECT rule_id, config_version, enabled, weight
               FROM selection_rule_settings ORDER BY rule_id"""
        ).fetchall()
    assert version == LATEST_SCHEMA_VERSION == 42
    assert [tuple(row) for row in rows] == [
        (PLAY_COUNT_RULE_ID, 1, 1, 10.0),
        (RATING_RULE_ID, 1, 1, 1.0),
    ]


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
            "INSERT INTO selection_rule_settings VALUES (?, 1, 1, 1, CURRENT_TIMESTAMP)",
            ("selection.future",),
        )
        connection.execute(
            "DELETE FROM selection_rule_settings WHERE rule_id=?", (PLAY_COUNT_RULE_ID,)
        )
    assert repository.load() == DEFAULT_SELECTION_SCORING_SETTINGS


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
