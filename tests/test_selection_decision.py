"""Behavior-neutral contracts for structured selection explanations."""

from dataclasses import FrozenInstanceError

import pytest

from party_player.enums import QueueStatus
from party_player.models import QueueEntry, Track
from party_player.selection_decision import (
    RuleOutcome,
    SelectionContext,
    SelectionOutcome,
)
from party_player.track_selection import SelectionDecision, TrackSelectionService


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
