from dataclasses import dataclass, replace
from pathlib import Path
import random
import sqlite3

import pytest

import party_player.automatic_selection_planning as planning_module
from party_player.automatic_selection import AutomaticSelectionHistory, AutomaticSelectionService
from party_player.automatic_selection_plan import AutomaticSelectionPlanBundle
from party_player.automatic_selection_planning import (
    AutomaticSelectionPlanCompletion,
    AutomaticSelectionPlanningService,
)
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import SessionStatus
from party_player.models import Track
from party_player.repositories.automatic_selection_plan_repository import (
    AutomaticSelectionPlanRepository,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.selection_decision import (
    RuleKind,
    SelectionContext,
    SelectionRuleInput,
    hard_rule_evaluation,
)
from party_player.selection_rule_settings import (
    DEFAULT_SELECTION_SCORING_SETTINGS,
    SelectionRuleSettingsRepository,
    SoftRuleSetting,
)
from party_player.track_selection import TrackSelectionService


def _track(track_id: int) -> Track:
    return Track(
        track_id,
        f"ignored-{track_id}.mp3",
        f"Track {track_id}",
        "Artist",
        "",
        180.0,
    )


@dataclass
class _HistorySnapshot:
    counts: dict[int, int]
    recent: list[int]

    def play_counts(self) -> dict[int, int]:
        return dict(self.counts)

    def recent_track_ids(self, limit: int) -> set[int]:
        return set(self.recent[:limit])

    def record_played(self, track: Track) -> None:
        self.counts[track.id] = self.counts.get(track.id, 0) + 1
        self.recent.insert(0, track.id)


@dataclass
class _History:
    calls: int = 0

    def preview_snapshot(self) -> _HistorySnapshot:
        self.calls += 1
        return _HistorySnapshot({}, [])


@dataclass
class _Tracks:
    values: tuple[Track, ...]
    calls: int = 0

    def automatic_candidates(self) -> list[Track]:
        self.calls += 1
        return list(self.values)


@dataclass
class _Writer:
    bundles: list[AutomaticSelectionPlanBundle]
    fail: bool = False

    def create(self, bundle: AutomaticSelectionPlanBundle) -> AutomaticSelectionPlanBundle:
        if self.fail:
            raise sqlite3.OperationalError("write failed")
        self.bundles.append(bundle)
        return bundle


@dataclass
class _Settings:
    value: object

    def load(self):
        return self.value


def _planner(
    tracks: tuple[Track, ...], *, productive_seed: int = 99, writer: _Writer | None = None
) -> tuple[AutomaticSelectionPlanningService, _Writer, AutomaticSelectionService]:
    target = writer or _Writer([])
    selector = AutomaticSelectionService(
        _Tracks(tracks),
        _History(),
        recent_track_limit=0,
        randomizer=random.Random(productive_seed),
    )
    return (
        AutomaticSelectionPlanningService(selector, TrackSelectionService(), target),
        target,
        selector,
    )


def test_same_seed_creates_same_sequence_without_consuming_productive_rng() -> None:
    tracks = tuple(_track(value) for value in range(1, 7))
    first, _, first_selector = _planner(tracks, productive_seed=1)
    second, _, second_selector = _planner(tracks, productive_seed=999)

    first_result = first.create_draft_plan(1, 5, planning_seed=720)
    second_result = second.create_draft_plan(1, 5, planning_seed=720)
    first_real = first_selector.select(TrackSelectionService())
    control_real = AutomaticSelectionService(
        _Tracks(tracks), _History(), recent_track_limit=0, randomizer=random.Random(1)
    ).select(TrackSelectionService())

    assert first_result.completion_status is AutomaticSelectionPlanCompletion.COMPLETE
    assert second_result.completion_status is AutomaticSelectionPlanCompletion.COMPLETE
    assert first_result.plan is not None and second_result.plan is not None
    assert [step.track_id for step in first_result.plan.steps] == [
        step.track_id for step in second_result.plan.steps
    ]
    assert [
        (
            step.previous_track_id,
            step.relaxation_stage_code,
            step.primary_play_count,
            step.secondary_score,
            step.tie_candidate_count,
            step.tie_break_method_code,
            step.selection_reason_code,
        )
        for step in first_result.plan.steps
    ] == [
        (
            step.previous_track_id,
            step.relaxation_stage_code,
            step.primary_play_count,
            step.secondary_score,
            step.tie_candidate_count,
            step.tie_break_method_code,
            step.selection_reason_code,
        )
        for step in second_result.plan.steps
    ]
    assert first_real is not None and control_real is not None
    assert first_real.id == control_real.id
    assert (
        first_result.plan.plan.rule_configuration_digest
        == second_result.plan.plan.rule_configuration_digest
    )


def test_different_seeds_can_change_only_tie_decisions() -> None:
    tracks = tuple(_track(value) for value in range(1, 7))
    first, _, _ = _planner(tracks)
    second, _, _ = _planner(tracks)

    first_result = first.create_draft_plan(1, 1, planning_seed=1)
    second_result = second.create_draft_plan(1, 1, planning_seed=2)

    assert first_result.plan is not None and second_result.plan is not None
    assert first_result.plan.steps[0].track_id != second_result.plan.steps[0].track_id
    assert first_result.plan.steps[0].primary_play_count == 0
    assert second_result.plan.steps[0].primary_play_count == 0
    assert first_result.plan.steps[0].tie_break_method_code == "SEEDED_RANDOM"
    assert second_result.plan.steps[0].tie_break_method_code == "SEEDED_RANDOM"


def test_plan_and_preview_share_the_same_sequence_for_the_same_rng_state() -> None:
    tracks = tuple(_track(value) for value in range(1, 6))
    seed = 48
    planner, _, _ = _planner(tracks, productive_seed=seed)
    result = planner.create_draft_plan(1, 4, planning_seed=seed)
    preview = AutomaticSelectionService(
        _Tracks(tracks), _History(), recent_track_limit=0, randomizer=random.Random(seed)
    ).preview(TrackSelectionService(), 4)

    assert result.plan is not None
    assert [step.track_id for step in result.plan.steps] == [
        step.track_id for step in preview.steps
    ]


def test_planning_does_not_replace_the_productive_last_rationale() -> None:
    planner, _, selector = _planner((_track(1), _track(2)))
    assert selector.select(TrackSelectionService()) is not None
    original = selector.last_rationale

    result = planner.create_draft_plan(1, 2, planning_seed=9)

    assert result.plan is not None
    assert selector.last_rationale is original


def test_plan_steps_preserve_actual_predecessor_and_decision_facts() -> None:
    planner, _, _ = _planner((_track(1), _track(2), _track(3)))

    result = planner.create_draft_plan(7, 3, planning_seed=4)

    assert result.plan is not None
    steps = result.plan.steps
    assert [step.previous_track_id for step in steps] == [
        None,
        steps[0].track_id,
        steps[1].track_id,
    ]
    assert all(step.relaxation_stage_code for step in steps)
    assert all(step.tie_candidate_count >= 1 for step in steps)
    assert all(step.tie_break_method_code in {"SEEDED_RANDOM", "SOLE_FINALIST"} for step in steps)
    assert all(step.selection_reason_code for step in steps)


def test_single_finalist_uses_no_random_tie_break() -> None:
    planner, _, _ = _planner((_track(1),))

    result = planner.create_draft_plan(1, 1, planning_seed=7)

    assert result.plan is not None
    assert result.plan.steps[0].tie_break_method_code == "SOLE_FINALIST"


class _BlockAfterFirst:
    rule_id = "test.block_after_first"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes: frozenset[str] = frozenset()
    played = False

    def evaluate_rule(self, _rule_input: SelectionRuleInput, context: SelectionContext):
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="PLANNED_STOP" if self.played else "PLANNING_ALLOWED",
            reason="stop" if self.played else "allowed",
            excluded=self.played,
        )

    def record_preview_played(self, _track: Track) -> None:
        self.played = True


