"""Persistent event suitability, separate from technical audio metadata."""

from dataclasses import dataclass
from enum import StrEnum

from party_player.database.connection import Database
from party_player.enums import QueueSource
from party_player.models import QueueEntry, Track
from party_player.selection_decision import RuleKind
from party_player.track_selection import SelectionDecision


class TrackSuitabilityStatus(StrEnum):
    SUITABLE = "SUITABLE"
    MANUAL_ONLY = "MANUAL_ONLY"
    UNSUITABLE = "UNSUITABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TrackSuitability:
    track_id: int
    status: TrackSuitabilityStatus
    reason: str = ""


class TrackSuitabilityRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, track_id: int) -> TrackSuitability:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT track_id, status, reason FROM track_suitability WHERE track_id = ?",
                (track_id,),
            ).fetchone()
        if row is None:
            return TrackSuitability(track_id, TrackSuitabilityStatus.UNKNOWN)
        return TrackSuitability(
            int(row["track_id"]),
            TrackSuitabilityStatus(str(row["status"])),
            str(row["reason"]),
        )

    def set(
        self,
        track_id: int,
        status: TrackSuitabilityStatus,
        reason: str = "",
    ) -> TrackSuitability:
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_suitability (track_id, status, reason)
                   VALUES (?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       status = excluded.status,
                       reason = excluded.reason,
                       updated_at = CURRENT_TIMESTAMP""",
                (track_id, status.value, reason.strip()),
            )
        return self.get(track_id)


class TrackSuitabilityService:
    """Require explicit suitability for non-operator queue sources."""

    rule_id = "selection.track_suitability"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION

    _OPERATOR_SOURCES = frozenset({QueueSource.MANUAL, QueueSource.PLAYLIST})

    def __init__(self, repository: TrackSuitabilityRepository) -> None:
        self._repository = repository
        self._operator_overrides: set[int] = set()

    def allow_queue_entry(self, queue_id: int) -> None:
        self._operator_overrides.add(queue_id)

    def operator_override_applies(self, entry: QueueEntry) -> bool:
        return entry.queue_id in self._operator_overrides

    def evaluate(self, entry: QueueEntry, track: Track) -> SelectionDecision | None:
        suitability = self._repository.get(track.id)
        if suitability.status is TrackSuitabilityStatus.SUITABLE:
            return None
        if entry.queue_id in self._operator_overrides:
            return None
        if (
            suitability.status
            in {TrackSuitabilityStatus.MANUAL_ONLY, TrackSuitabilityStatus.UNKNOWN}
            and entry.source in self._OPERATOR_SOURCES
        ):
            return None
        code = (
            "UNSUITABLE_TRACK"
            if suitability.status is TrackSuitabilityStatus.UNSUITABLE
            else "SUITABILITY_APPROVAL_REQUIRED"
        )
        return SelectionDecision.reject(
            code,
            reason=suitability.reason
            or "Titel ist für die automatische Auswahl nicht ausdrücklich freigegeben",
        )
