"""Tests for transparent, deterministic soft candidate scoring."""

from dataclasses import replace

from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry, Track
from party_player.selection_decision import (
    CandidateEvaluation,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    SelectionContext,
    SelectionRuleInput,
)
from party_player.selection_scoring import (
    CandidateScorer,
    PlayCountScoringRule,
    RatingScoringRule,
    soft_rule_evaluation,
)


def _values(*, rating: int | None = None) -> tuple[SelectionRuleInput, SelectionContext]:
    entry = QueueEntry(-7, 7, 0, QueueStatus.WAITING, source=QueueSource.AUTOMATIC)
    track = Track(7, "private/music.mp3", "Title", "Artist", "", 180.0, rating=rating)
    return SelectionRuleInput.from_values(entry, track), SelectionContext("context", "STRICT")


def _accepted(rule_input: SelectionRuleInput) -> CandidateEvaluation:
    return CandidateEvaluation(
        candidate=rule_input.candidate,
        accepted=True,
        code="",
        terminal_status=QueueStatus.SKIPPED,
        reason="",
        rules=(),
    )


def test_soft_result_supports_positive_negative_and_neutral_contributions() -> None:
    rule_input, context = _values()

    positive = soft_rule_evaluation(
        rule_id="test.soft",
        rule_version=1,
        context=context,
        reason_code="POS",
        reason="",
        score_delta=2,
    )
    negative = soft_rule_evaluation(
        rule_id="test.soft",
        rule_version=1,
        context=context,
        reason_code="NEG",
        reason="",
        score_delta=-2,
    )
    neutral = soft_rule_evaluation(
        rule_id="test.soft", rule_version=1, context=context, reason_code="ZERO", reason=""
    )

    assert rule_input.candidate.track_id == 7
    assert (positive.result_code, positive.score_delta) == (RuleOutcome.SCORE_DELTA, 2)
    assert (negative.result_code, negative.score_delta) == (RuleOutcome.SCORE_DELTA, -2)
    assert (neutral.result_code, neutral.score_delta) == (RuleOutcome.PASS, 0)


def test_not_applicable_and_unknown_metadata_are_always_neutral() -> None:
    _rule_input, context = _values()

    not_applicable = soft_rule_evaluation(
        rule_id="test.soft",
        rule_version=1,
        context=context,
        reason_code="NA",
        reason="",
        score_delta=99,
        applicable=False,
    )
    unknown = soft_rule_evaluation(
        rule_id="test.soft",
        rule_version=1,
        context=context,
        reason_code="UNKNOWN",
        reason="",
        score_delta=99,
        metadata_known=False,
    )

    assert (not_applicable.result_code, not_applicable.score_delta) == (
        RuleOutcome.NOT_APPLICABLE,
        0,
    )
    assert (unknown.result_code, unknown.score_delta) == (RuleOutcome.UNKNOWN_METADATA, 0)


def test_aggregator_ignores_score_values_from_non_scoring_outcomes() -> None:
    class MalformedNeutralRule:
        rule_id = "test.malformed-neutral"
        rule_version = 1
        rule_kind = RuleKind.SOFT_WEIGHT
        relaxable_reason_codes: frozenset[str] = frozenset()

        def evaluate_rule(self, _rule_input, context):
            return RuleEvaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                rule_kind=self.rule_kind,
                result_code=RuleOutcome.UNKNOWN_METADATA,
                reason_code="UNKNOWN",
                reason="",
                relaxation_stage=context.relaxation_stage,
                score_delta=99,
            )

    rule_input, context = _values()

    result = CandidateScorer((MalformedNeutralRule(),)).evaluate(
        rule_input, context, _accepted(rule_input)
    )

    assert result.total_score == 0


def test_play_count_defaults_missing_history_to_zero() -> None:
    rule_input, context = _values()

    result = PlayCountScoringRule({}).evaluate_rule(rule_input, context)

    assert result.result_code is RuleOutcome.PASS
    assert result.reason_code == "NO_PLAY_HISTORY"
    assert result.score_delta == 0
    assert result.facts == (("play_count", 0),)


def test_rating_is_bounded_and_missing_rating_is_explicitly_unknown() -> None:
    low_input, context = _values(rating=1)
    neutral_input, _ = _values(rating=3)
    high_input, _ = _values(rating=5)
    missing_input, _ = _values()
    rule = RatingScoringRule()

    assert rule.evaluate_rule(low_input, context).score_delta == -2
    assert rule.evaluate_rule(neutral_input, context).score_delta == 0
    assert rule.evaluate_rule(high_input, context).score_delta == 2
    missing = rule.evaluate_rule(missing_input, context)
    assert missing.result_code is RuleOutcome.UNKNOWN_METADATA
    assert missing.score_delta == 0


def test_scorer_aggregates_each_rule_once_without_mutating_inputs() -> None:
    class CountingRule:
        rule_id = "test.counting"
        rule_version = 1
        rule_kind = RuleKind.SOFT_WEIGHT
        relaxable_reason_codes: frozenset[str] = frozenset()

        def __init__(self, delta: float) -> None:
            self.delta = delta
            self.calls = 0

        def evaluate_rule(self, _rule_input, context):
            self.calls += 1
            return soft_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="COUNTED",
                reason="",
                score_delta=self.delta,
            )

    rule_input, context = _values(rating=5)
    before_entry = replace(rule_input.entry)
    before_track = rule_input.track
    first = CountingRule(2)
    second = CountingRule(-0.5)

    result = CandidateScorer((first, second)).evaluate(rule_input, context, _accepted(rule_input))

    assert result.total_score == 1.5
    assert [evaluation.score_delta for evaluation in result.rules] == [2, -0.5]
    assert (first.calls, second.calls) == (1, 1)
    assert rule_input.entry == before_entry
    assert rule_input.track == before_track


def test_hard_exclusion_is_never_scored() -> None:
    class FailingRule:
        rule_id = "test.must-not-run"
        rule_version = 1
        rule_kind = RuleKind.SOFT_WEIGHT
        relaxable_reason_codes: frozenset[str] = frozenset()

        def evaluate_rule(self, _rule_input, _context) -> RuleEvaluation:
            raise AssertionError("soft rule evaluated for excluded candidate")

    rule_input, context = _values()
    rejected = replace(_accepted(rule_input), accepted=False, code="BLOCKED_TRACK")

    assert CandidateScorer((FailingRule(),)).evaluate(rule_input, context, rejected) is rejected


def test_diagnostic_facts_contain_no_path_or_requester_data() -> None:
    rule_input, context = _values(rating=5)

    results = (
        PlayCountScoringRule({7: 2}).evaluate_rule(rule_input, context),
        RatingScoringRule().evaluate_rule(rule_input, context),
    )

    serialized = repr(results)
    assert "private/music.mp3" not in serialized
    assert "requested_by" not in serialized
    assert {name for result in results for name, _value in result.facts} == {
        "play_count",
        "rating",
    }