def test_partial_and_empty_results_have_explicit_terminal_semantics() -> None:
    writer = _Writer([])
    rule = _BlockAfterFirst()
    selector = AutomaticSelectionService(_Tracks((_track(1),)), _History(), recent_track_limit=0)
    partial = AutomaticSelectionPlanningService(
        selector, TrackSelectionService((rule,)), writer
    ).create_draft_plan(1, 3, planning_seed=2)
    empty, empty_writer, _ = _planner(())
    no_candidate = empty.create_draft_plan(1, 2, planning_seed=2)

    assert partial.completion_status is AutomaticSelectionPlanCompletion.PARTIAL
    assert partial.generated_depth == 1 and partial.plan is not None
    assert partial.terminal_reason_code == "NO_SAFE_CANDIDATE"
    assert not rule.played
    assert no_candidate.completion_status is AutomaticSelectionPlanCompletion.NO_SAFE_CANDIDATE
    assert no_candidate.plan is None and empty_writer.bundles == []


def test_technical_failure_returns_no_plan_and_persists_no_partial_result() -> None:
    writer = _Writer([], fail=True)
    planner, _, _ = _planner((_track(1),), writer=writer)

    result = planner.create_draft_plan(1, 1, planning_seed=8)

    assert result.completion_status is AutomaticSelectionPlanCompletion.FAILED
    assert result.plan is None
    assert result.terminal_reason_code == "PLANNING_TECHNICAL_ERROR"
    assert writer.bundles == []


