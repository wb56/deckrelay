"""Immutable explanation model for track-selection decisions.

The model describes both ordered hard-rule decisions and transparent soft
score contributions without coupling selection to the GUI.
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
import math
from typing import Protocol

from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry, Track
from party_player.selection_source import SourceResolution


class RuleKind(StrEnum):
    HARD_EXCLUSION = "HARD_EXCLUSION"
    SOFT_WEIGHT = "SOFT_WEIGHT"


class RuleOutcome(StrEnum):
    PASS = "PASS"
    EXCLUDE = "EXCLUDE"
    RELAXED = "RELAXED"
    OVERRIDDEN = "OVERRIDDEN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    SCORE_DELTA = "SCORE_DELTA"
    UNKNOWN_METADATA = "UNKNOWN_METADATA"


class SelectionOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NO_SAFE_CANDIDATE = "NO_SAFE_CANDIDATE"


class CandidateDecisionCategory(StrEnum):
    SELECTED = "SELECTED"
    EXCLUDED = "EXCLUDED"
    ELIGIBLE_NOT_SELECTED = "ELIGIBLE_NOT_SELECTED"


class CandidateDecisionReason(StrEnum):
    SELECTED_QUEUE_PRIORITY = "SELECTED_QUEUE_PRIORITY"
    SELECTED_HIGHEST_SCORE = "SELECTED_HIGHEST_SCORE"
    SELECTED_STABLE_TIE_BREAK = "SELECTED_STABLE_TIE_BREAK"
    SELECTED_RNG_TIE_BREAK = "SELECTED_RNG_TIE_BREAK"
    SELECTED_EMERGENCY_ORDER = "SELECTED_EMERGENCY_ORDER"
    EXCLUDED_HARD_RULE = "EXCLUDED_HARD_RULE"
    LOWER_TOTAL_SCORE = "LOWER_TOTAL_SCORE"
    STABLE_TIE_BREAK_LOSS = "STABLE_TIE_BREAK_LOSS"
    RNG_TIE_BREAK_LOSS = "RNG_TIE_BREAK_LOSS"
    ELIGIBLE_PENDING_SELECTION = "ELIGIBLE_PENDING_SELECTION"


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    queue_id: int
    track_id: int
    source: QueueSource
    priority: int
    position: int
    title: str
    artist: str

    @classmethod
    def from_entry(cls, entry: QueueEntry, track: Track | None) -> "SelectionCandidate":
        return cls(
            queue_id=entry.queue_id,
            track_id=entry.track_id,
            source=entry.source,
            priority=entry.priority,
            position=entry.position,
            title=track.title if track is not None else "",
            artist=track.artist if track is not None else "",
        )


@dataclass(frozen=True, slots=True)
class SelectionContext:
    context_id: str
    relaxation_stage: str = "NONE"
    relaxed_codes: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SelectionRuleInput:
    """Execution-only input; private source objects never enter diagnostics."""

    candidate: SelectionCandidate
    entry: QueueEntry = field(repr=False, compare=False)
    track: Track | None = field(repr=False, compare=False)

    @classmethod
    def from_values(
        cls,
        entry: QueueEntry,
        track: Track | None,
    ) -> "SelectionRuleInput":
        return cls(SelectionCandidate.from_entry(entry, track), entry, track)


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    rule_id: str
    rule_version: int
    rule_kind: RuleKind
    result_code: RuleOutcome
    reason_code: str
    reason: str
    relaxation_stage: str
    relaxable: bool = False
    operator_override: bool = False
    facts: tuple[tuple[str, str | int | float | bool | None], ...] = ()
    terminal_status: QueueStatus = QueueStatus.SKIPPED
    score_delta: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.score_delta):
            raise ValueError("Score-Beiträge müssen endlich sein")


class ExecutableSelectionRule(Protocol):
    """Small common contract for deterministic hard and soft selection rules."""

    rule_id: str
    rule_version: int
    rule_kind: RuleKind
    relaxable_reason_codes: frozenset[str]

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation: ...


def hard_rule_evaluation(
    *,
    rule_id: str,
    rule_version: int,
    context: SelectionContext,
    reason_code: str,
    reason: str,
    excluded: bool = False,
    operator_override: bool = False,
    applicable: bool = True,
    relaxable_reason_codes: frozenset[str] = frozenset(),
    facts: tuple[tuple[str, str | int | float | bool | None], ...] = (),
    terminal_status: QueueStatus = QueueStatus.SKIPPED,
) -> RuleEvaluation:
    """Build one hard-rule result and apply only explicitly allowed relaxation."""
    relaxable = reason_code in relaxable_reason_codes
    if not applicable:
        outcome = RuleOutcome.NOT_APPLICABLE
    elif operator_override:
        outcome = RuleOutcome.OVERRIDDEN
    elif excluded and relaxable and reason_code in context.relaxed_codes:
        outcome = RuleOutcome.RELAXED
    elif excluded:
        outcome = RuleOutcome.EXCLUDE
    else:
        outcome = RuleOutcome.PASS
    return RuleEvaluation(
        rule_id=rule_id,
        rule_version=rule_version,
        rule_kind=RuleKind.HARD_EXCLUSION,
        result_code=outcome,
        reason_code=reason_code,
        reason=reason,
        relaxation_stage=context.relaxation_stage,
        relaxable=relaxable,
        operator_override=operator_override,
        facts=facts,
        terminal_status=terminal_status,
    )


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: SelectionCandidate
    accepted: bool
    code: str
    terminal_status: QueueStatus
    reason: str
    rules: tuple[RuleEvaluation, ...]
    total_score: float = 0.0
    decision_category: CandidateDecisionCategory | None = None
    decision_reason_code: str = ""
    tie_break_method: str = "NONE"

    def __post_init__(self) -> None:
        if not math.isfinite(self.total_score):
            raise ValueError("Gesamtscores müssen endlich sein")
        if self.decision_category is None:
            category = (
                CandidateDecisionCategory.ELIGIBLE_NOT_SELECTED
                if self.accepted
                else CandidateDecisionCategory.EXCLUDED
            )
            object.__setattr__(self, "decision_category", category)
        if not self.decision_reason_code:
            reason = (
                CandidateDecisionReason.ELIGIBLE_PENDING_SELECTION
                if self.accepted
                else self.code or CandidateDecisionReason.EXCLUDED_HARD_RULE
            )
            object.__setattr__(
                self,
                "decision_reason_code",
                reason.value if isinstance(reason, CandidateDecisionReason) else reason,
            )


@dataclass(frozen=True, slots=True)
class ExclusionReasonSummary:
    reason_code: str
    count: int


@dataclass(frozen=True, slots=True)
class SelectionRationale:
    context_id: str
    outcome: SelectionOutcome
    selected_candidate: SelectionCandidate | None
    evaluated_candidates: tuple[CandidateEvaluation, ...]
    relaxation_stage: str
    tie_break_method: str
    warnings: tuple[str, ...] = ()
    evaluated_candidate_count: int = 0
    omitted_candidate_count: int = 0
    decision_reason_code: str = ""
    source_resolution: SourceResolution | None = None
    schema_version: int = 2
    excluded_candidate_count: int = 0
    exclusion_summary: tuple[ExclusionReasonSummary, ...] = ()

    @property
    def rule_evaluations(self) -> tuple[RuleEvaluation, ...]:
        return tuple(
            evaluation for candidate in self.evaluated_candidates for evaluation in candidate.rules
        )

    @property
    def exclusion_reasons(self) -> tuple[str, ...]:
        return tuple(
            evaluation.reason_code
            for evaluation in self.rule_evaluations
            if evaluation.result_code is RuleOutcome.EXCLUDE
        )


def attach_source_resolution(
    rationale: SelectionRationale,
    resolution: SourceResolution,
) -> SelectionRationale:
    """Attach an already-computed source decision without replaying selection."""
    if rationale.context_id != resolution.context.context_id:
        raise ValueError("Quellen- und Auswahlbegründung benötigen dieselbe Kontext-ID")
    return replace(rationale, source_resolution=resolution)


def finalize_candidate_decision(
    rationale: SelectionRationale,
    *,
    accepted: bool,
    code: str,
    reason: str,
    terminal_status: QueueStatus,
) -> SelectionRationale:
    """Apply an already-computed technical result without replaying any rule."""
    if accepted or not rationale.evaluated_candidates:
        return rationale
    target_index = next(
        (
            index
            for index, candidate in enumerate(rationale.evaluated_candidates)
            if rationale.selected_candidate is not None
            and candidate.candidate == rationale.selected_candidate
            and candidate.decision_category is CandidateDecisionCategory.SELECTED
        ),
        0,
    )
    target = rationale.evaluated_candidates[target_index]
    excluded = replace(
        target,
        accepted=False,
        code=code,
        reason=reason,
        terminal_status=terminal_status,
        decision_category=CandidateDecisionCategory.EXCLUDED,
        decision_reason_code=code or CandidateDecisionReason.EXCLUDED_HARD_RULE.value,
        tie_break_method="NONE",
    )
    candidates = list(rationale.evaluated_candidates)
    candidates[target_index] = excluded
    return replace(
        rationale,
        outcome=SelectionOutcome.REJECTED,
        selected_candidate=None,
        evaluated_candidates=tuple(candidates),
        tie_break_method="NONE",
        decision_reason_code=excluded.decision_reason_code,
    )


def source_selection_rationale(
    entry: QueueEntry | None,
    resolution: SourceResolution,
) -> SelectionRationale:
    """Build a source-only rationale without loading a track or evaluating rules."""
    if entry is None:
        return SelectionRationale(
            context_id=resolution.context.context_id,
            outcome=SelectionOutcome.NO_SAFE_CANDIDATE,
            selected_candidate=None,
            evaluated_candidates=(),
            relaxation_stage="NONE",
            tie_break_method="NONE",
            decision_reason_code=resolution.reason.value,
            source_resolution=resolution,
        )
    candidate = SelectionCandidate.from_entry(entry, None)
    evaluation = CandidateEvaluation(
        candidate=candidate,
        accepted=True,
        code=resolution.reason.value,
        terminal_status=entry.status,
        reason="",
        rules=(),
        decision_category=CandidateDecisionCategory.SELECTED,
        decision_reason_code=CandidateDecisionReason.SELECTED_QUEUE_PRIORITY.value,
        tie_break_method="QUEUE_PRIORITY_ORDER",
    )
    return SelectionRationale(
        context_id=resolution.context.context_id,
        outcome=SelectionOutcome.ACCEPTED,
        selected_candidate=candidate,
        evaluated_candidates=(evaluation,),
        relaxation_stage="NONE",
        tie_break_method="QUEUE_PRIORITY_ORDER",
        evaluated_candidate_count=1,
        decision_reason_code=CandidateDecisionReason.SELECTED_QUEUE_PRIORITY.value,
        source_resolution=resolution,
    )
