"""Persistent cue-point models, resolution, and validation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from party_player.database.connection import Database
from party_player.enums import ShortTrackPolicy
from party_player.models import QueueEntry, Track

if TYPE_CHECKING:
    from party_player.analysis.result import CueAnalysisResult


@dataclass(frozen=True, slots=True)
class TrackCuePoints:
    track_id: int
    manual_cue_in: float | None = None
    manual_cue_out: float | None = None
    manual_fade_duration: float | None = None
    automatic_cue_in: float | None = None
    automatic_cue_out: float | None = None
    automatic_fade_duration: float | None = None
    minimum_level_dbfs: float | None = None
    maximum_level_dbfs: float | None = None
    peak: float | None = None
    measured_window_count: int | None = None
    confidence: float | None = None
    analysis_version: str | None = None
    analysed_at: str | None = None
    analysis_backend: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedTrackBoundaries:
    cue_in: float
    cue_out: float
    fade_duration: float
    cue_in_source: str
    cue_out_source: str
    fade_source: str
    automatic_crossfade_allowed: bool = True
    warning: str = ""

    @property
    def crossfade_start(self) -> float:
        return self.cue_out - self.fade_duration


@dataclass(frozen=True, slots=True)
class QueueCueEditorState:
    queue_id: int
    title: str
    cue_in_override: float | None
    cue_out_override: float | None
    fade_duration_override: float | None
    resolved: ResolvedTrackBoundaries


class CuePointRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, track_id: int) -> TrackCuePoints:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT track_id, manual_cue_in, manual_cue_out, manual_fade_duration,
                          automatic_cue_in, automatic_cue_out, automatic_fade_duration,
                          minimum_level_dbfs, maximum_level_dbfs, peak,
                          measured_window_count, confidence, analysis_version,
                          analysed_at, analysis_backend
                   FROM track_cue_points WHERE track_id = ?""",
                (track_id,),
            ).fetchone()
        return TrackCuePoints(**dict(row)) if row else TrackCuePoints(track_id)

    def manual_track_ids(self, track_ids: list[int]) -> set[int]:
        result: set[int] = set()
        unique_ids = list(dict.fromkeys(track_ids))
        for start in range(0, len(unique_ids), 900):
            batch = unique_ids[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            with self._database.connect() as connection:
                rows = connection.execute(
                    f"""SELECT track_id FROM track_cue_points
                         WHERE track_id IN ({placeholders})
                           AND (manual_cue_in IS NOT NULL
                             OR manual_cue_out IS NOT NULL
                             OR manual_fade_duration IS NOT NULL)""",
                    batch,
                ).fetchall()
            result.update(int(row["track_id"]) for row in rows)
        return result

    def save_manual(
        self,
        track_id: int,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
    ) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_cue_points
                       (track_id, manual_cue_in, manual_cue_out, manual_fade_duration)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       manual_cue_in = excluded.manual_cue_in,
                       manual_cue_out = excluded.manual_cue_out,
                       manual_fade_duration = excluded.manual_fade_duration,
                       updated_at = CURRENT_TIMESTAMP""",
                (track_id, cue_in, cue_out, fade_duration),
            )

    def save_editor(
        self,
        track_id: int,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
        *,
        discard_automatic: bool,
        changed_fields: frozenset[str] | None = None,
    ) -> None:
        """Atomically persist manual values and an optional analysis discard."""
        allowed_fields = {
            "cue_in": ("manual_cue_in", cue_in),
            "cue_out": ("manual_cue_out", cue_out),
            "fade_duration": ("manual_fade_duration", fade_duration),
        }
        selected = frozenset(allowed_fields) if changed_fields is None else changed_fields
        unknown = selected.difference(allowed_fields)
        if unknown:
            raise ValueError(f"Unbekannte Cue-Änderungsfelder: {sorted(unknown)}")
        assignments = [
            f"{allowed_fields[field][0]} = ?"
            for field in ("cue_in", "cue_out", "fade_duration")
            if field in selected
        ]
        values = [
            allowed_fields[field][1]
            for field in ("cue_in", "cue_out", "fade_duration")
            if field in selected
        ]
        if discard_automatic:
            assignments.extend(
                f"{column} = NULL"
                for column in (
                    "automatic_cue_in",
                    "automatic_cue_out",
                    "automatic_fade_duration",
                    "minimum_level_dbfs",
                    "maximum_level_dbfs",
                    "peak",
                    "measured_window_count",
                    "confidence",
                    "analysis_version",
                    "analysed_at",
                    "analysis_backend",
                )
            )
        if not assignments:
            return
        with self._database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO track_cue_points (track_id) VALUES (?)",
                (track_id,),
            )
            connection.execute(
                f"""UPDATE track_cue_points
                    SET {", ".join(assignments)}, updated_at = CURRENT_TIMESTAMP
                    WHERE track_id = ?""",
                (*values, track_id),
            )

    def save_automatic(self, track_id: int, result: CueAnalysisResult) -> None:
        """Upsert automatic values and metadata without touching manual overrides."""
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_cue_points
                       (track_id, automatic_cue_in, automatic_cue_out,
                        automatic_fade_duration, minimum_level_dbfs,
                        maximum_level_dbfs, peak, measured_window_count, confidence,
                        analysis_version, analysed_at, analysis_backend)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       automatic_cue_in = excluded.automatic_cue_in,
                       automatic_cue_out = excluded.automatic_cue_out,
                       automatic_fade_duration = excluded.automatic_fade_duration,
                       minimum_level_dbfs = excluded.minimum_level_dbfs,
                       maximum_level_dbfs = excluded.maximum_level_dbfs,
                       peak = excluded.peak,
                       measured_window_count = excluded.measured_window_count,
                       confidence = excluded.confidence,
                       analysis_version = excluded.analysis_version,
                       analysed_at = excluded.analysed_at,
                       analysis_backend = excluded.analysis_backend,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    track_id,
                    result.cue_in,
                    result.cue_out,
                    result.suggested_fade_duration,
                    result.minimum_level_dbfs,
                    result.maximum_level_dbfs,
                    result.peak,
                    result.measured_window_count,
                    result.confidence,
                    result.analysis_version,
                    result.analyzed_at.isoformat(),
                    result.backend_name,
                ),
            )

    def clear_automatic(self, track_id: int) -> None:
        """Discard analysis output without changing any manual value."""
        with self._database.connect() as connection:
            connection.execute(
                """UPDATE track_cue_points SET
                       automatic_cue_in = NULL,
                       automatic_cue_out = NULL,
                       automatic_fade_duration = NULL,
                       minimum_level_dbfs = NULL,
                       maximum_level_dbfs = NULL,
                       peak = NULL,
                       measured_window_count = NULL,
                       confidence = NULL,
                       analysis_version = NULL,
                       analysed_at = NULL,
                       analysis_backend = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE track_id = ?""",
                (track_id,),
            )


class CuePointService:
    """Resolve manual, automatic, and file-boundary values in one place."""

    DURATION_TOLERANCE = 1.0

    def __init__(
        self,
        repository: CuePointRepository,
        global_fade_duration: float = 7.0,
        minimum_fade_duration: float = 0.5,
        minimum_playable_duration: float = 5.0,
        short_track_threshold: float = 30.0,
        short_track_policy: ShortTrackPolicy = ShortTrackPolicy.ALLOW,
        on_global_cues_changed: Callable[[int], None] | None = None,
    ) -> None:
        self.repository = repository
        self.global_fade_duration = global_fade_duration
        self.minimum_fade_duration = minimum_fade_duration
        self.minimum_playable_duration = minimum_playable_duration
        self.short_track_threshold = max(minimum_playable_duration, short_track_threshold)
        self.short_track_policy = short_track_policy
        self._on_global_cues_changed = on_global_cues_changed
        self._logger = logging.getLogger(__name__)

    def get(self, track_id: int) -> TrackCuePoints:
        return self.repository.get(track_id)

    def manual_track_ids(self, track_ids: list[int]) -> set[int]:
        return self.repository.manual_track_ids(track_ids)

    def save_manual(
        self,
        track: Track,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
    ) -> None:
        self.validate_values(track, cue_in, cue_out, fade_duration)
        self.repository.save_manual(track.id, cue_in, cue_out, fade_duration)
        self._notify_global_cues_changed(track.id)

    def save_automatic(self, track: Track, result: CueAnalysisResult) -> None:
        self.validate_values(
            track,
            result.cue_in,
            result.cue_out,
            result.suggested_fade_duration,
        )
        self.repository.save_automatic(track.id, result)
        self._notify_global_cues_changed(track.id)

    def clear_automatic(self, track_id: int) -> None:
        self.repository.clear_automatic(track_id)
        self._notify_global_cues_changed(track_id)

    def save_editor(
        self,
        track: Track,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
        *,
        discard_automatic: bool,
        changed_fields: frozenset[str] | None = None,
    ) -> None:
        self.validate_values(track, cue_in, cue_out, fade_duration)
        self.repository.save_editor(
            track.id,
            cue_in,
            cue_out,
            fade_duration,
            discard_automatic=discard_automatic,
            changed_fields=changed_fields,
        )
        self._notify_global_cues_changed(track.id)

    def _notify_global_cues_changed(self, track_id: int) -> None:
        if self._on_global_cues_changed is not None:
            self._on_global_cues_changed(track_id)

    def validate_values(
        self,
        track: Track,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
    ) -> None:
        duration = max(0.0, track.duration_seconds or 0.0)
        if cue_in is not None and (cue_in < 0 or cue_in >= duration):
            raise ValueError("Der Startpunkt liegt außerhalb des Titels.")
        effective_in = cue_in or 0.0
        if cue_out is not None and (
            cue_out <= effective_in or cue_out > duration + self.DURATION_TOLERANCE
        ):
            raise ValueError(
                "Der Endpunkt muss nach dem Startpunkt und innerhalb des Titels liegen."
            )
        effective_out = min(duration, cue_out) if cue_out is not None else duration
        if fade_duration is not None and (
            fade_duration < 0 or fade_duration > effective_out - effective_in
        ):
            raise ValueError("Die Überblenddauer darf nicht länger als der nutzbare Titel sein.")

    def resolve(
        self,
        track: Track,
        global_fade_duration: float | None = None,
        queue_entry: QueueEntry | None = None,
    ) -> ResolvedTrackBoundaries:
        cue = self.repository.get(track.id)
        duration = max(0.0, track.duration_seconds or 0.0)
        warnings: list[str] = []
        cue_in, in_source = self._resolve_value(cue.manual_cue_in, cue.automatic_cue_in, 0.0)
        cue_out, out_source = self._resolve_value(
            cue.manual_cue_out, cue.automatic_cue_out, duration
        )
        queue_source = self._queue_source(queue_entry)
        if queue_entry is not None and queue_entry.cue_in_override is not None:
            cue_in, in_source = queue_entry.cue_in_override, queue_source
        if queue_entry is not None and queue_entry.cue_out_override is not None:
            cue_out, out_source = queue_entry.cue_out_override, queue_source
        if cue_in < 0 or cue_in >= duration:
            self._logger.warning("Ungültiger Cue In für Titel %s; Dateianfang verwendet", track.id)
            warnings.append("Cue In ist ungültig; der Dateianfang wird verwendet")
            cue_in, in_source = 0.0, "FILE_BOUNDARY"
        if cue_out <= cue_in or cue_out > duration + self.DURATION_TOLERANCE:
            self._logger.warning("Ungültiger Cue Out für Titel %s; Dateiende verwendet", track.id)
            warnings.append("Cue Out ist ungültig; das Dateiende wird verwendet")
            cue_out, out_source = duration, "FILE_BOUNDARY"
        cue_out = min(cue_out, duration)
        fallback_fade = global_fade_duration or self.global_fade_duration
        fade, fade_source = self._resolve_value(
            cue.manual_fade_duration, cue.automatic_fade_duration, fallback_fade
        )
        if queue_entry is not None and queue_entry.fade_duration_override is not None:
            fade, fade_source = queue_entry.fade_duration_override, queue_source
        usable = max(0.0, cue_out - cue_in)
        if (
            usable < self.short_track_threshold
            and self.short_track_policy is ShortTrackPolicy.USE_REDUCED_FADE
        ):
            fade = min(
                fade,
                max(self.minimum_fade_duration, usable * 0.25),
                usable,
            )
            fade_source = "SHORT_TRACK_POLICY"
        if fade < 0:
            self._logger.warning(
                "Ungültige Überblenddauer für Titel %s; Standard verwendet", track.id
            )
            warnings.append("Die Überblenddauer ist ungültig; der Standardwert wird verwendet")
            fade, fade_source = fallback_fade, "GLOBAL"
        fade = min(fade, usable)
        allowed = True
        if usable < self.minimum_playable_duration:
            allowed = False
            warnings.append(
                f"Nutzbare Titellänge {usable:.1f} s unterschreitet das Minimum "
                f"von {self.minimum_playable_duration:.1f} s"
            )
        elif fade < self.minimum_fade_duration:
            allowed = False
            warnings.append(
                f"Überblenddauer {fade:.1f} s unterschreitet das Minimum "
                f"von {self.minimum_fade_duration:.1f} s"
            )
        return ResolvedTrackBoundaries(
            cue_in,
            cue_out,
            fade,
            in_source,
            out_source,
            fade_source,
            allowed,
            "; ".join(warnings),
        )

    @staticmethod
    def _resolve_value(
        manual: float | None, automatic: float | None, fallback: float
    ) -> tuple[float, str]:
        if manual is not None:
            return manual, "MANUAL"
        if automatic is not None:
            return automatic, "AUTOMATIC"
        return fallback, "FILE_BOUNDARY"

    @staticmethod
    def _queue_source(queue_entry: QueueEntry | None) -> str:
        if queue_entry is not None and queue_entry.cue_override_source == "snapshot":
            return "QUEUE_SNAPSHOT"
        return "QUEUE_OVERRIDE"
