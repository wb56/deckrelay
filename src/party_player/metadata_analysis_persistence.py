"""Main-process run persistence adapter for metadata analysis package 6A."""

from datetime import datetime, timezone
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from party_player.database.connection import Database
from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisRequest,
    MetadataAnalysisResult,
    MetadataFieldSuggestion,
    TempoAnalysisRangeSnapshot,
    TempoAnalysisScope,
)
from party_player.tempo_context import cue_milliseconds
from party_player.metadata_analysis_profiles import (
    ALGORITHM_VERSION,
    HIGH_CONFIDENCE,
    PROFILE_CONFIGURATIONS,
    TEMPO_CHANGE_STABILITY,
    MetadataAnalysisProfile,
)
from party_player.metadata_persistence import (
    AnalysisRunRepository,
    AnalysisRunStatus,
    serialize_metadata_value,
)
from party_player.metadata_rules import MetadataFieldKey, MetadataReviewStatus


class SqliteAnalysisRunPersistencePort:
    """Persist run lifecycle in the main process; never pass it to a worker."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._runs = AnalysisRunRepository(database)

    def create_job(self, request: MetadataAnalysisRequest) -> MetadataAnalysisJob:
        snapshot = request.input_snapshot
        run = self._runs.create(
            request.track_id,
            request.analysis_profile,
            request.analysis_version,
            snapshot.normalized_path,
            snapshot.size,
            snapshot.modified_ns,
            priority=request.priority,
            fingerprint=snapshot.fingerprint,
        )
        analysis_range = request.analysis_range
        context_id = (
            analysis_range.saved_queue_entry_id
            if analysis_range is not None and request.scope is TempoAnalysisScope.SAVED_QUEUE_ENTRY
            else (
                analysis_range.party_queue_id
                if analysis_range is not None
                and request.scope is TempoAnalysisScope.PARTY_QUEUE_SNAPSHOT
                else None
            )
        )
        with self._database.connect() as connection:
            connection.execute(
                """UPDATE metadata_analysis_runs SET
                       scope_type=?,context_id=?,range_signature=?,cue_in_ms=?,cue_out_ms=?,
                       fade_ms=?,physical_duration_ms=?,context_revision=?,
                       inherited_track_cues=?,range_resolved_at=? WHERE id=?""",
                (
                    request.scope.value,
                    context_id,
                    request.range_signature,
                    cue_milliseconds(analysis_range.cue_in) if analysis_range else None,
                    cue_milliseconds(analysis_range.cue_out) if analysis_range else None,
                    cue_milliseconds(analysis_range.fade_duration) if analysis_range else None,
                    (
                        cue_milliseconds(analysis_range.physical_duration)
                        if analysis_range
                        else None
                    ),
                    analysis_range.context_revision if analysis_range else None,
                    int(analysis_range.inherited_track_cues) if analysis_range else 0,
                    analysis_range.resolved_at if analysis_range else None,
                    run.run_id,
                ),
            )
        return MetadataAnalysisJob(
            str(uuid4()),
            run.run_id,
            request.track_id,
            snapshot,
            request.analysis_profile,
            request.analysis_version,
            request.requested_kinds,
            request.priority,
            request.timeout_seconds,
            datetime.now(timezone.utc).isoformat(),
            request.backend,
            request.technical_options,
            request.scope,
            request.analysis_range,
            request.range_signature,
        )

    def recover_interrupted_runs(self) -> int:
        """Never treat a RUNNING row left by a prior process as successful."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE metadata_analysis_runs
                   SET status='FAILED', finished_at=CURRENT_TIMESTAMP,
                       error_code='PROCESS_INTERRUPTED',
                       error_text='Analyse wurde durch einen früheren Programmabbruch unterbrochen.'
                   WHERE status='RUNNING'"""
            )
        return max(0, cursor.rowcount)

    def mark_running(self, job: MetadataAnalysisJob) -> None:
        run = self._runs.start(job.run_id)
        if run.status is not AnalysisRunStatus.RUNNING:
            raise RuntimeError("Persistenter Analyseauftrag ist nicht mehr wartend")

    def finish(self, result: MetadataAnalysisResult) -> None:
        if result.outcome in {
            MetadataAnalysisOutcome.SUCCESS,
            MetadataAnalysisOutcome.PARTIAL_SUCCESS,
        }:
            status = AnalysisRunStatus.COMPLETED
        elif result.outcome is MetadataAnalysisOutcome.CANCELLED:
            status = AnalysisRunStatus.CANCELLED
        else:
            status = AnalysisRunStatus.FAILED
        self._runs.finish(
            result.run_id,
            status,
            error_code=result.error_code or None,
            error_text=result.error_text or None,
        )

    def pending_jobs(
        self, ffmpeg: Path, ffprobe: Path, *, limit: int = 1000
    ) -> tuple[MetadataAnalysisJob, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT id,track_id,analysis_profile,analysis_version,priority,
                          file_path_snapshot,file_size,file_modified_ns,fingerprint,attempt_count,
                          scope_type,context_id,range_signature,cue_in_ms,cue_out_ms,fade_ms,
                          physical_duration_ms,context_revision,inherited_track_cues,
                          range_resolved_at
                   FROM metadata_analysis_runs WHERE status='PENDING'
                   ORDER BY priority DESC,created_at,id LIMIT ?""",
                (max(1, min(limit, 10_000)),),
            ).fetchall()
        jobs = []
        for row in rows:
            try:
                profile = MetadataAnalysisProfile(str(row["analysis_profile"]))
            except ValueError:
                continue
            configuration = PROFILE_CONFIGURATIONS[profile]
            scope = TempoAnalysisScope(str(row["scope_type"]))
            analysis_range = (
                TempoAnalysisRangeSnapshot(
                    int(row["cue_in_ms"]) / 1000.0,
                    int(row["cue_out_ms"]) / 1000.0,
                    int(row["fade_ms"]) / 1000.0,
                    int(row["physical_duration_ms"]) / 1000.0,
                    str(row["range_resolved_at"]),
                    str(row["context_revision"]),
                    (
                        int(row["context_id"])
                        if scope is TempoAnalysisScope.SAVED_QUEUE_ENTRY
                        and row["context_id"] is not None
                        else None
                    ),
                    (
                        int(row["context_id"])
                        if scope is TempoAnalysisScope.PARTY_QUEUE_SNAPSHOT
                        and row["context_id"] is not None
                        else None
                    ),
                    bool(row["inherited_track_cues"]),
                )
                if row["cue_in_ms"] is not None
                else None
            )
            jobs.append(
                MetadataAnalysisJob(
                    f"resume-{int(row['id'])}-{int(row['attempt_count'])}",
                    int(row["id"]),
                    int(row["track_id"]),
                    FileSnapshot(
                        str(Path(str(row["file_path_snapshot"])).resolve()),
                        int(row["file_size"]),
                        int(row["file_modified_ns"]),
                        str(row["fingerprint"]) if row["fingerprint"] is not None else None,
                    ),
                    profile.value,
                    str(row["analysis_version"]),
                    configuration.requested_kinds,
                    int(row["priority"]),
                    configuration.timeout_seconds,
                    datetime.now(timezone.utc).isoformat(),
                    MetadataAnalysisBackendKind.FFMPEG_TEMPO,
                    (
                        ("ffmpeg", str(ffmpeg)),
                        ("ffprobe", str(ffprobe)),
                        ("segment_strategy", configuration.segment_strategy),
                    ),
                    scope,
                    analysis_range,
                    str(row["range_signature"]),
                )
            )
        return tuple(jobs)