@pytest.mark.parametrize("seed", [-1, 2**63, True, 1.5])
def test_invalid_seed_is_rejected(seed: object) -> None:
    planner, _, _ = _planner((_track(1),))

    with pytest.raises(ValueError, match="planning_seed"):
        planner.create_draft_plan(1, 1, seed)  # type: ignore[arg-type]


def test_generated_seed_is_bounded_and_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planning_module.secrets, "randbelow", lambda bound: bound - 1)
    planner, _, _ = _planner((_track(1),))

    result = planner.create_draft_plan(1, 1)

    assert result.plan is not None
    assert result.plan.plan.planning_seed == 2**63 - 1


def test_default_depth_is_five() -> None:
    planner, _, _ = _planner(tuple(_track(value) for value in range(1, 6)))

    result = planner.create_draft_plan(1, planning_seed=14)

    assert result.requested_depth == result.generated_depth == 5


def test_real_repository_plan_uses_four_reads_independent_of_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "planning.db")
    migrate(database)
    party = PartyPlayerRepository(database)
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id,file_path,title,artist) VALUES (?,?,?,?)",
            [(value, f"{value}.mp3", f"Track {value}", "Artist") for value in range(1, 7)],
        )
    session_id = party.create_session("Planning").session_id
    statements: list[str] = []
    original_open = database._open_connection

    def traced_connection() -> sqlite3.Connection:
        connection = original_open()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_open_connection", traced_connection)
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        rule_settings=SelectionRuleSettingsRepository(database),
    )
    planning = AutomaticSelectionPlanningService(
        selector,
        TrackSelectionService(),
        AutomaticSelectionPlanRepository(database, party),
    )

    result = planning.create_draft_plan(session_id, 5, planning_seed=29)

    reads = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert result.completion_status is AutomaticSelectionPlanCompletion.COMPLETE
    assert len(reads) == 4
    assert result.plan is not None
    assert result.plan.plan.source_context_code == "AUTOMATIC_PLANNING"
    assert all(step.executed_queue_entry_id is None for step in result.plan.steps)
    with database.connect() as connection:
        queued = connection.execute("SELECT COUNT(*) FROM party_queue").fetchone()[0]
    assert queued == 0


