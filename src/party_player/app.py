"""Application composition root."""

import logging
from collections.abc import Callable
from datetime import datetime
from threading import Thread

from party_player.audio.vlc_backend import VlcAudioBackend
from party_player.audio.factory import VlcAudioBackendFactory
from party_player import __version__
from party_player.product import PRODUCT_NAME
from party_player.analysis import (
    CueAnalysisService,
    CueBoundaryEstimator,
    CueBoundarySettings,
    FfmpegAudioAnalysisBackend,
    SignalDetectionSettings,
)
from party_player.analysis.ebur128_backend import FfmpegEbur128Backend
from party_player.analysis.loudness_service import OfflineLoudnessAnalysisService
from party_player.controllers.main_controller import MainController
from party_player.controllers.cue_point_controller import CuePointController
from party_player.controllers.loudness_controller import LoudnessController
from party_player.controllers.overlay_controller import OverlayController
from party_player.ui.compact_deck_actions import bind_compact_decks
from party_player.core.logging_config import configure_logging
from party_player.core.paths import AppPaths
from party_player.crossfader_service import CrossfaderService
from party_player.ducking import DuckingController
from party_player.cue_points import CuePointRepository, CuePointService
from party_player.loudness import LoudnessRepository, LoudnessService
from party_player.replaygain_cache import ReplayGainCacheService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.deck_controller import DeckController
from party_player.playback_history_service import PlaybackHistoryService
from party_player.queue_service import QueueService
from party_player.track_selection import TrackSelectionService
from party_player.track_policy import PersistentTrackBlockService, TrackPolicyRepository
from party_player.artist_policy import (
    ArtistPolicyRepository,
    PersistentArtistBlockService,
)
from party_player.track_suitability import (
    TrackSuitabilityRepository,
    TrackSuitabilityService,
)
from party_player.repetition_policy import (
    PersistentRepetitionService,
    RepetitionHistoryRepository,
)
from party_player.enums import ShortTrackPolicy
from party_player.short_track_policy import ShortTrackSelectionRule
from party_player.automatic_selection import (
    AutomaticSelectionHistory,
    AutomaticSelectionService,
)
from party_player.enums import EmptyQueuePolicy
from party_player.file_availability import FileAvailabilityService
from party_player.emergency_playlist import (
    EmergencyMediaType,
    LocalEmergencyPlaylistService,
)
from party_player.emergency_storage import EmergencyStoragePolicy
from party_player.emergency_state import EmergencyStateService
from party_player.emergency_persistence import (
    EmergencyIncidentRepository,
    EmergencyPersistenceService,
)
from party_player.emergency_controller import EmergencyController
from party_player.emergency_playback import (
    EmergencyPlaybackResult,
    EmergencyPlaybackService,
)
from party_player.emergency_history import (
    EmergencyHistoryEntry,
    EmergencyHistoryRepository,
    EmergencyHistoryService,
)
from party_player.audio_recovery import AudioRecoveryService
from party_player.deck_health_monitor import DeckHealthMonitor
from party_player.repositories.track_repository import TrackRepository
from party_player.repositories.saved_queue_repository import SavedQueueRepository
from party_player.repositories.equalizer_repository import (
    EqualizerAssignmentRepository,
    EqualizerPresetRepository,
)
from party_player.equalizer_resolver import EqualizerResolver
from party_player.equalizer_transfer import EqualizerTransferService
from party_player.repository import PartyPlayerRepository
from party_player.models import Track
from party_player.services.library_service import LibraryService
from party_player.saved_queue_service import SavedQueueService
from party_player.tempo_context import TempoContextRepository
from party_player.metadata_analysis_contracts import TempoAnalysisScope
from party_player.session_service import PartySessionService
from party_player.settings_service import SettingsService
from party_player.dependency_locator import DependencyLocator
from party_player.dependency_validator import DependencyValidator
from party_player.system_dependency_service import SystemDependencyService
from party_player.first_run_controller import FirstRunController, FirstRunReason
from party_player.system_diagnostic_service import (
    SystemDiagnosticReport,
    SystemDiagnosticService,
)
from party_player.windows_audio_devices import WindowsAudioDeviceProvider
from party_player.network_source_check import NetworkSourceChecker
from party_player.diagnostic_export import DiagnosticReportExporter
from party_player.gui_event_dispatcher import GuiEventDispatcher
from party_player.gui_event_dispatcher import GuiEvent, GuiEventType
from party_player.gui_heartbeat_watchdog import GuiCallbackState
from party_player.performance_monitor import PerformanceMonitor, PerformanceSettings
from party_player.capability_snapshots import CapabilitySnapshotState
from party_player.worker_diagnostics import WorkerRegistry
from party_player.ui.main_window import MainWindow
from party_player.ui.first_run_dialog import FirstRunSetupDialog
from party_player.models import SavedQueueEntry
from party_player.overlay_player import OverlayAudioPlayer
from party_player.overlay import OverlayDefinition, OverlayPlayResult
from party_player.overlay_service import OverlayService
from party_player.repositories.overlay_repository import OverlayRepository
from party_player.restore_runtime import build_restore_runtime
from party_player.restore_safety import RestoreSafetyGate
from party_player.backup_restore_controller import BackupRestoreController
from party_player.playlist_transfer import PlaylistTransferService
from party_player.media_path_remap import MediaPathRemapService
from party_player.overlay_transfer import OverlayTransferService
from party_player.backup_service import BackupService
from party_player.application_restart import restart_current_application
from party_player.database_maintenance import DatabaseMaintenanceService
from party_player.metadata_analysis_coordinator import AnalysisOperatingState
from party_player.metadata_analysis_service import MetadataAnalysisService


