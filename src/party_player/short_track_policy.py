"""Queue selection rule for tracks with a short effective cue duration."""

from party_player.cue_points import CuePointService
from party_player.enums import QueueSource, ShortTrackPolicy
from party_player.models import QueueEntry, Track
from party_player.selection_decision import RuleKind
from party_player.track_selection import SelectionDecision


class ShortTrackSelectionRule:
    rule_id = "selection.short_track"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION

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
        boundaries = self._cue_points.resolve(track, queue_entry=entry)
        effective_duration = max(0.0, boundaries.cue_out - boundaries.cue_in)
        if effective_duration >= self.threshold_seconds:
            return None
        if self.policy in {ShortTrackPolicy.ALLOW, ShortTrackPolicy.USE_REDUCED_FADE}:
            return None
        if self.policy is ShortTrackPolicy.MANUAL_ONLY:
            if entry.source is QueueSource.MANUAL:
                return None
            return SelectionDecision.reject(
                "SHORT_TRACK_MANUAL_ONLY",
                reason="Kurztitel ist nur für eine manuelle Auswahl freigegeben",
            )
        if entry.source is QueueSource.MANUAL:
            return None
        return SelectionDecision.reject(
            "SHORT_TRACK_SKIPPED",
            reason="Kurztitel wird in der automatischen Auswahl übersprungen",
        )
