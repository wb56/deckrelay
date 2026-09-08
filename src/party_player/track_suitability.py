"""Persistent event suitability, separate from technical audio metadata."""

from dataclasses import dataclass
from enum import StrEnum

from party_player.database.connection import Database
from party_player.enums import QueueSource
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

    def get_many(self, track_ids: tuple[int, ...]) -> dict[int, TrackSuitability]:
        unique_ids = tuple(dict.fromkeys(track_ids))
        if not unique_ids:
            return {}
        result: dict[int, TrackSuitability] = {}
        for start in range(0, len(unique_ids), 900):
            batch = unique_ids[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            with self._database.connect() as connection:
                rows = connection.execute(
                    f"SELECT track_id, status, reason FROM track_suitability "
                    f"WHERE track_id IN ({placeholders})",
                    batch,
                ).fetchall()
            for row in rows:
                track_id = int(row["track_id"])
                result[track_id] = TrackSuitability(
                    track_id,
                    TrackSuitabilityStatus(str(row["status"])),
                    str(row["reason"]),
                )
        return {
            track_id: result.get(
                track_id, TrackSuitability(track_id, TrackSuitabilityStatus.UNKNOWN)
            )
            for track_id in unique_ids
        }

    def set_many(
        self,
        track_ids: tuple[int, ...],
        status: TrackSuitabilityStatus,
        reason: str = "",
    ) -> int:
        """Persist one explicit status for all tracks in one transaction."""
        unique_ids = tuple(dict.fromkeys(track_ids))
        if not unique_ids:
            return 0
        with self._database.transaction() as connection:
            existing = 0
            for start in range(0, len(unique_ids), 900):
                batch = unique_ids[start : start + 900]
                placeholders = ",".join("?" for _ in batch)
                existing += int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM tracks WHERE id IN ({placeholders})",
                        batch,
                    ).fetchone()[0]
                )
            if existing != len(unique_ids):
                raise ValueError("Mindestens ein ausgewählter Titel existiert nicht mehr")
            connection.executemany(
                """INSERT INTO track_suitability (track_id, status, reason)
                   VALUES (?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       status = excluded.status,
                       reason = excluded.reason,
                       updated_at = CURRENT_TIMESTAMP""",
                ((track_id, status.value, reason.strip()) for track_id in unique_ids),
            )
        return len(unique_ids)


class TrackSuitabilityService:
    """Require explicit suitability for non-operator queue sources."""

    rule_id = "selection.track_suitability"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes: frozenset[str] = frozenset()

    _OPERATOR_SOURCES = frozenset({QueueSource.MANUAL, QueueSource.PLAYLIST})

    def __init__(self, repository: TrackSuitabilityRepository) -> None:
        self._repository = repository
        self._operator_overrides: set[int] = set()

    def allow_queue_entry(self, queue_id: int) -> None:
        self._operator_overrides.add(queue_id)

    def operator_override_applies(self, entry: QueueEntry) -> bool:
        return entry.queue_id in self._operator_overrides

    def evaluate(self, entry: QueueEntry, track: Track) -> SelectionDecision | None:
        return selection_decision_from_evaluation(
            self.evaluate_rule(
                SelectionRuleInput.from_values(entry, track),
                SelectionContext("legacy-track-suitability"),
            )
        )

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        assert track is not None
        suitability = self._repository.get(track.id)
        if suitability.status is TrackSuitabilityStatus.SUITABLE:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="TRACK_SUITABLE",
                reason="Titel ist für die Auswahl freigegeben",
            )
        code = (
            "UNSUITABLE_TRACK"
            if suitability.status is TrackSuitabilityStatus.UNSUITABLE
            else "SUITABILITY_APPROVAL_REQUIRED"
        )
        reason = suitability.reason or (
            "Titel ist für die automatische Auswahl nicht ausdrücklich freigegeben"
        )
        if rule_input.entry.queue_id in self._operator_overrides:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code=code,
                reason=reason,
                operator_override=True,
            )
        if (
            suitability.status
            in {TrackSuitabilityStatus.MANUAL_ONLY, TrackSuitabilityStatus.UNKNOWN}
            and rule_input.entry.source in self._OPERATOR_SOURCES
        ):
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="SUITABILITY_OPERATOR_SOURCE_ALLOWED",
                reason="Die Operatorquelle darf den Titel gemäß Eignungsrichtlinie auswählen",
            )
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code=code,
            reason=reason,
            excluded=True,
        )
