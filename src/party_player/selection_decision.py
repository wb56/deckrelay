"""Immutable explanation model for track-selection decisions.

The model deliberately contains no scoring concepts yet.  It describes the
existing ordered hard-rule pipeline without changing its outcome.
"""

from dataclasses import dataclass
from enum import StrEnum

from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry, Track


class RuleKind(StrEnum):
    HARD_EXCLUSION = "HARD_EXCLUSION"


class RuleOutcome(StrEnum):
    PASS = "PASS"
    EXCLUDE = "EXCLUDE"
    RELAXED = "RELAXED"
    OVERRIDDEN = "OVERRIDDEN"


class SelectionOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NO_SAFE_CANDIDATE = "NO_SAFE_CANDIDATE"


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
class RuleEvaluation:
    rule_id: str
    rule_version: int
    rule_kind: RuleKind
    result_code: RuleOutcome
    reason_code: str
    reason: str
    relaxation_stage: str
    operator_override: bool = False
    facts: tuple[tuple[str, str | int | float | bool | None], ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: SelectionCandidate
    accepted: bool
    code: str
    terminal_status: QueueStatus
    reason: str
    rules: tuple[RuleEvaluation, ...]


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
