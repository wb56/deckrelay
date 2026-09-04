"""Queue selection rule for tracks with a short effective cue duration."""

from party_player.cue_points import CuePointService
from party_player.enums import QueueSource, ShortTrackPolicy
from party_player.models import QueueEntry, Track
from party_player.selection_decision import (
    RuleEvaluation,
    RuleKind,
    SelectionContext,
    SelectionRuleInput,
    hard_rule_evaluation,
)
from party_player.track_selection import (
    SelectionDecision,
    selection_decision_from_evaluation,
)


class ShortTrackSelectionRule:
    rule_id = "selection.short_track"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes: frozenset[str] = frozenset()

    def __init__(
        self,
        cue_points: CuePointService,
        *,
        threshold_seconds: float = 30.0,
        policy: ShortTrackPolicy = ShortTrackPolicy.ALLOW,
    ) -> None:
        self._cue_points = cue_points
        self.threshold_seconds = max(1.0, threshold_seconds)
        self.policy = policy

    def evaluate(self, entry: QueueEntry, track: Track) -> SelectionDecision | None:
        return selection_decision_from_evaluation(
            self.evaluate_rule(
                SelectionRuleInput.from_values(entry, track),
                SelectionContext("legacy-short-track"),
            )
        )

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        assert track is not None
        entry = rule_input.entry
        boundaries = self._cue_points.resolve(track, queue_entry=entry)
        effective_duration = max(0.0, boundaries.cue_out - boundaries.cue_in)
        if effective_duration >= self.threshold_seconds:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="SHORT_TRACK_NOT_APPLICABLE",
                reason="Nutzbare Titellänge liegt oberhalb der Kurztrack-Schwelle",
                applicable=False,
                facts=(("effective_duration_seconds", effective_duration),),
            )
        if self.policy in {ShortTrackPolicy.ALLOW, ShortTrackPolicy.USE_REDUCED_FADE}:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="SHORT_TRACK_ALLOWED",
                reason="Kurztrack-Behandlung erlaubt die Auswahl",
                facts=(("effective_duration_seconds", effective_duration),),
            )
        if self.policy is ShortTrackPolicy.MANUAL_ONLY:
            if entry.source is QueueSource.MANUAL:
                return hard_rule_evaluation(
                    rule_id=self.rule_id,
                    rule_version=self.rule_version,
                    context=context,
                    reason_code="SHORT_TRACK_MANUAL_SOURCE_ALLOWED",
                    reason="Die manuelle Quelle darf den Kurztitel auswählen",
                    facts=(("effective_duration_seconds", effective_duration),),
                )
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="SHORT_TRACK_MANUAL_ONLY",
                reason="Kurztitel ist nur für eine manuelle Auswahl freigegeben",
                excluded=True,
                facts=(("effective_duration_seconds", effective_duration),),
            )
        if entry.source is QueueSource.MANUAL:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="SHORT_TRACK_MANUAL_SOURCE_ALLOWED",
                reason="Die manuelle Quelle darf den Kurztitel auswählen",
                facts=(("effective_duration_seconds", effective_duration),),
            )
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="SHORT_TRACK_SKIPPED",
            reason="Kurztitel wird in der automatischen Auswahl übersprungen",
            excluded=True,
            facts=(("effective_duration_seconds", effective_duration),),
        )
