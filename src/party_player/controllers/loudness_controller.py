"""Controller for manual loudness editing, independent from playback orchestration."""

from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass

from party_player.analysis.loudness_backend import LoudnessAnalysisResult
from party_player.analysis.loudness_service import (
    LoudnessAnalysisJob,
    OfflineLoudnessAnalysisService,
)
from party_player.restore_lifecycle import PersistenceParticipant
from party_player.deck_controller import DeckController
from party_player.gui_event_dispatcher import GuiEvent, GuiEventDispatcher, GuiEventType
from party_player.loudness import (
    LoudnessService,
    ResolvedLoudnessSettings,
    TrackLoudness,
)
from party_player.services.library_service import LibraryService
from party_player.settings_service import SettingsService


@dataclass(frozen=True, slots=True)
class LoudnessEditorState:
    track_id: int
    title: str
    manual_gain_db: float | None
    resolved: ResolvedLoudnessSettings
    source_text: str
    clip_protection_text: str
    metadata_status_text: str = "Noch nicht geprüft"
    stored: TrackLoudness | None = None


@dataclass(frozen=True, slots=True)
class NormalizationSettingsState:
    enabled: bool
    clip_protection_enabled: bool
    mode: str
    target_loudness_lufs: float
    maximum_positive_gain_db: float
    maximum_negative_gain_db: float
    maximum_output_peak_db: float
    headroom_db: float
    fallback_positive_gain_db: float
    smoothing_seconds: float


