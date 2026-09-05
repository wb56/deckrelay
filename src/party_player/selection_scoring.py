"""Deterministic and explainable soft scoring for eligible candidates."""

from dataclasses import replace
import math
from party_player.selection_decision import (
    CandidateEvaluation,
    ExecutableSelectionRule,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    SelectionContext,
    SelectionRuleInput,
)


def soft_rule_evaluation(
    *,
    rule_id: str,
    rule_version: int,
    context: SelectionContext,
    reason_code: str,
    reason: str,
    score_delta: float = 0.0,
    applicable: bool = True,
    metadata_known: bool = True,
    facts: tuple[tuple[str, str | int | float | bool | None], ...] = (),
) -> RuleEvaluation:
    """Build one soft result; neutral states can never contribute a score."""
    if not math.isfinite(score_delta):
        raise ValueError("Score-Beiträge müssen endlich sein")
    if not applicable:
        outcome = RuleOutcome.NOT_APPLICABLE
        score_delta = 0.0
    elif not metadata_known:
        outcome = RuleOutcome.UNKNOWN_METADATA
        score_delta = 0.0
    elif score_delta:
        outcome = RuleOutcome.SCORE_DELTA
    else:
        outcome = RuleOutcome.PASS
    return RuleEvaluation(
        rule_id=rule_id,
        rule_version=rule_version,
        rule_kind=RuleKind.SOFT_WEIGHT,
        result_code=outcome,
        reason_code=reason_code,
        reason=reason,
        relaxation_stage=context.relaxation_stage,
        facts=facts,
        score_delta=score_delta,
    )


class PlayCountScoringRule:
    """Preserve the historic strict preference for the least-played tracks."""

    rule_id = "selection.play_count"
    rule_version = 1
    rule_kind = RuleKind.SOFT_WEIGHT
    relaxable_reason_codes: frozenset[str] = frozenset()
    points_per_play = -10.0

    def __init__(self, play_counts: dict[int, int]) -> None:
        self._play_counts = dict(play_counts)

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        play_count = max(0, self._play_counts.get(rule_input.candidate.track_id, 0))
        return soft_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="PLAY_COUNT_WEIGHTED" if play_count else "NO_PLAY_HISTORY",
            reason=(
                "Bisherige Wiedergaben verringern die Auswahlbewertung"
                if play_count
                else "Keine bisherige Wiedergabe; der Titel erhält keinen Abzug"
            ),
            score_delta=play_count * self.points_per_play,
            facts=(("play_count", play_count),),
        )


class RatingScoringRule:
    """Apply a bounded rating preference without penalising missing metadata."""

    rule_id = "selection.rating"
    rule_version = 1
    rule_kind = RuleKind.SOFT_WEIGHT
    relaxable_reason_codes: frozenset[str] = frozenset()

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        rating = track.rating if track is not None else None
        if rating is None or not 1 <= rating <= 5:
            return soft_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="RATING_UNKNOWN" if rating is None else "RATING_INVALID",
                reason=(
                    "Titelbewertung fehlt und bleibt neutral"
                    if rating is None
                    else "Titelbewertung liegt außerhalb des gültigen Bereichs und bleibt neutral"
                ),
                metadata_known=False,
                facts=(("rating", None),),
            )
        score_delta = float(rating - 3)
        return soft_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="RATING_WEIGHTED" if score_delta else "RATING_NEUTRAL",
            reason="Titelbewertung liefert einen begrenzten Auswahlbeitrag",
            score_delta=score_delta,
            facts=(("rating", rating),),
        )


class CandidateScorer:
    """Evaluate each soft rule exactly once and aggregate its contributions."""

    def __init__(self, rules: tuple[ExecutableSelectionRule, ...]) -> None:
        if any(rule.rule_kind is not RuleKind.SOFT_WEIGHT for rule in rules):
            raise ValueError("CandidateScorer akzeptiert ausschließlich weiche Regeln")
        self._rules = rules

    def evaluate(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
        hard_evaluation: CandidateEvaluation,
    ) -> CandidateEvaluation:
        if not hard_evaluation.accepted:
            return hard_evaluation
        evaluations = tuple(rule.evaluate_rule(rule_input, context) for rule in self._rules)
        total_score = sum(
            evaluation.score_delta
            for evaluation in evaluations
            if evaluation.result_code is RuleOutcome.SCORE_DELTA
        )
        return replace(
            hard_evaluation,
            rules=(*hard_evaluation.rules, *evaluations),
            total_score=total_score,
        )