class SqliteAnalysisResultPersistencePort:
    """Atomically persist valid suggestions, bounded metrics, ranges and run completion."""

    _FIELD_MAP = {
        "bpm": MetadataFieldKey.BPM,
        "alternative_bpm": MetadataFieldKey.ALTERNATIVE_BPM,
        "energy_experimental": MetadataFieldKey.ENERGY,
    }
    _METRIC_KEYS = frozenset(
        {
            "rms_mean",
            "rms_variability",
            "peak",
            "crest_factor",
            "transient_density",
            "bpm",
            "energy_experimental",
        }
    )

    def __init__(self, database: Database) -> None:
        self._database = database

    def persist_valid_result(self, result: MetadataAnalysisResult) -> None:
        if result.outcome not in {
            MetadataAnalysisOutcome.SUCCESS,
            MetadataAnalysisOutcome.PARTIAL_SUCCESS,
        }:
            raise ValueError("Nur gültige Erfolgsresultate dürfen persistiert werden")
        if (
            result.analysis_version != ALGORITHM_VERSION
            or result.backend_version != ALGORITHM_VERSION
        ):
            raise ValueError("Analyseversion stimmt nicht mit dem Produktivbackend überein")
        with self._database.transaction() as connection:
            run = connection.execute(
                """SELECT track_id,analysis_profile,analysis_version,status,
                          file_path_snapshot,file_size,file_modified_ns,fingerprint,
                          scope_type,range_signature
                   FROM metadata_analysis_runs WHERE id=?""",
                (result.run_id,),
            ).fetchone()
            if run is None or str(run["status"]) != "RUNNING":
                raise ValueError("Analyselauf ist nicht aktiv")
            snapshot = result.input_snapshot
            if (
                int(run["track_id"]) != result.track_id
                or str(run["analysis_profile"]) != result.analysis_profile
                or str(run["analysis_version"]) != result.analysis_version
                or str(run["file_path_snapshot"]) != snapshot.normalized_path
                or int(run["file_size"]) != snapshot.size
                or int(run["file_modified_ns"]) != snapshot.modified_ns
                or (str(run["fingerprint"]) if run["fingerprint"] is not None else None)
                != snapshot.fingerprint
                or str(run["scope_type"]) != result.scope.value
                or str(run["range_signature"]) != result.range_signature
            ):
                raise ValueError("Ergebnis gehört nicht zum aktiven Dateisnapshot")
            self._persist_ranges(connection, result)
            self._persist_metrics(connection, result)
            self._persist_diagnostics(connection, result)
            self._persist_context_result(connection, result)
            if result.scope in {
                TempoAnalysisScope.TRACK_FULL,
                TempoAnalysisScope.TRACK_DEFAULT_CUES,
            }:
                for suggestion in result.suggestions:
                    self._persist_suggestion(connection, result, suggestion)
            connection.execute(
                """UPDATE metadata_analysis_runs
                   SET status='COMPLETED', finished_at=CURRENT_TIMESTAMP,
                       error_code=NULL,error_text=NULL WHERE id=? AND status='RUNNING'""",
                (result.run_id,),
            )

    @staticmethod
    def _persist_diagnostics(
        connection: sqlite3.Connection, result: MetadataAnalysisResult
    ) -> None:
        area = result.analysis_range
        payload = {
            "job_id": result.job_id,
            "run_id": result.run_id,
            "scope": result.scope.value,
            "profile": result.analysis_profile,
            "algorithm_version": result.analysis_version,
            "backend": result.backend_name,
            "snapshot": {
                "path": result.input_snapshot.normalized_path,
                "size": result.input_snapshot.size,
                "modified_ns": result.input_snapshot.modified_ns,
                "fingerprint": result.input_snapshot.fingerprint,
            },
            "probed_duration_seconds": result.probed_duration_seconds,
            "catalog_duration_seconds": area.physical_duration if area is not None else None,
            "canonical_range": (
                {
                    "cue_in": area.cue_in,
                    "cue_out": area.cue_out,
                    "fade_duration": area.fade_duration,
                }
                if area is not None
                else None
            ),
            "decoded_segments": [asdict(item) for item in result.segment_diagnostics],
            "aggregated": {
                "bpm": result.aggregated_bpm,
                "alternative_bpm": result.aggregated_alternative_bpm,
                "confidence": result.aggregated_confidence,
                "rhythm_stability": result.rhythm_stability,
            },
            "thresholds_and_parameters": dict(result.effective_parameters),
            "confidence_components": dict(result.confidence_components),
            "decision_reasons": result.decision_reasons,
            "warnings": result.warnings,
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(serialized) > 50_000:
            raise ValueError("Tempo-Diagnose überschreitet die Speichergrenze")
        connection.execute(
            "UPDATE metadata_analysis_runs SET diagnostics_json=? WHERE id=?",
            (serialized, result.run_id),
        )

    def _persist_context_result(
        self, connection: sqlite3.Connection, result: MetadataAnalysisResult
    ) -> None:
        analysis_range = result.analysis_range
        if analysis_range is None or len(result.range_signature) != 64:
            return
        suggestions = {item.field_key: item for item in result.suggestions}
        metrics = {item.name: item.value for item in result.technical_metrics}
        bpm_item = suggestions.get("bpm")
        alternative_item = suggestions.get("alternative_bpm")
        context_id = (
            analysis_range.saved_queue_entry_id
            if result.scope is TempoAnalysisScope.SAVED_QUEUE_ENTRY
            else (
                analysis_range.party_queue_id
                if result.scope is TempoAnalysisScope.PARTY_QUEUE_SNAPSHOT
                else None
            )
        )
        connection.execute(
            """UPDATE tempo_analysis_results
               SET is_current=0,stale_reason='Durch neuere Bereichsanalyse abgelöst'
               WHERE track_id=? AND scope_type=? AND context_id IS ? AND is_current=1""",
            (result.track_id, result.scope.value, context_id),
        )
        connection.execute(
            """INSERT INTO tempo_analysis_results
                   (track_id,scope_type,context_id,run_id,range_signature,cue_in_ms,
                    cue_out_ms,fade_ms,physical_duration_ms,context_revision,
                    inherited_track_cues,primary_bpm,alternative_bpm,confidence,
                    rhythm_stability,warnings_json,experimental_energy,backend,
                    algorithm_version,analyzed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result.track_id,
                result.scope.value,
                context_id,
                result.run_id,
                result.range_signature,
                cue_milliseconds(analysis_range.cue_in),
                cue_milliseconds(analysis_range.cue_out),
                cue_milliseconds(analysis_range.fade_duration),
                cue_milliseconds(analysis_range.physical_duration),
                analysis_range.context_revision,
                int(analysis_range.inherited_track_cues),
                self._number(bpm_item) if bpm_item is not None else metrics.get("bpm"),
                self._number(alternative_item) if alternative_item is not None else None,
                bpm_item.confidence if bpm_item is not None else None,
                result.rhythm_stability,
                json.dumps(result.warnings, ensure_ascii=False),
                metrics.get("energy_experimental"),
                result.backend_name,
                result.backend_version,
                result.finished_at,
            ),
        )

    def _persist_ranges(
        self, connection: sqlite3.Connection, result: MetadataAnalysisResult
    ) -> None:
        if len(result.analyzed_ranges) > 8:
            raise ValueError("Zu viele Analysebereiche")
        for index, region in enumerate(result.analyzed_ranges):
            connection.execute(
                """INSERT INTO metadata_analysis_run_ranges
                       (run_id,range_index,start_seconds,duration_seconds)
                   VALUES (?,?,?,?)""",
                (result.run_id, index, region.start_seconds, region.duration_seconds),
            )

    def _persist_metrics(
        self, connection: sqlite3.Connection, result: MetadataAnalysisResult
    ) -> None:
        metrics = {item.name: (item.value, item.unit) for item in result.technical_metrics}
        if set(metrics) - self._METRIC_KEYS:
            raise ValueError("Unbekannter technischer Messwert")
        metrics["rhythm_stability"] = (result.rhythm_stability, "ratio")
        for suggestion in result.suggestions:
            if suggestion.field_key == "bpm":
                metrics["bpm"] = (self._number(suggestion), "BPM")
            elif suggestion.field_key == "energy_experimental":
                metrics["energy_experimental"] = (
                    self._number(suggestion),
                    "percent",
                )
        for key, (value, unit) in metrics.items():
            connection.execute(
                """INSERT INTO metadata_analysis_run_metrics
                       (run_id,metric_key,metric_value,unit,algorithm_version,experimental)
                   VALUES (?,?,?,?,?,?)""",
                (
                    result.run_id,
                    key,
                    value,
                    unit,
                    result.analysis_version,
                    int(key == "energy_experimental"),
                ),
            )

    def _persist_suggestion(
        self,
        connection: sqlite3.Connection,
        result: MetadataAnalysisResult,
        suggestion: MetadataFieldSuggestion,
    ) -> None:
        field_name = suggestion.field_key
        field_key = self._FIELD_MAP.get(field_name)
        if field_key is None:
            raise ValueError("Unbekannter Analysevorschlag")
        serialized = serialize_metadata_value(field_key, suggestion.canonical_value)
        scope_detail = (
            "catalog_cue_range"
            if result.scope is TempoAnalysisScope.TRACK_DEFAULT_CUES
            else "full_recording"
        )
        identical = connection.execute(
            """SELECT id FROM track_metadata_suggestions
               WHERE track_id=? AND field_key=? AND source_type='AUDIO_ANALYSIS'
                 AND source_detail LIKE ? AND serialized_value=? AND status='PENDING' LIMIT 1""",
            (result.track_id, field_key.value, f"{scope_detail};%", serialized),
        ).fetchone()
        if identical is not None:
            return
        connection.execute(
            """UPDATE track_metadata_suggestions
               SET status='SUPERSEDED',decided_at=CURRENT_TIMESTAMP,
                   decision_reason='Durch neuere Audioanalyse abgelöst'
               WHERE track_id=? AND field_key=? AND source_type='AUDIO_ANALYSIS'
                 AND source_detail LIKE ? AND status='PENDING'""",
            (result.track_id, field_key.value, f"{scope_detail};%"),
        )
        confidence = suggestion.confidence
        review = (
            MetadataReviewStatus.SUGGESTED
            if confidence >= HIGH_CONFIDENCE and result.rhythm_stability >= TEMPO_CHANGE_STABILITY
            else MetadataReviewStatus.REVIEW_REQUIRED
        )
        detail = (
            f"{scope_detail}; "
            + ("energy_experimental; " if field_name == "energy_experimental" else "")
            + result.backend_name
        )
        connection.execute(
            """INSERT INTO track_metadata_suggestions
                   (track_id,analysis_run_id,field_key,serialized_value,source_type,
                    source_detail,confidence,review_status)
               VALUES (?,?,?,?,'AUDIO_ANALYSIS',?,?,?)""",
            (
                result.track_id,
                result.run_id,
                field_key.value,
                serialized,
                detail[:200],
                confidence,
                review.value,
            ),
        )

    @staticmethod
    def _number(suggestion: MetadataFieldSuggestion) -> float:
        value = suggestion.canonical_value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Numerischer Analysevorschlag ist ungültig")
        return float(value)