class LoudnessController:
    """Validate edits and update only decks currently holding the changed track."""

    def __init__(
        self,
        service: LoudnessService,
        library: LibraryService,
        deck_a: DeckController,
        deck_b: DeckController,
        schedule: Callable[[int, Callable[[], None]], object],
        *,
        smoothing_seconds: float | None = None,
        settings_service: SettingsService | None = None,
        analysis_service: OfflineLoudnessAnalysisService | None = None,
        analysis_unavailable_reason: str | None = None,
        gui_dispatcher: GuiEventDispatcher | None = None,
    ) -> None:
        self._service = service
        self._library = library
        self._decks = (deck_a, deck_b)
        self._schedule = schedule
        self._settings = settings_service
        self._analysis_service = analysis_service
        self._analysis_unavailable_reason = analysis_unavailable_reason
        self._gui_dispatcher = gui_dispatcher
        self._batch_analysis_job: LoudnessAnalysisJob | None = None
        self._batch_analysis_running = False
        self._batch_analysis_cancelled = False
        self._smoothing_seconds = (
            settings_service.gain_smoothing_seconds()
            if smoothing_seconds is None and settings_service is not None
            else smoothing_seconds if smoothing_seconds is not None else 0.5
        )

    def state(self, track_id: int) -> LoudnessEditorState:
        track = self._library.get_track(track_id)
        if track is None:
            raise ValueError("Titel wurde nicht gefunden.")
        stored = self._service.get(track_id)
        title = f"{track.artist} — {track.title}" if track.artist else track.title
        resolved = self._service.resolve(track_id)
        return LoudnessEditorState(
            track.id,
            title,
            stored.manual_gain_db,
            resolved,
            self.source_text(resolved.source),
            ("Clip-Schutz aktiv" if resolved.peak_limited else "Clip-Schutz nicht aktiv"),
            self.metadata_status_text(stored.metadata_status),
            stored,
        )

    def save_manual_gain(self, track_id: int, gain_db: float | None) -> LoudnessEditorState:
        self.state(track_id)
        self._service.save_manual_gain(track_id, gain_db)
        state = self.state(track_id)
        for deck in self._decks:
            if deck.model.loaded_track is not None and deck.model.loaded_track.id == track_id:
                deck.smooth_resolved_loudness(
                    state.resolved,
                    self._smoothing_seconds,
                    self._schedule,
                )
        return state

    def analyze_track(
        self,
        track_id: int,
        completed: Callable[[LoudnessAnalysisResult | None, str | None], None],
    ) -> LoudnessAnalysisJob:
        """Start one analysis and report its result on the GUI thread."""
        service = self._require_analysis_service()
        track = self._library.get_track(track_id)
        if track is None:
            raise ValueError("Titel wurde nicht gefunden.")
        job = service.analyze(track)

        def worker_finished(future: Future[LoudnessAnalysisResult]) -> None:
            def apply_result() -> None:
                try:
                    completed(future.result(), None)
                except CancelledError:
                    completed(None, "Analyse wurde abgebrochen.")
                except Exception as exc:
                    completed(None, str(exc))

            self._publish_callback(apply_result, job.job_id)

        job.future.add_done_callback(worker_finished)
        return job

    def analyze_catalog(
        self,
        progress: Callable[[int, int, int, int], None],
        completed: Callable[[int, int], None],
        *,
        outdated_only: bool = False,
    ) -> None:
        """Analyze a catalog serially so the bounded worker queue never floods."""
        service = self._require_analysis_service()
        if self._batch_analysis_running:
            raise RuntimeError("Eine Lautheitsanalyse des Katalogs läuft bereits.")
        tracks = []
        offset = 0
        while True:
            page = self._library.page(self._library.MAX_PAGE_SIZE, offset)
            if not page:
                break
            tracks.extend(page)
            offset += len(page)
        if outdated_only:
            tracks = [track for track in tracks if service.needs_analysis(track.id)]
        if not tracks:
            raise ValueError(
                "Alle Katalogtitel besitzen bereits die aktuelle Lautheitsanalyse."
                if outdated_only
                else "Es wurden keine analysierbaren Katalogtitel gefunden."
            )
        self._batch_analysis_running = True
        self._batch_analysis_cancelled = False
        processed = succeeded = failed = 0
        total = len(tracks)

        def submit_next() -> None:
            nonlocal processed, succeeded, failed
            if self._batch_analysis_cancelled or processed >= total:
                self._batch_analysis_running = False
                self._batch_analysis_job = None
                completed(succeeded, failed)
                return
            job = service.analyze(tracks[processed])
            self._batch_analysis_job = job

            def worker_finished(future: Future[LoudnessAnalysisResult]) -> None:
                def apply_result() -> None:
                    nonlocal processed, succeeded, failed
                    try:
                        future.result()
                        succeeded += 1
                    except CancelledError:
                        pass
                    except Exception:
                        if not job.cancellation_requested:
                            failed += 1
                    processed += 1
                    progress(processed, total, succeeded, failed)
                    submit_next()

                self._publish_callback(apply_result, job.job_id)

            job.future.add_done_callback(worker_finished)

        progress(0, total, 0, 0)
        submit_next()

    def cancel_batch_analysis(self) -> None:
        self._batch_analysis_cancelled = True
        job = self._batch_analysis_job
        if job is not None and not job.future.done():
            job.cancel()

    def close(self, *, wait: bool = True) -> None:
        self.cancel_batch_analysis()
        if self._analysis_service is not None:
            self._analysis_service.close(wait=wait)

    @property
    def active_analysis_job_count(self) -> int:
        return self._analysis_service.active_job_count if self._analysis_service else 0

    def restore_participant(self) -> PersistenceParticipant | None:
        if self._analysis_service is None:
            return None
        return self._analysis_service.restore_participant()

    def _require_analysis_service(self) -> OfflineLoudnessAnalysisService:
        if self._analysis_unavailable_reason:
            raise RuntimeError(self._analysis_unavailable_reason)
        if self._analysis_service is None:
            raise RuntimeError("Die EBU-R128-Lautheitsanalyse ist nicht verfügbar.")
        return self._analysis_service

    def analysis_availability(self) -> tuple[bool, str]:
        """Describe the capability gate without starting a process."""
        if self._analysis_unavailable_reason:
            return False, self._analysis_unavailable_reason
        if self._analysis_service is None:
            return False, "Die EBU-R128-Lautheitsanalyse ist nicht eingerichtet."
        return True, "FFmpeg ist für die Lautheitsanalyse verfügbar."

    def _publish_callback(self, callback: Callable[[], None], operation_id: str) -> None:
        if self._gui_dispatcher is None:
            self._schedule(0, callback)
            return
        self._gui_dispatcher.publish(
            GuiEvent(
                GuiEventType.CALLBACK,
                "loudness-analysis",
                callback,
                operation_id=operation_id,
            )
        )

    def update_normalization_settings(
        self,
        *,
        enabled: bool | None = None,
        clip_protection_enabled: bool | None = None,
        mode: str | None = None,
        target_loudness_lufs: float | None = None,
        maximum_positive_gain_db: float | None = None,
        maximum_negative_gain_db: float | None = None,
        maximum_output_peak_db: float | None = None,
        headroom_db: float | None = None,
        fallback_positive_gain_db: float | None = None,
        smoothing_seconds: float | None = None,
    ) -> None:
        """Persist settings and smoothly re-resolve every currently loaded deck."""
        if mode is not None and mode not in {"OFF", "TRACK", "ALBUM"}:
            raise ValueError("Unbekannter Normalisierungsmodus")
        if smoothing_seconds is not None and not 0.05 <= smoothing_seconds <= 10.0:
            raise ValueError("Die Gain-Glättung muss zwischen 0,05 und 10 Sekunden liegen.")
        ranges = (
            ("Ziel-Lautheit", target_loudness_lufs, -23.0, -10.0),
            ("Maximale positive Verstärkung", maximum_positive_gain_db, 0.0, 12.0),
            ("Maximale negative Verstärkung", maximum_negative_gain_db, -24.0, 0.0),
            ("Maximaler Ausgangspegel", maximum_output_peak_db, -6.0, 0.0),
            ("Headroom", headroom_db, 0.0, 6.0),
            ("Fallback-Verstärkung", fallback_positive_gain_db, 0.0, 6.0),
        )
        for label, value, minimum, maximum in ranges:
            if value is not None and not minimum <= value <= maximum:
                raise ValueError(f"{label} liegt außerhalb des gültigen Bereichs.")

        if enabled is not None:
            self._service.enabled = enabled
            if self._settings is not None:
                self._settings.set_normalization_enabled(enabled)
        if clip_protection_enabled is not None:
            self._service.clip_protection_enabled = clip_protection_enabled
            if self._settings is not None:
                self._settings.set_clip_protection_enabled(clip_protection_enabled)
        if mode is not None:
            self._service.mode = mode
            if self._settings is not None:
                self._settings.set_normalization_mode(mode)
        if target_loudness_lufs is not None:
            self._service.target_loudness_lufs = target_loudness_lufs
            if self._settings is not None:
                self._settings.set_target_loudness(target_loudness_lufs)
        setting_updates = (
            (
                "maximum_positive_gain_db",
                maximum_positive_gain_db,
                "set_maximum_positive_gain",
            ),
            (
                "maximum_negative_gain_db",
                maximum_negative_gain_db,
                "set_maximum_negative_gain",
            ),
            (
                "maximum_output_peak_db",
                maximum_output_peak_db,
                "set_maximum_output_peak",
            ),
            (
                "fallback_positive_gain_db",
                fallback_positive_gain_db,
                "set_fallback_positive_gain",
            ),
        )
        for attribute, value, setter_name in setting_updates:
            if value is None:
                continue
            setattr(self._service, attribute, value)
            if self._settings is not None:
                getattr(self._settings, setter_name)(value)
        if smoothing_seconds is not None:
            self._smoothing_seconds = smoothing_seconds
            if self._settings is not None:
                self._settings.set_gain_smoothing_seconds(smoothing_seconds)
        if headroom_db is not None:
            self._service.headroom_db = headroom_db
            if self._settings is not None:
                self._settings.set_headroom(headroom_db)

        for deck in self._decks:
            track = deck.model.loaded_track
            if track is not None:
                deck.smooth_resolved_loudness(
                    self._service.resolve(track.id),
                    self._smoothing_seconds,
                    self._schedule,
                )

    def settings_state(self) -> NormalizationSettingsState:
        return NormalizationSettingsState(
            self._service.enabled,
            self._service.clip_protection_enabled,
            self._service.mode,
            self._service.target_loudness_lufs,
            self._service.maximum_positive_gain_db,
            self._service.maximum_negative_gain_db,
            self._service.maximum_output_peak_db,
            self._service.headroom_db,
            self._service.fallback_positive_gain_db,
            self._smoothing_seconds,
        )

    @staticmethod
    def source_text(source: str) -> str:
        return {
            "MANUAL": "Manuell angepasst",
            "REPLAYGAIN_TAG": "ReplayGain",
            "ANALYSIS": "Eigene Analyse",
            "NONE": "Keine Anpassung",
        }.get(source, "Keine Anpassung")

    @staticmethod
    def metadata_status_text(status: str) -> str:
        return {
            "NOT_ANALYSED": "Noch nicht geprüft",
            "INCOMPLETE": "Metadaten unvollständig",
            "COMPLETE": "Metadaten vollständig",
            "FAILED": "Metadaten konnten nicht gelesen werden",
        }.get(status, "Status unbekannt")
