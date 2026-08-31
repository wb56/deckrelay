"""Cue-aware tempo scopes, signatures, persistence and pure resolution rules."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
import hashlib
import json
from typing import Any, Iterable, Protocol

from party_player.database.connection import Database
from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    TempoAnalysisRangeSnapshot,
    TempoAnalysisScope,
)
from party_player.metadata_analysis_profiles import HIGH_CONFIDENCE


def cue_milliseconds(value: float) -> int:
    """Canonicalize cue values without float string comparisons."""
    if value < 0:
        raise ValueError("Cue-Wert darf nicht negativ sein")
    return int((Decimal(str(value)) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def tempo_range_signature(
    scope: TempoAnalysisScope,
    track_id: int,
    snapshot: FileSnapshot,
    analysis_range: TempoAnalysisRangeSnapshot,
    algorithm_version: str,
) -> str:
    context_id = (
        analysis_range.saved_queue_entry_id
        if scope is TempoAnalysisScope.SAVED_QUEUE_ENTRY
        else (
            analysis_range.party_queue_id
            if scope is TempoAnalysisScope.PARTY_QUEUE_SNAPSHOT
            else None
        )
    )
    payload = {
        "scope": scope.value,
        "track_id": track_id,
        "context_id": context_id,
        "cue_in_ms": cue_milliseconds(analysis_range.cue_in),
        "cue_out_ms": cue_milliseconds(analysis_range.cue_out),
        "fade_ms": cue_milliseconds(analysis_range.fade_duration),
        "file_size": snapshot.size,
        "file_modified_ns": snapshot.modified_ns,
        "algorithm_version": algorithm_version,
        "context_revision": analysis_range.context_revision,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TempoValueSource(StrEnum):
    MANUAL_CATALOG = "MANUAL_CATALOG"
    TRACK_DEFAULT_CUES = "TRACK_DEFAULT_CUES"
    TRACK_FULL = "TRACK_FULL"
    MANUAL_SAVED_QUEUE = "MANUAL_SAVED_QUEUE"
    SAVED_QUEUE_ENTRY = "SAVED_QUEUE_ENTRY"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class TempoAnalysisValue:
    bpm: float | None
    alternative_bpm: float | None
    source: TempoValueSource
    scope: TempoAnalysisScope | None
    confidence: float | None
    current: bool
    range_signature: str | None
    warnings: tuple[str, ...] = ()
    confirmed: bool = False
    rhythm_stability: float | None = None

    @property
    def usable(self) -> bool:
        return self.current and self.bpm is not None

    @property
    def reliable(self) -> bool:
        """Return whether automation may consume this value without review."""
        return (
            self.usable
            and (self.confirmed or (self.confidence or 0.0) >= HIGH_CONFIDENCE)
            and (self.confirmed or self.rhythm_stability is None or self.rhythm_stability >= 0.65)
        )


@dataclass(frozen=True, slots=True)
class TempoResolution:
    """Keep confirmed truth, analysis proposal and planning fallback distinct."""

    confirmed: TempoAnalysisValue
    best_analysis_proposal: TempoAnalysisValue
    planning: TempoAnalysisValue


@dataclass(frozen=True, slots=True)
class SavedQueueManualTempo:
    saved_queue_entry_id: int
    bpm: float
    confirmed: bool
    source: TempoValueSource
    changed_at: str
    based_on_signature: str | None = None


@dataclass(frozen=True, slots=True)
class PartyQueueTempoSnapshot:
    track_id: int
    cue_in: float
    cue_out: float
    fade_duration: float
    bpm: float | None
    alternative_bpm: float | None
    bpm_source: TempoValueSource
    confidence: float | None
    range_signature: str | None
    algorithm_version: str | None


class TempoResolver:
    """Pure priority resolver; it performs no persistence or widget access."""

    @staticmethod
    def catalog(
        manual_catalog_bpm: float | None,
        cue_value: TempoAnalysisValue | None,
        full_value: TempoAnalysisValue | None,
    ) -> TempoResolution:
        none = TempoResolver._none()
        confirmed = (
            TempoAnalysisValue(
                manual_catalog_bpm,
                None,
                TempoValueSource.MANUAL_CATALOG,
                None,
                None,
                True,
                None,
                confirmed=True,
            )
            if manual_catalog_bpm is not None
            else none
        )
        proposal = TempoResolver._first_usable(cue_value, full_value) or none
        planning = (
            confirmed
            if confirmed.usable
            else (TempoResolver._first_reliable(cue_value, full_value) or none)
        )
        return TempoResolution(confirmed, proposal, planning)

    @staticmethod
    def saved_queue(
        manual: SavedQueueManualTempo | None,
        saved_value: TempoAnalysisValue | None,
        catalog: TempoResolution,
        full_value: TempoAnalysisValue | None,
        *,
        current_range_signature: str | None = None,
    ) -> TempoResolution:
        none = TempoResolver._none()
        confirmed = (
            TempoAnalysisValue(
                manual.bpm,
                None,
                TempoValueSource.MANUAL_SAVED_QUEUE,
                TempoAnalysisScope.SAVED_QUEUE_ENTRY,
                None,
                True,
                manual.based_on_signature,
                (
                    ("Manuelle Playlistkorrektur beruht auf einem geänderten Cue-Bereich.",)
                    if manual.based_on_signature is not None
                    and current_range_signature is not None
                    and manual.based_on_signature != current_range_signature
                    else ()
                ),
                confirmed=manual.confirmed,
            )
            if manual is not None and manual.confirmed
            else none
        )
        proposal = (
            TempoResolver._first_usable(saved_value, catalog.best_analysis_proposal, full_value)
            or none
        )
        planning = confirmed
        if not planning.usable:
            planning = (
                TempoResolver._first_reliable(saved_value, catalog.planning, full_value) or none
            )
        return TempoResolution(confirmed, proposal, planning)

    @staticmethod
    def party_queue_snapshot(
        track_id: int,
        analysis_range: TempoAnalysisRangeSnapshot,
        resolution: TempoResolution,
        algorithm_version: str | None,
    ) -> PartyQueueTempoSnapshot:
        value = resolution.planning
        return PartyQueueTempoSnapshot(
            track_id,
            analysis_range.cue_in,
            analysis_range.cue_out,
            analysis_range.fade_duration,
            value.bpm,
            value.alternative_bpm,
            value.source,
            value.confidence,
            value.range_signature,
            algorithm_version,
        )

    @staticmethod
    def _first_usable(*values: TempoAnalysisValue | None) -> TempoAnalysisValue | None:
        return next((value for value in values if value is not None and value.usable), None)

    @staticmethod
    def _first_reliable(*values: TempoAnalysisValue | None) -> TempoAnalysisValue | None:
        return next((value for value in values if value is not None and value.reliable), None)

    @staticmethod
    def _none() -> TempoAnalysisValue:
        return TempoAnalysisValue(None, None, TempoValueSource.NONE, None, None, False, None)


class TempoContextRepository:
    """Serial SQLite adapter for contextual tempo results and playlist corrections."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def save_result(
        self,
        *,
        track_id: int,
        scope: TempoAnalysisScope,
        context_id: int | None,
        run_id: int | None,
        signature: str,
        analysis_range: TempoAnalysisRangeSnapshot,
        bpm: float | None,
        alternative_bpm: float | None,
        confidence: float | None,
        rhythm_stability: float | None,
        warnings: Iterable[str],
        experimental_energy: float | None,
        backend: str,
        algorithm_version: str,
        analyzed_at: str,
    ) -> None:
        if len(signature) != 64 or track_id <= 0 or not backend or not algorithm_version:
            raise ValueError("Kontextbezogenes Tempoergebnis ist unvollständig")
        warning_values = tuple(str(item)[:500] for item in warnings)
        with self._database.transaction() as connection:
            connection.execute(
                """UPDATE tempo_analysis_results
                   SET is_current=0,stale_reason='Durch neuere Bereichsanalyse abgelöst'
                   WHERE track_id=? AND scope_type=? AND context_id IS ? AND is_current=1""",
                (track_id, scope.value, context_id),
            )
            connection.execute(
                """INSERT INTO tempo_analysis_results
                       (track_id,scope_type,context_id,run_id,range_signature,
                        cue_in_ms,cue_out_ms,fade_ms,physical_duration_ms,
                        context_revision,inherited_track_cues,primary_bpm,alternative_bpm,
                        confidence,rhythm_stability,warnings_json,experimental_energy,
                        backend,algorithm_version,analyzed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    track_id,
                    scope.value,
                    context_id,
                    run_id,
                    signature,
                    cue_milliseconds(analysis_range.cue_in),
                    cue_milliseconds(analysis_range.cue_out),
                    cue_milliseconds(analysis_range.fade_duration),
                    cue_milliseconds(analysis_range.physical_duration),
                    analysis_range.context_revision,
                    int(analysis_range.inherited_track_cues),
                    bpm,
                    alternative_bpm,
                    confidence,
                    rhythm_stability,
                    json.dumps(warning_values, ensure_ascii=False),
                    experimental_energy,
                    backend,
                    algorithm_version,
                    analyzed_at,
                ),
            )

    def current_value(
        self,
        track_id: int,
        scope: TempoAnalysisScope,
        *,
        context_id: int | None = None,
        expected_signature: str | None = None,
    ) -> TempoAnalysisValue | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT primary_bpm,alternative_bpm,confidence,rhythm_stability,
                          range_signature,warnings_json,is_current
                   FROM tempo_analysis_results
                   WHERE track_id=? AND scope_type=? AND context_id IS ?
                   ORDER BY is_current DESC,analyzed_at DESC,id DESC LIMIT 1""",
                (track_id, scope.value, context_id),
            ).fetchone()
        if row is None:
            return None
        signature = str(row["range_signature"])
        current = bool(row["is_current"]) and (
            expected_signature is None or expected_signature == signature
        )
        source = {
            TempoAnalysisScope.TRACK_FULL: TempoValueSource.TRACK_FULL,
            TempoAnalysisScope.TRACK_DEFAULT_CUES: TempoValueSource.TRACK_DEFAULT_CUES,
            TempoAnalysisScope.SAVED_QUEUE_ENTRY: TempoValueSource.SAVED_QUEUE_ENTRY,
            TempoAnalysisScope.PARTY_QUEUE_SNAPSHOT: TempoValueSource.SAVED_QUEUE_ENTRY,
        }[scope]
        return TempoAnalysisValue(
            float(row["primary_bpm"]) if row["primary_bpm"] is not None else None,
            (float(row["alternative_bpm"]) if row["alternative_bpm"] is not None else None),
            source,
            scope,
            float(row["confidence"]) if row["confidence"] is not None else None,
            current,
            signature,
            tuple(json.loads(str(row["warnings_json"]))),
            rhythm_stability=(
                float(row["rhythm_stability"]) if row["rhythm_stability"] is not None else None
            ),
        )

    def save_manual_saved_queue_bpm(
        self,
        saved_queue_entry_id: int,
        bpm: float,
        *,
        confirmed: bool = True,
        based_on_signature: str | None = None,
    ) -> SavedQueueManualTempo:
        if saved_queue_entry_id <= 0 or not 20.0 <= bpm <= 400.0:
            raise ValueError("Playlistbezogener BPM-Wert ist ungültig")
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO saved_queue_entry_tempo_overrides
                       (saved_queue_entry_id,bpm,confirmed,source,based_on_signature,changed_at)
                   VALUES (?,?,?,'MANUAL_SAVED_QUEUE',?,CURRENT_TIMESTAMP)
                   ON CONFLICT(saved_queue_entry_id) DO UPDATE SET
                       bpm=excluded.bpm,confirmed=excluded.confirmed,source=excluded.source,
                       based_on_signature=excluded.based_on_signature,
                       changed_at=CURRENT_TIMESTAMP""",
                (saved_queue_entry_id, bpm, int(confirmed), based_on_signature),
            )
        return self.manual_saved_queue_bpm(saved_queue_entry_id)  # type: ignore[return-value]

    def manual_saved_queue_bpm(self, saved_queue_entry_id: int) -> SavedQueueManualTempo | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT saved_queue_entry_id,bpm,confirmed,source,changed_at,
                          based_on_signature FROM saved_queue_entry_tempo_overrides
                   WHERE saved_queue_entry_id=?""",
                (saved_queue_entry_id,),
            ).fetchone()
        if row is None:
            return None
        return SavedQueueManualTempo(
            int(row["saved_queue_entry_id"]),
            float(row["bpm"]),
            bool(row["confirmed"]),
            TempoValueSource(str(row["source"])),
            str(row["changed_at"]),
            str(row["based_on_signature"]) if row["based_on_signature"] else None,
        )

    def reset_manual_saved_queue_bpm(self, saved_queue_entry_id: int) -> None:
        with self._database.connect() as connection:
            connection.execute(
                "DELETE FROM saved_queue_entry_tempo_overrides WHERE saved_queue_entry_id=?",
                (saved_queue_entry_id,),
            )

    def mark_scope_stale(
        self,
        track_id: int,
        scope: TempoAnalysisScope,
        reason: str,
        *,
        inherited_only: bool = False,
    ) -> int:
        sql = (
            "UPDATE tempo_analysis_results SET is_current=0,stale_reason=? "
            "WHERE track_id=? AND scope_type=? AND is_current=1"
        )
        values: list[object] = [reason[:300], track_id, scope.value]
        if inherited_only:
            sql += " AND inherited_track_cues=1"
        with self._database.transaction() as connection:
            cursor = connection.execute(sql, values)
            if scope is TempoAnalysisScope.TRACK_DEFAULT_CUES:
                connection.execute(
                    """UPDATE track_metadata_suggestions
                       SET review_status='OUTDATED'
                       WHERE analysis_run_id IN (
                           SELECT run_id FROM tempo_analysis_results
                           WHERE track_id=? AND scope_type='TRACK_DEFAULT_CUES'
                       ) AND status='PENDING'""",
                    (track_id,),
                )
        return max(0, cursor.rowcount)