class PartyPlayerApplication:
    """Build and run all application components."""

    def __init__(self, paths: AppPaths | None = None) -> None:
        self._paths = paths or AppPaths.for_project()

    def run(self) -> None:
        """Initialize persistence and display the main window."""
        self._paths.ensure_runtime_directories()
        configure_logging(self._paths.log_file)
        logger = logging.getLogger(__name__)
        logger.info("%s wird gestartet", PRODUCT_NAME)

        database = Database(self._paths.database_file)
        migrate(database)
        tracks = TrackRepository(database)
        loudness_repository = LoudnessRepository(database)
        library_service = LibraryService(tracks, loudness_repository)
        party_repository = PartyPlayerRepository(database)
        overlay_repository = OverlayRepository(database)
        overlay_service = OverlayService(overlay_repository)
        settings = SettingsService(party_repository)
        performance_settings = PerformanceSettings(
            enabled=settings.performance_diagnostics_enabled()
        )
        performance_monitor = PerformanceMonitor(
            warning_rate_limit_seconds=performance_settings.slow_warning_rate_limit_seconds,
            enabled=performance_settings.enabled,
        )
        dependency_service = SystemDependencyService(
            DependencyLocator(),
            DependencyValidator(),
            application_version=__version__,
            performance_monitor=performance_monitor,
        )
        diagnostic_service = SystemDiagnosticService(
            database,
            application_version=__version__,
            audio_device_provider=WindowsAudioDeviceProvider(),
            network_source_provider=tracks.network_roots,
            network_source_probe=NetworkSourceChecker(),
            performance_monitor=performance_monitor,
        )
        first_run_controller = FirstRunController(
            settings,
            dependency_service,
            __version__,
            performance_monitor,
        )
        pending_setup_reason = first_run_controller.pending_setup_reason()
        startup_decision = (
            first_run_controller.check_quick_startup() if pending_setup_reason is None else None
        )
        dependencies = startup_decision.resolution if startup_decision else None
        audio_backend = settings.audio_backend()
        if audio_backend != "vlc":
            logger.warning(
                "Audio-Backend %s ist nicht verfügbar; VLC wird verwendet",
                audio_backend,
            )
        audio_output_device = settings.audio_output_device()
        session_service = PartySessionService(party_repository)
        session = session_service.restore_or_start(settings.restore_last_session())
        tempo_context = TempoContextRepository(database)

        def invalidate_cue_tempo(track_id: int) -> None:
            tempo_context.mark_scope_stale(
                track_id,
                TempoAnalysisScope.TRACK_DEFAULT_CUES,
                "Wirksame globale Titel-Cues wurden geändert",
            )
            tempo_context.mark_scope_stale(
                track_id,
                TempoAnalysisScope.SAVED_QUEUE_ENTRY,
                "Geerbte globale Titel-Cues wurden geändert",
                inherited_only=True,
            )

        cue_points = CuePointService(
            CuePointRepository(database),
            settings.fade_duration(7.0),
            settings.minimum_fade_duration(),
            settings.minimum_playable_duration(),
            short_track_threshold=30.0,
            short_track_policy=ShortTrackPolicy.USE_REDUCED_FADE,
            on_global_cues_changed=invalidate_cue_tempo,
        )
        track_blocks = PersistentTrackBlockService(TrackPolicyRepository(database))
        artist_blocks = PersistentArtistBlockService(
            ArtistPolicyRepository(database), session.session_id
        )
        suitability = TrackSuitabilityService(TrackSuitabilityRepository(database))
        repetition = PersistentRepetitionService(
            RepetitionHistoryRepository(
                database,
                settings.repetition_partial_ratio_threshold(),
            )
        )
        for queue_id in party_repository.repetition_override_queue_ids(session.session_id):
            repetition.allow_queue_entry(queue_id)
        short_tracks = ShortTrackSelectionRule(
            cue_points,
            threshold_seconds=30.0,
            policy=ShortTrackPolicy.USE_REDUCED_FADE,
        )
        emergency_playlist = LocalEmergencyPlaylistService(
            tracks,
            FileAvailabilityService(),
            settings.emergency_track_ids(),
            audit=lambda event_code, details: party_repository.record_session_event(
                session.session_id,
                event_code,
                entity_type="EMERGENCY_PLAYLIST",
                details=details,
            ),
            media_track_ids={
                EmergencyMediaType.BREAK_MUSIC: settings.emergency_media_track_ids("BREAK_MUSIC"),
                EmergencyMediaType.JINGLE: settings.emergency_media_track_ids("JINGLE"),
                EmergencyMediaType.ANNOUNCEMENT: settings.emergency_media_track_ids("ANNOUNCEMENT"),
            },
            storage_policy=EmergencyStoragePolicy(
                settings.emergency_local_ssd_roots(),
                approved_removable_roots=settings.emergency_approved_removable_roots(),
            ),
        )
        automatic_selection = AutomaticSelectionService(
            tracks,
            AutomaticSelectionHistory(database),
            emergency_playlist=emergency_playlist,
        )
        saved_queue_repository = SavedQueueRepository(database)
        equalizer_resolver = EqualizerResolver(
            EqualizerPresetRepository(database),
            EqualizerAssignmentRepository(database),
        )

        def repeat_playlist_entries() -> list[SavedQueueEntry]:
            saved_queue_id = party_repository.selected_playlist_id(session.session_id)
            saved = (
                saved_queue_repository.get(saved_queue_id) if saved_queue_id is not None else None
            )
            return list(saved.entries) if saved is not None else []

        queue_service = QueueService(
            party_repository,
            tracks,
            session.session_id,
            cue_points=cue_points,
            selection_service=TrackSelectionService(
                (track_blocks, artist_blocks, suitability, repetition, short_tracks)
            ),
            empty_queue_policy=EmptyQueuePolicy.AUTOMATIC_SELECTION,
            automatic_selection=automatic_selection,
            repeat_playlist_entries=repeat_playlist_entries,
        )
        loudness = LoudnessService(
            loudness_repository,
            enabled=settings.normalization_enabled(),
            clip_protection_enabled=settings.clip_protection_enabled(),
            mode=settings.normalization_mode(),
            target_loudness_lufs=settings.target_loudness(),
            maximum_positive_gain_db=settings.maximum_positive_gain(),
            maximum_negative_gain_db=settings.maximum_negative_gain(),
            maximum_output_peak_db=settings.maximum_output_peak(),
            headroom_db=settings.headroom(),
            fallback_positive_gain_db=settings.fallback_positive_gain(),
            maximum_backend_volume_factor=VlcAudioBackend.MAXIMUM_VOLUME_FACTOR,
        )
        gui_dispatcher = GuiEventDispatcher(
            capacity=performance_settings.gui_event_queue_capacity,
            max_items_per_cycle=performance_settings.gui_event_max_items_per_cycle,
            budget_ms=performance_settings.gui_event_budget_ms,
            diagnostics_enabled=performance_settings.enabled,
        )
        worker_registry = WorkerRegistry(enabled=performance_settings.enabled)

        callback_state = GuiCallbackState()
        window = MainWindow(
            performance_monitor,
            callback_state,
            saved_geometry=settings.main_window_geometry(),
            save_geometry=settings.set_main_window_geometry,
            presentation_preference=settings.presentation_preference(),
            presentation_workspace=settings.presentation_workspace(),
            save_presentation_preference=settings.set_presentation_preference,
            save_presentation_workspace=settings.set_presentation_workspace,
        )
        if pending_setup_reason is not None or (
            startup_decision is not None and startup_decision.requires_setup
        ):

            def full_diagnostic_check() -> SystemDiagnosticReport:
                resolution = dependency_service.check_configured(settings)
                return diagnostic_service.check(resolution, full=True)

            setup = FirstRunSetupDialog(
                window,
                dependencies,
                full_diagnostic_check,
                first_run_controller.complete_setup,
                first_run_controller.select_vlc_directory,
                first_run_controller.select_ffmpeg_directory,
                DiagnosticReportExporter(self._paths.diagnostics_directory).export,
            )
            if not setup.show():
                logger.info("Einrichtung abgebrochen; Partybetrieb wird nicht gestartet")
                window.destroy()
                return
            dependencies = setup.resolution
        assert dependencies is not None
        capability_snapshots = CapabilitySnapshotState(dependencies.snapshot)
        capabilities = capability_snapshots.active.capabilities
        reason = pending_setup_reason or (
            startup_decision.reason if startup_decision else FirstRunReason.READY
        )
        logger.info(
            "Systemprüfung: Modus=%s, Erststartgrund=%s, VLC=%s (%s), "
            "FFmpeg=%s (%s), FFprobe=%s (%s)",
            "full" if pending_setup_reason is not None else "quick",
            reason.value,
            dependencies.snapshot.vlc.status.value,
            dependencies.snapshot.vlc.source or "unbekannt",
            dependencies.snapshot.ffmpeg.status.value,
            dependencies.snapshot.ffmpeg.source or "unbekannt",
            dependencies.snapshot.ffprobe.status.value,
            dependencies.snapshot.ffprobe.source or "unbekannt",
        )
        vlc_directory = dependencies.snapshot.vlc.installation_directory
        ffmpeg_executable = dependencies.snapshot.ffmpeg.executable_path
        ffprobe_executable = dependencies.snapshot.ffprobe.executable_path
        deck_a: DeckController | None = None
        try:
            if not capabilities.playback_available or vlc_directory is None:
                raise RuntimeError(
                    dependencies.snapshot.vlc.message or "Keine gültige VLC-Installation"
                )
            audio_backend_factory = VlcAudioBackendFactory(
                audio_output_device,
                vlc_directory,
            )
            deck_a = DeckController("A", audio_backend_factory.create_deck_backend("A"))
            deck_b = DeckController("B", audio_backend_factory.create_deck_backend("B"))
        except Exception:
            if deck_a is not None:
                deck_a.close()
            logger.exception("VLC oder das gewählte Audiogerät konnte nicht initialisiert werden")
            window.show_error(
                "Audioausgabe nicht verfügbar",
                "VLC oder das gewählte Audiogerät konnte nicht gestartet werden. "
                f"Bitte prüfe die Audioverbindung und starte {PRODUCT_NAME} erneut.",
            )
            window.destroy()
            return
        deck_a.set_volume(settings.deck_volume("A"))
        deck_b.set_volume(settings.deck_volume("B"))
        crossfader = CrossfaderService(
            deck_a,
            deck_b,
            position=settings.crossfader_position(),
            master_volume=settings.master_volume(),
        )
        replaygain_cache = ReplayGainCacheService(library_service, loudness)
        emergency_incidents = EmergencyIncidentRepository(database)
        unresolved_emergency_incident = emergency_incidents.latest_unresolved()
        emergency_persistence = EmergencyPersistenceService(emergency_incidents)

        def emergency_audit(event_code: str, details: dict[str, object]) -> None:
            emergency_persistence.record(
                session.session_id,
                event_code,
                details,
                emergency_state.snapshot(),
                audio_output_device,
            )

        emergency_state = EmergencyStateService(emergency_audit)
        emergency_history = EmergencyHistoryService(EmergencyHistoryRepository(database))
        emergency_safety = {
            entry.track.id: (
                cue_points.resolve(entry.track),
                loudness.resolve(entry.track.id),
            )
            for entry in emergency_playlist.media_entries()
        }

        def record_emergency_start(
            result: EmergencyPlaybackResult,
            media_type: EmergencyMediaType,
            track: Track,
        ) -> None:
            details = {
                "source": "EMERGENCY",
                "track_id": track.id,
                "deck_id": result.deck_id,
                "media_type": media_type.value,
                "cue_in": result.cue_in,
                "effective_gain_db": result.effective_gain_db,
                "clip_protection_enabled": result.clip_protection_enabled,
            }
            emergency_history.record_started(
                EmergencyHistoryEntry(
                    session.session_id,
                    track.id,
                    result.deck_id or "",
                    media_type.value,
                    track.title,
                    track.file_path,
                    result.cue_in,
                    result.effective_gain_db,
                    result.clip_protection_enabled,
                )
            )
            emergency_persistence.record(
                session.session_id,
                "EMERGENCY_PLAYBACK_CONFIRMED",
                details,
                emergency_state.snapshot(),
                audio_output_device,
            )

        audio_recovery = AudioRecoveryService(
            emergency_state,
            deck_a,
            deck_b,
            audio_backend_factory,
            audit=emergency_audit,
            emergency_track_provider=lambda: (
                emergency_playlist.candidates()[0] if emergency_playlist.candidates() else None
            ),
        )
        emergency_controller = EmergencyController(
            EmergencyPlaybackService(
                emergency_playlist,
                emergency_state,
                deck_a,
                deck_b,
                crossfader,
                cue_provider=lambda track: emergency_safety[track.id][0],
                loudness_provider=lambda track: emergency_safety[track.id][1],
                playback_started=record_emergency_start,
            ),
            emergency_state,
            audit=emergency_audit,
            recovery=audio_recovery,
        )
        window.set_fullscreen(settings.fullscreen_on_start())
        controller = MainController(
            window,
            library_service,
            queue_service,
            deck_a,
            deck_b,
            crossfader,
            PlaybackHistoryService(
                party_repository,
                session.session_id,
                played_ratio_threshold=settings.played_ratio_threshold(),
                played_seconds_threshold=settings.played_seconds_threshold(),
            ),
            settings,
            SavedQueueService(saved_queue_repository, queue_service, cue_points),
            session=session,
            session_service=session_service,
            cue_points=cue_points,
            loudness=loudness,
            gui_dispatcher=gui_dispatcher,
            performance_monitor=performance_monitor,
            performance_settings=performance_settings,
            worker_registry=worker_registry,
            background_analysis_enabled=settings.background_analysis_enabled(),
            diagnostics_directory=self._paths.diagnostics_directory,
            callback_state=callback_state,
            replaygain_cache=replaygain_cache,
            equalizer_resolver=equalizer_resolver,
            repetition_service=repetition,
            emergency_state_service=emergency_state,
            emergency_controller=emergency_controller,
            deck_health_monitor=DeckHealthMonitor(emergency_state),
            unresolved_emergency_incident=unresolved_emergency_incident,
            resolve_emergency_incident=emergency_persistence.resolve_reviewed,
        )
        replaygain_cache.refresh_catalog()
        window.bind_controller(controller)
        bind_compact_decks(controller, window.compact_deck_a, window.compact_deck_b)

        def run_system_diagnostic() -> SystemDiagnosticReport:
            resolution = dependency_service.check_configured(settings)
            return diagnostic_service.check(resolution, full=True)

        initial_diagnostic_report = diagnostic_service.check(dependencies)
        window.bind_system_diagnostics(
            initial_diagnostic_report,
            run_system_diagnostic,
            DiagnosticReportExporter(self._paths.diagnostics_directory).export,
        )

        def report_after_vlc_selection(directory: str) -> SystemDiagnosticReport:
            if not controller.can_change_vlc_installation():
                raise ValueError("VLC kann während aktiver Audioaktionen nicht geändert werden")
            return diagnostic_service.check(
                first_run_controller.select_vlc_directory(directory), full=True
            )

        def report_after_ffmpeg_selection(directory: str) -> SystemDiagnosticReport:
            if (
                cue_controller.active_analysis_job_count
                or loudness_controller.active_analysis_job_count
                or metadata_analysis.active_job_count
            ):
                raise ValueError("FFmpeg kann während laufender Analysen nicht geändert werden")
            return diagnostic_service.check(
                first_run_controller.select_ffmpeg_directory(directory), full=True
            )

        def report_after_vlc_reset() -> SystemDiagnosticReport:
            if not controller.can_change_vlc_installation():
                raise ValueError("VLC kann während aktiver Audioaktionen nicht geändert werden")
            settings.reset_vlc_installation_path()
            return run_system_diagnostic()

        def report_after_ffmpeg_reset() -> SystemDiagnosticReport:
            if (
                cue_controller.active_analysis_job_count
                or loudness_controller.active_analysis_job_count
                or metadata_analysis.active_job_count
            ):
                raise ValueError("FFmpeg kann während laufender Analysen nicht geändert werden")
            settings.reset_ffmpeg_bin_path()
            return run_system_diagnostic()

        window.bind_external_program_settings(
            settings.dependency_settings,
            initial_diagnostic_report,
            run_system_diagnostic,
            report_after_vlc_selection,
            report_after_ffmpeg_selection,
            report_after_vlc_reset,
            report_after_ffmpeg_reset,
            controller.can_change_vlc_installation,
            lambda: (
                not (
                    cue_controller.active_analysis_job_count
                    or loudness_controller.active_analysis_job_count
                    or metadata_analysis.active_job_count
                )
            ),
            capability_snapshots,
        )
        overlay_player = OverlayAudioPlayer(
            audio_backend_factory.create_auxiliary_backend("overlay")
        )
        controller.bind_overlay_output_device(overlay_player.set_output_device)
        controller.bind_overlay_master_mute(overlay_player.set_master_muted)

        def dispatch_overlay_status(callback: Callable[[], None]) -> None:
            gui_dispatcher.publish(
                GuiEvent(
                    GuiEventType.CALLBACK,
                    "overlay_status",
                    callback,
                    coalesce_key="overlay_status",
                )
            )

        def record_overlay_history(
            definition: OverlayDefinition,
            started_at: datetime,
            completed_at: datetime,
            result: OverlayPlayResult,
            error_message: str,
        ) -> None:
            overlay_repository.add_definition_history(
                definition,
                started_at=started_at,
                completed_at=completed_at,
                result=result,
                error_message=error_message,
            )

        def publish_ducking(factor: float, phase: str) -> None:
            def show_ducking() -> None:
                window.show_ducking_status(factor, phase)

            gui_dispatcher.publish(
                GuiEvent(
                    GuiEventType.CALLBACK,
                    "overlay_ducking",
                    show_ducking,
                    coalesce_key="overlay_ducking",
                )
            )

        ducking_controller = DuckingController(
            crossfader.set_ducking_factor,
            on_changed=publish_ducking,
        )
        overlay_controller = OverlayController(
            overlay_player,
            ducking_controller,
            publish_status=window.show_overlay_status,
            dispatch=dispatch_overlay_status,
            record_history=record_overlay_history,
            performance_monitor=performance_monitor,
        )
        overlay_player.set_status_callback(overlay_controller.player_status_changed)
        controller.bind_overlay_activity(overlay_controller.is_active)
        window.bind_overlay(overlay_controller, overlay_service)
        analysis_signal_on, analysis_signal_off = settings.cue_analysis_thresholds()
        cue_analysis_available = capabilities.cue_analysis_available
        analysis_unavailable_reason = (
            None
            if cue_analysis_available
            else "FFmpeg und FFprobe sind nicht verfügbar. Neue Analysen sind unter "
            "„Einstellungen → System / Externe Programme“ deaktiviert; gespeicherte "
            "Cue- und Lautheitswerte bleiben nutzbar."
        )
        cue_controller = CuePointController(
            cue_points,
            library_service,
            deck_a,
            deck_b,
            MainController.AUTOMATIC_OVERLAP_SECONDS,
            preview_backend_factory=lambda: audio_backend_factory.create_auxiliary_backend(
                "preview"
            ),
            gui_dispatcher=gui_dispatcher,
            performance_monitor=performance_monitor,
            worker_registry=worker_registry,
            analysis_service=(
                CueAnalysisService(
                    FfmpegAudioAnalysisBackend(
                        str(ffmpeg_executable),
                        str(ffprobe_executable),
                    ),
                    cue_points,
                    signal_settings=SignalDetectionSettings(
                        signal_on_dbfs=analysis_signal_on,
                        signal_off_dbfs=analysis_signal_off,
                        minimum_signal_seconds=settings.cue_analysis_minimum_signal_seconds(),
                        minimum_silence_seconds=settings.cue_analysis_minimum_silence_seconds(),
                    ),
                    estimator=CueBoundaryEstimator(
                        CueBoundarySettings(
                            edge_window_seconds=settings.cue_analysis_edge_window_seconds(),
                            preferred_fade_seconds=settings.fade_duration(7.0),
                            minimum_fade_seconds=settings.minimum_fade_duration(),
                        )
                    ),
                    level_window_seconds=settings.cue_analysis_level_window_seconds(),
                )
                if cue_analysis_available
                and ffmpeg_executable is not None
                and ffprobe_executable is not None
                else None
            ),
            analysis_unavailable_reason=analysis_unavailable_reason,
        )
        cue_controller.warm_persistence_worker()
        window.bind_cue_controller(cue_controller)
        loudness_controller = LoudnessController(
            loudness,
            library_service,
            deck_a,
            deck_b,
            window.schedule,
            settings_service=settings,
            analysis_service=(
                OfflineLoudnessAnalysisService(
                    FfmpegEbur128Backend(str(ffmpeg_executable)),
                    loudness_repository,
                )
                if capabilities.loudness_analysis_available and ffmpeg_executable is not None
                else None
            ),
            analysis_unavailable_reason=analysis_unavailable_reason,
            gui_dispatcher=gui_dispatcher,
        )
        window.bind_loudness_controller(loudness_controller)

        def metadata_analysis_progress(event: str, job_id: str, detail: str) -> None:
            def deliver() -> None:
                logger.debug(
                    "Metadatenanalyse: event=%s job=%s detail=%s",
                    event,
                    job_id,
                    detail,
                )
                window.show_metadata_analysis_progress(event, job_id, detail)

            gui_dispatcher.publish(
                GuiEvent(
                    GuiEventType.CALLBACK,
                    "metadata_analysis_progress",
                    deliver,
                    coalesce_key="metadata_analysis_progress",
                )
            )

        metadata_analysis = MetadataAnalysisService(
            database,
            tracks,
            ffmpeg=(ffmpeg_executable if capabilities.metadata_analysis_available else None),
            ffprobe=(ffprobe_executable if capabilities.metadata_analysis_available else None),
            operating_state=lambda: AnalysisOperatingState(
                production_mode=not settings.background_analysis_enabled(),
                audio_recovery=controller.metadata_analysis_audio_recovery_active(),
                automation_active=controller.metadata_analysis_automation_active(),
                playback_active=controller.is_audio_active(),
            ),
            publish_progress=metadata_analysis_progress,
            worker_registry=worker_registry,
            cue_points=cue_points,
        )
        window.bind_metadata_analysis(metadata_analysis)

        required_restore_participants = [
            controller.restore_participant(),
            cue_controller.restore_participant(),
            emergency_persistence.restore_participant(),
            emergency_history.restore_participant(),
            replaygain_cache.restore_participant(),
            metadata_analysis.restore_participant(),
        ]
        loudness_participant = loudness_controller.restore_participant()
        if loudness_participant is not None:
            required_restore_participants.append(loudness_participant)
        maintenance_safety_gate = RestoreSafetyGate(
            lambda: controller.restore_safety_snapshot(
                cue_analysis_active=cue_controller.active_analysis_job_count > 0,
                loudness_analysis_active=loudness_controller.active_analysis_job_count > 0,
            )
        )
        restore_runtime = build_restore_runtime(
            self._paths.database_file,
            database,
            tuple(required_restore_participants),
            expected_participant_count=len(required_restore_participants),
            safety_gate=maintenance_safety_gate,
            safety_retention_limit=settings.safety_backup_retention_limit(),
            performance_monitor=performance_monitor,
        )
        logger.info(
            "Restore-Runtime: %s",
            ("verfügbar (noch ohne UI)" if restore_runtime.available else restore_runtime.reason),
        )
        backup_restore_controller = BackupRestoreController(
            BackupService(
                database,
                safety_retention_limit=settings.safety_backup_retention_limit(),
                performance_monitor=performance_monitor,
            ),
            restore_runtime.pipeline,
            window.schedule,
            window.show_backup_restore_result,
            restore_unavailable_reason=restore_runtime.reason,
            maintenance_service=DatabaseMaintenanceService(
                self._paths.database_file,
                safety_gate=maintenance_safety_gate,
                backup_service=BackupService(
                    database,
                    safety_retention_limit=settings.safety_backup_retention_limit(),
                    performance_monitor=performance_monitor,
                ),
                safety_backup_directory=self._paths.backups_directory / "Safety",
                quiesce=(restore_runtime.lifecycle.quiesce if restore_runtime.lifecycle else None),
                resume=(
                    restore_runtime.lifecycle.resume_after_rollback
                    if restore_runtime.lifecycle
                    else None
                ),
            ),
            manual_backup_recorded=settings.set_last_manual_backup,
            last_manual_backup=settings.last_manual_backup(),
            performance_monitor=performance_monitor,
            playlist_transfer_service=PlaylistTransferService(saved_queue_repository, tracks),
            media_path_remap_service=MediaPathRemapService(database),
            equalizer_transfer_service=EqualizerTransferService(
                EqualizerPresetRepository(database)
            ),
            overlay_transfer_service=OverlayTransferService(overlay_repository),
        )
        window.bind_backup_restore(backup_restore_controller, self._paths.backups_directory)
        controller.bind_database_diagnostic_status(backup_restore_controller.diagnostic_status)

        # Let Windows paint the complete shell before catalog/session recovery can
        # preload or automatically start audio.  This guarantees that the operator
        # sees and can control DeckRelay before the first audible playback.
        def initialize() -> None:
            controller.initialize()
            if settings.emergency_preload_primary():
                Thread(
                    target=emergency_controller.preload_primary_silently,
                    name="emergency-silent-preload",
                    daemon=True,
                ).start()

        def poll_metadata_analysis() -> None:
            if not window.winfo_exists():
                return
            metadata_analysis.tick()
            window.after(100, poll_metadata_analysis)

        window.after(150, initialize)
        window.after(200, poll_metadata_analysis)
        try:
            window.mainloop()
        finally:
            metadata_analysis.close()
            backup_restore_controller.close()
            overlay_controller.close()
            # Network/FFmpeg analysis cancellation is cooperative.  Normal app
            # shutdown must not keep the closed GUI alive while workers return.
            cue_controller.close(wait=False)
            loudness_controller.close(wait=False)
            emergency_persistence.close()
            emergency_history.close()
            logger.info("%s wurde beendet", PRODUCT_NAME)
        if window.restart_requested:
            restart_current_application()
