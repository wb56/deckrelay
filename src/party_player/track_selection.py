"""Composable, GUI-independent rules for automatic queue candidates."""

from collections import deque
from dataclasses import dataclass
import math
from typing import Protocol, cast
import re
import uuid

from party_player.enums import QueueStatus
from party_player.models import QueueEntry, Track
from party_player.selection_decision import (
    CandidateEvaluation,
    ExecutableSelectionRule,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    SelectionCandidate,
    SelectionContext,
    SelectionOutcome,
    SelectionRationale,
    SelectionRuleInput,
    hard_rule_evaluation,
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


class TrackExistsRule:
    rule_id = "core.track_exists"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes: frozenset[str] = frozenset()

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        missing = rule_input.track is None
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="TRACK_MISSING" if missing else "TRACK_PRESENT",
            reason="Katalogeintrag nicht gefunden" if missing else "Katalogeintrag vorhanden",
            excluded=missing,
            facts=(("track_id", rule_input.candidate.track_id),),
            terminal_status=QueueStatus.FAILED if missing else QueueStatus.SKIPPED,
        )


class RequiredMetadataRule:
    rule_id = "core.required_metadata"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes: frozenset[str] = frozenset()

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        if track is None:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="TRACK_MISSING",
                reason="Stammdatenprüfung ist ohne Katalogeintrag nicht anwendbar",
                applicable=False,
            )
        invalid = (
            not track.file_path.strip()
            or not track.title.strip()
            or (
                track.duration_seconds is not None
                and (not math.isfinite(track.duration_seconds) or track.duration_seconds <= 0)
            )
        )
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="INVALID_METADATA" if invalid else "REQUIRED_METADATA_VALID",
            reason=(
                "Der Katalogeintrag enthält ungültige Metadaten"
                if invalid
                else "Erforderliche Stammdaten sind gültig"
            ),
            excluded=invalid,
            terminal_status=QueueStatus.FAILED if invalid else QueueStatus.SKIPPED,
        )


class LegacySelectionRuleAdapter:
    """Compatibility path for injected rules that still return SelectionDecision."""

    rule_kind = RuleKind.HARD_EXCLUSION
    _LEGACY_RELAXABLE_CODES = frozenset({"ARTIST_REPETITION", "TRACK_REPETITION"})

    def __init__(self, rule: SelectionRule) -> None:
        self._rule = rule
        rule_type = type(rule)
        self.rule_id = str(
            getattr(rule, "rule_id", f"{rule_type.__module__}.{rule_type.__qualname__}")
        )
        self.rule_version = int(getattr(rule, "rule_version", 1))
        self.relaxable_reason_codes = self._LEGACY_RELAXABLE_CODES

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        if track is None:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="TRACK_MISSING",
                reason="Legacy-Regel ist ohne Katalogeintrag nicht anwendbar",
                applicable=False,
            )
        override_check = getattr(self._rule, "operator_override_applies", None)
        operator_override = (
            bool(override_check(rule_input.entry)) if callable(override_check) else False
        )
        decision = self._rule.evaluate(rule_input.entry, track)
        if decision is None or decision.accepted:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="OPERATOR_OVERRIDE" if operator_override else "RULE_PASSED",
                reason=(
                    "Regel wurde durch eine Operatorfreigabe übersteuert"
                    if operator_override
                    else "Regel erfüllt"
                ),
                operator_override=operator_override,
                relaxable_reason_codes=self.relaxable_reason_codes,
            )
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code=decision.code,
            reason=decision.reason,
            excluded=True,
            relaxable_reason_codes=self.relaxable_reason_codes,
            terminal_status=decision.terminal_status,
        )


def selection_decision_from_evaluation(
    evaluation: RuleEvaluation,
) -> SelectionDecision | None:
    if evaluation.result_code is not RuleOutcome.EXCLUDE:
        return None
    return SelectionDecision.reject(
        evaluation.reason_code,
        terminal_status=evaluation.terminal_status,
        reason=evaluation.reason,
    )


class TrackSelectionService:
    """Evaluate availability first and then injected event/business rules."""

    def __init__(
        self,
        rules: tuple[SelectionRule | ExecutableSelectionRule, ...] = (),
    ) -> None:
        self._rules: tuple[ExecutableSelectionRule, ...] = (
            TrackExistsRule(),
            RequiredMetadataRule(),
            *(self._as_executable(rule) for rule in rules),
        )

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
        rule_input = SelectionRuleInput.from_values(entry, track)
        candidate = rule_input.candidate
        evaluations: list[RuleEvaluation] = []
        for rule in self._rules:
            evaluation = rule.evaluate_rule(rule_input, context)
            evaluations.append(evaluation)
            rule_decision = selection_decision_from_evaluation(evaluation)
            if rule_decision is not None:
                return rule_decision, self._single_rationale(
                    candidate, rule_decision, evaluations, context
                )
        allowed = SelectionDecision.allow()
        return allowed, self._single_rationale(candidate, allowed, evaluations, context)

    @staticmethod
    def _as_executable(
        rule: SelectionRule | ExecutableSelectionRule,
    ) -> ExecutableSelectionRule:
        if callable(getattr(rule, "evaluate_rule", None)):
            return cast(ExecutableSelectionRule, rule)
        return LegacySelectionRuleAdapter(cast(SelectionRule, rule))

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
    relaxable_reason_codes: frozenset[str] = frozenset()

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
        entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None:
        return selection_decision_from_evaluation(
            self.evaluate_rule(
                SelectionRuleInput.from_values(entry, track),
                SelectionContext("legacy-block-service"),
            )
        )

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        assert track is not None
        if track.id in self._blocked_track_ids:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="BLOCKED_TRACK",
                reason="Titel ist für die automatische Auswahl gesperrt",
                excluded=True,
            )
        if normalize_artist_name(track.artist) in self._blocked_artists:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="BLOCKED_ARTIST",
                reason="Interpret ist für die automatische Auswahl gesperrt",
                excluded=True,
            )
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="BLOCK_POLICY_ALLOWED",
            reason="Titel und Interpret sind nicht gesperrt",
        )


class RepetitionService:
    """Reject tracks or artists present in bounded recent-play windows."""

    rule_id = "selection.repetition"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes = frozenset({"TRACK_REPETITION", "ARTIST_REPETITION"})

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
        entry: QueueEntry,
        track: Track,
    ) -> SelectionDecision | None:
        return selection_decision_from_evaluation(
            self.evaluate_rule(
                SelectionRuleInput.from_values(entry, track),
                SelectionContext("legacy-repetition-service"),
            )
        )

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        assert track is not None
        if (
            self.track_window_size
            and track.id in tuple(self._recent_track_ids)[-self.track_window_size :]
        ):
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="TRACK_REPETITION",
                reason="Titel wurde innerhalb des Wiederholungsfensters bereits gespielt",
                excluded=True,
                relaxable_reason_codes=self.relaxable_reason_codes,
            )
        artist = normalize_artist_name(track.artist)
        if (
            self.artist_window_size
            and artist in tuple(self._recent_artists)[-self.artist_window_size :]
        ):
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="ARTIST_REPETITION",
                reason="Interpret wurde innerhalb des Wiederholungsfensters bereits gespielt",
                excluded=True,
                relaxable_reason_codes=self.relaxable_reason_codes,
            )
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="REPETITION_ALLOWED",
            reason="Titel und Interpret liegen außerhalb der Wiederholungsfenster",
            relaxable_reason_codes=self.relaxable_reason_codes,
        )
