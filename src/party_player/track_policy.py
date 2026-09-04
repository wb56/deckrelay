"""Persistent track eligibility policy for automatic queue selection."""

from dataclasses import dataclass
from enum import StrEnum

from party_player.database.connection import Database
from party_player.models import QueueEntry, Track
from party_player.selection_decision import RuleKind
from party_player.track_selection import SelectionDecision


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
        policy = self._repository.get(track.id)
        if policy.status is TrackPolicyStatus.ALLOWED:
            return None
        if entry.queue_id in self._operator_overrides:
            return None
        code = "BLOCKED_TRACK" if policy.status is TrackPolicyStatus.BLOCKED else "RESTRICTED_TRACK"
        return SelectionDecision.reject(
            code,
            reason=policy.reason or "Titel benötigt eine ausdrückliche Operatorfreigabe",
        )