class CueBoundaryResolver(Protocol):
    def resolve(
        self, track: Any, global_fade_duration: float | None = None, queue_entry: Any = None
    ) -> Any: ...


class TempoAnalysisContextResolver:
    """Resolve all cue inheritance in the main process before job creation."""

    def __init__(self, cues: CueBoundaryResolver) -> None:
        self._cues = cues

    def track_default_cues(self, track: Any, revision: str) -> TempoAnalysisRangeSnapshot:
        boundaries = self._cues.resolve(track)
        return resolved_now(
            boundaries.cue_in,
            boundaries.cue_out,
            boundaries.fade_duration,
            float(track.duration_seconds or 0.0),
            revision,
        )

    def saved_queue_entry(
        self, track: Any, entry: Any, revision: str
    ) -> TempoAnalysisRangeSnapshot:
        has_snapshot = any(
            value is not None for value in (entry.cue_in, entry.cue_out, entry.fade_duration)
        )
        override = type(
            "_ImmutableSavedCueOverride",
            (),
            {
                "cue_in_override": entry.cue_in,
                "cue_out_override": entry.cue_out,
                "fade_duration_override": entry.fade_duration,
                "cue_override_source": "snapshot",
            },
        )()
        boundaries = self._cues.resolve(track, queue_entry=override if has_snapshot else None)
        entry_id = getattr(entry, "saved_queue_entry_id", None)
        if entry_id is None:
            raise ValueError("Saved-Queue-Analyse benötigt eine persistierte Eintrags-ID")
        return resolved_now(
            boundaries.cue_in,
            boundaries.cue_out,
            boundaries.fade_duration,
            float(track.duration_seconds or 0.0),
            revision,
            saved_queue_entry_id=int(entry_id),
            inherited_track_cues=not has_snapshot,
        )


def resolved_now(
    cue_in: float,
    cue_out: float,
    fade_duration: float,
    physical_duration: float,
    context_revision: str,
    **context: object,
) -> TempoAnalysisRangeSnapshot:
    """Convenience factory used only in the main process."""
    return TempoAnalysisRangeSnapshot(
        cue_in,
        cue_out,
        fade_duration,
        physical_duration,
        datetime.now(timezone.utc).isoformat(),
        context_revision,
        **context,  # type: ignore[arg-type]
    )
