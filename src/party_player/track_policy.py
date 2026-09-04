"""Persistent track eligibility policy for automatic queue selection."""

from dataclasses import dataclass
from enum import StrEnum

from party_player.database.connection import Database
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


class TrackPolicyStatus(StrEnum):
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    RESTRICTED = "RESTRICTED"


@dataclass(frozen=True, slots=True)
class TrackPolicy:
    track_id: int
    status: TrackPolicyStatus
    reason: str = ""


class TrackPolicyRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, track_id: int) -> TrackPolicy:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT track_id, status, reason
                   FROM track_playback_policies WHERE track_id = ?""",
                (track_id,),
            ).fetchone()
        if row is None:
            return TrackPolicy(track_id, TrackPolicyStatus.ALLOWED)
        return TrackPolicy(
            int(row["track_id"]),
            TrackPolicyStatus(str(row["status"])),
            str(row["reason"]),
        )

    def set(self, track_id: int, status: TrackPolicyStatus, reason: str = "") -> TrackPolicy:
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_playback_policies (track_id, status, reason)
                   VALUES (?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       status = excluded.status,
                       reason = excluded.reason,
                       updated_at = CURRENT_TIMESTAMP""",
                (track_id, status.value, reason.strip()),
            )
        return self.get(track_id)


class PersistentTrackBlockService:
    """Selection rule with explicit per-queue operator overrides."""

    rule_id = "selection.track_policy"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes: frozenset[str] = frozenset()

    def __init__(self, repository: TrackPolicyRepository) -> None:
        self._repository = repository
        self._operator_overrides: set[int] = set()

    def set_policy(
        self,
        track_id: int,
        status: TrackPolicyStatus,
        reason: str = "",
    ) -> TrackPolicy:
        return self._repository.set(track_id, status, reason)

    def allow_queue_entry(self, queue_id: int) -> None:
        self._operator_overrides.add(queue_id)

    def operator_override_applies(self, entry: QueueEntry) -> bool:
        return entry.queue_id in self._operator_overrides

    def evaluate(self, entry: QueueEntry, track: Track) -> SelectionDecision | None:
        return selection_decision_from_evaluation(
            self.evaluate_rule(
                SelectionRuleInput.from_values(entry, track),
                SelectionContext("legacy-track-policy"),
            )
        )

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        assert track is not None
        policy = self._repository.get(track.id)
        if policy.status is TrackPolicyStatus.ALLOWED:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="TRACK_POLICY_ALLOWED",
                reason="Titelrichtlinie erlaubt die Auswahl",
            )
        code = "BLOCKED_TRACK" if policy.status is TrackPolicyStatus.BLOCKED else "RESTRICTED_TRACK"
        reason = policy.reason or "Titel benötigt eine ausdrückliche Operatorfreigabe"
        if rule_input.entry.queue_id in self._operator_overrides:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code=code,
                reason=reason,
                operator_override=True,
            )
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code=code,
            reason=reason,
            excluded=True,
        )
