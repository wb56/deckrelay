"""Programmatic single and serial batch operations for productive metadata analysis."""

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from time import monotonic, sleep

from party_player.database.connection import Database
from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisRequest,
    MetadataAnalysisResult,
    TempoAnalysisRangeSnapshot,
    TempoAnalysisScope,
)
from party_player.tempo_context import resolved_now, tempo_range_signature
from party_player.tempo_context import (
    SavedQueueManualTempo,
    TempoAnalysisContextResolver,
    TempoContextRepository,
    TempoResolution,
    TempoResolver,
)
from party_player.cue_points import CuePointService
from party_player.models import SavedQueueEntry, Track
from party_player.metadata_analysis_coordinator import (
    AnalysisOperatingState,
    MetadataAnalysisCoordinator,
)
from party_player.metadata_analysis_persistence import (
    SqliteAnalysisResultPersistencePort,
    SqliteAnalysisRunPersistencePort,
)
from party_player.metadata_analysis_profiles import (
    ALGORITHM_VERSION,
    HIGH_CONFIDENCE,
    MINIMUM_SUGGESTION_CONFIDENCE,
    PROFILE_CONFIGURATIONS,
    TEMPO_CHANGE_STABILITY,
    MetadataAnalysisProfile,
)
from party_player.metadata_analysis_supervisor import MetadataAnalysisProcessSupervisor
from party_player.repositories.track_repository import TrackRepository
from party_player.restore_lifecycle import PersistenceParticipant
from party_player.worker_diagnostics import WorkerRegistry
from party_player.analysis import (
    AnalysisBackendUnavailableError,
    AudioFileInfo,
    FfmpegAudioAnalysisBackend,
)
from party_player.technical_audio_info import TechnicalAudioInfoService


@dataclass(slots=True)
class MetadataAnalysisDiagnostics:
    started_runs: int = 0
    completed_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    runs_without_bpm: int = 0
    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0
    half_double_warnings: int = 0
    tempo_changes: int = 0
    snapshot_conflicts: int = 0
    timeouts: int = 0
    cancellations: int = 0
    worker_crashes: int = 0
    total_duration_seconds: float = 0.0
    maximum_duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class TempoAnalysisView:
    track_id: int
    run_id: int | None
    status: str
    profile: str
    algorithm_version: str
    backend: str
    finished_at: str
    bpm: float | None
    alternative_bpm: float | None
    bpm_confidence: float | None
    rhythm_stability: float | None
    experimental_energy: int | None
    energy_confidence: float | None
    warnings: tuple[str, ...]
    error_text: str
    scope: TempoAnalysisScope = TempoAnalysisScope.TRACK_FULL
    range_signature: str = ""
    current: bool = True
    cue_in: float | None = None
    cue_out: float | None = None
    fade_duration: float | None = None


@dataclass(frozen=True, slots=True)
class TempoBatchPreview:
    track_ids: tuple[int, ...]
    selected: int
    current: int
    planned: int
    missing_files: int
    open_suggestions: int
    estimated_seconds: float | None
    outdated: int = 0
    invalid_cues: int = 0


@dataclass(frozen=True, slots=True)
class TempoBatchProgress:
    total: int
    completed: int
    successful: int
    without_bpm: int
    review_required: int
    failed: int
    skipped: int
    cancelled: int
    current_track_id: int | None
    current_title: str
    state: str
    reason: str
    estimated_remaining_seconds: float | None


@dataclass(frozen=True, slots=True)
class SavedQueueTempoView:
    entry_id: int
    track_id: int
    title: str
    analysis: TempoAnalysisView
    manual: SavedQueueManualTempo | None
    resolution: TempoResolution
    cue_in: float
    cue_out: float
    fade_duration: float
    inherited_cues: bool
    range_signature: str


_ANALYSIS_ERROR_TEXTS = {
    "BACKEND_UNAVAILABLE": "FFmpeg oder FFprobe ist nicht verfügbar.",
    "BACKEND_NOT_FOUND": "FFmpeg oder FFprobe ist nicht verfügbar.",
    "FILE_MISSING": "Die Musikdatei fehlt oder ist nicht erreichbar.",
    "FILE_CHANGED": "Die Musikdatei wurde seit Auftragserstellung verändert.",
    "UNSUPPORTED_FORMAT": "Dieses Dateiformat wird von der Tempoanalyse nicht unterstützt.",
    "WORKER_CRASHED": "Der Analyseprozess wurde unerwartet beendet.",
    "TIMEOUT": "Die Tempoanalyse hat das Zeitlimit überschritten.",
    "CANCELLED": "Die Tempoanalyse wurde abgebrochen.",
    "BATCH_CANCELLED": "Der wartende Analyseauftrag wurde abgebrochen.",
    "RESULT_REJECTED": "Das Analyseergebnis war nicht mehr gültig und wurde verworfen.",
}