def test_missing_session_cannot_leave_a_persisted_plan(tmp_path: Path) -> None:
    database = Database(tmp_path / "missing-session.db")
    migrate(database)
    party = PartyPlayerRepository(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (id,file_path,title,artist) VALUES (1,'1.mp3','One','Artist')"
        )
    planning = AutomaticSelectionPlanningService(
        AutomaticSelectionService(
            TrackRepository(database), AutomaticSelectionHistory(database), recent_track_limit=0
        ),
        TrackSelectionService(),
        AutomaticSelectionPlanRepository(database, party),
    )

    result = planning.create_draft_plan(999, 1, planning_seed=5)
    finished_session = party.create_session("Finished")
    party.set_session_status(finished_session.session_id, SessionStatus.FINISHED)
    finished_result = planning.create_draft_plan(finished_session.session_id, 1, planning_seed=6)

    assert result.completion_status is AutomaticSelectionPlanCompletion.FAILED
    assert finished_result.completion_status is AutomaticSelectionPlanCompletion.FAILED
    with database.connect() as connection:
        stored = connection.execute("SELECT COUNT(*) FROM automatic_selection_plans").fetchone()[0]
    assert stored == 0


def test_configuration_digest_changes_with_settings_and_contains_no_track_data() -> None:
    first, _, _ = _planner((_track(1),))
    second, _, selector = _planner((_track(1),))
    selector.recent_track_limit = 12

    first_result = first.create_draft_plan(1, 1, planning_seed=3)
    second_result = second.create_draft_plan(1, 1, planning_seed=3)

    assert first_result.plan is not None and second_result.plan is not None
    assert (
        first_result.plan.plan.rule_configuration_digest
        != second_result.plan.plan.rule_configuration_digest
    )
    assert "ignored-1.mp3" not in repr(first_result.plan)


def test_configuration_digest_changes_with_activation_and_weight() -> None:
    baseline_settings = DEFAULT_SELECTION_SCORING_SETTINGS
    disabled_settings = replace(
        baseline_settings,
        rating=SoftRuleSetting("selection.rating", False, 1.0),
    )
    weighted_settings = replace(
        baseline_settings,
        rating=SoftRuleSetting("selection.rating", True, 0.5),
    )

    def create(settings):
        return AutomaticSelectionPlanningService(
            AutomaticSelectionService(
                _Tracks((_track(1),)),
                _History(),
                recent_track_limit=0,
                rule_settings=_Settings(settings),  # type: ignore[arg-type]
            ),
            TrackSelectionService(),
            _Writer([]),
        ).create_draft_plan(1, 1, planning_seed=2)

    baseline = create(baseline_settings)
    disabled = create(disabled_settings)
    weighted = create(weighted_settings)

    assert baseline.plan is not None and disabled.plan is not None and weighted.plan is not None
    digest = baseline.plan.plan.rule_configuration_digest
    assert digest != disabled.plan.plan.rule_configuration_digest
    assert digest != weighted.plan.plan.rule_configuration_digest


class _ConfiguredRule:
    rule_id = "test.configured"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes: frozenset[str] = frozenset()

    def __init__(self, value: object) -> None:
        self.value = value

    def evaluate_rule(self, _rule_input: SelectionRuleInput, context: SelectionContext):
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="CONFIGURED_ALLOWED",
            reason="allowed",
        )

    def selection_configuration(self) -> dict[str, object]:
        return {"threshold": self.value}


def test_hard_rule_configuration_changes_digest_and_rejects_paths() -> None:
    def create(value: object):
        selector = AutomaticSelectionService(
            _Tracks((_track(1),)), _History(), recent_track_limit=0
        )
        return AutomaticSelectionPlanningService(
            selector,
            TrackSelectionService((_ConfiguredRule(value),)),
            _Writer([]),
        ).create_draft_plan(1, 1, planning_seed=3)

    first = create(10)
    second = create(20)
    unsafe = create("C:/music/private/song.mp3")
    nonfinite = create(float("nan"))

    assert first.plan is not None and second.plan is not None
    assert first.plan.plan.rule_configuration_digest != second.plan.plan.rule_configuration_digest
    assert unsafe.completion_status is AutomaticSelectionPlanCompletion.FAILED
    assert unsafe.plan is None
    assert nonfinite.completion_status is AutomaticSelectionPlanCompletion.FAILED
    assert nonfinite.plan is None
