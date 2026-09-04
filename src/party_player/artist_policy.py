"""Persistent artist blocking policies for automatic selection."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
    normalize_artist_name,
    selection_decision_from_evaluation,
)


class ArtistPolicyScope(StrEnum):
    PERMANENT = "PERMANENT"
    SESSION = "SESSION"
    TEMPORARY = "TEMPORARY"


@dataclass(frozen=True, slots=True)
class ArtistPolicy:
    normalized_artist: str
    display_name: str
    scope: ArtistPolicyScope
    session_id: int | None
    expires_at: datetime | None
    reason: str


class ArtistPolicyRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, artist: str) -> ArtistPolicy | None:
        normalized = normalize_artist_name(artist)
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT normalized_artist, display_name, scope, session_id,
                          expires_at, reason
                   FROM artist_playback_policies
                   WHERE normalized_artist = ?""",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        expires_at = (
            datetime.fromisoformat(str(row["expires_at"]))
            if row["expires_at"] is not None
            else None
        )
        return ArtistPolicy(
            str(row["normalized_artist"]),
            str(row["display_name"]),
            ArtistPolicyScope(str(row["scope"])),
            int(row["session_id"]) if row["session_id"] is not None else None,
            expires_at,
            str(row["reason"]),
        )

    def set(
        self,
        artist: str,
        scope: ArtistPolicyScope,
        *,
        session_id: int | None = None,
        expires_at: datetime | None = None,
        reason: str = "",
    ) -> ArtistPolicy:
        normalized = normalize_artist_name(artist)
        if not normalized:
            raise ValueError("Interpret darf nicht leer sein")
        if scope is ArtistPolicyScope.SESSION and session_id is None:
            raise ValueError("Sitzungssperre benötigt eine Session-ID")
        if scope is ArtistPolicyScope.TEMPORARY and expires_at is None:
            raise ValueError("Temporäre Sperre benötigt ein Enddatum")
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO artist_playback_policies
                   (normalized_artist, display_name, scope, session_id, expires_at, reason)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(normalized_artist) DO UPDATE SET
                       display_name = excluded.display_name,
                       scope = excluded.scope,
                       session_id = excluded.session_id,
                       expires_at = excluded.expires_at,
                       reason = excluded.reason,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    normalized,
                    artist.strip(),
                    scope.value,
                    session_id,
                    expires_at.isoformat() if expires_at is not None else None,
                    reason.strip(),
                ),
            )
        policy = self.get(artist)
        assert policy is not None
        return policy


class PersistentArtistBlockService:
    rule_id = "selection.artist_policy"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes: frozenset[str] = frozenset()

    def __init__(
        self,
        repository: ArtistPolicyRepository,
        session_id: int,
        *,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._repository = repository
        self._session_id = session_id
        self._clock = clock

    def evaluate(self, entry: QueueEntry, track: Track) -> SelectionDecision | None:
        return selection_decision_from_evaluation(
            self.evaluate_rule(
                SelectionRuleInput.from_values(entry, track),
                SelectionContext("legacy-artist-policy"),
            )
        )

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        track = rule_input.track
        assert track is not None
        policy = self._repository.get(track.artist)
        if policy is None:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="NO_ARTIST_POLICY",
                reason="Für den Interpreten ist keine Sperrrichtlinie hinterlegt",
                applicable=False,
            )
        active = (
            policy.scope is ArtistPolicyScope.PERMANENT
            or (policy.scope is ArtistPolicyScope.SESSION and policy.session_id == self._session_id)
            or (
                policy.scope is ArtistPolicyScope.TEMPORARY
                and policy.expires_at is not None
                and policy.expires_at > self._clock()
            )
        )
        if not active:
            return hard_rule_evaluation(
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                context=context,
                reason_code="ARTIST_POLICY_INACTIVE",
                reason="Die Interpretensperre ist im aktuellen Kontext nicht aktiv",
                applicable=False,
            )
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="BLOCKED_ARTIST",
            reason=policy.reason or "Interpret ist für die automatische Auswahl gesperrt",
            excluded=True,
        )