class _OperatingStatePort:
    def __init__(self, provider: Callable[[], AnalysisOperatingState]) -> None:
        self._provider = provider

    def snapshot(self) -> AnalysisOperatingState:
        return self._provider()


class _ProgressPort:
    def __init__(self, publish: Callable[[str, str, str], None]) -> None:
        self._publish = publish

    def publish(self, event: str, job_id: str, detail: str = "") -> None:
        self._publish(event, job_id, detail[:120])


class MetadataAnalysisService:
    """Own the productive coordinator without starting work implicitly."""

    def __init__(
        self,
        database: Database,
        tracks: TrackRepository,
        *,
        ffmpeg: Path | None,
        ffprobe: Path | None,
        operating_state: Callable[[], AnalysisOperatingState],
        publish_progress: Callable[[str, str, str], None] = lambda *_args: None,
        worker_registry: WorkerRegistry | None = None,
        cue_points: CuePointService | None = None,
    ) -> None:
        self._database = database
        self._tracks = tracks
        self._cue_points = cue_points
        self._tempo_context = TempoContextRepository(database)
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._technical_audio_info = (
            TechnicalAudioInfoService(
                FfmpegAudioAnalysisBackend(str(ffmpeg or "ffmpeg"), str(ffprobe))
            )
            if ffprobe is not None
            else None
        )
        self._runs = SqliteAnalysisRunPersistencePort(database)
        self._supervisor = MetadataAnalysisProcessSupervisor(worker_registry)
        self._coordinator = MetadataAnalysisCoordinator(
            self._supervisor,
            self._runs,
            SqliteAnalysisResultPersistencePort(database),
            _OperatingStatePort(operating_state),
            _ProgressPort(publish_progress),
        )
        self._closed = False
        self._accepting = True
        self._started: dict[str, float] = {}
        self._global_batch_job_ids: frozenset[str] = frozenset()
        self._global_batch_run_ids: tuple[int, ...] = ()
        self.diagnostics = MetadataAnalysisDiagnostics()
        self.interrupted_on_start = self._coordinator.recover_interrupted_runs()

    @property
    def available(self) -> bool:
        return bool(
            self._ffmpeg is not None
            and self._ffprobe is not None
            and self._ffmpeg.is_file()
            and self._ffprobe.is_file()
        )

    @property
    def active_job_count(self) -> int:
        return int(self._coordinator.current_job is not None)

    def technical_audio_info(self, track_id: int) -> AudioFileInfo:
        """Probe one catalog file through the established FFprobe backend."""
        track = self._tracks.get_active(track_id)
        if track is None:
            raise KeyError(f"Katalogtitel {track_id} wurde nicht gefunden")
        if self._technical_audio_info is None:
            raise AnalysisBackendUnavailableError("FFprobe ist nicht verfügbar")
        return self._technical_audio_info.probe(Path(track.file_path))

    def analyze_track(
        self,
        track_id: int,
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
        *,
        batch: bool = False,
        scope: TempoAnalysisScope = TempoAnalysisScope.TRACK_FULL,
        analysis_range: TempoAnalysisRangeSnapshot | None = None,
    ) -> MetadataAnalysisJob:
        if self._closed or not self._accepting:
            raise RuntimeError("Metadatenanalyse ist geschlossen")
        reason = self.block_reason(batch=batch)
        if reason and not batch:
            raise RuntimeError(reason)
        track = self._tracks.get_active(track_id)
        if track is None:
            raise KeyError(f"Katalogtitel {track_id} wurde nicht gefunden")
        path = Path(track.file_path).resolve()
        snapshot = FileSnapshot.capture(str(path))
        configuration = PROFILE_CONFIGURATIONS[profile]
        if analysis_range is None:
            duration = float(track.duration_seconds or 0.0)
            if scope is TempoAnalysisScope.TRACK_DEFAULT_CUES:
                if self._cue_points is None:
                    raise RuntimeError("Cue-Bereichsanalyse ist nicht eingerichtet")
                analysis_range = TempoAnalysisContextResolver(self._cue_points).track_default_cues(
                    track,
                    self._cue_revision(track_id),
                )
            elif duration <= 0:
                if scope is not TempoAnalysisScope.TRACK_FULL:
                    raise ValueError("Kontextanalyse benötigt eine bekannte Dateidauer")
            else:
                analysis_range = resolved_now(
                    0.0,
                    duration,
                    0.0,
                    duration,
                    "track-full",
                )
        signature = (
            tempo_range_signature(scope, track_id, snapshot, analysis_range, ALGORITHM_VERSION)
            if analysis_range is not None
            else ""
        )
        request = MetadataAnalysisRequest(
            track_id,
            snapshot,
            profile.value,
            ALGORITHM_VERSION,
            configuration.requested_kinds,
            timeout_seconds=configuration.timeout_seconds,
            backend=MetadataAnalysisBackendKind.FFMPEG_TEMPO,
            technical_options=self._technical_options(configuration.segment_strategy),
            scope=scope,
            analysis_range=analysis_range,
            range_signature=signature,
        )
        job = self._runs.create_job(request)
        if not self.available:
            result = self._unavailable_result(job, "FFmpeg oder FFprobe ist nicht verfügbar.")
            self._runs.finish(result)
            self._record_result(result)
            return job
        self._coordinator.enqueue(job, batch=batch)
        return job

    def _cue_revision(self, track_id: int) -> str:
        cue = self._cue_points.get(track_id) if self._cue_points is not None else None
        if cue is None:
            return "file-boundaries"
        values = (
            cue.manual_cue_in,
            cue.manual_cue_out,
            cue.manual_fade_duration,
            cue.automatic_cue_in,
            cue.automatic_cue_out,
            cue.automatic_fade_duration,
            cue.analysis_version,
            cue.analysed_at,
        )
        return "cue:" + "|".join("" if value is None else str(value) for value in values)

    def analyze_saved_queue_entry(
        self,
        saved_queue_entry_id: int,
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
    ) -> MetadataAnalysisJob:
        track, entry, area = self._resolve_saved_queue_entry(saved_queue_entry_id)
        return self.analyze_track(
            track.id,
            profile,
            scope=TempoAnalysisScope.SAVED_QUEUE_ENTRY,
            analysis_range=area,
        )

    def saved_queue_tempo_view(self, saved_queue_entry_id: int) -> SavedQueueTempoView:
        track, _entry, area = self._resolve_saved_queue_entry(saved_queue_entry_id)
        signature = tempo_range_signature(
            TempoAnalysisScope.SAVED_QUEUE_ENTRY,
            track.id,
            FileSnapshot.capture(track.file_path),
            area,
            ALGORITHM_VERSION,
        )
        analysis = self.latest_for_track(
            track.id,
            TempoAnalysisScope.SAVED_QUEUE_ENTRY,
            context_id=saved_queue_entry_id,
        )
        saved_value = self._tempo_context.current_value(
            track.id,
            TempoAnalysisScope.SAVED_QUEUE_ENTRY,
            context_id=saved_queue_entry_id,
            expected_signature=signature,
        )
        cue_value = self._tempo_context.current_value(
            track.id, TempoAnalysisScope.TRACK_DEFAULT_CUES
        )
        full_value = self._tempo_context.current_value(track.id, TempoAnalysisScope.TRACK_FULL)
        catalog = TempoResolver.catalog(track.bpm, cue_value, full_value)
        manual = self._tempo_context.manual_saved_queue_bpm(saved_queue_entry_id)
        resolution = TempoResolver.saved_queue(
            manual,
            saved_value,
            catalog,
            full_value,
            current_range_signature=signature,
        )
        return SavedQueueTempoView(
            saved_queue_entry_id,
            track.id,
            f"{track.artist} — {track.title}" if track.artist else track.title,
            analysis,
            manual,
            resolution,
            area.cue_in,
            area.cue_out,
            area.fade_duration,
            area.inherited_track_cues,
            signature,
        )

    def save_manual_saved_queue_bpm(
        self, saved_queue_entry_id: int, bpm: float
    ) -> SavedQueueManualTempo:
        view = self.saved_queue_tempo_view(saved_queue_entry_id)
        return self._tempo_context.save_manual_saved_queue_bpm(
            saved_queue_entry_id,
            bpm,
            based_on_signature=view.range_signature,
        )

    def reset_manual_saved_queue_bpm(self, saved_queue_entry_id: int) -> None:
        self._tempo_context.reset_manual_saved_queue_bpm(saved_queue_entry_id)

    def _resolve_saved_queue_entry(
        self, saved_queue_entry_id: int
    ) -> tuple[Track, SavedQueueEntry, TempoAnalysisRangeSnapshot]:
        if self._cue_points is None:
            raise RuntimeError("Cue-Auflösung ist nicht verfügbar")
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT track_id,position,cue_in,cue_out,fade_duration,cue_source
                   FROM saved_queue_entries WHERE id=?""",
                (saved_queue_entry_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Playlist-Eintrag wurde nicht gefunden")
        track = self._tracks.get_active(int(row["track_id"]))
        if track is None:
            raise ValueError("Katalogtitel wurde nicht gefunden")
        entry = SavedQueueEntry(
            track.id,
            int(row["position"]),
            row["cue_in"],
            row["cue_out"],
            row["fade_duration"],
            str(row["cue_source"]),
            saved_queue_entry_id,
        )
        area = TempoAnalysisContextResolver(self._cue_points).saved_queue_entry(
            track,
            entry,
            "saved-entry:"
            + "|".join(
                "" if value is None else str(value)
                for value in (entry.cue_in, entry.cue_out, entry.fade_duration, entry.cue_source)
            ),
        )
        return track, entry, area

    def analyze_selected(
        self,
        track_ids: Iterable[int],
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
        *,
        scope: TempoAnalysisScope = TempoAnalysisScope.TRACK_FULL,
    ) -> tuple[MetadataAnalysisJob, ...]:
        jobs = tuple(
            self.analyze_track(track_id, profile, batch=True, scope=scope) for track_id in track_ids
        )
        self._global_batch_job_ids = frozenset(job.job_id for job in jobs)
        self._global_batch_run_ids = tuple(job.run_id for job in jobs)
        return jobs

    def global_batch_progress(self, job_id: str) -> TempoBatchProgress | None:
        """Return progress only for the explicitly started visible batch."""
        if job_id not in self._global_batch_job_ids or not self._global_batch_run_ids:
            return None
        return self.batch_progress(self._global_batch_run_ids)

    def analyze_without_current_bpm_suggestion(
        self,
        *,
        limit: int = 1000,
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
    ) -> tuple[MetadataAnalysisJob, ...]:
        ids = self._candidate_ids(
            """NOT EXISTS (
                   SELECT 1 FROM track_metadata_suggestions s
                   WHERE s.track_id=t.id AND s.field_key='bpm' AND s.status='PENDING'
               )""",
            limit,
        )
        return self.analyze_selected(ids, profile)

    def analyze_outdated_version(
        self,
        *,
        limit: int = 1000,
        profile: MetadataAnalysisProfile = MetadataAnalysisProfile.TEMPO,
    ) -> tuple[MetadataAnalysisJob, ...]:
        ids = self._candidate_ids(
            """EXISTS (
                   SELECT 1 FROM metadata_analysis_runs r
                   WHERE r.track_id=t.id AND r.analysis_profile=?
                     AND r.analysis_version<>?
               ) AND NOT EXISTS (
                   SELECT 1 FROM metadata_analysis_runs current
                   WHERE current.track_id=t.id AND current.analysis_profile=?
                     AND current.analysis_version=? AND current.status='COMPLETED'
               )""",
            limit,
            (profile.value, ALGORITHM_VERSION, profile.value, ALGORITHM_VERSION),
        )
        return self.analyze_selected(ids, profile)

    def tick(self) -> MetadataAnalysisResult | None:
        before = self._coordinator.current_job
        result = self._coordinator.tick()
        current = self._coordinator.current_job
        if before is None and current is not None:
            self._started[current.job_id] = monotonic()
            self.diagnostics.started_runs += 1
        if result is not None:
            self._record_result(result)
        return result

    def pause(self) -> None:
        self._coordinator.pause()

    def resume_persistent_pending(self, *, limit: int = 1000) -> int:
        """Explicitly resume retained PENDING runs; never called automatically at startup."""
        if not self.available or self._ffmpeg is None or self._ffprobe is None:
            return 0
        jobs = self._runs.pending_jobs(self._ffmpeg, self._ffprobe, limit=limit)
        for job in jobs:
            self._coordinator.enqueue(job, batch=True)
        return len(jobs)

    def discard_persistent_pending(self) -> int:
        if self._coordinator.pending_count or self._coordinator.current_job is not None:
            raise RuntimeError(
                "Aktive Analyseaufträge können nicht als Altbestand verworfen werden."
            )
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE metadata_analysis_runs
                   SET status='CANCELLED',finished_at=CURRENT_TIMESTAMP,
                       error_code='USER_DISCARDED',
                       error_text='Wartender Analyseauftrag wurde verworfen.'
                   WHERE status='PENDING'"""
            )
        return max(0, cursor.rowcount)

    def resume(self) -> None:
        self._coordinator.resume()

    def cancel_all(self) -> tuple[MetadataAnalysisResult, ...]:
        results = list(self._coordinator.cancel_pending())
        current = self.cancel_current()
        if current is not None:
            results.append(current)
        return tuple(results)

    def cancel_current(self) -> MetadataAnalysisResult | None:
        result = self._coordinator.cancel_current()
        if result is not None:
            self._record_result(result)
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        self._coordinator.close()

    def restore_participant(self) -> PersistenceParticipant:
        def block() -> bool:
            self._accepting = False
            self.pause()
            return True

        def drain(timeout: float) -> bool:
            deadline = monotonic() + max(0.0, timeout)
            while self._coordinator.current_job is not None and monotonic() < deadline:
                self.tick()
                sleep(0.02)
            return self._coordinator.current_job is None

        def resume() -> bool:
            if self._closed:
                return False
            self._accepting = True
            self.resume()
            return True

        return PersistenceParticipant(
            "metadata-analysis",
            block,
            drain,
            lambda: True,
            resume,
        )

    def support_snapshot(self) -> dict[str, object]:
        """Return bounded diagnostics without file paths or worker PID."""
        values = asdict(self.diagnostics)
        values["average_duration_seconds"] = (
            self.diagnostics.total_duration_seconds / self.diagnostics.completed_runs
            if self.diagnostics.completed_runs
            else 0.0
        )
        with self._database.connect() as connection:
            waiting = connection.execute(
                "SELECT COUNT(*) FROM metadata_analysis_runs WHERE status='PENDING'"
            ).fetchone()[0]
        values["waiting_runs"] = int(waiting)
        values["active_profile"] = (
            self._coordinator.current_job.analysis_profile
            if self._coordinator.current_job is not None
            else ""
        )
        values["backend"] = "ffmpeg-onset-autocorrelation"
        values["algorithm_version"] = ALGORITHM_VERSION
        values["coordinator_state"] = self._coordinator.state.value
        values["block_reason"] = self.block_reason(batch=True)
        with self._database.connect() as connection:
            latest = connection.execute(
                """SELECT id,track_id,status,analysis_profile FROM metadata_analysis_runs
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        if latest is not None:
            view = self.latest_for_track(int(latest["track_id"]))
            confidence = view.bpm_confidence
            values["latest_run_id"] = int(latest["id"])
            values["latest_profile"] = str(latest["analysis_profile"])
            values["latest_status"] = str(latest["status"])
            values["latest_confidence_class"] = (
                "HIGH"
                if confidence is not None and confidence >= HIGH_CONFIDENCE
                else (
                    "MEDIUM"
                    if confidence is not None and confidence >= MINIMUM_SUGGESTION_CONFIDENCE
                    else "LOW"
                )
            )
            values["latest_warnings"] = view.warnings[:8]
        return values

    def latest_for_track(
        self,
        track_id: int,
        scope: TempoAnalysisScope | None = None,
        *,
        context_id: int | None = None,
    ) -> TempoAnalysisView:
        scope_filter = " AND scope_type=?" if scope is not None else ""
        parameters: tuple[object, ...] = (
            (track_id, scope.value) if scope is not None else (track_id,)
        )
        with self._database.connect() as connection:
            run = connection.execute(
                f"""SELECT id,analysis_profile,analysis_version,status,finished_at,
                           error_code,error_text,scope_type,range_signature,cue_in_ms,
                           cue_out_ms,fade_ms
                    FROM metadata_analysis_runs WHERE track_id=?{scope_filter}
                    ORDER BY id DESC LIMIT 1""",
                parameters,
            ).fetchone()
            if run is None:
                return TempoAnalysisView(
                    track_id,
                    None,
                    "NOT_ANALYSED",
                    "",
                    ALGORITHM_VERSION,
                    "ffmpeg-onset-autocorrelation",
                    "",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    (),
                    "",
                    scope or TempoAnalysisScope.TRACK_FULL,
                )
            metrics = {
                str(row["metric_key"]): float(row["metric_value"])
                for row in connection.execute(
                    """SELECT metric_key,metric_value FROM metadata_analysis_run_metrics
                       WHERE run_id=?""",
                    (int(run["id"]),),
                ).fetchall()
            }
            suggestions = {
                str(row["field_key"]): (
                    float(row["serialized_value"]),
                    float(row["confidence"]),
                )
                for row in connection.execute(
                    """SELECT field_key,serialized_value,confidence
                       FROM track_metadata_suggestions
                       WHERE analysis_run_id=? AND status='PENDING'""",
                    (int(run["id"]),),
                ).fetchall()
                if str(row["field_key"]) in {"bpm", "alternative_bpm", "energy"}
            }
            result_row = connection.execute(
                """SELECT primary_bpm,alternative_bpm,confidence,rhythm_stability,
                          experimental_energy,warnings_json,is_current,range_signature
                   FROM tempo_analysis_results WHERE run_id=?
                     AND (? IS NULL OR context_id IS ?)
                   ORDER BY id DESC LIMIT 1""",
                (int(run["id"]), context_id, context_id),
            ).fetchone()
            if result_row is not None:
                if result_row["primary_bpm"] is not None:
                    suggestions["bpm"] = (
                        float(result_row["primary_bpm"]),
                        float(result_row["confidence"] or 0.0),
                    )
                if result_row["alternative_bpm"] is not None:
                    suggestions["alternative_bpm"] = (
                        float(result_row["alternative_bpm"]),
                        float(result_row["confidence"] or 0.0) * 0.8,
                    )
                if result_row["experimental_energy"] is not None:
                    suggestions["energy"] = (
                        float(result_row["experimental_energy"]),
                        float(result_row["confidence"] or 0.0) * 0.7,
                    )
                if result_row["rhythm_stability"] is not None:
                    metrics["rhythm_stability"] = float(result_row["rhythm_stability"])
        bpm = suggestions.get("bpm")
        alternative = suggestions.get("alternative_bpm")
        energy = suggestions.get("energy")
        stability = metrics.get("rhythm_stability")
        warnings: list[str] = []
        if alternative is not None:
            warnings.append("Halb-/Doppeltempo-Alternative vorhanden.")
        if stability is not None and stability < TEMPO_CHANGE_STABILITY:
            warnings.append("Möglicher Tempowechsel oder instabiles Tempo.")
        if bpm is not None and bpm[1] < HIGH_CONFIDENCE:
            warnings.append("Prüfung erforderlich: mittlere Konfidenz.")
        if str(run["status"]) == "COMPLETED" and bpm is None:
            warnings.append("Kein belastbarer BPM-Vorschlag erzeugt.")
        return TempoAnalysisView(
            track_id,
            int(run["id"]),
            str(run["status"]),
            str(run["analysis_profile"]),
            str(run["analysis_version"]),
            "ffmpeg-onset-autocorrelation",
            str(run["finished_at"] or ""),
            bpm[0] if bpm else None,
            alternative[0] if alternative else None,
            bpm[1] if bpm else None,
            stability,
            round(energy[0]) if energy else None,
            energy[1] if energy else None,
            tuple(warnings),
            _ANALYSIS_ERROR_TEXTS.get(
                str(run["error_code"] or ""),
                str(run["error_text"] or run["error_code"] or ""),
            ),
            TempoAnalysisScope(str(run["scope_type"])),
            str(run["range_signature"] or ""),
            bool(result_row["is_current"]) if result_row is not None else True,
            int(run["cue_in_ms"]) / 1000.0 if run["cue_in_ms"] is not None else None,
            int(run["cue_out_ms"]) / 1000.0 if run["cue_out_ms"] is not None else None,
            int(run["fade_ms"]) / 1000.0 if run["fade_ms"] is not None else None,
        )

    def tempo_diagnostics_text(self, track_id: int) -> str:
        """Return the latest full/cue runs as copyable diagnostic text."""
        scopes = (TempoAnalysisScope.TRACK_FULL, TempoAnalysisScope.TRACK_DEFAULT_CUES)
        sections: list[str] = []
        with self._database.connect() as connection:
            for scope in scopes:
                row = connection.execute(
                    """SELECT id,created_at,finished_at,status,error_code,error_text,
                              analysis_profile,analysis_version,diagnostics_json
                       FROM metadata_analysis_runs
                       WHERE track_id=? AND scope_type=? ORDER BY id DESC LIMIT 1""",
                    (track_id, scope.value),
                ).fetchone()
                title = (
                    "Vollständige Aufnahme"
                    if scope is TempoAnalysisScope.TRACK_FULL
                    else "Wirksamer Cue-Bereich"
                )
                if row is None:
                    sections.append(f"{title}\nNicht analysiert")
                    continue
                status = str(row["status"])
                if status in {"PENDING", "RUNNING"}:
                    label = "Analyse wartet" if status == "PENDING" else "Analyse läuft"
                    sections.append(
                        f"{title}\n{label}\n"
                        f"Run-ID: {int(row['id'])}\n"
                        f"Profil: {str(row['analysis_profile'])}\n"
                        f"Algorithmus: {str(row['analysis_version'])}"
                    )
                    continue
                if status in {"FAILED", "CANCELLED"}:
                    label = (
                        "Analyse fehlgeschlagen" if status == "FAILED" else "Analyse abgebrochen"
                    )
                    reason = str(row["error_text"] or row["error_code"] or "Kein Grund angegeben")
                    sections.append(
                        f"{title}\n{label}\n"
                        f"Run-ID: {int(row['id'])}\n"
                        f"Profil: {str(row['analysis_profile'])}\n"
                        f"Algorithmus: {str(row['analysis_version'])}\n"
                        f"Grund: {reason}"
                    )
                    continue
                try:
                    details = json.loads(str(row["diagnostics_json"] or "{}"))
                except json.JSONDecodeError:
                    details = {"diagnostic_error": "Gespeicherte Diagnose ist ungültig"}
                legacy_score = False
                for segment in details.get("decoded_segments", ()):
                    if (
                        isinstance(segment, dict)
                        and "correlation_score" in segment
                        and "harmonic_quality_score" not in segment
                    ):
                        segment["harmonic_quality_score"] = segment.pop("correlation_score")
                        legacy_score = True
                if legacy_score:
                    details["compatibility_note"] = (
                        "Früheres Feld correlation_score wird als unbeschränkter "
                        "harmonic_quality_score dargestellt."
                    )
                if not details:
                    details = {
                        "diagnostic_status": (
                            "Für diesen abgeschlossenen älteren Lauf sind keine "
                            "Detaildaten gespeichert."
                        )
                    }
                header = {
                    "run_id": int(row["id"]),
                    "created_at": str(row["created_at"]),
                    "finished_at": str(row["finished_at"] or ""),
                    "profile": str(row["analysis_profile"]),
                    "algorithm_version": str(row["analysis_version"]),
                }
                sections.append(
                    f"{title}\n"
                    + json.dumps(
                        {"run": header, "diagnostics": details},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
        return (
            "Tempoanalyse-Diagnose\n"
            "Hinweis: Cue-Grenzen sind nicht mit den dekodierten Stichproben gleichzusetzen.\n\n"
            + "\n\n".join(sections)
        )

    def preview_tracks(
        self,
        track_ids: Iterable[int],
        *,
        skip_current: bool,
        scope: TempoAnalysisScope = TempoAnalysisScope.TRACK_FULL,
    ) -> TempoBatchPreview:
        ids = tuple(dict.fromkeys(int(value) for value in track_ids))
        current = missing = open_suggestions = outdated = invalid_cues = 0
        planned_ids: list[int] = []
        with self._database.connect() as connection:
            for track_id in ids:
                row = connection.execute(
                    "SELECT file_path FROM tracks WHERE id=? AND catalog_visible=1",
                    (track_id,),
                ).fetchone()
                if row is None or not Path(str(row["file_path"])).is_file():
                    missing += 1
                    continue
                track = self._tracks.get_active(track_id)
                if track is None:
                    missing += 1
                    continue
                try:
                    area: TempoAnalysisRangeSnapshot | None
                    if scope is TempoAnalysisScope.TRACK_DEFAULT_CUES:
                        if self._cue_points is None:
                            raise ValueError("Cue-Auflösung ist nicht verfügbar")
                        area = TempoAnalysisContextResolver(self._cue_points).track_default_cues(
                            track, self._cue_revision(track_id)
                        )
                    else:
                        duration = float(track.duration_seconds or 0.0)
                        area = (
                            resolved_now(0.0, duration, 0.0, duration, "track-full")
                            if duration > 0
                            else None
                        )
                    signature = (
                        tempo_range_signature(
                            scope,
                            track_id,
                            FileSnapshot.capture(str(row["file_path"])),
                            area,
                            ALGORITHM_VERSION,
                        )
                        if area is not None
                        else ""
                    )
                except (OSError, ValueError):
                    invalid_cues += 1
                    continue
                has_current = (
                    (
                        connection.execute(
                            """SELECT 1 FROM tempo_analysis_results
                               WHERE track_id=? AND scope_type=? AND range_signature=?
                                 AND is_current=1 LIMIT 1""",
                            (track_id, scope.value, signature),
                        ).fetchone()
                        is not None
                    )
                    if signature
                    else (
                        connection.execute(
                            """SELECT 1 FROM metadata_analysis_runs
                               WHERE track_id=? AND scope_type='TRACK_FULL'
                                 AND analysis_version=? AND status='COMPLETED' LIMIT 1""",
                            (track_id, ALGORITHM_VERSION),
                        ).fetchone()
                        is not None
                    )
                )
                current += int(has_current)
                outdated += int(
                    not has_current
                    and connection.execute(
                        """SELECT 1 FROM tempo_analysis_results
                           WHERE track_id=? AND scope_type=? LIMIT 1""",
                        (track_id, scope.value),
                    ).fetchone()
                    is not None
                )
                open_suggestions += int(
                    connection.execute(
                        """SELECT COUNT(*) FROM track_metadata_suggestions
                           WHERE track_id=? AND status='PENDING'""",
                        (track_id,),
                    ).fetchone()[0]
                )
                if not skip_current or not has_current:
                    planned_ids.append(track_id)
        average = (
            self.diagnostics.total_duration_seconds / self.diagnostics.completed_runs
            if self.diagnostics.completed_runs
            else 0.0
        )
        return TempoBatchPreview(
            tuple(planned_ids),
            len(ids),
            current,
            len(planned_ids),
            missing,
            open_suggestions,
            average * len(planned_ids) if average > 0 else None,
            outdated,
            invalid_cues,
        )

    def batch_progress(self, run_ids: Iterable[int]) -> TempoBatchProgress:
        ids = tuple(dict.fromkeys(int(value) for value in run_ids))
        if not ids:
            return TempoBatchProgress(0, 0, 0, 0, 0, 0, 0, 0, None, "", "IDLE", "", None)
        placeholders = ",".join("?" for _ in ids)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""SELECT r.id,r.track_id,r.status,r.error_code,t.title,t.artist,
                            EXISTS(SELECT 1 FROM track_metadata_suggestions s
                                   WHERE s.analysis_run_id=r.id AND s.field_key='bpm'
                                     AND s.status='PENDING') AS has_bpm,
                            EXISTS(SELECT 1 FROM track_metadata_suggestions s
                                   WHERE s.analysis_run_id=r.id AND s.field_key='bpm'
                                     AND s.status='PENDING' AND s.confidence<0.80) AS review
                     FROM metadata_analysis_runs r JOIN tracks t ON t.id=r.track_id
                     WHERE r.id IN ({placeholders})""",
                ids,
            ).fetchall()
        completed = sum(str(row["status"]) not in {"PENDING", "RUNNING"} for row in rows)
        successful = sum(str(row["status"]) == "COMPLETED" for row in rows)
        without_bpm = sum(str(row["status"]) == "COMPLETED" and not row["has_bpm"] for row in rows)
        review = sum(bool(row["review"]) for row in rows)
        failed = sum(str(row["status"]) == "FAILED" for row in rows)
        cancelled = sum(str(row["status"]) == "CANCELLED" for row in rows)
        active = next((row for row in rows if str(row["status"]) == "RUNNING"), None)
        state = self._coordinator.state.value
        reason = (
            "Vom Benutzer pausiert."
            if state == "PAUSED"
            else self.block_reason(batch=True) if active is None else ""
        )
        average = (
            self.diagnostics.total_duration_seconds / self.diagnostics.completed_runs
            if self.diagnostics.completed_runs and completed < len(ids)
            else 0.0
        )
        return TempoBatchProgress(
            len(ids),
            completed,
            successful,
            without_bpm,
            review,
            failed,
            0,
            cancelled,
            int(active["track_id"]) if active is not None else None,
            (f"{active['artist']} — {active['title']}" if active is not None else ""),
            state,
            reason,
            average * (len(ids) - completed) if average > 0 else None,
        )

    def block_reason(self, *, batch: bool) -> str:
        reason = self._coordinator.block_reason(batch=batch)
        return {
            None: "",
            "PRODUCTION_MODE": "Produktionsmodus ist aktiv.",
            "AUDIO_RECOVERY": "Audio-Wiederherstellung ist aktiv.",
            "DATABASE_MAINTENANCE": "Datenbankwartung ist aktiv.",
            "RESTORE_OR_MIGRATION": "Wiederherstellung oder Migration ist aktiv.",
            "AUTOMATION_ACTIVE": "Automatikbetrieb ist aktiv.",
            "PLAYBACK_ACTIVE": "Wiedergabe ist aktiv; währenddessen wird keine Audiodatei analysiert.",
        }[reason]

    def _technical_options(self, strategy: str) -> tuple[tuple[str, str], ...]:
        return (
            ("ffmpeg", str(self._ffmpeg) if self._ffmpeg is not None else "ffmpeg"),
            ("ffprobe", str(self._ffprobe) if self._ffprobe is not None else "ffprobe"),
            ("segment_strategy", strategy),
        )

    def _candidate_ids(
        self, condition: str, limit: int, parameters: tuple[object, ...] = ()
    ) -> tuple[int, ...]:
        bounded = max(1, min(int(limit), 10_000))
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""SELECT t.id FROM tracks t
                    WHERE t.catalog_visible=1 AND {condition}
                    ORDER BY t.id LIMIT ?""",
                (*parameters, bounded),
            ).fetchall()
        return tuple(int(row["id"]) for row in rows)

    def _record_result(self, result: MetadataAnalysisResult) -> None:
        self.diagnostics.completed_runs += 1
        duration = max(0.0, monotonic() - self._started.pop(result.job_id, monotonic()))
        self.diagnostics.total_duration_seconds += duration
        self.diagnostics.maximum_duration_seconds = max(
            self.diagnostics.maximum_duration_seconds, duration
        )
        if result.outcome in {
            MetadataAnalysisOutcome.SUCCESS,
            MetadataAnalysisOutcome.PARTIAL_SUCCESS,
        }:
            self.diagnostics.successful_runs += 1
        else:
            self.diagnostics.failed_runs += 1
        bpm = next((item for item in result.suggestions if item.field_key == "bpm"), None)
        if bpm is None:
            self.diagnostics.runs_without_bpm += 1
            self.diagnostics.low_confidence += 1
        elif bpm.confidence >= HIGH_CONFIDENCE:
            self.diagnostics.high_confidence += 1
        elif bpm.confidence >= MINIMUM_SUGGESTION_CONFIDENCE:
            self.diagnostics.medium_confidence += 1
        else:
            self.diagnostics.low_confidence += 1
        if result.rhythm_stability < TEMPO_CHANGE_STABILITY:
            self.diagnostics.tempo_changes += 1
        self.diagnostics.half_double_warnings += sum(
            "Halb-/Doppeltempo" in warning for warning in result.warnings
        )
        self.diagnostics.timeouts += int(result.outcome is MetadataAnalysisOutcome.TIMEOUT)
        self.diagnostics.cancellations += int(result.outcome is MetadataAnalysisOutcome.CANCELLED)
        self.diagnostics.worker_crashes += int(
            result.outcome is MetadataAnalysisOutcome.WORKER_CRASHED
        )
        self.diagnostics.snapshot_conflicts = self._coordinator.snapshot_conflicts

    @staticmethod
    def _unavailable_result(job: MetadataAnalysisJob, text: str) -> MetadataAnalysisResult:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        return MetadataAnalysisResult(
            job.job_id,
            job.run_id,
            job.track_id,
            job.input_snapshot,
            job.analysis_profile,
            job.analysis_version,
            now,
            now,
            MetadataAnalysisOutcome.BACKEND_UNAVAILABLE,
            error_code="BACKEND_UNAVAILABLE",
            error_text=text,
            backend_name="ffmpeg-onset-autocorrelation",
            backend_version=ALGORITHM_VERSION,
            scope=job.scope,
            analysis_range=job.analysis_range,
            range_signature=job.range_signature,
        )
