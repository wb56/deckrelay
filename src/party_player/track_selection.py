"""Composable, GUI-independent rules for automatic queue candidates."""

from collections import deque
from dataclasses import dataclass
import math
from typing import Protocol
import re
import uuid

from party_player.enums import QueueStatus
from party_player.models import QueueEntry, Track
from party_player.selection_decision import (
    CandidateEvaluation,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    SelectionCandidate,
    SelectionContext,
    SelectionOutcome,
    SelectionRationale,
)


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    """Stable result of evaluating one ordered queue candidate."""

    accepted: bool
    code: str = ""
    terminal_status: QueueStatus = QueueStatus.SKIPPED
    reason: str = ""

    @classmethod
    def allow(cls) -> "SelectionDecision":
        return cls(True)

    @classmethod
    def reject(
        cls,
        code: str,
        *,
        terminal_status: QueueStatus = QueueStatus.SKIPPED,
        reason: str = "",
    ) -> "SelectionDecision":
        if terminal_status not in {QueueStatus.SKIPPED, QueueStatus.FAILED}:
            raise ValueError("Auswahlablehnung benötigt SKIPPED oder FAILED")
        return cls(False, code, terminal_status, reason)


class SelectionRule(Protocol):
    """One deterministic rule without GUI or deck dependencies."""

    def evaluate(
        self,
        entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None: ...


class ExplainedSelectionRule(Protocol):
    rule_id: str
    rule_version: int
    rule_kind: RuleKind

    def operator_override_applies(self, entry: QueueEntry) -> bool: ...


class TrackSelectionService:
    """Evaluate availability first and then injected event/business rules."""

    _NON_RELAXABLE_CODES = frozenset(
        {
            "BLOCKED_TRACK",
            "BLOCKED_ARTIST",
            "RESTRICTED_TRACK",
            "UNSUITABLE_TRACK",
        }
    )

    def __init__(self, rules: tuple[SelectionRule, ...] = ()) -> None:
        self._rules = rules

    def evaluate(
        self,
        entry: QueueEntry,
        track: Track | None,
        *,
        relaxed_codes: frozenset[str] = frozenset(),
    ) -> SelectionDecision:
        decision, _rationale = self.evaluate_with_rationale(
            entry,
            track,
            relaxed_codes=relaxed_codes,
        )
        return decision

    def evaluate_with_rationale(
        self,
        entry: QueueEntry,
        track: Track | None,
        *,
        relaxed_codes: frozenset[str] = frozenset(),
        context: SelectionContext | None = None,
    ) -> tuple[SelectionDecision, SelectionRationale]:
        context = context or SelectionContext(
            context_id=uuid.uuid4().hex,
            relaxed_codes=relaxed_codes,
        )
        candidate = SelectionCandidate.from_entry(entry, track)
        evaluations: list[RuleEvaluation] = []
        if track is None:
            decision = SelectionDecision.reject(
                "TRACK_MISSING",
                terminal_status=QueueStatus.FAILED,
                reason="Katalogeintrag nicht gefunden",
            )
            evaluations.append(
                self._evaluation(
                    "core.track_exists",
                    decision,
                    context,
                    facts={"track_id": entry.track_id},
                )
            )
            return decision, self._single_rationale(candidate, decision, evaluations, context)
        evaluations.append(self._pass("core.track_exists", context))
        if (
            not track.file_path.strip()
            or not track.title.strip()
            or (
                track.duration_seconds is not None
                and (not math.isfinite(track.duration_seconds) or track.duration_seconds <= 0)
            )
        ):
            decision = SelectionDecision.reject(
                "INVALID_METADATA",
                terminal_status=QueueStatus.FAILED,
                reason="Der Katalogeintrag enthält ungültige Metadaten",
            )
            evaluations.append(self._evaluation("core.required_metadata", decision, context))
            return decision, self._single_rationale(candidate, decision, evaluations, context)
        evaluations.append(self._pass("core.required_metadata", context))
        for rule in self._rules:
            rule_id = str(getattr(rule, "rule_id", self._fallback_rule_id(rule)))
            rule_version = int(getattr(rule, "rule_version", 1))
            rule_kind = getattr(rule, "rule_kind", RuleKind.HARD_EXCLUSION)
            override_check = getattr(rule, "operator_override_applies", None)
            operator_override = bool(override_check(entry)) if callable(override_check) else False
            rule_decision = rule.evaluate(entry, track)
            if rule_decision is not None and not rule_decision.accepted:
                if (
                    rule_decision.code in context.relaxed_codes
                    and rule_decision.code not in self._NON_RELAXABLE_CODES
                ):
                    evaluations.append(
                        self._evaluation(
                            rule_id,
                            rule_decision,
                            context,
                            rule_version=rule_version,
                            rule_kind=rule_kind,
                            result=RuleOutcome.RELAXED,
                        )
                    )
                    continue
                evaluations.append(
                    self._evaluation(
                        rule_id,
                        rule_decision,
                        context,
                        rule_version=rule_version,
                        rule_kind=rule_kind,
                        operator_override=operator_override,
                    )
                )
                return rule_decision, self._single_rationale(
                    candidate, rule_decision, evaluations, context
                )
            evaluations.append(
                RuleEvaluation(
                    rule_id=rule_id,
                    rule_version=rule_version,
                    rule_kind=rule_kind,
                    result_code=(RuleOutcome.OVERRIDDEN if operator_override else RuleOutcome.PASS),
                    reason_code=("OPERATOR_OVERRIDE" if operator_override else "RULE_PASSED"),
                    reason=(
                        "Regel wurde durch eine Operatorfreigabe übersteuert"
                        if operator_override
                        else "Regel erfüllt"
                    ),
                    relaxation_stage=context.relaxation_stage,
                    operator_override=operator_override,
                )
            )
        allowed = SelectionDecision.allow()
        return allowed, self._single_rationale(candidate, allowed, evaluations, context)

    @staticmethod
    def _fallback_rule_id(rule: SelectionRule) -> str:
        rule_type = type(rule)
        return f"{rule_type.__module__}.{rule_type.__qualname__}"

    @staticmethod
    def _pass(rule_id: str, context: SelectionContext) -> RuleEvaluation:
        return RuleEvaluation(
            rule_id=rule_id,
            rule_version=1,
            rule_kind=RuleKind.HARD_EXCLUSION,
            result_code=RuleOutcome.PASS,
            reason_code="RULE_PASSED",
            reason="Regel erfüllt",
            relaxation_stage=context.relaxation_stage,
        )

    @staticmethod
    def _evaluation(
        rule_id: str,
        decision: SelectionDecision,
        context: SelectionContext,
        *,
        rule_version: int = 1,
        rule_kind: RuleKind = RuleKind.HARD_EXCLUSION,
        result: RuleOutcome = RuleOutcome.EXCLUDE,
        operator_override: bool = False,
        facts: dict[str, str | int | float | bool | None] | None = None,
    ) -> RuleEvaluation:
        return RuleEvaluation(
            rule_id=rule_id,
            rule_version=rule_version,
            rule_kind=rule_kind,
            result_code=result,
            reason_code=decision.code,
            reason=decision.reason,
            relaxation_stage=context.relaxation_stage,
            operator_override=operator_override,
            facts=tuple(sorted((facts or {}).items())),
        )

    @staticmethod
    def _single_rationale(
        candidate: SelectionCandidate,
        decision: SelectionDecision,
        evaluations: list[RuleEvaluation],
        context: SelectionContext,
    ) -> SelectionRationale:
        candidate_evaluation = CandidateEvaluation(
            candidate=candidate,
            accepted=decision.accepted,
            code=decision.code,
            terminal_status=decision.terminal_status,
            reason=decision.reason,
            rules=tuple(evaluations),
        )
        return SelectionRationale(
            context_id=context.context_id,
            outcome=(SelectionOutcome.ACCEPTED if decision.accepted else SelectionOutcome.REJECTED),
            selected_candidate=candidate if decision.accepted else None,
            evaluated_candidates=(candidate_evaluation,),
            relaxation_stage=context.relaxation_stage,
            tie_break_method="QUEUE_ORDER",
            evaluated_candidate_count=1,
        )


def normalize_artist_name(name: str) -> str:
    """Provide one stable baseline identity for artist selection rules."""
    normalized = name.casefold().strip()
    normalized = re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+", "|", normalized)
    normalized = re.sub(r"\s*(?:&|/|;)\s*", "|", normalized)
    return "|".join(" ".join(part.split()) for part in normalized.split("|") if part.strip())


class BlockService:
    """Reject explicitly blocked track and normalized artist identities."""

    rule_id = "selection.block"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION

    def __init__(
        self,
        blocked_track_ids: set[int] | None = None,
        blocked_artists: set[str] | None = None,
    ) -> None:
        self._blocked_track_ids = set(blocked_track_ids or ())
        self._blocked_artists = {normalize_artist_name(artist) for artist in blocked_artists or ()}

    def block_track(self, track_id: int) -> None:
        self._blocked_track_ids.add(track_id)

    def allow_track(self, track_id: int) -> None:
        self._blocked_track_ids.discard(track_id)

    def block_artist(self, artist: str) -> None:
        self._blocked_artists.add(normalize_artist_name(artist))

    def allow_artist(self, artist: str) -> None:
        self._blocked_artists.discard(normalize_artist_name(artist))

    def evaluate(
        self,
        _entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None:
        if track.id in self._blocked_track_ids:
            return SelectionDecision.reject(
                "BLOCKED_TRACK",
                reason="Titel ist für die automatische Auswahl gesperrt",
            )
        if normalize_artist_name(track.artist) in self._blocked_artists:
            return SelectionDecision.reject(
                "BLOCKED_ARTIST",
                reason="Interpret ist für die automatische Auswahl gesperrt",
            )
        return None


class RepetitionService:
    """Reject tracks or artists present in bounded recent-play windows."""

    rule_id = "selection.repetition"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION

    def __init__(
        self,
        *,
        track_window_size: int = 0,
        artist_window_size: int = 0,
    ) -> None:
        self.track_window_size = max(0, track_window_size)
        self.artist_window_size = max(0, artist_window_size)
        maximum = max(1, self.track_window_size, self.artist_window_size)
        self._recent_track_ids: deque[int] = deque(maxlen=maximum)
        self._recent_artists: deque[str] = deque(maxlen=maximum)

    def record_played(self, track: Track) -> None:
        self._recent_track_ids.append(track.id)
        self._recent_artists.append(normalize_artist_name(track.artist))

    def evaluate(
        self,
        _entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None:
        if (
            self.track_window_size
            and track.id in tuple(self._recent_track_ids)[-self.track_window_size :]
        ):
            return SelectionDecision.reject(
                "TRACK_REPETITION",
                reason="Titel wurde innerhalb des Wiederholungsfensters bereits gespielt",
            )
        artist = normalize_artist_name(track.artist)
        if (
            self.artist_window_size
            and artist in tuple(self._recent_artists)[-self.artist_window_size :]
        ):
            return SelectionDecision.reject(
                "ARTIST_REPETITION",
                reason="Interpret wurde innerhalb des Wiederholungsfensters bereits gespielt",
            )
        return None
