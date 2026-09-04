"""Behavior-neutral contracts for structured selection explanations."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import QueueSource, QueueStatus, ShortTrackPolicy
from party_player.models import QueueEntry, Track
from party_player.selection_decision import (
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    SelectionContext,
    SelectionOutcome,
    SelectionRuleInput,
    hard_rule_evaluation,
)
from party_player.track_selection import SelectionDecision, TrackSelectionService
from party_player.track_selection import RepetitionService
from party_player.short_track_policy import ShortTrackSelectionRule
from party_player.track_suitability import (
    TrackSuitabilityRepository,
    TrackSuitabilityService,
    TrackSuitabilityStatus,
)


def _entry_and_track() -> tuple[QueueEntry, Track]:
    return (
        QueueEntry(17, 7, 3, QueueStatus.WAITING),
        Track(7, "song.mp3", "Song", "Artist", "Album", 120.0),
    )


def test_accepted_candidate_has_structured_rule_results() -> None:
    class PassingRule:
        rule_id = "test.passing"
        rule_version = 3

        def evaluate(self, _entry: QueueEntry, _track: Track) -> None:
            return None

    entry, track = _entry_and_track()
    decision, rationale = TrackSelectionService((PassingRule(),)).evaluate_with_rationale(
        entry,
        track,
        context=SelectionContext("stable-context"),
    )

    assert decision == SelectionDecision.allow()
    assert rationale.context_id == "stable-context"
    assert rationale.outcome is SelectionOutcome.ACCEPTED
    assert rationale.selected_candidate is not None
    assert [evaluation.rule_id for evaluation in rationale.rule_evaluations] == [
        "core.track_exists",
        "core.required_metadata",
        "test.passing",
    ]
    assert rationale.rule_evaluations[-1].rule_version == 3
    assert rationale.rule_evaluations[-1].result_code is RuleOutcome.PASS


def test_rejection_exposes_stable_rule_and_reason_codes() -> None:
    class RejectingRule:
        rule_id = "test.blocked_track"
        rule_version = 2

        def evaluate(self, _entry: QueueEntry, _track: Track) -> SelectionDecision:
            return SelectionDecision.reject("BLOCKED_TRACK", reason="Operator block")

    entry, track = _entry_and_track()
    decision, rationale = TrackSelectionService((RejectingRule(),)).evaluate_with_rationale(
        entry, track
    )

    assert not decision.accepted
    assert rationale.outcome is SelectionOutcome.REJECTED
    assert rationale.exclusion_reasons == ("BLOCKED_TRACK",)
    rejection = rationale.rule_evaluations[-1]
    assert rejection.rule_id == "test.blocked_track"
    assert rejection.rule_version == 2
    assert rejection.reason == "Operator block"


def test_relaxed_rule_is_explained_and_evaluated_only_once() -> None:
    class RepetitionRule:
        rule_id = "test.repetition"
        rule_version = 1

        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, _entry: QueueEntry, _track: Track) -> SelectionDecision:
            self.calls += 1
            return SelectionDecision.reject("ARTIST_REPETITION", reason="too recent")

    entry, track = _entry_and_track()
    rule = RepetitionRule()
    context = SelectionContext(
        "relaxed-context",
        "ARTIST_DISTANCE",
        frozenset({"ARTIST_REPETITION"}),
    )
    decision, rationale = TrackSelectionService((rule,)).evaluate_with_rationale(
        entry,
        track,
        relaxed_codes=context.relaxed_codes,
        context=context,
    )

    assert decision.accepted
    assert rule.calls == 1
    evaluation = rationale.rule_evaluations[-1]
    assert evaluation.result_code is RuleOutcome.RELAXED
    assert evaluation.reason_code == "ARTIST_REPETITION"
    assert evaluation.relaxation_stage == "ARTIST_DISTANCE"


def test_operator_override_is_visible_without_second_rule_evaluation() -> None:
    class OverriddenRule:
        rule_id = "test.operator_override"
        rule_version = 1

        def __init__(self) -> None:
            self.calls = 0

        def operator_override_applies(self, _entry: QueueEntry) -> bool:
            return True

        def evaluate(self, _entry: QueueEntry, _track: Track) -> None:
            self.calls += 1
            return None

    entry, track = _entry_and_track()
    rule = OverriddenRule()

    decision, rationale = TrackSelectionService((rule,)).evaluate_with_rationale(entry, track)

    assert decision.accepted
    assert rule.calls == 1
    evaluation = rationale.rule_evaluations[-1]
    assert evaluation.result_code is RuleOutcome.OVERRIDDEN
    assert evaluation.reason_code == "OPERATOR_OVERRIDE"
    assert evaluation.operator_override


def test_decision_model_is_immutable() -> None:
    entry, track = _entry_and_track()
    _decision, rationale = TrackSelectionService().evaluate_with_rationale(entry, track)

    with pytest.raises(FrozenInstanceError):
        rationale.context_id = "changed"  # type: ignore[misc]


def test_diagnostic_contract_does_not_expose_paths_or_requester_data() -> None:
    entry = QueueEntry(17, 7, 3, QueueStatus.WAITING, requested_by="Private Guest")
    track = Track(7, r"C:\Private\Music\song.mp3", "Song", "Artist", "Album", 120.0)

    _decision, rationale = TrackSelectionService().evaluate_with_rationale(entry, track)

    candidate_fields = set(rationale.evaluated_candidates[0].candidate.__dataclass_fields__)
    assert "file_path" not in candidate_fields
    assert "requested_by" not in candidate_fields
    assert all(
        key == "track_id"
        for evaluation in rationale.rule_evaluations
        for key, _ in evaluation.facts
    )


def test_executable_rule_contract_runs_in_order_once_and_builds_same_rationale() -> None:
    calls: list[str] = []

    class ExecutableRule:
        rule_version = 1
        rule_kind = RuleKind.HARD_EXCLUSION
        relaxable_reason_codes: frozenset[str] = frozenset()

        def __init__(self, rule_id: str, *, excluded: bool = False) -> None:
            self.rule_id = rule_id
            self.excluded = excluded

        def evaluate_rule(
            self,
            _rule_input: SelectionRuleInput,
            context: SelectionContext,
        ) -> RuleEvaluation:
            calls.append(self.rule_id)
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="TEST_REJECTED" if self.excluded else "TEST_PASSED",
                reason="test",
                excluded=self.excluded,
            )

    entry, track = _entry_and_track()
    decision, rationale = TrackSelectionService(
        (
            ExecutableRule("test.first"),
            ExecutableRule("test.second", excluded=True),
            ExecutableRule("test.not_reached"),
        )
    ).evaluate_with_rationale(entry, track, context=SelectionContext("contract"))

    assert not decision.accepted
    assert calls == ["test.first", "test.second"]
    assert [evaluation.rule_id for evaluation in rationale.rule_evaluations] == [
        "core.track_exists",
        "core.required_metadata",
        "test.first",
        "test.second",
    ]
    assert rationale.rule_evaluations[-1].reason_code == decision.code
    assert rationale.rule_evaluations[-1].reason == decision.reason


def test_hard_rule_cannot_be_relaxed_without_explicit_declaration() -> None:
    evaluation = hard_rule_evaluation(
        rule_id="test.hard",
        rule_version=1,
        context=SelectionContext("hard", "TRACK_DISTANCE", frozenset({"BLOCKED_TRACK"})),
        reason_code="BLOCKED_TRACK",
        reason="blocked",
        excluded=True,
        relaxable_reason_codes=frozenset(),
    )

    assert evaluation.result_code is RuleOutcome.EXCLUDE
    assert not evaluation.relaxable


def test_missing_optional_metadata_does_not_change_existing_acceptance() -> None:
    entry = QueueEntry(1, 1, 1, QueueStatus.WAITING)
    track = Track(
        1,
        "song.mp3",
        "Song",
        "Artist",
        "",
        120.0,
        genre="",
        year=None,
        original_release_year=None,
        bpm=None,
    )

    decision, rationale = TrackSelectionService().evaluate_with_rationale(entry, track)

    assert decision.accepted
    assert rationale.outcome is SelectionOutcome.ACCEPTED
    assert rationale.rule_evaluations[-1].reason_code == "REQUIRED_METADATA_VALID"


def test_not_applicable_continues_without_overriding_later_exclusion() -> None:
    class NotApplicableRule:
        rule_id = "test.not_applicable"
        rule_version = 1
        rule_kind = RuleKind.HARD_EXCLUSION
        relaxable_reason_codes: frozenset[str] = frozenset()

        def evaluate_rule(
            self,
            _rule_input: SelectionRuleInput,
            context: SelectionContext,
        ) -> RuleEvaluation:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="NOT_RELEVANT",
                reason="not relevant",
                applicable=False,
            )

    class ExcludingRule(NotApplicableRule):
        rule_id = "test.excluding"

        def evaluate_rule(
            self,
            _rule_input: SelectionRuleInput,
            context: SelectionContext,
        ) -> RuleEvaluation:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="BLOCKED_TRACK",
                reason="blocked",
                excluded=True,
            )

    entry, track = _entry_and_track()
    decision, rationale = TrackSelectionService(
        (NotApplicableRule(), ExcludingRule())
    ).evaluate_with_rationale(entry, track)

    assert not decision.accepted
    assert rationale.rule_evaluations[-2].result_code is RuleOutcome.NOT_APPLICABLE
    assert rationale.rule_evaluations[-1].result_code is RuleOutcome.EXCLUDE
    assert decision.code == "BLOCKED_TRACK"


def test_rule_execution_does_not_mutate_input_objects_or_context() -> None:
    entry, track = _entry_and_track()
    original_entry = deepcopy(entry)
    context = SelectionContext("immutable-context", "STRICT")
    original_context = deepcopy(context)

    TrackSelectionService().evaluate_with_rationale(entry, track, context=context)

    assert entry == original_entry
    assert track == _entry_and_track()[1]
    assert context == original_context


def test_relaxable_reason_codes_are_shared_only_as_immutable_values() -> None:
    first = RepetitionService(track_window_size=1)
    second = RepetitionService(track_window_size=1)

    assert isinstance(first.relaxable_reason_codes, frozenset)
    assert first.relaxable_reason_codes == second.relaxable_reason_codes
    with pytest.raises(AttributeError):
        first.relaxable_reason_codes.add("BLOCKED_TRACK")  # type: ignore[attr-defined]
    assert "BLOCKED_TRACK" not in second.relaxable_reason_codes


def test_legacy_adapter_never_relaxes_undeclared_hard_code() -> None:
    class LegacyHardRule:
        def evaluate(
            self,
            _entry: QueueEntry,
            _track: Track,
        ) -> SelectionDecision:
            return SelectionDecision.reject("BLOCKED_TRACK", reason="blocked")

    entry, track = _entry_and_track()
    context = SelectionContext(
        "legacy-hard",
        "TRACK_DISTANCE",
        frozenset({"BLOCKED_TRACK", "ARTIST_REPETITION", "TRACK_REPETITION"}),
    )

    decision, rationale = TrackSelectionService((LegacyHardRule(),)).evaluate_with_rationale(
        entry,
        track,
        context=context,
    )

    assert not decision.accepted
    assert rationale.rule_evaluations[-1].result_code is RuleOutcome.EXCLUDE
    assert not rationale.rule_evaluations[-1].relaxable


def test_source_allowance_and_explicit_override_have_distinct_outcomes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "suitability-outcomes.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (id, file_path, title, artist) VALUES (1, 'song.mp3', 'Song', 'Artist')"
        )
    repository = TrackSuitabilityRepository(database)
    track = Track(1, "song.mp3", "Song", "Artist", "", 60)
    manual = QueueEntry(7, 1, 1, QueueStatus.WAITING, source=QueueSource.MANUAL)
    service = TrackSuitabilityService(repository)

    source_allowed = service.evaluate_rule(
        SelectionRuleInput.from_values(manual, track),
        SelectionContext("source-allowed"),
    )

    assert source_allowed.result_code is RuleOutcome.PASS
    assert not source_allowed.operator_override
    repository.set(1, TrackSuitabilityStatus.UNSUITABLE)
    service.allow_queue_entry(manual.queue_id)

    explicitly_overridden = service.evaluate_rule(
        SelectionRuleInput.from_values(manual, track),
        SelectionContext("explicit-override"),
    )

    assert explicitly_overridden.result_code is RuleOutcome.OVERRIDDEN
    assert explicitly_overridden.operator_override


def test_manual_short_track_source_is_allowed_without_operator_override(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "short-track-outcome.db")
    migrate(database)
    cues = CuePointService(
        CuePointRepository(database),
        global_fade_duration=8,
        minimum_fade_duration=0.5,
        minimum_playable_duration=5,
        short_track_threshold=30,
        short_track_policy=ShortTrackPolicy.MANUAL_ONLY,
    )
    track = Track(1, "song.mp3", "Song", "Artist", "", 180)
    manual = QueueEntry(
        7,
        1,
        1,
        QueueStatus.WAITING,
        source=QueueSource.MANUAL,
        cue_in_override=40,
        cue_out_override=60,
        cue_override_source="queue",
    )
    rule = ShortTrackSelectionRule(
        cues,
        threshold_seconds=30,
        policy=ShortTrackPolicy.MANUAL_ONLY,
    )

    evaluation = rule.evaluate_rule(
        SelectionRuleInput.from_values(manual, track),
        SelectionContext("manual-source"),
    )

    assert evaluation.result_code is RuleOutcome.PASS
    assert not evaluation.operator_override
