"""Main DeckRelay controller."""

import logging
import gc
from dataclasses import dataclass, replace
from concurrent.futures import Executor
from datetime import datetime
import sqlite3
import tracemalloc
from time import monotonic, sleep
from pathlib import Path
from threading import Lock, Thread, current_thread, main_thread
from collections.abc import Callable
from typing import Protocol, TypeVar
from uuid import uuid4

from party_player.crossfader_service import CrossfaderService
from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.persistence_participant import single_worker_participant
from party_player.restore_lifecycle import PersistenceParticipant
from party_player.restore_safety import RestoreSafetySnapshot
from party_player import __version__
from party_player.product import PRODUCT_NAME, PRODUCT_SLUG
from party_player.gui_event_dispatcher import GuiEvent, GuiEventDispatcher, GuiEventType
from party_player.performance_monitor import GuiHeartbeat, PerformanceMonitor, PerformanceSettings
from party_player.gui_heartbeat_watchdog import GuiCallbackState, GuiHeartbeatWatchdog
from party_player.cover_processing import prepare_cover_canvas
from party_player.worker_diagnostics import WorkerInfo, WorkerRegistry, collect_thread_snapshot
from party_player.cue_points import (
    CuePointService,
    QueueCueEditorState,
    ResolvedTrackBoundaries,
)
from party_player.loudness import LoudnessService, ResolvedLoudnessSettings
from party_player.loudness_playback import (
    DeckResolvedLoudnessPlayback,
    ResolvedLoudnessPlayback,
)
from party_player.replaygain_cache import ReplayGainCacheService
from party_player.repetition_policy import PersistentRepetitionService
from party_player.memory_monitor import MemoryMonitor
from party_player.audio.vlc_backend import VlcAudioBackend
from party_player.deck_controller import DeckController
from party_player.diagnostic_scenario import DiagnosticScenario
from party_player.diagnostic_retention import retain_latest
from party_player.enums import (
    CompletionStatus,
    DeckState,
    HistoryReasonCode,
    PlayerMode,
    QueueStatus,
)
from party_player.equalizer import (
    BUILTIN_EQUALIZER_PRESETS,
    EqualizerPreset,
    EqualizerService,
    QueueEqualizerContext,
    ResolvedEqualizerPreset,
)
from party_player.equalizer_resolver import EqualizerResolver
from party_player.emergency_state import DeckHealth, EmergencyStateService, EmergencyStateSnapshot
from party_player.emergency_controller import EmergencyController, EmergencyEscalationResult
from party_player.emergency_playback import EmergencyPlaybackResult
from party_player.audio_recovery import (
    AudioRecoveryPolicy,
    AudioRecoveryResult,
    DeckRestartAssessment,
    GlobalAudioRecoveryResult,
)
from party_player.deck_health_monitor import DeckHealthMonitor
from party_player.emergency_persistence import EmergencyIncident
from party_player.emergency_actions import EmergencyActionProfile
from party_player.emergency_playlist import EmergencyMediaType
from party_player.source_availability_monitor import (
    SourceAvailabilityMonitor,
    SourceAvailabilitySnapshot,
    SourceAvailabilityState,
)
from party_player.one_deck_mode import (
    AudioOperatingMode,
    AudioOperatingModeSnapshot,
    OneDeckModeService,
)
from party_player.models import Deck, PartySession, QueueEntry, QueueStats, SavedQueue, Track
from party_player.playback_history_service import HistoryPersistRequest, PlaybackHistoryService
from party_player.queue_service import QueueService
from party_player.controllers.queue_controller import QueueController
from party_player.queue_view_events import (
    QueueViewEvent,
    QueueViewEventType,
    queue_view_events,
)
from party_player.queue_origin import derive_queue_origin
from party_player.services.library_service import LibraryService
from party_player.metadata_editor import MetadataEditorService
from party_player.catalog_maintenance import CatalogMaintenanceService
from party_player.saved_queue_service import SavedQueueService
from party_player.settings_service import SettingsService
from party_player.transition_controller import TransitionController, TransitionState
from party_player.session_service import PartySessionService


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PreparedPreloadResult:
    """Worker-prepared values that the Tk callback can apply without I/O."""

    media: object
    loudness: ResolvedLoudnessSettings | None
    boundaries: ResolvedTrackBoundaries | None
    equalizer: ResolvedEqualizerPreset


@dataclass(frozen=True, slots=True)
class DeckStatusViewModel:
    """Immutable regularly rendered state for one deck."""

    values: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class PlaybackStatusViewModel:
    """One comparable snapshot separating status acquisition from Tk writes."""

    deck_a: DeckStatusViewModel
    deck_b: DeckStatusViewModel
    crossfade_percent: int
    master_percent: int
    queue_size: int
    automatic_run: bool
    transition_state: str


@dataclass(frozen=True, slots=True)
class QueueViewUpdate:
    """Immutable dispatcher payload for applying queue changes in the GUI thread."""

    events: tuple[QueueViewEvent, ...]
    entries: tuple[QueueEntry, ...]
    tracks: dict[int, Track]


@dataclass(frozen=True, slots=True)
class EqualizerDialogState:
    deck_id: str
    track_title: str
    genre: str
    effective_name: str
    effective_source: str
    title_preset_key: str | None
    queue_preset_key: str | None
    playlist_preset_key: str | None
    genre_preset_key: str | None
    saved_queue_id: int | None


@dataclass(frozen=True, slots=True)
class RecoveryReturnAssessment:
    allowed: bool
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryReturnRequirement:
    code: str
    fulfilled: bool
    label: str


@dataclass(frozen=True, slots=True)
class EmergencyDashboardViewModel:
    system_state: str
    reason: str
    deck_a_health: str
    deck_b_health: str
    deck_a_source: str
    deck_b_source: str
    audio_state: str
    media_ready: bool
    media_summary: str
    current_action: str
    last_result: str


class MainView(Protocol):
    """UI operations used by the controller."""

    def show_catalog(self, tracks: list[Track], summary: str) -> None: ...
    def show_track_cues_changed(self, track_id: int, has_manual_cues: bool) -> None: ...

    def show_track_metadata_changed(self, track: Track) -> None: ...
    def show_catalog_paging(self, page: int, page_count: int) -> None: ...
    def show_session(self, session: PartySession) -> None: ...
    def show_start_settings(self, restore_session: bool, fullscreen: bool) -> None: ...
    def show_file_browser_setting(self, enabled: bool) -> None: ...
    def show_production_mode(self, enabled: bool) -> None: ...
    def show_diagnostic_saved(self, path: Path) -> None: ...
    def show_diagnostic_state(self, state: str, context: str) -> None: ...
    def widget_diagnostics(self) -> dict[str, int]: ...
    def memory_gauges(self) -> dict[str, int]: ...
    def show_audio_devices(self, devices: list[tuple[str, str]], selected_device: str) -> None: ...
    def show_audio_device_recovery(self, state: str, message: str) -> None: ...
    def show_recovery_return_requirements(
        self, requirements: tuple[RecoveryReturnRequirement, ...], visible: bool
    ) -> None: ...
    def show_unresolved_emergency_incident(self, incident_id: int, summary: str) -> None: ...
    def hide_unresolved_emergency_incident(self) -> None: ...
    def show_emergency_dashboard(self, dashboard: EmergencyDashboardViewModel) -> None: ...
    def show_queue(self, entries: list[QueueEntry], tracks: dict[int, Track]) -> None: ...
    def show_restored_queue_entries(self, queue_ids: set[int]) -> None: ...
    def show_queue_cue_warnings(self, warnings: dict[int, str]) -> None: ...
    def show_queue_entry(self, entry: QueueEntry, track: Track | None) -> None: ...
    def show_queue_events(
        self,
        events: tuple[QueueViewEvent, ...],
        entries: list[QueueEntry],
        tracks: dict[int, Track],
    ) -> None: ...
    def show_queue_stats(self, stats: QueueStats) -> None: ...
    def show_queue_origin(self, text: str) -> None: ...
    def show_deck(self, deck: Deck) -> None: ...
    def show_deck_cover(self, deck_id: str, image_data: object | None) -> None: ...
    def show_mixer(self, crossfader: float, master: float) -> None: ...
    def show_crossfader(self, crossfader: float) -> None: ...
    def show_fade_settings(self, duration: float, stop_after: bool) -> None: ...
    def show_player_mode(self, mode: str) -> None: ...
    def show_queue_duplicate_policy(self, policy: str) -> None: ...
    def show_queue_duration_mode(self, use_effective_cues: bool) -> None: ...
    def show_queue_artist_repetition(self, enabled: bool) -> None: ...
    def show_directory_import_result(self, added: int, skipped: int, failed: int) -> None: ...
    def show_catalog_import_result(self, created: int, updated: int, failed: int) -> None: ...
    def show_directory_import_progress(
        self, processed: int, total: int | None, active: bool
    ) -> None: ...
    def show_saved_queues(self, queues: list[SavedQueue]) -> None: ...
    def select_saved_queue(self, saved_queue_id: int) -> None: ...
    def show_saved_queue_load_result(self, added: int, skipped: int) -> None: ...
    def show_playlist(self, playlist: SavedQueue, tracks: list[Track]) -> None: ...
    def show_queue_shuffle_result(self, shuffled: int) -> None: ...
    def show_automatic_playback(self, active: bool) -> None: ...
    def show_automatic_status(self, state: str, detail: str = "") -> None: ...
    def show_error(self, title: str, message: str) -> None: ...
    def show_queue_warning(self, message: str) -> None: ...
    def confirm_replace(self, deck_id: str) -> bool: ...
    def confirm_queue_cue_change(self, status: str) -> bool: ...
    def schedule(self, delay_ms: int, callback: object) -> object: ...


class MainController:
    """Coordinate the view, library, queue and both audio decks."""

    AUTOMATIC_OVERLAP_SECONDS = 7.0
    ONE_DECK_FADE_SECONDS = 1.0
    ONE_DECK_START_WAIT_STEPS = 160
    ONE_DECK_START_WAIT_INTERVAL_MS = 50
    ONE_DECK_START_RETRY_STEP = 20
    CATALOG_PAGE_SIZE = 50
    MINIMUM_TRANSITION_PREPARATION_SECONDS = 0.5
    DIAGNOSTIC_CONTEXTS = {
        "idle",
        "normal_playback",
        "crossfade",
        "queue_stress",
        "nas_playback",
        "cue_preview",
        "directory_import",
        "database_delay",
        "memory_stress",
    }
    QUEUE_INSTRUMENTATION_OPERATIONS = (
        "gui.queue_render.total",
        "gui.queue_render.fetch_view_models",
        "gui.queue_render.create_rows",
        "gui.queue_render.bind_rows",
        "gui.queue_render.configure_widgets",
        "gui.queue_render.tooltip_update",
        "gui.queue_render.layout",
    )

    def __init__(
        self,
        view: MainView,
        library_service: LibraryService,
        queue_service: QueueService,
        deck_a: DeckController,
        deck_b: DeckController,
        crossfader: CrossfaderService,
        history_service: PlaybackHistoryService | None = None,
        settings_service: SettingsService | None = None,
        saved_queue_service: SavedQueueService | None = None,
        fade_duration: float = 5.0,
        fade_out_stops_deck: bool = False,
        session: PartySession | None = None,
        session_service: PartySessionService | None = None,
        background_preload: bool = True,
        cue_points: CuePointService | None = None,
        loudness: LoudnessService | None = None,
        gui_dispatcher: GuiEventDispatcher | None = None,
        performance_monitor: PerformanceMonitor | None = None,
        performance_settings: PerformanceSettings | None = None,
        worker_registry: WorkerRegistry | None = None,
        background_analysis_enabled: bool = True,
        diagnostics_directory: Path = Path("diagnostics"),
        callback_state: GuiCallbackState | None = None,
        heartbeat_watchdog_enabled: bool = True,
        persistence_executor: Executor | None = None,
        loudness_playback: ResolvedLoudnessPlayback | None = None,
        replaygain_cache: ReplayGainCacheService | None = None,
        preparation_timeout_seconds: float = 15.0,
        equalizer_service: EqualizerService | None = None,
        equalizer_resolver: EqualizerResolver | None = None,
        default_equalizer_preset: str | None = None,
        repetition_service: PersistentRepetitionService | None = None,
        emergency_state_service: EmergencyStateService | None = None,
        emergency_controller: EmergencyController | None = None,
        deck_health_monitor: DeckHealthMonitor | None = None,
        unresolved_emergency_incident: EmergencyIncident | None = None,
        resolve_emergency_incident: Callable[[int, dict[str, object]], bool] | None = None,
        source_availability_monitor: SourceAvailabilityMonitor | None = None,
    ) -> None:
        self._view = view
        self._library_service = library_service
        self._metadata_editor = MetadataEditorService(library_service.database)
        self._catalog_maintenance = CatalogMaintenanceService(library_service.database)
        self._queue_service = queue_service
        self._queue = QueueController(queue_service)
        self.deck_a = deck_a
        self.deck_b = deck_b
        self.crossfader = crossfader
        self._history = history_service
        self._settings = settings_service
        self._saved_queues = saved_queue_service
        self._session = session
        self._session_service = session_service
        self.player_mode = (
            settings_service.player_mode()
            if settings_service is not None
            else PlayerMode.SEMI_AUTOMATIC
        )
        self.automatic_deck_loading = self.player_mode != PlayerMode.MANUAL
        self._auto_load_suppressed_decks: set[str] = set()
        self._automatic_run_active = False
        self._automatic_run_paused = False
        self._automatic_pause_reason: str | None = None
        self._automatic_audio_paused_decks: set[str] = set()
        self._automatic_start_assessment_bypass = False
        self._automatic_status_state = "ready"
        self._automatic_status_reason = ""
        self._last_automatic_status_render: tuple[str, str] | None = None
        self.queue_duplicate_policy = (
            settings_service.queue_duplicate_policy() if settings_service is not None else "allow"
        )
        self._queue_service.allow_duplicates = self.queue_duplicate_policy == "allow"
        self._repetition = repetition_service
        self._emergency_state = emergency_state_service or EmergencyStateService()
        self._emergency = emergency_controller
        self._deck_health_monitor = deck_health_monitor
        self._unresolved_emergency_incident = unresolved_emergency_incident
        self._resolve_emergency_incident = resolve_emergency_incident
        self._source_availability_monitor = (
            source_availability_monitor or SourceAvailabilityMonitor()
        )
        self._source_availability: dict[str, SourceAvailabilitySnapshot] = {
            "A": SourceAvailabilitySnapshot("", SourceAvailabilityState.EMPTY),
            "B": SourceAvailabilitySnapshot("", SourceAvailabilityState.EMPTY),
        }
        self._source_availability_checking: set[str] = set()
        self._next_source_availability_check = {"A": 0.0, "B": 0.0}
        self._next_audio_device_health_check = 0.0
        self._next_recovery_return_render = 0.0
        self._last_recovery_return_render: tuple[RecoveryReturnRequirement, ...] | None = None
        self._audio_device_loss_active = False
        self._audio_device_ready_for_confirmation = False
        self._global_audio_recovery_requested = False
        self._global_audio_recovery_ready_for_release = False
        self._recovery_return_validation_required = False
        self._emergency_action_active = False
        self._overlay_activity: Callable[[], bool] = lambda: False
        self._database_diagnostic_status: Callable[[], tuple[str, ...]] = lambda: ()
        self._deck_recovery_action_active = False
        self._current_emergency_action = "Keine"
        self._last_emergency_action_result = "Noch keine Aktion"
        self._last_emergency_dashboard: EmergencyDashboardViewModel | None = None
        self._one_deck_mode = OneDeckModeService(
            lambda code, details: self._queue_service.record_audit_event(code, details=details)
        )
        self._one_deck_start_generation = 0
        self._one_deck_start_pending: str | None = None
        if self._deck_health_monitor is not None:
            self._deck_health_monitor.bind(deck_a)
            self._deck_health_monitor.bind(deck_b)

        self.queue_artist_repetition_enabled = (
            settings_service.queue_artist_repetition_enabled()
            if settings_service is not None
            else True
        )
        if self._repetition is not None:
            self._repetition.queue_artist_repetition_enabled = self.queue_artist_repetition_enabled
        self.queue_stats_use_effective_cues = (
            settings_service.queue_stats_use_effective_cues()
            if settings_service is not None
            else False
        )
        self.fade_duration = (
            settings_service.fade_duration(fade_duration)
            if settings_service is not None
            else fade_duration
        )
        self.fade_out_stops_deck = (
            settings_service.fade_out_stops_deck(fade_out_stops_deck)
            if settings_service is not None
            else fade_out_stops_deck
        )
        self._catalog: list[Track] = []
        self._catalog_query = ""
        self._catalog_page = 0
        self._queue_entries_cache: list[QueueEntry] = []
        self._queue_entries_by_id_cache: dict[int, QueueEntry] = {}
        self._queue_tracks_cache: dict[int, Track] = {}
        self._queue_stats_dirty = True
        self._queue_stats_durations: dict[int, float] = {}
        self._queue_stats_total_duration = 0.0
        self._queue_stats_remaining_duration = 0.0
        self._queue_stats_remaining_ids: tuple[int, ...] = ()
        self._queue_stats_playing_ids: tuple[int, ...] = ()
        self._queue_stats_rebuild_pending = False
        self._queue_stats_signature: tuple[object, ...] | None = None
        self._queue_stats_generation = 0
        self._queue_revision = 0
        self._selected_queue_entry_id: int | None = None
        self._last_deck_render: dict[str, tuple[object, ...]] = {}
        self._last_mixer_render: tuple[float, float] | None = None
        self._last_playback_status: PlaybackStatusViewModel | None = None
        self._deck_queue_ids: dict[str, int | None] = {"A": None, "B": None}
        self._queue_playback_generations: dict[int, int] = {}
        self._queue_playback_generation_lock = Lock()
        self._closed = False
        self._background_preload = background_preload
        self._cue_points = cue_points
        self._loudness = loudness
        self._loudness_playback = loudness_playback or DeckResolvedLoudnessPlayback(deck_a, deck_b)
        self._replaygain_cache = replaygain_cache
        self._equalizer = equalizer_service or EqualizerService()
        self._equalizer_resolver = equalizer_resolver
        self._default_equalizer_preset = (
            settings_service.global_equalizer_preset(default_equalizer_preset)
            if settings_service is not None
            else default_equalizer_preset
        )
        self._queue_equalizer_preset_id: int | None = None
        self._equalizer_preview_previous: dict[str, ResolvedEqualizerPreset] = {}
        self._deck_equalizer_snapshots: dict[str, ResolvedEqualizerPreset] = {}
        self._overlay_output_device_setter: Callable[[str], None] | None = None
        self._overlay_master_mute_setter: Callable[[bool], None] | None = None
        self._preload_in_progress = False
        self._preload_generation = 0
        self._no_safe_candidate_warning_active = False
        self._preparation_timeout_seconds = max(1.0, preparation_timeout_seconds)
        self._cue_timing_warning: dict[str, int] = {}
        self._logger = logging.getLogger(__name__)
        self._performance_settings = performance_settings or PerformanceSettings()
        self._performance = performance_monitor or PerformanceMonitor(
            warning_rate_limit_seconds=(self._performance_settings.slow_warning_rate_limit_seconds)
        )
        self._queue_service.set_performance_monitor(self._performance)
        self._memory_monitor = MemoryMonitor(
            enabled=self._performance_settings.enabled,
            tracemalloc_enabled=False,
            maximum_samples=500,
        )
        self._memory_gauges_cache: dict[str, int] = {}
        self._gui_dispatcher = gui_dispatcher or GuiEventDispatcher(
            capacity=self._performance_settings.gui_event_queue_capacity,
            max_items_per_cycle=self._performance_settings.gui_event_max_items_per_cycle,
            budget_ms=self._performance_settings.gui_event_budget_ms,
            diagnostics_enabled=self._performance_settings.enabled,
        )
        self._diagnostic_context = "idle"
        self._diagnostics_directory = diagnostics_directory
        self._diagnostic_scenario = DiagnosticScenario()
        self._memory_stress_was_populated = False
        self._memory_stress_peak_queue_size = 0
        self._memory_stress_cycle_number = 0
        self._callback_state = callback_state or GuiCallbackState()
        self._heartbeat = GuiHeartbeat(self._performance_settings)
        self._heartbeat_watchdog = GuiHeartbeatWatchdog(
            self._callback_state,
            diagnostics_directory=diagnostics_directory,
            test_context=lambda: self._diagnostic_context,
            playback_state=self._watchdog_playback_state,
            dispatcher_state=self._watchdog_dispatcher_state,
            warning_threshold_ms=self._performance_settings.gui_heartbeat_warning_ms,
            critical_threshold_ms=self._performance_settings.gui_heartbeat_critical_ms,
        )
        self._heartbeat_watchdog_enabled = heartbeat_watchdog_enabled
        self._worker_registry = worker_registry or WorkerRegistry()
        self._background_analysis_enabled = background_analysis_enabled
        self._status_tick_running = False
        self._heartbeat_started = False
        self._transition_completion_pending = False
        self._deck_completion_pending: set[str] = set()
        self._one_deck_fade_pending: set[str] = set()
        self._queue_render_pending = False
        self._pending_queue_view_update: QueueViewUpdate | None = None
        self._cover_executor = BoundedThreadPoolExecutor(
            max_workers=2, maximum_pending=100, thread_name_prefix="cover-worker"
        )
        self._preload_executor = BoundedThreadPoolExecutor(
            max_workers=1, maximum_pending=2, thread_name_prefix="preload-worker"
        )
        self._next_preload_candidate_search_at = 0.0
        self._preload_candidate_misses = 0
        self._statistics_executor = BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=1,
            thread_name_prefix="statistics-worker",
        )
        self._track_editor_executor = BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=4,
            thread_name_prefix="track-editor-load",
        )
        self._persistence_executor = persistence_executor or BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=500,
            thread_name_prefix="playback-persist-worker",
        )
        self._owns_persistence_executor = persistence_executor is None
        self._transition = TransitionController(
            self.crossfader,
            self._view.schedule,
            self._refresh_crossfade_display,
            self._complete_automatic_transition,
            self.AUTOMATIC_OVERLAP_SECONDS,
            self._performance,
            failure=self._handle_transition_failure,
        )
        self.deck_a.set_volume_changed_callback(self._mixer_changed)
        self.deck_b.set_volume_changed_callback(self._mixer_changed)
        if self._deck_health_monitor is not None:
            self._deck_health_monitor.set_diagnostic_context_provider(
                self._audio_stall_diagnostic_context
            )

    def _audio_stall_diagnostic_context(self, deck_id: str) -> dict[str, object]:
        """Correlate a stall sample using cached state only; never probe media or VLC."""
        callback = self._callback_state.snapshot()
        availability = self._source_availability[deck_id]
        return {
            "gui_heartbeat_age_ms": round(
                max(0.0, monotonic() - callback.last_heartbeat_monotonic) * 1000.0, 1
            ),
            "active_gui_callback": callback.active_gui_callback or "",
            "active_workers": tuple(worker.name for worker in self._worker_registry.active()),
            "queue_statistics_pending": self._queue_stats_rebuild_pending,
            "queue_statistics_dirty": self._queue_stats_dirty,
            "preload_in_progress": self._preload_in_progress,
            "source_state": availability.state.value,
            "source_path": availability.path,
            "source_checked_at": availability.checked_at,
        }

    def emergency_snapshot(self) -> EmergencyStateSnapshot:
        return self._emergency_state.snapshot()

    def resolve_unresolved_emergency_incident(self) -> bool:
        """Persist explicit review of the startup incident and remove its warning."""
        incident = self._unresolved_emergency_incident
        if incident is None or self._resolve_emergency_incident is None:
            return False
        accepted = self._resolve_emergency_incident(
            incident.incident_id,
            {
                "review": "OPERATOR_CONFIRMED",
                "previous_system_state": incident.system_state,
                "previous_reason": incident.reason,
            },
        )
        if not accepted:
            self._view.show_queue_warning(
                "Incident-Prüfung konnte nicht zur Speicherung eingeplant werden."
            )
            return False
        self._queue_service.record_audit_event(
            "EMERGENCY_INCIDENT_REVIEWED",
            details={"incident_id": incident.incident_id},
        )
        self._unresolved_emergency_incident = None
        self._view.hide_unresolved_emergency_incident()
        self._view.show_queue_warning(
            f"Audiovorfall #{incident.incident_id} wurde als geprüft geschlossen."
        )
        return True

    def emergency_action_profile(self) -> EmergencyActionProfile:
        stored = (
            self._settings.emergency_action_profile()
            if self._settings is not None
            else EmergencyActionProfile.PLAY_EMERGENCY.value
        )
        return EmergencyActionProfile(stored)

    def emergency_dashboard(self) -> EmergencyDashboardViewModel:
        snapshot = self._emergency_state.snapshot()
        validation_method = getattr(self._emergency, "playlist_validation", None)
        validation = validation_method() if callable(validation_method) else None
        media_ready = bool(validation is not None and validation.ready)
        if validation is None:
            media_summary = "Nicht konfiguriert"
        elif validation.ready:
            accepted_media = {
                media_type.value: len(track_ids)
                for media_type, track_ids in getattr(validation, "accepted_media", ())
            }
            roles = (
                f"Primär {accepted_media.get('PRIMARY', 1)} · "
                f"Pause {accepted_media.get('BREAK_MUSIC', 0)} · "
                f"Jingles {accepted_media.get('JINGLE', 0)} · "
                f"Ansagen {accepted_media.get('ANNOUNCEMENT', 0)}"
            )
            media_summary = (
                f"Bereit · Primärtitel #{validation.primary_track_id} · "
                f"{roles} · geprüft {validation.validated_at}"
            )
        else:
            media_summary = (
                f"Nicht bereit · {len(validation.issues)} Problem(e) · "
                f"geprüft {validation.validated_at}"
            )
        return EmergencyDashboardViewModel(
            snapshot.system.value,
            snapshot.reason or "Kein aktiver Grund",
            snapshot.deck_a.value,
            snapshot.deck_b.value,
            self._deck_source_status(self.deck_a),
            self._deck_source_status(self.deck_b),
            self.audio_output_device_recovery_state(),
            media_ready,
            media_summary,
            self._current_emergency_action,
            self._last_emergency_action_result,
        )

    @staticmethod
    def _source_kind(file_path: str) -> str:
        return "NAS/NETZWERK" if file_path.startswith(("\\\\", "//")) else "LOKAL"

    def _deck_source_status(self, deck: DeckController) -> str:
        track = deck.model.loaded_track
        if track is None:
            return "LEER · keine Quelldatei geladen"
        path = track.file_path
        source = self._source_kind(path)
        availability = self._source_availability[deck.model.deck_id]
        state = availability.state.value if availability.path == path else "UNBEKANNT"
        checked = f" · geprüft {availability.checked_at}" if availability.checked_at else ""
        return f"{source} · {state}{checked} · {track.title} · {path}"

    def _schedule_source_availability_checks(self) -> None:
        now = monotonic()
        for deck in (self.deck_a, self.deck_b):
            deck_id = deck.model.deck_id
            track = deck.model.loaded_track
            if track is None:
                self._source_availability[deck_id] = SourceAvailabilitySnapshot(
                    "", SourceAvailabilityState.EMPTY
                )
                continue
            path = track.file_path
            cached = self._source_availability[deck_id]
            if deck_id in self._source_availability_checking:
                continue
            if cached.path == path and now < self._next_source_availability_check[deck_id]:
                continue
            self._source_availability_checking.add(deck_id)
            self._source_availability[deck_id] = SourceAvailabilitySnapshot(
                path, SourceAvailabilityState.CHECKING
            )
            self._publish_emergency_dashboard(force=True)

            def worker(deck_id: str = deck_id, path: str = path) -> None:
                result = self._source_availability_monitor.check(path)

                def apply() -> None:
                    self._source_availability_checking.discard(deck_id)
                    current = self._deck(deck_id).model.loaded_track
                    if current is None or current.file_path != path:
                        return
                    self._source_availability[deck_id] = result
                    self._next_source_availability_check[deck_id] = monotonic() + 30.0
                    self._publish_emergency_dashboard(force=True)

                self._publish_gui_callback(
                    apply, "source_availability", coalesce_key=f"source-{deck_id}"
                )

            if not self._start_worker(
                worker,
                f"source-availability-{deck_id}",
                "source_availability",
            ):
                self._source_availability_checking.discard(deck_id)

    def _publish_emergency_dashboard(self, *, force: bool = False) -> None:
        dashboard = self.emergency_dashboard()
        if force or dashboard != self._last_emergency_dashboard:
            self._view.show_emergency_dashboard(dashboard)
            self._last_emergency_dashboard = dashboard

    def set_emergency_action_profile(self, profile: str | EmergencyActionProfile) -> None:
        selected = EmergencyActionProfile(profile)
        if self._settings is not None:
            self._settings.set_emergency_action_profile(selected.value)

    def start_deck_recovery_action(
        self,
        deck_id: str,
        policy: AudioRecoveryPolicy = AudioRecoveryPolicy.RESUME_POSITION,
    ) -> bool:
        """Run an explicitly confirmed single-deck recovery outside the GUI thread."""
        normalized = deck_id.upper()
        if normalized not in {"A", "B"} or self._deck_recovery_action_active:
            return False
        if self._emergency is None:
            self._view.show_queue_warning("Einzeldeck-Recovery ist nicht konfiguriert.")
            return False
        assessment = self._emergency.can_restart_deck_independently(normalized)
        if not assessment.allowed:
            self._last_emergency_action_result = (
                f"DECK {normalized} · BLOCKED · {assessment.error_code}"
            )
            self._view.show_queue_warning(
                f"Deck {normalized} kann nicht einzeln repariert werden: "
                f"{assessment.message or assessment.error_code}"
            )
            self._publish_emergency_dashboard(force=True)
            return False
        self._deck_recovery_action_active = True
        self._current_emergency_action = f"Deck-Recovery {normalized}"
        self._pause_automatic_queue(f"Deck-Recovery {normalized} angefordert")
        self._publish_emergency_dashboard(force=True)

        def recover() -> None:
            assert self._emergency is not None
            result = self._emergency.recover_deck(normalized, policy)

            def finish() -> None:
                self._deck_recovery_action_active = False
                self._current_emergency_action = "Keine"
                self._recovery_return_validation_required = True
                self._last_emergency_action_result = (
                    f"DECK {normalized} · {result.state} · "
                    f"{'OK' if result.success else result.error_code or 'FEHLER'} · "
                    f"Versuch {result.attempt}"
                )
                if result.success:
                    self._view.show_queue_warning(
                        f"Deck {normalized} wurde repariert und bleibt sicher stumm."
                    )
                else:
                    self._view.show_queue_warning(
                        f"Deck-Recovery {normalized} fehlgeschlagen: "
                        f"{result.message or result.error_code}"
                    )
                self._publish_emergency_dashboard(force=True)
                self._publish_recovery_return_requirements(force=True)

            self._view.schedule(0, finish)

        Thread(target=recover, name=f"deck-recovery-{normalized}", daemon=True).start()
        return True

    def start_emergency_action(self, profile: str | EmergencyActionProfile) -> bool:
        """Run a hold-confirmed emergency profile, off-thread where it can block."""
        if self._emergency_action_active:
            return False
        selected = EmergencyActionProfile(profile)
        self.set_emergency_action_profile(selected)
        self._emergency_action_active = True
        self._recovery_return_validation_required = True
        self._current_emergency_action = selected.value
        self._publish_emergency_dashboard(force=True)
        self._pause_automatic_queue(f"Notfallprofil {selected.value}")
        if selected != EmergencyActionProfile.PLAY_EMERGENCY:
            try:
                self._execute_immediate_emergency_action(selected)
            finally:
                self._emergency_action_active = False
                self._current_emergency_action = "Keine"
                self._publish_emergency_dashboard(force=True)
            return True

        if self._emergency is None:
            self._emergency_action_active = False
            self._current_emergency_action = "Keine"
            self._last_emergency_action_result = "BLOCKED · RECOVERY_NOT_CONFIGURED"
            self._publish_emergency_dashboard(force=True)
            self._view.show_queue_warning("Notfallwiedergabe ist nicht konfiguriert.")
            return False

        def play_emergency() -> None:
            assert self._emergency is not None
            prepared = self._emergency.prepare()
            result = self._emergency.activate() if prepared.success else prepared

            def finish() -> None:
                self._emergency_action_active = False
                self._current_emergency_action = "Keine"
                self._last_emergency_action_result = (
                    f"{result.state} · "
                    f"{'OK' if result.success else result.error_code or 'FEHLER'}"
                )
                if result.success:
                    self._view.show_queue_warning("Notfalltitel wurde bestätigt gestartet.")
                else:
                    self._view.show_queue_warning(
                        f"Notfalltitel konnte nicht gestartet werden: "
                        f"{result.message or result.error_code}"
                    )
                self._publish_emergency_dashboard(force=True)

            self._view.schedule(0, finish)

        Thread(target=play_emergency, name="emergency-profile-play", daemon=True).start()
        return True

    def start_emergency_media_action(
        self, media_type: str | EmergencyMediaType, *, loop: bool = False
    ) -> bool:
        """Start a typed, prevalidated local emergency medium off the GUI thread."""
        if self._emergency_action_active or self._emergency is None:
            return False
        selected_type = EmergencyMediaType(media_type)
        self._emergency_action_active = True
        self._recovery_return_validation_required = True
        suffix = " (Schleife)" if loop else ""
        self._current_emergency_action = f"{selected_type.value}{suffix}"
        self._publish_emergency_dashboard(force=True)
        self._pause_automatic_queue(f"Notfallmedium {selected_type.value}")

        def play_media() -> None:
            assert self._emergency is not None
            result = self._emergency.play_media(selected_type, loop=loop)

            def finish() -> None:
                self._emergency_action_active = False
                self._current_emergency_action = "Keine"
                self._last_emergency_action_result = (
                    f"{selected_type.value} · {result.state} · "
                    f"{'OK' if result.success else result.error_code or 'FEHLER'}"
                )
                label = {
                    EmergencyMediaType.BREAK_MUSIC: "Pausenmusik",
                    EmergencyMediaType.JINGLE: "Jingle",
                    EmergencyMediaType.ANNOUNCEMENT: "Ansage",
                    EmergencyMediaType.PRIMARY: "Notfalltitel",
                }[selected_type]
                if result.success:
                    self._view.show_queue_warning(f"{label} wurde bestätigt gestartet.")
                else:
                    self._view.show_queue_warning(
                        f"{label} konnte nicht gestartet werden: "
                        f"{result.message or result.error_code}"
                    )
                self._publish_emergency_dashboard(force=True)

            self._view.schedule(0, finish)

        Thread(
            target=play_media,
            name=f"emergency-media-{selected_type.value.lower()}",
            daemon=True,
        ).start()
        return True

    def start_immediate_replace_action(self, deck_id: str) -> bool:
        """Immediately mute one unacceptable deck and replace it off the GUI thread."""
        normalized = deck_id.upper()
        if normalized not in {"A", "B"} or self._emergency_action_active or self._emergency is None:
            return False
        affected = self.deck_a if normalized == "A" else self.deck_b
        affected.set_emergency_muted(True)
        self._emergency_action_active = True
        self._recovery_return_validation_required = True
        self._current_emergency_action = f"IMMEDIATE_REPLACE {normalized}"
        self._pause_automatic_queue(f"Unzumutbare Ausgabe auf Deck {normalized}")
        self._publish_emergency_dashboard(force=True)

        def replace() -> None:
            assert self._emergency is not None
            result = self._emergency.immediate_replace(normalized)

            def finish() -> None:
                self._emergency_action_active = False
                self._current_emergency_action = "Keine"
                self._last_emergency_action_result = (
                    f"IMMEDIATE_REPLACE {normalized} · {result.state} · "
                    f"{'OK' if result.success else result.error_code or 'FEHLER'}"
                )
                if result.success:
                    self._view.show_queue_warning(
                        f"Deck {normalized} wurde sofort stummgeschaltet; "
                        "der Notfalltitel läuft bestätigt."
                    )
                else:
                    self._view.show_queue_warning(
                        f"Sofortersatz für Deck {normalized} fehlgeschlagen: "
                        f"{result.message or result.error_code}"
                    )
                self._publish_emergency_dashboard(force=True)
                self._publish_recovery_return_requirements(force=True)

            self._view.schedule(0, finish)

        Thread(
            target=replace,
            name=f"emergency-immediate-replace-{normalized}",
            daemon=True,
        ).start()
        return True

    def _execute_immediate_emergency_action(self, profile: EmergencyActionProfile) -> None:
        self.set_panic_muted(True)
        self._last_emergency_action_result = f"{profile.value} · OK"
        if profile == EmergencyActionProfile.MUTE_ALL:
            self._view.show_queue_warning("NOTFALL: Gesamte Audioausgabe stummgeschaltet.")
            return
        for deck_id in ("A", "B"):
            self.deck_action(deck_id, "stop")
        if profile == EmergencyActionProfile.SAFE_RESET:
            self._transition.reset()
            self.crossfader.set_position(0.5)
            self.deck_a.set_emergency_muted(False)
            self.deck_b.set_emergency_muted(False)
            self.crossfader.apply()
            self._view.show_queue_warning(
                "NOTFALL: Sicherer stiller Grundzustand hergestellt; Panic-Mute bleibt aktiv."
            )
            return
        self._view.show_queue_warning("NOTFALL: Beide Decks wurden gestoppt und stummgeschaltet.")

    def prepare_emergency_audio(self) -> EmergencyPlaybackResult:
        if self._emergency is None:
            raise ValueError("Notfallwiedergabe ist nicht konfiguriert")
        return self._emergency.prepare()

    def activate_emergency_audio(self) -> EmergencyPlaybackResult:
        if self._emergency is None:
            raise ValueError("Notfallwiedergabe ist nicht konfiguriert")
        return self._emergency.activate()

    def can_restart_deck_independently(self, deck_id: str) -> DeckRestartAssessment:
        if self._emergency is None:
            return DeckRestartAssessment(False, "RECOVERY_NOT_CONFIGURED")
        return self._emergency.can_restart_deck_independently(deck_id)

    def recover_audio_deck(
        self,
        deck_id: str,
        policy: AudioRecoveryPolicy = AudioRecoveryPolicy.RESUME_POSITION,
    ) -> AudioRecoveryResult:
        if self._emergency is None:
            return AudioRecoveryResult(False, "BLOCKED", deck_id.upper(), "RECOVERY_NOT_CONFIGURED")
        return self._emergency.recover_deck(deck_id, policy)

    def stabilize_failed_audio_deck(self, deck_id: str) -> EmergencyEscalationResult:
        """Secure confirmed emergency audio before an isolated recovery attempt."""
        if self._emergency is None:
            return EmergencyEscalationResult(
                False,
                "BLOCKED",
                deck_id.upper(),
                error_code="RECOVERY_NOT_CONFIGURED",
                message="Notfallwiedergabe ist nicht konfiguriert",
            )
        result = self._emergency.stabilize_failed_deck(deck_id)
        if result.state == "EMERGENCY_PLAYING_SINGLE_DECK" and result.playback is not None:
            assert result.playback.deck_id is not None
            self.enter_one_deck_mode(result.playback.deck_id, result.message)
        return result

    def _handle_transition_failure(
        self,
        reason: str,
        outgoing: DeckController,
        incoming: DeckController,
    ) -> None:
        """Restore the last audible deck and isolate a failed incoming handover."""
        outgoing_id = outgoing.model.deck_id
        incoming_id = incoming.model.deck_id
        skip_unstarted_track = reason == "INCOMING_PLAYBACK_NOT_CONFIRMED"
        if not skip_unstarted_track:
            self._pause_automatic_queue(f"Übergang fehlgeschlagen: {reason}")
        else:
            # This failure describes the current medium, not a confirmed deck or
            # backend failure. Invalidate any older preload completion before the
            # failed medium is removed, then make the deck available again.
            self._preload_generation += 1
            self._preload_in_progress = False
        incoming.set_emergency_muted(True)
        outgoing.set_emergency_muted(False)
        outgoing.set_transition_muted(False)
        outgoing.cancel_fade()
        outgoing.set_fade_level_immediately(1.0)
        self.crossfader.set_position(0.0 if outgoing_id == "A" else 1.0)
        incoming_queue_id = self._deck_queue_ids.get(incoming_id)
        if incoming_queue_id is not None:
            entry = self._queue_service.entry(incoming_queue_id)
            if entry is not None and entry.status == QueueStatus.PLAYING:
                with self._queue_playback_generation_lock:
                    self._queue_playback_generations[incoming_queue_id] = (
                        self._queue_playback_generations.get(incoming_queue_id, 0) + 1
                    )
                if skip_unstarted_track:
                    if self._history is not None:
                        self._history.finish(
                            incoming_id,
                            CompletionStatus.FAILED,
                            incoming.model.position,
                            error_message=reason,
                            skip_code=HistoryReasonCode.PLAYBACK_ERROR,
                        )
                    self._queue_service.mark_skipped(
                        incoming_queue_id,
                        "Wiedergabe auf dem eingehenden Deck nicht bestätigt",
                        code="INCOMING_PLAYBACK_NOT_CONFIRMED",
                    )
                    incoming.eject()
                    self._deck_queue_ids[incoming_id] = None
                    self._view.show_deck_cover(incoming_id, None)
                else:
                    self._queue_service.mark_loaded(incoming_queue_id, incoming_id)
        if skip_unstarted_track:
            incoming.set_emergency_muted(False)
            incoming.set_transition_muted(False)
            self._auto_load_suppressed_decks.discard(incoming_id)
            self._transition.reset()
            self._automatic_run_active = True
            self._automatic_run_paused = False
            self._automatic_pause_reason = None
            self._auto_load()
        else:
            if self._emergency is not None:
                self._emergency.report_transition_failure(outgoing_id, incoming_id, reason)
            else:
                self._emergency_state.set_deck_health(incoming_id, DeckHealth.FAILED, reason)
            if self._one_deck_mode.snapshot().mode != AudioOperatingMode.ONE_DECK:
                self.enter_one_deck_mode(outgoing_id, f"Übergang fehlgeschlagen: {reason}")
        self._queue_service.record_audit_event(
            "TRANSITION_FAILURE_STABILIZED",
            details={
                "reason": reason,
                "outgoing_deck_id": outgoing_id,
                "incoming_deck_id": incoming_id,
                "incoming_queue_id": incoming_queue_id,
            },
        )
        if skip_unstarted_track:
            self._view.show_queue_warning(
                f"Titel auf Deck {incoming_id} ohne bestätigte Wiedergabe übersprungen. "
                f"Deck {outgoing_id} bleibt hörbar; Deck {incoming_id} steht wieder für "
                "den nächsten Preload bereit."
            )
        else:
            self._view.show_queue_warning(
                f"Übergang fehlgeschlagen. Deck {outgoing_id} bleibt hörbar; "
                f"Deck {incoming_id} wurde stummgeschaltet. Bitte Deck {incoming_id} "
                "reparieren und danach die Automatik über die Rückkehrprüfung fortsetzen."
            )
        self._refresh_all()

    def recover_all_audio_backends(self) -> GlobalAudioRecoveryResult:
        """Run only an explicit global recovery and keep both outputs safety-muted."""
        if self._emergency is None:
            return GlobalAudioRecoveryResult(
                False, "BLOCKED", "RECOVERY_NOT_CONFIGURED", "Recovery ist nicht konfiguriert"
            )
        self._prepare_global_audio_recovery()
        result = self._emergency.recover_all_audio_backends()
        self._finish_global_audio_recovery(result)
        return result

    def start_global_audio_recovery(self) -> bool:
        """Start an operator-requested global recovery outside the GUI thread."""
        if (
            self._emergency is None
            or self._global_audio_recovery_requested
            or self._global_audio_recovery_ready_for_release
        ):
            return False
        self._global_audio_recovery_requested = True
        self._prepare_global_audio_recovery()

        def recover() -> None:
            assert self._emergency is not None
            result = self._emergency.recover_all_audio_backends()
            self._view.schedule(0, lambda: self._finish_global_audio_recovery(result))

        Thread(target=recover, name="global-audio-recovery", daemon=True).start()
        return True

    def global_audio_recovery_active(self) -> bool:
        return self._global_audio_recovery_requested

    def global_audio_recovery_ready_for_release(self) -> bool:
        return self._global_audio_recovery_ready_for_release

    def release_global_audio_recovery_mute(self) -> bool:
        """Explicitly release both decks after the operator reviewed recovery."""
        if not self._global_audio_recovery_ready_for_release:
            return False
        self._global_audio_recovery_ready_for_release = False
        self.deck_a.set_emergency_muted(False)
        self.deck_b.set_emergency_muted(False)
        self.crossfader.apply()
        if self._overlay_master_mute_setter is not None:
            self._overlay_master_mute_setter(self.crossfader.output_muted)
        self._queue_service.record_audit_event("AUDIO_GLOBAL_RECOVERY_MUTE_RELEASED", details={})
        self._view.show_queue_warning(
            "Audioausgabe bewusst freigegeben. Die Automatik bleibt pausiert."
        )
        self._publish_recovery_return_requirements(force=True)
        return True

    def _prepare_global_audio_recovery(self) -> None:
        self._recovery_return_validation_required = True
        self._pause_automatic_queue("Globale Audio-Reparatur angefordert")
        self._preload_generation += 1
        self._preload_in_progress = False
        if not self._transition.is_transitioning:
            self._transition.reset()
        self.deck_a.set_emergency_muted(True)
        self.deck_b.set_emergency_muted(True)
        self.crossfader.apply()
        if self._overlay_master_mute_setter is not None:
            self._overlay_master_mute_setter(True)
        self._view.show_queue_warning(
            "Globale Audio-Reparatur läuft. Die gesamte Ausgabe bleibt gesperrt."
        )
        self._publish_recovery_return_requirements(force=True)

    def _finish_global_audio_recovery(self, result: GlobalAudioRecoveryResult) -> None:
        self._global_audio_recovery_requested = False
        self._global_audio_recovery_ready_for_release = True
        if result.success:
            self._view.show_queue_warning(
                "Globale Audio-Reparatur abgeschlossen. Beide Decks bleiben bis zur Freigabe stumm."
            )
        else:
            self._view.show_queue_warning(
                f"Globale Audio-Reparatur fehlgeschlagen: {result.message or result.error_code}"
            )
        self._publish_recovery_return_requirements(force=True)

    def enter_one_deck_mode(self, active_deck_id: str, reason: str) -> AudioOperatingModeSnapshot:
        snapshot = self._one_deck_mode.enter(active_deck_id, reason)
        self._recovery_return_validation_required = True
        assert snapshot.unavailable_deck_id is not None
        self._preload_generation += 1
        self._preload_in_progress = False
        self._auto_load_suppressed_decks.add(snapshot.unavailable_deck_id)
        self._deck(snapshot.unavailable_deck_id).set_emergency_muted(True)
        self.crossfader.set_position(0.0 if active_deck_id.upper() == "A" else 1.0)
        self._show_automatic_status(
            self._automatic_status_state,
            f"EIN-DECK-BETRIEB · Deck {active_deck_id.upper()}",
        )
        self._publish_recovery_return_requirements(force=True)
        return snapshot

    def return_to_two_deck_mode(self) -> AudioOperatingModeSnapshot:
        assessment = self.assess_recovery_return()
        if not assessment.allowed:
            raise RuntimeError(assessment.message)
        recovery_active = self._emergency is not None and self._emergency.recovery_active()
        snapshot = self._one_deck_mode.return_to_two_deck(
            self._emergency_state.snapshot(), recovery_active=recovery_active
        )
        if self._transition.state != TransitionState.IDLE:
            self._transition.reset()
        self._recovery_return_validation_required = False
        self._auto_load_suppressed_decks.difference_update({"A", "B"})
        self.deck_a.set_emergency_muted(False)
        self.deck_b.set_emergency_muted(False)
        self._show_automatic_status(self._automatic_status_state, "Zwei-Deck-Betrieb")
        self._auto_load()
        self._publish_recovery_return_requirements(force=True)
        return snapshot

    def assess_recovery_return(self) -> RecoveryReturnAssessment:
        """Validate every safety prerequisite before normal operation can resume."""
        recovery_active = self._global_audio_recovery_requested or (
            self._emergency is not None and self._emergency.recovery_active()
        )
        if recovery_active:
            return RecoveryReturnAssessment(False, "RECOVERY_ACTIVE", "Recovery läuft noch")
        if self._global_audio_recovery_ready_for_release:
            return RecoveryReturnAssessment(
                False,
                "GLOBAL_MUTE_NOT_RELEASED",
                "Die globale Sicherheits-Stummschaltung wurde noch nicht freigegeben",
            )
        if self.audio_output_device_recovery_state() != "normal":
            return RecoveryReturnAssessment(
                False,
                "OUTPUT_DEVICE_NOT_CONFIRMED",
                "Das Audiogerät wurde noch nicht wiederhergestellt und bestätigt",
            )
        health = self._emergency_state.snapshot()
        if health.deck_a != DeckHealth.HEALTHY or health.deck_b != DeckHealth.HEALTHY:
            return RecoveryReturnAssessment(
                False, "DECKS_NOT_HEALTHY", "Beide Decks müssen gesund sein"
            )
        if self._settings is not None:
            configured_device = self._settings.audio_output_device().strip()
            if configured_device:
                try:
                    available_devices = {
                        device_id for device_id, _name in self.deck_a.backend.list_output_devices()
                    }
                except Exception as exc:
                    return RecoveryReturnAssessment(
                        False,
                        "OUTPUT_DEVICE_CHECK_FAILED",
                        f"Audiogerät konnte nicht geprüft werden: {exc}",
                    )
                if configured_device not in available_devices:
                    return RecoveryReturnAssessment(
                        False,
                        "OUTPUT_DEVICE_UNAVAILABLE",
                        "Das konfigurierte Audiogerät ist nicht verfügbar",
                    )
        if self._transition.is_transitioning:
            return RecoveryReturnAssessment(
                False,
                "TRANSITION_STATE_UNSAFE",
                f"Übergangszustand ist nicht stabil: {self._transition.state.value}",
            )
        queue_error = self._queue_deck_consistency_error()
        if queue_error:
            return RecoveryReturnAssessment(
                False, "QUEUE_DECK_ASSIGNMENT_INCONSISTENT", queue_error
            )
        return RecoveryReturnAssessment(True)

    def recovery_return_requirements(self) -> tuple[RecoveryReturnRequirement, ...]:
        """Return all recovery gates for a complete operator-facing checklist."""
        recovery_finished = not (
            self._global_audio_recovery_requested
            or (self._emergency is not None and self._emergency.recovery_active())
        )
        mute_released = not self._global_audio_recovery_ready_for_release
        device_confirmed = self.audio_output_device_recovery_state() == "normal"
        health = self._emergency_state.snapshot()
        decks_healthy = health.deck_a == DeckHealth.HEALTHY and health.deck_b == DeckHealth.HEALTHY
        transition_stable = not self._transition.is_transitioning
        queue_consistent = not self._queue_deck_consistency_error()
        return (
            RecoveryReturnRequirement("RECOVERY_FINISHED", recovery_finished, "Recovery beendet"),
            RecoveryReturnRequirement(
                "GLOBAL_MUTE_RELEASED", mute_released, "Sicherheits-Mute freigegeben"
            ),
            RecoveryReturnRequirement(
                "OUTPUT_DEVICE_CONFIRMED", device_confirmed, "Audiogerät bestätigt"
            ),
            RecoveryReturnRequirement("DECKS_HEALTHY", decks_healthy, "Beide Decks gesund"),
            RecoveryReturnRequirement(
                "TRANSITION_STABLE", transition_stable, "Übergangszustand stabil"
            ),
            RecoveryReturnRequirement(
                "QUEUE_CONSISTENT", queue_consistent, "Queue-/Deck-Zuordnung konsistent"
            ),
        )

    def resume_automatic_after_recovery(self) -> bool:
        """Explicitly resume automation only after every recovery gate passes."""
        if not self._recovery_return_gate_required():
            self._view.show_queue_warning("Keine ausstehende Notfall-Rückkehrprüfung vorhanden.")
            return False
        assessment = self.assess_recovery_return()
        if not assessment.allowed:
            self._logger.warning(
                "Automatik-Reaktivierung abgelehnt: code=%s, grund=%s, "
                "transition=%s, preload_aktiv=%s, runner_aktiv=%s, pausegrund=%s",
                assessment.error_code,
                assessment.message,
                self._transition.state.value,
                self._preload_in_progress,
                self._automatic_run_active,
                self._automatic_pause_reason or "-",
            )
            self._view.show_queue_warning(f"Automatik bleibt gesperrt: {assessment.message}")
            self._queue_service.record_audit_event(
                "AUTOMATIC_RECOVERY_RESUME_REJECTED",
                details={"reason": assessment.error_code},
            )
            self._publish_recovery_return_requirements(force=True)
            return False
        self._recovery_return_validation_required = False
        self._queue_service.record_audit_event("AUTOMATIC_RECOVERY_RESUME_CONFIRMED", details={})
        if self._one_deck_mode.snapshot().mode == AudioOperatingMode.ONE_DECK:
            self.return_to_two_deck_mode()
        self.start_automatic_queue()
        self._publish_recovery_return_requirements(force=True)
        return True

    def _publish_recovery_return_requirements(self, *, force: bool = False) -> None:
        visible = self._recovery_return_gate_required()
        if not visible:
            if force or self._last_recovery_return_render is not None:
                self._view.show_recovery_return_requirements((), False)
                self._last_recovery_return_render = None
            return
        now = monotonic()
        if not force and now < self._next_recovery_return_render:
            return
        self._next_recovery_return_render = now + 1.0
        requirements = self.recovery_return_requirements()
        if force or requirements != self._last_recovery_return_render:
            self._view.show_recovery_return_requirements(requirements, True)
            self._last_recovery_return_render = requirements

    def _queue_deck_consistency_error(self) -> str:
        mapped_ids = [
            queue_id for queue_id in self._deck_queue_ids.values() if queue_id is not None
        ]
        if len(mapped_ids) != len(set(mapped_ids)):
            return "Ein Queue-Eintrag ist mehreren Decks zugeordnet"
        active_statuses = {QueueStatus.LOADED, QueueStatus.PLAYING}
        active_entries = [
            entry for entry in self._queue_service.entries() if entry.status in active_statuses
        ]
        for deck_id in ("A", "B"):
            queue_id = self._deck_queue_ids[deck_id]
            assigned = [entry for entry in active_entries if entry.loaded_deck == deck_id]
            if queue_id is None:
                if assigned:
                    return f"Deck {deck_id} besitzt eine Queue-Zuordnung ohne Controllerbezug"
                continue
            entry = self._queue_service.entry(queue_id)
            deck_track = self._deck(deck_id).model.loaded_track
            if entry is None or entry.status not in active_statuses:
                return f"Queue-Eintrag für Deck {deck_id} ist nicht aktiv"
            if entry.loaded_deck != deck_id:
                return f"Queue-Eintrag verweist nicht auf Deck {deck_id}"
            if deck_track is None or deck_track.id != entry.track_id:
                return f"Geladener Titel und Queue-Eintrag von Deck {deck_id} unterscheiden sich"
            if len(assigned) != 1 or assigned[0].queue_id != queue_id:
                return f"Deck {deck_id} besitzt mehrere oder abweichende Queue-Zuordnungen"
        return ""

    def audio_operating_mode(self) -> AudioOperatingModeSnapshot:
        return self._one_deck_mode.snapshot()

    def set_deck_emergency_muted(self, deck_id: str, muted: bool) -> None:
        normalized = deck_id.upper()
        if normalized not in {"A", "B"}:
            raise ValueError("Unbekanntes Deck")
        deck = self.deck_a if normalized == "A" else self.deck_b
        deck.set_emergency_muted(muted)
        self.crossfader.apply()
        self._queue_service.record_audit_event(
            "DECK_EMERGENCY_MUTE",
            details={"deck_id": normalized, "muted": bool(muted)},
        )

    def initialize(self) -> None:
        """Load bounded data and start one shared status loop."""
        if self._unresolved_emergency_incident is not None:
            incident = self._unresolved_emergency_incident
            device = incident.audio_device_id or "Systemstandard/unbekannt"
            summary = (
                f"Zustand: {incident.system_state} · Grund: {incident.reason or 'ohne Angabe'}\n"
                f"Deck A: {incident.deck_a_health} · Deck B: {incident.deck_b_health} · "
                f"Audiogerät: {device}\nLetzte Aktualisierung: {incident.updated_at}"
            )
            self._view.show_unresolved_emergency_incident(incident.incident_id, summary)
            self._view.show_queue_warning(
                f"Ungelöster Audiovorfall #{incident.incident_id} aus einer früheren Ausführung."
            )
        self._refresh_catalog_page()
        self._publish_emergency_dashboard(force=True)
        self._queue_service.restore_deck_assignments(self.deck_a, self.deck_b)
        self._recover_deck_queue_ids()
        restored_queue_ids = (
            {entry.queue_id for entry in self._queue_service.entries()}
            if self._session is not None and self._session.status.value == "recovered"
            else set()
        )
        self._view.show_restored_queue_entries(restored_queue_ids)
        for deck in (self.deck_a, self.deck_b):
            if deck.model.loaded_track is not None:
                self._apply_cue_points(deck)
                self._load_cover_async(deck.model.deck_id, deck.model.loaded_track)
        self._refresh_queue()
        self._schedule_source_availability_checks()
        self._refresh_all()
        self._view.show_player_mode(self.player_mode.value)
        self._view.show_automatic_playback(False)
        self._show_automatic_status("ready")
        self._view.show_queue_duplicate_policy(self.queue_duplicate_policy)
        self._view.show_queue_duration_mode(self.queue_stats_use_effective_cues)
        self._view.show_queue_artist_repetition(self.queue_artist_repetition_enabled)
        self._view.show_fade_settings(self.fade_duration, self.fade_out_stops_deck)
        if self._session is not None:
            self._view.show_session(self._session)
        if self._settings is not None:
            self._view.show_start_settings(
                self._settings.restore_last_session(), self._settings.fullscreen_on_start()
            )
            self._view.show_file_browser_setting(self._settings.file_browser_enabled())
            self._view.show_production_mode(
                not self._settings.performance_diagnostics_enabled()
                and not self._settings.background_analysis_enabled()
            )
            try:
                devices = self.deck_a.backend.list_output_devices()
                self._view.show_audio_devices(
                    devices,
                    self._settings.audio_output_device(),
                )
                if self._deck_health_monitor is not None:
                    self._deck_health_monitor.report_output_device(
                        self._settings.audio_output_device(),
                        {device_id for device_id, _name in devices},
                    )
            except Exception as exc:
                self._handle_error("Audiogeräte konnten nicht ermittelt werden", exc)
        self._refresh_saved_queues()
        if self._session is not None and self._session.selected_playlist is not None:
            self._view.select_saved_queue(self._session.selected_playlist)
        self._auto_load()
        self._view.schedule(self._status_interval_ms(), self._status_tick)
        if self._performance_settings.enabled:
            self._callback_state.heartbeat()
            self._view.schedule(
                self._performance_settings.gui_heartbeat_interval_ms, self._heartbeat_tick
            )
            self._view.schedule(5000, self._memory_tick)

    def search(self, query: str) -> None:
        try:
            self._catalog_query = query.strip()
            self._catalog_page = 0
            self._refresh_catalog_page()
        except Exception as exc:
            self._handle_error("Suche fehlgeschlagen", exc)

    def change_catalog_page(self, direction: int) -> None:
        target = max(0, self._catalog_page + (-1 if direction < 0 else 1))
        total = self._library_service.count(self._catalog_query)
        page_count = max(1, (total + self.CATALOG_PAGE_SIZE - 1) // self.CATALOG_PAGE_SIZE)
        self._catalog_page = min(target, page_count - 1)
        self._refresh_catalog_page()

    def load_catalog_track(self, track_id: int, deck_id: str) -> None:
        track = next((item for item in self._catalog if item.id == track_id), None)
        if track is None:
            self._view.show_error(
                "Titel nicht gefunden", "Der ausgewählte Titel ist nicht mehr verfügbar."
            )
            return
        self._load_track(track, self._deck(deck_id))

    def import_file(self, file_path: str, deck_id: str) -> None:
        """Import an explicitly chosen file and load it into a deck."""
        try:
            track = self._library_service.import_file(Path(file_path))
            self._refresh_catalog_page()
            self._load_track(track, self._deck(deck_id))
        except Exception as exc:
            self._handle_error("Datei konnte nicht geladen werden", exc)

    def import_file_to_queue(self, file_path: str) -> None:
        """Import one explicitly selected file and append it to the active queue."""
        try:
            track = self._library_service.import_file(Path(file_path))
            self._queue.add(track.id, source="catalog")
            self._refresh_catalog_page()
            self._refresh_queue()
            self._auto_load()
        except Exception as exc:
            self._handle_error("Datei konnte nicht zur Queue hinzugefügt werden", exc)

    def import_file_to_catalog(self, file_path: str) -> None:
        """Import or refresh one file without loading a deck or changing the queue."""
        path = Path(file_path)
        try:
            existed = str(path.resolve()).casefold() in self._library_service.known_file_paths(
                [path]
            )
            self._library_service.import_file(path)
            self._refresh_catalog_page()
            self._view.show_catalog_import_result(0 if existed else 1, 1 if existed else 0, 0)
        except Exception as exc:
            self._handle_error("Datei konnte nicht in den Katalog aufgenommen werden", exc)

    def add_catalog_track_to_queue(self, track_id: int) -> None:
        try:
            self._queue.add(track_id)
            self._refresh_queue()
            self._auto_load()
        except Exception as exc:
            self._handle_error("Queue konnte nicht geändert werden", exc)

    def remove_catalog_track(self, track_id: int) -> None:
        try:
            self._library_service.remove_from_catalog(track_id)
            self._catalog = [track for track in self._catalog if track.id != track_id]
            self._refresh_catalog_page()
        except ValueError as exc:
            self._handle_error("Titel konnte nicht entfernt werden", exc)

    def import_directory_to_queue(self, directory: str) -> None:
        """Import a directory playlist without blocking the Tk main thread."""
        root = Path(directory)
        self._view.show_directory_import_progress(0, None, True)

        def worker() -> None:
            added = 0
            skipped = 0
            failed = 0
            try:
                files = self._library_service.directory_audio_files(root)
            except Exception as exc:
                self._logger.exception("Verzeichnisimport fehlgeschlagen: %s", exc)
                error = exc

                def report_error() -> None:
                    self._view.show_directory_import_progress(0, None, False)
                    self._handle_error("Verzeichnisimport fehlgeschlagen", error)

                self._publish_gui_callback(report_error, "directory_import")
                return
            total = len(files)
            self._publish_gui_callback(
                lambda: self._view.show_directory_import_progress(0, total, True),
                "directory_import",
                coalesce_key="directory-import-progress",
            )
            for processed, file_path in enumerate(files, start=1):
                try:
                    track = self._library_service.import_file(file_path)
                    self._queue.add(track.id, source=f"directory:{root}")
                    added += 1
                except ValueError as exc:
                    if "bereits in der aktiven Queue" in str(exc):
                        skipped += 1
                    else:
                        failed += 1
                        self._logger.warning("Datei übersprungen: %s: %s", file_path, exc)
                except Exception as exc:
                    failed += 1
                    self._logger.exception("Dateiimport fehlgeschlagen: %s: %s", file_path, exc)
                if processed == total or processed % 10 == 0:
                    current = processed
                    self._publish_gui_callback(
                        lambda current=current: self._view.show_directory_import_progress(  # type: ignore[misc]
                            current, total, True
                        ),
                        "directory_import",
                        coalesce_key="directory-import-progress",
                    )

            def finish() -> None:
                # Keep the dispatcher callback short.  Catalog queries, queue snapshot
                # delivery, statistics and autoload each get their own Tk turn so a
                # large directory cannot monopolize the event loop on completion.
                def refresh_catalog() -> None:
                    self._refresh_catalog_page()
                    self._view.schedule(1, refresh_queue)

                def refresh_queue() -> None:
                    self._refresh_queue(refresh_stats=False)
                    self._view.schedule(1, refresh_statistics)

                def refresh_statistics() -> None:
                    self._refresh_queue_stats()
                    self._view.schedule(1, complete_import)

                def complete_import() -> None:
                    self._auto_load()
                    self._view.show_directory_import_progress(total, total, False)
                    self._view.show_directory_import_result(added, skipped, failed)

                self._view.schedule(1, refresh_catalog)

            if not self._closed:
                self._publish_gui_callback(finish, "directory_import")

        self._start_worker(worker, "directory-playlist-import", "directory_import")

    def import_directory_to_catalog(self, directory: str) -> None:
        """Import a directory recursively without changing decks or the queue."""
        root = Path(directory)
        self._view.show_directory_import_progress(0, None, True)

        def worker() -> None:
            try:
                files = self._library_service.directory_audio_files(root)
                known = self._library_service.known_file_paths(files)
            except Exception as exc:
                error = exc

                def report_error() -> None:
                    self._view.show_directory_import_progress(0, None, False)
                    self._handle_error("Katalogimport fehlgeschlagen", error)

                self._publish_gui_callback(report_error, "catalog_directory_import")
                return
            total = len(files)
            created = updated = failed = 0
            self._publish_gui_callback(
                lambda: self._view.show_directory_import_progress(0, total, True),
                "catalog_directory_import",
                coalesce_key="directory-import-progress",
            )
            for processed, file_path in enumerate(files, start=1):
                existed = str(file_path.resolve()).casefold() in known
                try:
                    self._library_service.import_file(file_path)
                    if existed:
                        updated += 1
                    else:
                        created += 1
                except Exception as exc:
                    failed += 1
                    self._logger.warning("Katalogdatei übersprungen: %s: %s", file_path, exc)
                if processed == total or processed % 10 == 0:
                    current = processed
                    self._publish_gui_callback(
                        lambda current=current: self._view.show_directory_import_progress(  # type: ignore[misc]
                            current, total, True
                        ),
                        "catalog_directory_import",
                        coalesce_key="directory-import-progress",
                    )

            def finish() -> None:
                self._refresh_catalog_page()
                self._view.show_directory_import_progress(total, total, False)
                self._view.show_catalog_import_result(created, updated, failed)

            if not self._closed:
                self._publish_gui_callback(finish, "catalog_directory_import")

        self._start_worker(worker, "catalog-directory-import", "catalog_directory_import")

    def load_queue_track(self, queue_id: int, deck_id: str) -> None:
        entry = next(
            (item for item in self._queue_service.entries() if item.queue_id == queue_id), None
        )
        if entry is None:
            return
        if entry.status != QueueStatus.WAITING:
            self._view.show_queue_warning(
                "Nur wartende Queue-Titel können direkt in ein Deck geladen werden. "
                "Gespielte Titel zuerst wieder auf wartend setzen."
            )
            return
        track = self._library_service.get_track(entry.track_id)
        if track is None:
            self._view.show_queue_warning("Der Queue-Titel wurde im Katalog nicht gefunden")
            return
        if not self._load_track(track, self._deck(deck_id), queue_id=queue_id):
            return
        try:
            self._queue_service.mark_loaded(queue_id, deck_id)
        except ValueError as exc:
            self._view.show_queue_warning(str(exc))
            return
        self._refresh_queue()

    def remove_queue_track(self, queue_id: int) -> None:
        try:
            self._queue.remove(queue_id)
        except ValueError as exc:
            self._handle_error("Queue-Eintrag kann nicht entfernt werden", exc)
            return
        self._refresh_queue()

    def remove_selected_queue_track(self) -> bool:
        """Remove the selected non-playing row, unloading prepared media first."""
        queue_id = self._selected_queue_entry_id
        if queue_id is None:
            self._view.show_queue_warning("Bitte zuerst einen Queue-Titel markieren")
            return False
        entry = self._queue_service.entry(queue_id)
        if entry is None:
            self.select_queue_entry(None)
            self._refresh_queue()
            return False
        if entry.status == QueueStatus.PLAYING:
            self._view.show_queue_warning(
                "Der aktuell spielende Titel kann nicht aus der Queue gelöscht werden"
            )
            return False
        try:
            if entry.status in {QueueStatus.PREPARING, QueueStatus.READY}:
                self._release_prepared_queue_entry(entry)
                self._queue_service.remove_prepared(queue_id)
            else:
                self._queue.remove(queue_id)
        except ValueError as exc:
            self._handle_error("Markierter Queue-Titel kann nicht gelöscht werden", exc)
            return False
        self.select_queue_entry(None)
        self._refresh_queue()
        self._auto_load()
        return True

    def remove_prepared_queue_track(self, queue_id: int) -> None:
        entry = self._queue_service.entry(queue_id)
        if entry is None:
            self._handle_error(
                "Vorbereiteter Titel kann nicht entfernt werden",
                ValueError("Queue-Eintrag nicht gefunden"),
            )
            return
        try:
            self._release_prepared_queue_entry(entry)
            self._queue_service.remove_prepared(queue_id)
        except ValueError as exc:
            self._handle_error("Vorbereiteter Titel kann nicht entfernt werden", exc)
            return
        self._refresh_queue()
        self._auto_load()

    def move_prepared_queue_track(self, queue_id: int, direction: int) -> None:
        entry = self._queue_service.entry(queue_id)
        if entry is None:
            self._handle_error(
                "Vorbereiteter Titel kann nicht verschoben werden",
                ValueError("Queue-Eintrag nicht gefunden"),
            )
            return
        try:
            self._release_prepared_queue_entry(entry)
            self._queue_service.reset_prepared(queue_id)
            self._queue.move(queue_id, direction)
        except ValueError as exc:
            self._handle_error("Vorbereiteter Titel kann nicht verschoben werden", exc)
            return
        self._refresh_queue()

    def _release_prepared_queue_entry(self, entry: QueueEntry) -> None:
        if entry.status == QueueStatus.PREPARING:
            self._preload_generation += 1
            self._preload_in_progress = False
            self._transition.abort("Vorbereitung durch Operator abgebrochen")
            self._transition.reset()
            return
        if entry.status != QueueStatus.READY:
            raise ValueError("Der Queue-Eintrag ist weder in Vorbereitung noch bereit")
        if entry.loaded_deck not in {"A", "B"}:
            raise ValueError("Vorbereiteter Titel besitzt keine gültige Deck-Zuordnung")
        deck = self._deck(entry.loaded_deck)
        if deck.backend.is_playing() or deck.model.state == DeckState.PLAYING:
            raise ValueError("Ein spielender Queue-Eintrag kann nicht bearbeitet werden")
        cleanup = deck.detach_for_cleanup()
        self._start_worker(
            cleanup,
            f"prepared-release-{entry.loaded_deck}",
            "deck_cleanup",
            executor=self._preload_executor,
        )
        self._deck_queue_ids[entry.loaded_deck] = None
        self._view.show_deck_cover(entry.loaded_deck, None)

    def move_queue_track(self, queue_id: int, direction: int) -> None:
        try:
            if direction < 0:
                self._queue_service.move_up(queue_id)
            else:
                self._queue_service.move_down(queue_id)
        except ValueError as exc:
            self._handle_error("Queue-Eintrag kann nicht verschoben werden", exc)
            return
        self._refresh_queue()

    def move_queue_track_to_top(self, queue_id: int) -> None:
        self._queue.move_to_top(queue_id)
        self._refresh_queue()

    def move_queue_track_to_end(self, queue_id: int) -> None:
        self._queue.move_to_end(queue_id)
        self._refresh_queue()

    def set_queue_track_priority(self, queue_id: int, priority: int) -> None:
        self._queue.set_priority(queue_id, priority)
        self._refresh_queue()

    def toggle_queue_track_lock(self, queue_id: int) -> None:
        self._queue.toggle_lock(queue_id)
        self._refresh_queue()

    def queue_cue_state(self, queue_id: int) -> QueueCueEditorState:
        if self._cue_points is None:
            raise ValueError("Cue-Service ist nicht verfügbar")
        entry = self._queue_service.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        track = self._queue_service.track(entry.track_id)
        if track is None:
            raise ValueError("Titel nicht gefunden")
        resolved = self._cue_points.resolve(track, self.AUTOMATIC_OVERLAP_SECONDS, entry)
        title = f"{track.artist} — {track.title}" if track.artist else track.title
        return QueueCueEditorState(
            queue_id,
            title,
            entry.cue_in_override,
            entry.cue_out_override,
            entry.fade_duration_override,
            resolved,
        )

    def save_queue_cues(
        self,
        queue_id: int,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
    ) -> QueueCueEditorState:
        if self._cue_points is None:
            raise ValueError("Cue-Service ist nicht verfügbar")
        entry = self._queue_service.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        self._confirm_queue_cue_edit(entry)
        track = self._queue_service.track(entry.track_id)
        if track is None:
            raise ValueError("Titel nicht gefunden")
        self._cue_points.validate_values(track, cue_in, cue_out, fade_duration)
        self._queue_service.set_cue_overrides(queue_id, cue_in, cue_out, fade_duration, "queue")
        self._apply_changed_queue_cues(entry)
        self._refresh_queue()
        return self.queue_cue_state(queue_id)

    def refresh_queue_view(self) -> None:
        self._refresh_queue()

    def track_cues_changed(self, track_id: int, has_manual_cues: bool) -> None:
        """Refresh only catalog/queue rows whose cue presentation changed."""
        self._view.show_track_cues_changed(track_id, has_manual_cues)

    @property
    def metadata_editor_service(self) -> MetadataEditorService:
        return self._metadata_editor

    @property
    def catalog_maintenance_service(self) -> CatalogMaintenanceService:
        return self._catalog_maintenance

    def library_track(self, track_id: int) -> Track | None:
        """Load one catalog track for an existing worker-backed UI request."""
        return self._library_service.get_track(track_id)

    def track_metadata_changed(self, track_id: int) -> None:
        """Refresh only visible catalog and queue rows for one changed track."""

        def publish(track: Track | None) -> None:
            if track is not None:
                self._view.show_track_metadata_changed(track)

        self.load_track_editor_view_model(
            lambda: self._library_service.get_track(track_id),
            publish,
            lambda error: self._logger.warning(
                "Titelzeile nach Metadatenänderung nicht aktualisiert: %s", error
            ),
        )

    def track_editor_equalizer_state(
        self,
        track: Track,
    ) -> tuple[str | None, str | None, str]:
        """Resolve cached title/genre/global EQ assignment for editor presentation."""
        if self._equalizer_resolver is None:
            key = self._default_equalizer_preset
            return key, key, "GLOBAL" if key else "DISABLED"
        preset, source = self._equalizer_resolver.resolve(
            track,
            None,
            self._default_equalizer_preset,
        )
        if preset is None:
            return None, "Aus", source
        return preset.preset_id, preset.name, source

    def load_track_editor_view_model(
        self,
        load: Callable[[], T],
        completed: Callable[[T], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        """Build editor data in a bounded worker and dispatch its immutable result."""

        def worker() -> None:
            try:
                with self._performance.measure(
                    "track_editor.load",
                    warning_threshold_ms=250.0,
                ):
                    view_model = load()
            except Exception as exc:

                def publish_failure(error: Exception = exc) -> None:
                    failed(error)

                self._publish_gui_callback(publish_failure, "track_editor_load")
                return
            self._publish_gui_callback(
                lambda: completed(view_model),
                "track_editor_load",
            )

        return self._start_worker(
            worker,
            "track-editor-load",
            "track_editor_load",
            executor=self._track_editor_executor,
        )

    def select_queue_entry(self, queue_id: int | None) -> None:
        """Publish targeted deselection/selection events without changing queue data."""
        if queue_id is not None and not any(
            entry.queue_id == queue_id for entry in self._queue_entries_cache
        ):
            return
        previous = self._selected_queue_entry_id
        if previous == queue_id:
            return
        self._selected_queue_entry_id = queue_id
        self._queue_revision += 1
        events = tuple(
            QueueViewEvent(
                QueueViewEventType.SELECTION_CHANGED,
                entry_id,
                self._queue_revision,
                selected=selected,
            )
            for entry_id, selected in ((previous, False), (queue_id, True))
            if entry_id is not None
        )
        self._deliver_queue_view_update(
            QueueViewUpdate(
                events,
                tuple(self._queue_entries_cache),
                dict(self._queue_tracks_cache),
            )
        )

    def adopt_title_cues_for_queue(self, queue_id: int) -> QueueCueEditorState:
        if self._cue_points is None:
            raise ValueError("Cue-Service ist nicht verfügbar")
        entry = self._editable_queue_entry(queue_id)
        track = self._queue_service.track(entry.track_id)
        if track is None:
            raise ValueError("Titel nicht gefunden")
        resolved = self._cue_points.resolve(track, self.AUTOMATIC_OVERLAP_SECONDS)
        self._queue_service.set_cue_overrides(
            queue_id,
            resolved.cue_in,
            resolved.cue_out,
            resolved.fade_duration,
            "queue",
        )
        self._apply_changed_queue_cues(entry)
        self._refresh_queue()
        return self.queue_cue_state(queue_id)

    def reset_queue_cues(self, queue_id: int) -> QueueCueEditorState:
        entry = self._editable_queue_entry(queue_id)
        self._queue_service.set_cue_overrides(queue_id, None, None, None, "inherited")
        self._apply_changed_queue_cues(entry)
        self._refresh_queue()
        return self.queue_cue_state(queue_id)

    def _editable_queue_entry(self, queue_id: int) -> QueueEntry:
        entry = self._queue_service.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        self._confirm_queue_cue_edit(entry)
        return entry

    def _confirm_queue_cue_edit(self, entry: QueueEntry) -> None:
        if entry.status == QueueStatus.WAITING:
            return
        if entry.status not in {QueueStatus.LOADED, QueueStatus.PLAYING}:
            raise ValueError("Dieser Queue-Eintrag kann nicht mehr bearbeitet werden")
        if not self._view.confirm_queue_cue_change(entry.status.value):
            raise ValueError("Cue-Änderung wurde abgebrochen")

    def _apply_changed_queue_cues(self, original_entry: QueueEntry) -> None:
        deck = next(
            (
                candidate
                for candidate in (self.deck_a, self.deck_b)
                if self._deck_queue_ids[candidate.model.deck_id] == original_entry.queue_id
            ),
            None,
        )
        if deck is None or self._transition.is_transitioning or self._cue_points is None:
            return
        updated = self._queue_service.entry(original_entry.queue_id)
        track = deck.model.loaded_track
        if updated is None or track is None:
            return
        boundaries = self._cue_points.resolve(track, self.AUTOMATIC_OVERLAP_SECONDS, updated)
        if (
            deck.model.state == DeckState.PLAYING
            and boundaries.cue_out - deck.model.position
            < self.MINIMUM_TRANSITION_PREPARATION_SECONDS
        ):
            deck.model.cue_warning = (
                "Cue-Änderung ist für diesen Durchlauf zu spät und gilt ab der nächsten Wiedergabe"
            )
            return
        if self._queue_service.apply_cues_to_deck(updated, deck) is None:
            self._apply_cue_points(deck)

    def clear_waiting_queue(self) -> None:
        self._queue_service.reconcile_deck_assignments(self.deck_a, self.deck_b)
        self._queue.clear_waiting()
        self._refresh_queue()

    def clear_complete_queue(self) -> None:
        self.stop_automatic_queue(reason_code="QUEUE_CLEARED")
        try:
            prepared = [
                entry
                for entry in self._queue_service.entries()
                if entry.status in {QueueStatus.PREPARING, QueueStatus.READY}
            ]
            for entry in prepared:
                deck = self._deck(entry.loaded_deck) if entry.loaded_deck in {"A", "B"} else None
                if deck is not None and (
                    deck.backend.is_playing() or deck.model.state == DeckState.PLAYING
                ):
                    if entry.status == QueueStatus.READY:
                        self._queue_service.mark_playing(entry.queue_id)
                    continue
                self._release_prepared_queue_entry(entry)
            self._queue.clear_complete()
        except ValueError as exc:
            self._handle_error("Queue kann nicht geleert werden", exc)
            return
        self._refresh_queue()

    def shuffle_waiting_queue(self) -> None:
        self._queue_service.reconcile_deck_assignments(self.deck_a, self.deck_b)
        shuffled = self._queue.shuffle_waiting()
        self._refresh_queue()
        self._view.show_queue_shuffle_result(shuffled)

    def save_current_queue(self, name: str, snapshot_cues: bool = True) -> None:
        if self._saved_queues is None:
            self._view.show_error("Speichern nicht verfügbar", "Queue-Service fehlt")
            return
        try:
            self._saved_queues.save_current(name, snapshot_cues)
            self._refresh_saved_queues()
        except ValueError as exc:
            self._handle_error("Queue konnte nicht gespeichert werden", exc)

    def load_saved_queue(
        self,
        saved_queue_id: int,
        replace_waiting: bool,
        shuffle_tracks: bool = False,
        use_saved_cues: bool = True,
        play_all_in_order: bool = False,
    ) -> None:
        if self._saved_queues is None:
            self._view.show_error("Laden nicht verfügbar", "Queue-Service fehlt")
            return
        try:
            if replace_waiting:
                prepared = [
                    entry
                    for entry in self._queue_service.entries()
                    if entry.status in {QueueStatus.PREPARING, QueueStatus.READY}
                ]
                for entry in prepared:
                    deck = (
                        self._deck(entry.loaded_deck) if entry.loaded_deck in {"A", "B"} else None
                    )
                    if deck is not None and (
                        deck.backend.is_playing() or deck.model.state == DeckState.PLAYING
                    ):
                        continue
                    self._release_prepared_queue_entry(entry)
                    self._queue_service.remove_prepared(entry.queue_id)
            previous_queue_ids = {entry.queue_id for entry in self._queue_service.entries()}
            added, skipped = self._saved_queues.load(
                saved_queue_id, replace_waiting, shuffle_tracks, use_saved_cues
            )
            loaded_entries = [
                entry
                for entry in self._queue_service.entries()
                if entry.queue_id not in previous_queue_ids
            ]
            if play_all_in_order and self._repetition is not None:
                for entry in loaded_entries:
                    self._repetition.allow_queue_entry(entry.queue_id)
                    self._queue_service.record_audit_event(
                        "REPETITION_OVERRIDE",
                        entity_type="QUEUE",
                        entity_id=entry.queue_id,
                        details={
                            "reason": "PLAY_ALL_IN_ORDER",
                            "saved_queue_id": saved_queue_id,
                        },
                    )
                self._queue_service.record_audit_event(
                    "PLAY_ALL_IN_ORDER_ENABLED",
                    entity_type="SAVED_QUEUE",
                    entity_id=saved_queue_id,
                    details={
                        "queue_ids": [entry.queue_id for entry in loaded_entries],
                        "replace_waiting": replace_waiting,
                    },
                )
            if self._session is not None and self._session_service is not None:
                self._session_service.select_playlist(self._session.session_id, saved_queue_id)
                self._session = replace(self._session, selected_playlist=saved_queue_id)
            self._refresh_queue()
            self._auto_load()
            self._view.show_saved_queue_load_result(added, skipped)
        except ValueError as exc:
            self._handle_error("Gespeicherte Queue konnte nicht geladen werden", exc)

    def show_saved_queue(self, saved_queue_id: int) -> None:
        if self._saved_queues is None:
            return
        try:
            playlist = self._saved_queues.get(saved_queue_id)
            tracks_by_id = self._library_service.get_tracks(list(playlist.track_ids))
            tracks = [
                tracks_by_id[track_id]
                for track_id in playlist.track_ids
                if track_id in tracks_by_id
            ]
            self._view.show_playlist(playlist, tracks)
        except ValueError as exc:
            self._handle_error("Playlist konnte nicht angezeigt werden", exc)

    def load_playlist_track(self, track_id: int, deck_id: str) -> None:
        track = self._library_service.get_track(track_id)
        if track is None:
            self._view.show_error("Titel nicht gefunden", "Der Playlist-Titel ist nicht verfügbar.")
            return
        self._load_track(track, self._deck(deck_id))

    def add_playlist_track_to_queue(self, track_id: int) -> None:
        self.add_catalog_track_to_queue(track_id)

    def mark_queue_track_played(self, queue_id: int) -> None:
        self._queue.mark_played(queue_id)
        self._refresh_queue()

    def mark_queue_track_skipped(self, queue_id: int, reason: str | None = None) -> None:
        entry = next(
            (item for item in self._queue_service.entries() if item.queue_id == queue_id), None
        )
        self._queue.mark_skipped(queue_id, reason)
        if entry is not None and entry.loaded_deck in {"A", "B"}:
            deck = self._deck(entry.loaded_deck)
            if self._deck_queue_ids[entry.loaded_deck] == queue_id:
                position = deck.model.position
                if self._history is not None:
                    self._history.finish(
                        entry.loaded_deck,
                        CompletionStatus.SKIPPED,
                        position,
                        skip_code=HistoryReasonCode.OPERATOR_SKIP,
                    )
                deck.eject()
                self._deck_queue_ids[entry.loaded_deck] = None
                self._view.show_deck_cover(entry.loaded_deck, None)
        self._refresh_queue()
        self._auto_load()

    def retry_queue_track(self, queue_id: int) -> None:
        try:
            self._queue.retry(queue_id)
            self._refresh_queue()
            self._auto_load()
        except ValueError as exc:
            self._handle_error("Erneuter Versuch nicht möglich", exc)

    def play_repetition_skipped_queue_track(self, queue_id: int) -> None:
        entry = self._queue_service.entry(queue_id)
        if (
            entry is None
            or entry.status != QueueStatus.SKIPPED
            or entry.skip_code not in {"TRACK_REPETITION", "ARTIST_REPETITION"}
        ):
            self._view.show_queue_warning(
                "Nur wegen Wiederholungsschutz übersprungene Titel können erneut freigegeben werden"
            )
            return
        if self._repetition is None:
            self._view.show_queue_warning("Wiederholungsschutz ist nicht verfügbar")
            return
        try:
            self._repetition.allow_queue_entry(queue_id)
            self._queue_service.override_repetition_skip(queue_id)
            self._queue_service.record_audit_event(
                "REPETITION_OVERRIDE",
                entity_type="QUEUE",
                entity_id=queue_id,
                details={"reason": "OPERATOR_PLAY_ANYWAY", "skip_code": entry.skip_code},
            )
        except ValueError as exc:
            self._handle_error("Titel kann nicht freigegeben werden", exc)
            return
        self._refresh_queue()
        self._auto_load()

    def reset_played_queue_track(self, queue_id: int) -> None:
        try:
            self._queue.reset_played(queue_id)
            self._refresh_queue()
        except ValueError as exc:
            self._handle_error("Zurücksetzen nicht möglich", exc)

    def deck_action(self, deck_id: str, action: str, *, automatic: bool = False) -> None:
        resume_automatic_after_action = (
            not automatic and action == "resume" and self._automatic_run_paused
        )
        if not automatic:
            if action == "pause" and self._automatic_run_active:
                self._pause_automatic_queue(f"Deck {deck_id} pausiert")
            elif not resume_automatic_after_action:
                self._manual_override(f"Manuelle Deck-Aktion: {deck_id} {action}")
        deck = self._deck(deck_id)
        try:
            previous_state = deck.model.state
            previous_position = deck.model.position
            previous_track = deck.model.loaded_track
            operations = {
                "play": deck.play,
                "pause": deck.pause,
                "resume": deck.resume,
                "stop": deck.stop,
                "eject": deck.eject,
            }
            if (
                action == "play"
                and deck.model.state in {DeckState.LOADED, DeckState.STOPPED}
                and self._cue_points is not None
                and deck.model.loaded_track is not None
            ):
                deck.seek(self._resolved_boundaries(deck).cue_in)
            if not automatic and action in {"play", "resume"}:
                deck.set_transition_muted(False)
            operations[action]()
            if self._history is not None and action == "pause":
                self._history.pause(deck_id)
            elif self._history is not None and action == "resume":
                self._history.resume(deck_id)
            if action in {"play", "resume"} and deck.model.loaded_track is not None:
                queue_id = self._deck_queue_ids[deck_id]
                if queue_id is not None:
                    try:
                        with self._performance.measure(
                            "deck_action.playback_start.queue",
                            warning_threshold_ms=25.0,
                            context={"deck": deck_id, "queue_id": queue_id},
                        ):
                            with self._queue_playback_generation_lock:
                                self._queue_playback_generations[queue_id] = (
                                    self._queue_playback_generations.get(queue_id, 0) + 1
                                )
                                self._queue_service.mark_playing(queue_id)
                    except ValueError:
                        queue_id = None
                if queue_id is None:
                    with self._performance.measure(
                        "deck_action.playback_start.queue_fallback",
                        warning_threshold_ms=25.0,
                        context={"deck": deck_id},
                    ):
                        queue_id = self._queue_service.mark_playing_for_deck(
                            deck_id, deck.model.loaded_track.id
                        )
                    self._deck_queue_ids[deck_id] = queue_id
                if self._history is not None:
                    with self._performance.measure(
                        "deck_action.playback_start.history",
                        warning_threshold_ms=25.0,
                        context={"deck": deck_id},
                    ):
                        self._history.start(
                            deck_id,
                            deck.model.loaded_track,
                            queue_id,
                            effective_cue_in=deck.model.cue_in,
                            effective_cue_out=deck.model.cue_out,
                        )
                with self._performance.measure(
                    "deck_action.playback_start.refresh_queue",
                    warning_threshold_ms=25.0,
                    context={"deck": deck_id},
                ):
                    self._refresh_queue()
            elif (
                action in {"stop", "eject"}
                and previous_track is not None
                and (action == "eject" or previous_state in {DeckState.PLAYING, DeckState.PAUSED})
            ):
                completion = (
                    CompletionStatus.SKIPPED if action == "eject" else CompletionStatus.STOPPED
                )
                queue_status = QueueStatus.SKIPPED if action == "eject" else QueueStatus.PLAYED
                if self._history is not None:
                    self._history.finish(
                        deck_id,
                        completion,
                        previous_position,
                        skip_code=(
                            HistoryReasonCode.DECK_EJECT
                            if action == "eject"
                            else HistoryReasonCode.DECK_STOP
                        ),
                    )
                queue_id = self._deck_queue_ids[deck_id]
                if queue_id is not None:
                    self._queue_service.mark_finished(queue_id, queue_status)
                else:
                    self._queue_service.mark_finished_for_deck(deck_id, queue_status)
                self._refresh_queue()
            self.crossfader.apply()
            self._view.show_deck(deck.model)
            if resume_automatic_after_action:
                self.start_automatic_queue()
            if action == "eject":
                self._deck_queue_ids[deck_id] = None
                self._auto_load_suppressed_decks.add(deck_id)
                self._view.show_deck_cover(deck_id, None)
        except Exception as exc:
            queue_id = self._deck_queue_ids[deck_id]
            if queue_id is not None:
                self._queue_service.mark_error(queue_id)
            if self._history is not None:
                self._history.finish(
                    deck_id,
                    CompletionStatus.ERROR,
                    deck.model.position,
                    error_message=str(exc),
                    skip_code=HistoryReasonCode.PLAYBACK_ERROR,
                )
            self._refresh_queue()
            self._view.show_deck(deck.model)
            if automatic:
                self._handle_queue_error(f"Deck {deck_id}", exc)
            else:
                self._handle_error(f"Deck {deck_id}", exc)

    def toggle_deck_play_pause(self, deck_id: str) -> None:
        state = self._deck(deck_id).model.state
        if state == DeckState.PLAYING:
            self.deck_action(deck_id, "pause")
        elif state == DeckState.PAUSED:
            self.deck_action(deck_id, "resume")
        else:
            self.deck_action(deck_id, "play")

    def seek(self, deck_id: str, position: float) -> None:
        self._transition.abort(f"Seek auf Deck {deck_id}; Übergang wird neu bewertet")
        try:
            self._deck(deck_id).seek(position)
        except Exception as exc:
            self._handle_error(f"Deck {deck_id}: Springen fehlgeschlagen", exc)

    def set_deck_volume(self, deck_id: str, volume: float) -> None:
        self._deck(deck_id).set_volume(volume)
        if self._settings is not None:
            self._settings.set_deck_volume(deck_id, self._deck(deck_id).model.volume)

    def fade(self, deck_id: str, fade_in: bool) -> None:
        self._manual_override(f"Manueller Fade auf Deck {deck_id}")
        deck = self._deck(deck_id)
        if fade_in:
            deck.fade_level = 0.0
        deck.start_fade(
            1.0 if fade_in else 0.0,
            self.fade_duration,
            self._view.schedule,
            stop_after=not fade_in and self.fade_out_stops_deck,
        )

    def cancel_fade(self, deck_id: str) -> None:
        self._deck(deck_id).cancel_fade()

    def set_fade_duration(self, duration: float) -> None:
        self.fade_duration = max(1.0, min(float(duration), 30.0))
        if self._settings is not None:
            self._settings.set_fade_duration(self.fade_duration)

    def set_fade_out_stops_deck(self, enabled: bool) -> None:
        self.fade_out_stops_deck = bool(enabled)
        if self._settings is not None:
            self._settings.set_fade_out_stops_deck(self.fade_out_stops_deck)

    def set_queue_stats_use_effective_cues(self, enabled: bool) -> None:
        self.queue_stats_use_effective_cues = bool(enabled)
        self._queue_stats_dirty = True
        if self._settings is not None:
            self._settings.set_queue_stats_use_effective_cues(self.queue_stats_use_effective_cues)
        self._refresh_queue_stats()

    def workspace_catalog_ratio(self) -> float:
        return self._settings.workspace_catalog_ratio() if self._settings is not None else 0.5

    def set_workspace_catalog_ratio(self, ratio: float) -> None:
        if self._settings is not None:
            self._settings.set_workspace_catalog_ratio(ratio)

    def set_queue_artist_repetition_enabled(self, enabled: bool) -> None:
        self.queue_artist_repetition_enabled = bool(enabled)
        if self._repetition is not None:
            self._repetition.queue_artist_repetition_enabled = bool(enabled)
        if self._settings is not None:
            self._settings.set_queue_artist_repetition_enabled(bool(enabled))
        if not enabled:
            restored = self._queue_service.restore_artist_repetition_skips()
            if restored:
                self._refresh_queue()
                if self._automatic_run_active:
                    self._auto_load()

    def set_restore_last_session(self, enabled: bool) -> None:
        if self._settings is not None:
            self._settings.set_restore_last_session(enabled)

    def set_fullscreen_on_start(self, enabled: bool) -> None:
        if self._settings is not None:
            self._settings.set_fullscreen_on_start(enabled)

    def set_file_browser_enabled(self, enabled: bool) -> None:
        if self._settings is not None:
            self._settings.set_file_browser_enabled(enabled)
        self._view.show_file_browser_setting(enabled)

    def set_production_mode(self, enabled: bool) -> None:
        if self._settings is None:
            return
        diagnostics_and_analysis = not enabled
        self._settings.set_performance_diagnostics_enabled(diagnostics_and_analysis)
        self._settings.set_background_analysis_enabled(diagnostics_and_analysis)
        self._view.show_production_mode(enabled)

    def save_performance_diagnostic(self, test_context: str) -> None:
        self._diagnostic_context = test_context
        scenario = self._diagnostic_scenario.snapshot()
        if test_context == "database_delay" and scenario is not None and scenario.active:
            self._view.show_diagnostic_state("stopping", test_context)

            def wait_and_save() -> None:
                self._diagnostic_scenario.wait_for_persistence()
                self._sample_memory(query_view=False)
                self._memory_monitor.end_snapshot()
                self._diagnostic_scenario.end()

                def complete_report() -> None:
                    self._save_performance_diagnostic_now(test_context)

                self._publish_gui_callback(complete_report, "database_delay_report")

            self._start_worker(wait_and_save, "database-delay-report", "diagnostic_report")
            return
        if scenario is not None and scenario.active:
            self._view.show_diagnostic_state("stopping", test_context)
            self._sample_memory()
            self._memory_monitor.end_snapshot()
            self._diagnostic_scenario.end()
        self._save_performance_diagnostic_now(test_context)

    def _save_performance_diagnostic_now(self, test_context: str) -> None:
        try:
            path = self.save_diagnostic_report(test_context)
        except RuntimeError as exc:
            self._view.show_diagnostic_state("stopped", test_context)
            self._view.show_error("Diagnose deaktiviert", str(exc))
            return
        except (OSError, ValueError) as exc:
            self._view.show_diagnostic_state("stopped", test_context)
            self._handle_error("Diagnosebericht konnte nicht gespeichert werden", exc)
            return
        self._view.show_diagnostic_state("stopped", test_context)
        self._view.show_diagnostic_saved(path.resolve())

    def begin_diagnostic_scenario(self, test_context: str, database_delay_ms: int = 1000) -> None:
        """Reset all diagnostic windows and explicitly enable one test scenario."""
        if not self._performance_settings.enabled:
            self._view.show_error(
                "Diagnose deaktiviert",
                (
                    "Testszenarien sind im Produktionsbetrieb deaktiviert. "
                    "Bitte den Produktionsmodus in den Einstellungen ausschalten und "
                    "DeckRelay neu starten. Administratorrechte sind nicht erforderlich; "
                    "die portable Version muss in einem beschreibbaren Ordner liegen."
                ),
            )
            return
        if test_context not in self.DIAGNOSTIC_CONTEXTS:
            raise ValueError("Unbekannter Diagnosekontext")
        self._performance.reset_statistics()
        self._performance.begin_scenario()
        self._heartbeat.reset_statistics()
        self._gui_dispatcher.reset_statistics()
        self._worker_registry.reset_history()
        self._memory_monitor.reset()
        self._memory_monitor.enable_tracemalloc()
        self._sample_memory()
        self._memory_monitor.begin_snapshot()
        self._diagnostic_context = test_context
        if test_context == "memory_stress":
            self._memory_stress_was_populated = False
            self._memory_stress_peak_queue_size = 0
            self._memory_stress_cycle_number = 0
        self._diagnostic_scenario.begin(test_context, database_delay_ms)
        self._view.show_diagnostic_state("running", test_context)

    def _watchdog_playback_state(self) -> str:
        """Return controller-only playback state without accessing Tk widgets."""
        playback = ", ".join(
            f"Deck {deck.model.deck_id}={deck.model.state.value}"
            for deck in (self.deck_a, self.deck_b)
        )
        return f"{playback}, transition={self._transition.state.value}"

    def _watchdog_dispatcher_state(self) -> str:
        """Return the dispatcher's lock-protected counters for a watchdog dump."""
        dispatcher = self._gui_dispatcher.statistics()
        return (
            f"pending={dispatcher.pending}, processed={dispatcher.processed}, "
            f"published={dispatcher.published}"
        )

    def set_audio_output_device(self, device_id: str) -> None:
        try:
            self.deck_a.backend.set_output_device(device_id)
            self.deck_b.backend.set_output_device(device_id)
            if self._overlay_output_device_setter is not None:
                self._overlay_output_device_setter(device_id)
            if self._settings is not None:
                self._settings.set_audio_output_device(device_id)
        except Exception as exc:
            self._handle_error("Audiogerät konnte nicht aktiviert werden", exc)

    def bind_overlay_output_device(self, setter: Callable[[str], None]) -> None:
        """Include the independent overlay player in subsequent device changes."""
        self._overlay_output_device_setter = setter

    def _check_audio_device_health(self) -> None:
        """Periodically verify an explicitly selected output device."""
        if self._deck_health_monitor is None or self._settings is None:
            return
        now = monotonic()
        if now < self._next_audio_device_health_check:
            return
        self._next_audio_device_health_check = now + 10.0
        try:
            devices = self.deck_a.backend.list_output_devices()
        except Exception as exc:
            self._deck_health_monitor.report_command_result(
                "A", "Audiogeräte prüfen", False, str(exc)
            )
            return
        configured_device = self._settings.audio_output_device()
        available = self._deck_health_monitor.report_output_device(
            configured_device, {device_id for device_id, _name in devices}
        )
        if not available:
            self._handle_audio_output_device_loss(configured_device)

    def _handle_audio_output_device_loss(self, device_id: str) -> None:
        """Enter a silent, operator-controlled state after explicit device loss."""
        if self._audio_device_loss_active:
            return
        self._audio_device_loss_active = True
        self._recovery_return_validation_required = True
        self._audio_device_ready_for_confirmation = False
        self._pause_automatic_queue("Konfiguriertes Audiogerät nicht verfügbar")
        self.deck_a.set_emergency_muted(True)
        self.deck_b.set_emergency_muted(True)
        self.crossfader.apply()
        if self._overlay_master_mute_setter is not None:
            self._overlay_master_mute_setter(True)
        self._queue_service.record_audit_event(
            "AUDIO_OUTPUT_DEVICE_LOST", details={"device_id": device_id}
        )
        self._view.show_queue_warning(
            "Audiogerät getrennt: Ausgabe stummgeschaltet. Gerät erneut anwenden und bestätigen."
        )
        self._view.show_audio_device_recovery(
            "device_lost", "Audiogerät getrennt · Ausgabe gesperrt"
        )
        self._publish_recovery_return_requirements(force=True)

    def retry_audio_output_device(self) -> bool:
        """Reapply only the configured device; keep all output muted until confirmed."""
        if not self._audio_device_loss_active or self._settings is None:
            return False
        device_id = self._settings.audio_output_device().strip()
        try:
            available_ids = {
                candidate_id for candidate_id, _name in self.deck_a.backend.list_output_devices()
            }
            if not device_id or device_id not in available_ids:
                self._audio_device_ready_for_confirmation = False
                self._queue_service.record_audit_event(
                    "AUDIO_OUTPUT_DEVICE_REAPPLY_FAILED",
                    details={"device_id": device_id, "reason": "not_available"},
                )
                self._view.show_audio_device_recovery(
                    "device_lost", "Gerät weiterhin nicht verfügbar"
                )
                return False
            self.deck_a.backend.set_output_device(device_id)
            self.deck_b.backend.set_output_device(device_id)
            if self._overlay_output_device_setter is not None:
                self._overlay_output_device_setter(device_id)
        except Exception as exc:
            self._audio_device_ready_for_confirmation = False
            self._queue_service.record_audit_event(
                "AUDIO_OUTPUT_DEVICE_REAPPLY_FAILED",
                details={"device_id": device_id, "reason": str(exc)},
            )
            self._handle_error("Audiogerät konnte nicht erneut aktiviert werden", exc)
            self._view.show_audio_device_recovery(
                "device_lost", "Gerät konnte nicht angewendet werden"
            )
            return False
        self._audio_device_ready_for_confirmation = True
        self._queue_service.record_audit_event(
            "AUDIO_OUTPUT_DEVICE_READY_FOR_CONFIRMATION",
            details={"device_id": device_id},
        )
        self._view.show_audio_device_recovery(
            "ready_for_confirmation", "Gerät angewendet · Ausgabe noch gesperrt"
        )
        self._publish_recovery_return_requirements(force=True)
        return True

    def confirm_audio_output_device_recovered(self) -> bool:
        """Release the safety mute, but leave automatic playback paused."""
        if not (self._audio_device_loss_active and self._audio_device_ready_for_confirmation):
            return False
        self._audio_device_loss_active = False
        self._audio_device_ready_for_confirmation = False
        if self._deck_health_monitor is not None:
            self._deck_health_monitor.confirm_output_device_recovered()
        self.deck_a.set_emergency_muted(False)
        self.deck_b.set_emergency_muted(False)
        self.crossfader.apply()
        if self._overlay_master_mute_setter is not None:
            self._overlay_master_mute_setter(self.crossfader.output_muted)
        self._queue_service.record_audit_event("AUDIO_OUTPUT_DEVICE_RECOVERY_CONFIRMED", details={})
        self._view.show_queue_warning(
            "Audiogerät bestätigt. Die Automatik bleibt bis zum manuellen Start pausiert."
        )
        self._view.show_audio_device_recovery("normal", "Audioausgabe bereit")
        self._publish_recovery_return_requirements(force=True)
        return True

    def audio_output_device_recovery_state(self) -> str:
        """Expose the small recovery state machine without leaking implementation flags."""
        if self._audio_device_ready_for_confirmation:
            return "ready_for_confirmation"
        if self._audio_device_loss_active:
            return "device_lost"
        return "normal"

    def bind_overlay_master_mute(self, setter: Callable[[bool], None]) -> None:
        """Include the overlay channel in the global master-mute state."""

        self._overlay_master_mute_setter = setter
        setter(self._audio_device_loss_active or self.crossfader.output_muted)

    def set_automatic_deck_loading(self, enabled: bool) -> None:
        self.set_player_mode(PlayerMode.SEMI_AUTOMATIC if enabled else PlayerMode.MANUAL)

    def set_player_mode(self, mode: str | PlayerMode) -> None:
        selected = PlayerMode(mode)
        if (
            selected == PlayerMode.AUTOMATIC
            and not self._automatic_start_assessment_bypass
            and self._recovery_return_gate_required()
        ):
            assessment = self.assess_recovery_return()
            if not assessment.allowed:
                self._logger.warning(
                    "Automatikstart abgelehnt: code=%s, grund=%s, "
                    "transition=%s, preload_aktiv=%s, runner_aktiv=%s, pausegrund=%s",
                    assessment.error_code,
                    assessment.message,
                    self._transition.state.value,
                    self._preload_in_progress,
                    self._automatic_run_active,
                    self._automatic_pause_reason or "-",
                )
                self._view.show_queue_warning(f"Automatik gesperrt: {assessment.message}")
                self._queue_service.record_audit_event(
                    "AUTOMATIC_START_REJECTED",
                    details={"reason": assessment.error_code},
                )
                return
            if self._transition.state == TransitionState.ABORTED:
                self._transition.reset()
            self._recovery_return_validation_required = False
            self._publish_recovery_return_requirements(force=True)
        if selected != PlayerMode.AUTOMATIC:
            self.stop_automatic_queue(reason_code="MODE_CHANGED")
        else:
            self._automatic_run_paused = False
            self._automatic_pause_reason = None
            self._automatic_run_active = True
        self.player_mode = selected
        self.automatic_deck_loading = selected != PlayerMode.MANUAL
        if self._settings is not None:
            self._settings.set_player_mode(selected)
        if self.automatic_deck_loading:
            self._auto_load_suppressed_decks.clear()
            self._auto_load()
        if selected == PlayerMode.AUTOMATIC:
            self._automatic_playback_tick()
            self._view.show_automatic_playback(True)
            self._show_automatic_status("running")
        self._view.show_player_mode(self.player_mode.value)

    def start_automatic_queue(
        self,
        *,
        from_selected: bool = False,
        skip_earlier: bool = True,
    ) -> None:
        """Start queue playback, normally from the first waiting entry.

        Queue selection is primarily a navigation concept.  It only changes the
        automatic starting point when the caller explicitly requests that behavior.
        This prevents a stale or incidental selection from silently skipping tracks.
        """
        if self._recovery_return_gate_required():
            assessment = self.assess_recovery_return()
            if not assessment.allowed:
                self._logger.warning(
                    "Automatikstart abgelehnt: code=%s, grund=%s, "
                    "transition=%s, preload_aktiv=%s, runner_aktiv=%s, pausegrund=%s",
                    assessment.error_code,
                    assessment.message,
                    self._transition.state.value,
                    self._preload_in_progress,
                    self._automatic_run_active,
                    self._automatic_pause_reason or "-",
                )
                self._view.show_queue_warning(f"Automatik gesperrt: {assessment.message}")
                self._queue_service.record_audit_event(
                    "AUTOMATIC_START_REJECTED",
                    details={"reason": assessment.error_code},
                )
                self._show_automatic_status("paused", assessment.message)
                return
            self._recovery_return_validation_required = False
            self._publish_recovery_return_requirements(force=True)
        active_entries = [
            entry
            for entry in self._queue_service.entries()
            if entry.status in {QueueStatus.WAITING, QueueStatus.READY, QueueStatus.PLAYING}
        ]
        if not active_entries:
            message = "Die Automatik kann nicht starten: Die Queue enthält keinen Titel."
            self._view.show_queue_warning(message)
            self._queue_service.record_audit_event(
                "AUTOMATIC_START_REJECTED",
                details={"reason": "EMPTY_QUEUE"},
            )
            self._show_automatic_status("ready", "Queue ist leer")
            return
        was_paused = self._automatic_run_paused
        self._automatic_run_paused = False
        self._automatic_pause_reason = None
        if was_paused:
            self._resume_automatic_queue_audio()
        if from_selected and not was_paused:
            self._prepare_automatic_start_from_selection(skip_earlier=skip_earlier)
        if self._transition.state in {TransitionState.ABORTED, TransitionState.FAILED}:
            self._transition.reset()
        self._automatic_start_assessment_bypass = True
        try:
            self.set_player_mode(PlayerMode.AUTOMATIC)
        finally:
            self._automatic_start_assessment_bypass = False
        if was_paused:
            self._queue_service.record_audit_event("AUTOMATIC_RESUMED")
            self._logger.info("Automatik-Runner fortgesetzt")
        else:
            self._queue_service.record_audit_event(
                "AUTOMATIC_STARTED",
                details={
                    "start_mode": (
                        "SELECTED_SKIP_EARLIER"
                        if from_selected and skip_earlier
                        else "SELECTED_KEEP_EARLIER" if from_selected else "FIRST_WAITING"
                    )
                },
            )

    def is_automatic_queue_paused(self) -> bool:
        return self._automatic_run_paused

    def _recovery_return_gate_required(self) -> bool:
        return (
            self._recovery_return_validation_required
            or self._global_audio_recovery_requested
            or self._global_audio_recovery_ready_for_release
            or self.audio_output_device_recovery_state() != "normal"
        )

    def pause_automatic_queue(self) -> None:
        self._pause_automatic_queue("Vom Benutzer pausiert", pause_audio=True)

    def automatic_start_has_earlier_waiting_entries(self) -> bool:
        """Return whether starting at the selection would skip waiting entries."""
        queue_id = self._selected_queue_entry_id
        if queue_id is None:
            return False
        selected = self._queue_service.entry(queue_id)
        if selected is None or selected.status not in {QueueStatus.WAITING, QueueStatus.READY}:
            return False
        return any(
            entry.status == QueueStatus.WAITING and entry.position < selected.position
            for entry in self._queue_service.entries()
        )

    def automatic_start_summary(
        self,
        *,
        from_selected: bool = False,
        skip_earlier: bool = True,
    ) -> str:
        """Describe the non-mutating queue/rule preview shown before automatic start."""
        with self._performance.measure(
            "automatic_start.summary",
            warning_threshold_ms=50.0,
        ):
            entries = [
                entry
                for entry in self._queue_service.entries()
                if entry.status in {QueueStatus.WAITING, QueueStatus.READY}
            ]
            if from_selected and self._selected_queue_entry_id is not None:
                selected = self._queue_service.entry(self._selected_queue_entry_id)
                if selected is not None:
                    if skip_earlier:
                        entries = [
                            entry for entry in entries if entry.position >= selected.position
                        ]
                    else:
                        entries = [selected] + [
                            entry for entry in entries if entry.queue_id != selected.queue_id
                        ]

            previews = self._queue_service.preview_candidate_decisions(entries)
            blocked: list[tuple[QueueEntry, str]] = []
            playable: list[QueueEntry] = []
            for entry in entries:
                _track, decision = previews[entry.queue_id]
                if entry.status == QueueStatus.READY or decision.accepted:
                    playable.append(entry)
                else:
                    blocked.append((entry, decision.reason or decision.code))

            start_entry = playable[0] if playable else None
            start_track = previews[start_entry.queue_id][0] if start_entry is not None else None
            lines = [
                f"Starttitel: {start_track.title if start_track is not None else 'Kein spielbarer Titel'}",
                f"Wartend/vorbereitet: {len(entries)}",
                f"Voraussichtlich spielbar: {len(playable)}",
                f"Durch Regeln blockiert: {len(blocked)}",
            ]
            if blocked:
                lines.append("")
                lines.append("Blockierte Titel:")
                for entry, reason in blocked[:5]:
                    track = previews[entry.queue_id][0]
                    lines.append(
                        f"• {track.title if track is not None else entry.track_id}: {reason}"
                    )
                if len(blocked) > 5:
                    lines.append(f"• … und {len(blocked) - 5} weitere")
            return "\n".join(lines)

    def _prepare_automatic_start_from_selection(self, *, skip_earlier: bool = True) -> None:
        """Make the selected queue row the next automatic starting point."""
        queue_id = self._selected_queue_entry_id
        if queue_id is None:
            return
        selected = self._queue_service.entry(queue_id)
        if selected is None or selected.status not in {QueueStatus.WAITING, QueueStatus.READY}:
            return
        self._preload_generation += 1
        self._preload_in_progress = False
        # Media adoption happens before READY persistence.  Inspect the physical
        # decks as well as queue rows so a restart cannot leave a WAITING row
        # occupying a deck during this short split-state window.
        for deck in (self.deck_a, self.deck_b):
            if (
                deck.model.loaded_track is None
                or deck.backend.is_playing()
                or deck.model.state == DeckState.PLAYING
            ):
                continue
            cleanup = deck.detach_for_cleanup()
            self._deck_queue_ids[deck.model.deck_id] = None
            self._view.show_deck_cover(deck.model.deck_id, None)
            self._start_worker(
                cleanup,
                f"automatic-restart-cleanup-{deck.model.deck_id}",
                "deck_cleanup",
                executor=self._preload_executor,
            )
        for entry in self._queue_service.entries():
            if entry.status in {QueueStatus.PREPARING, QueueStatus.READY}:
                assigned_deck = (
                    self._deck(entry.loaded_deck) if entry.loaded_deck in {"A", "B"} else None
                )
                if assigned_deck is not None and (
                    assigned_deck.backend.is_playing()
                    or assigned_deck.model.state == DeckState.PLAYING
                ):
                    continue
                self._queue_service.reset_prepared(entry.queue_id)
                entry = self._queue_service.entry(entry.queue_id) or entry
            if entry.queue_id == selected.queue_id:
                continue
            if (
                skip_earlier
                and entry.status == QueueStatus.WAITING
                and entry.position < selected.position
            ):
                self._queue_service.mark_skipped(
                    entry.queue_id,
                    "Vor dem gewählten Automatik-Startpunkt",
                    code="AUTOMATIC_START_POSITION",
                )
        if not skip_earlier:
            self._queue_service.move_to_top(selected.queue_id)
        self._refresh_queue()

    def stop_automatic_queue(
        self,
        *,
        reason_code: str = "USER_STOP",
        completed: bool = False,
    ) -> None:
        was_engaged = (
            self._automatic_run_active
            or self._automatic_run_paused
            or self._transition.is_transitioning
        )
        self._automatic_run_active = False
        self._automatic_run_paused = False
        self._automatic_pause_reason = None
        self._automatic_audio_paused_decks.clear()
        self._transition.abort("Automatik beendet")
        self._transition.reset()
        if self.player_mode == PlayerMode.AUTOMATIC:
            self.player_mode = PlayerMode.SEMI_AUTOMATIC
            self.automatic_deck_loading = True
            if self._settings is not None:
                self._settings.set_player_mode(self.player_mode)
            self._view.show_player_mode(self.player_mode.value)
        if was_engaged:
            event_code = "AUTOMATIC_COMPLETED" if completed else "AUTOMATIC_STOPPED"
            self._queue_service.record_audit_event(
                event_code,
                details={"reason": reason_code},
            )
            self._logger.info(
                "Automatik-Runner %s: %s",
                "regulär beendet" if completed else "abgebrochen",
                reason_code,
            )
        self._view.show_automatic_playback(False)
        reason_labels = {
            "USER_STOP": "Vom Benutzer beendet",
            "MODE_CHANGED": "Betriebsart gewechselt",
            "QUEUE_CLEARED": "Queue geleert",
        }
        self._show_automatic_status(
            "completed" if completed else "stopped" if was_engaged else "ready",
            (
                "Queue vollständig abgespielt"
                if completed
                else reason_labels.get(reason_code, reason_code) if was_engaged else ""
            ),
        )

    def set_queue_duplicate_policy(self, policy: str) -> None:
        if policy not in {"allow", "prevent"}:
            raise ValueError("Unbekannte Queue-Duplikatregel")
        self.queue_duplicate_policy = policy
        self._queue_service.allow_duplicates = policy == "allow"
        if self._settings is not None:
            self._settings.set_queue_duplicate_policy(policy)
        self._view.show_queue_duplicate_policy(policy)

    def set_crossfader(self, position: float) -> None:
        normalized = min(1.0, max(0.0, float(position)))
        if abs(normalized - self.crossfader.position) > 0.005:
            self._pause_automatic_queue("Crossfader manuell bewegt")
        self.crossfader.set_position(normalized)
        if self._settings is not None:
            self._settings.set_crossfader_position(self.crossfader.position)
        self._refresh_all()

    def set_master_volume(self, volume: float) -> None:
        self.crossfader.set_master_volume(volume)
        if self._overlay_master_mute_setter is not None:
            self._overlay_master_mute_setter(self.crossfader.output_muted)
        if self._settings is not None:
            self._settings.set_master_volume(self.crossfader.master_volume)
        self._refresh_all()

    def toggle_mute(self) -> None:
        if self.crossfader.master_muted:
            self.crossfader.unmute()
        else:
            self.crossfader.mute()
        if self._overlay_master_mute_setter is not None:
            self._overlay_master_mute_setter(self.crossfader.output_muted)
        self._refresh_all()

    def set_panic_muted(self, muted: bool) -> None:
        self.crossfader.set_panic_muted(muted)
        if self._overlay_master_mute_setter is not None:
            self._overlay_master_mute_setter(self.crossfader.output_muted)
        self._queue_service.record_audit_event("PANIC_MUTE_CHANGED", details={"muted": bool(muted)})
        self._refresh_all()

    def close(self, finish_session: bool = False) -> None:
        self._closed = True
        self._heartbeat_watchdog.stop()
        self._preload_generation += 1
        for deck in (self.deck_a, self.deck_b):
            if self._history is not None:
                request = self._history.prepare_finish(
                    deck.model.deck_id,
                    CompletionStatus.STOPPED,
                    deck.model.position,
                    skip_code=HistoryReasonCode.APPLICATION_SHUTDOWN,
                )
                if request is not None:
                    self._enqueue_history_persist(request)
            if finish_session:
                self._queue_service.mark_finished_for_deck(
                    deck.model.deck_id,
                    QueueStatus.PLAYED,
                    unplayed_skip_reason="Session beendet, bevor der Titel gestartet wurde",
                )
        self._cover_executor.shutdown(wait=False, cancel_futures=True)
        self._preload_executor.shutdown(wait=False, cancel_futures=True)
        self._statistics_executor.shutdown(wait=False, cancel_futures=True)
        self._track_editor_executor.shutdown(wait=False, cancel_futures=True)
        if self._replaygain_cache is not None:
            self._replaygain_cache.close()
        if self._owns_persistence_executor:
            # Critical queue/session state is persisted synchronously. Slow
            # history or diagnostic follow-up writes must not block Tk closing.
            self._persistence_executor.shutdown(wait=False, cancel_futures=False)
        self.deck_a.close()
        self.deck_b.close()
        if finish_session and self._session is not None and self._session_service is not None:
            self._session_service.finish(self._session.session_id)
        self._memory_monitor.close()

    def restore_participant(self) -> PersistenceParticipant | None:
        if not isinstance(self._persistence_executor, BoundedThreadPoolExecutor):
            return None
        return single_worker_participant(
            "main-persistence",
            self._persistence_executor,
            self._queue_service.close_cached_connection,
        )

    def is_audio_active(self) -> bool:
        return self.deck_a.backend.is_playing() or self.deck_b.backend.is_playing()

    def metadata_analysis_audio_recovery_active(self) -> bool:
        emergency_recovery_active = (
            self._emergency is not None and self._emergency.recovery_active()
        )
        return bool(
            self._global_audio_recovery_requested
            or emergency_recovery_active
            or self._deck_recovery_action_active
            or self._emergency_action_active
        )

    def metadata_analysis_automation_active(self) -> bool:
        return self._automatic_run_active

    def restore_safety_snapshot(
        self, *, cue_analysis_active: bool, loudness_analysis_active: bool
    ) -> RestoreSafetySnapshot:
        """Capture all I/O-free runtime facts required by the restore gate."""
        emergency_recovery_active = (
            self._emergency is not None and self._emergency.recovery_active()
        )
        stopped_states = {DeckState.EMPTY, DeckState.STOPPED}
        return RestoreSafetySnapshot(
            self.deck_a.model.state in stopped_states and not self.deck_a.backend.is_playing(),
            self.deck_b.model.state in stopped_states and not self.deck_b.backend.is_playing(),
            self._transition.is_transitioning,
            self._overlay_activity(),
            self._global_audio_recovery_requested or emergency_recovery_active,
            self._deck_recovery_action_active,
            self._emergency_action_active,
            cue_analysis_active,
            loudness_analysis_active,
        )

    def can_change_vlc_installation(self) -> bool:
        emergency_recovery_active = (
            self._emergency is not None and self._emergency.recovery_active()
        )
        return (
            not self.is_audio_active()
            and self._transition.state == TransitionState.IDLE
            and not self._deck_recovery_action_active
            and not self._emergency_action_active
            and not self._global_audio_recovery_requested
            and not self._global_audio_recovery_ready_for_release
            and not emergency_recovery_active
            and not self._overlay_activity()
        )

    def bind_overlay_activity(self, activity: Callable[[], bool]) -> None:
        """Bind an I/O-free overlay activity gate after overlay composition."""
        self._overlay_activity = activity

    def bind_database_diagnostic_status(self, status: Callable[[], tuple[str, ...]]) -> None:
        self._database_diagnostic_status = status

    def _load_track(self, track: Track, deck: DeckController, queue_id: int | None = None) -> bool:
        with self._performance.measure(
            "playback.direct_track_load",
            warning_threshold_ms=500.0,
            context={"deck": deck.model.deck_id, "track_id": track.id},
        ):
            return self._load_track_impl(track, deck, queue_id)

    def _load_track_impl(
        self, track: Track, deck: DeckController, queue_id: int | None = None
    ) -> bool:
        self._manual_override(f"Titel auf Deck {deck.model.deck_id} manuell ersetzt")
        if deck.model.state == DeckState.PLAYING and not self._view.confirm_replace(
            deck.model.deck_id
        ):
            return False
        try:
            deck.load(track)
            self._apply_resolved_equalizer(deck, self._resolve_equalizer(deck, track, queue_id))
            self._apply_cue_points(deck)
            self._apply_loudness(deck)
            self._queue_service.release_deck_assignments(deck.model.deck_id, queue_id)
            self._deck_queue_ids[deck.model.deck_id] = queue_id
            self.crossfader.apply()
            self._view.show_deck(deck.model)
            self._load_cover_async(deck.model.deck_id, track)
            return True
        except Exception as exc:
            self._handle_error(f"Deck {deck.model.deck_id}: Laden fehlgeschlagen", exc)
            self._view.show_deck(deck.model)
            return False

    def _auto_load(self) -> None:
        if not self.automatic_deck_loading:
            return
        if self._background_preload:
            layout = self._callback_state.snapshot()
            if not self._heartbeat_started and (
                layout.pending_layout_refreshes
                or layout.pending_focus_request
                or layout.pending_catalog_chunks
                or layout.pending_queue_chunks
            ):
                return
            self._auto_load_in_background()
            return
        changed = False
        while True:
            with self._performance.measure(
                "playback.autoload_candidate_search", warning_threshold_ms=100.0
            ):
                result = self._queue_service.load_next_into_free_deck(
                    self.deck_a, self.deck_b, self._auto_load_suppressed_decks
                )
            if result is None:
                break
            entry, deck = result
            self._deck_queue_ids[deck.model.deck_id] = entry.queue_id
            if deck.model.loaded_track is not None:
                self._apply_resolved_equalizer(
                    deck,
                    self._resolve_equalizer(deck, deck.model.loaded_track, entry.queue_id),
                )
                if not deck.model.cue_boundaries_ready:
                    self._apply_cue_points(deck)
                self._apply_loudness(deck)
                self._load_cover_async(deck.model.deck_id, deck.model.loaded_track)
            changed = True
        if changed:
            self._refresh_queue()
            self._refresh_all()
            self._no_safe_candidate_warning_active = False
        else:
            self._show_no_safe_candidate_warning()

    def _auto_load_in_background(self) -> None:
        if self._preload_in_progress or self._closed:
            return
        # Keep repository work out of the status cycle while both logical decks
        # are occupied. This deliberately avoids querying VLC from autoload.
        if not self._free_decks_requiring_preload():
            return
        if monotonic() < self._next_preload_candidate_search_at:
            return
        if not any(entry.status == QueueStatus.WAITING for entry in self._queue_entries_cache):
            self._show_no_safe_candidate_warning()
            return
        self._preload_in_progress = True
        self._preload_generation += 1
        generation = self._preload_generation

        def search_worker() -> None:
            with self._performance.measure(
                "worker.preload.candidate_search", warning_threshold_ms=3000.0
            ):
                assignments_changed = self._queue_service.reconcile_deck_assignments(
                    self.deck_a, self.deck_b
                )
                candidate = self._queue_service.next_load_candidate(
                    self.deck_a, self.deck_b, self._auto_load_suppressed_decks
                )

            def search_complete() -> None:
                if generation != self._preload_generation or self._closed:
                    return
                self._preload_in_progress = False
                if assignments_changed:
                    self._refresh_queue()
                if candidate is None:
                    self._preload_candidate_misses += 1
                    delay = self._candidate_search_backoff_seconds(self._preload_candidate_misses)
                    self._next_preload_candidate_search_at = monotonic() + delay
                    self._show_no_safe_candidate_warning()
                    return
                self._preload_candidate_misses = 0
                self._next_preload_candidate_search_at = 0.0
                self._start_background_preload(candidate)

            self._publish_gui_callback(search_complete, "preload-candidate-search")

        if not self._start_worker(
            search_worker,
            "preload-candidate-search",
            "preload-candidate-search",
            executor=self._preload_executor,
        ):
            self._preload_in_progress = False

    def _free_decks_requiring_preload(self) -> tuple[str, ...]:
        """Return genuinely empty, unassigned decks without touching an audio backend."""
        return tuple(
            deck.model.deck_id
            for deck in (self.deck_a, self.deck_b)
            if deck.model.deck_id not in self._auto_load_suppressed_decks
            and deck.model.loaded_track is None
            and deck.model.state == DeckState.EMPTY
            and self._deck_queue_ids.get(deck.model.deck_id) is None
            and not any(
                entry.loaded_deck == deck.model.deck_id
                and entry.status
                in {
                    QueueStatus.PREPARING,
                    QueueStatus.LOADED,
                    QueueStatus.PLAYING,
                }
                for entry in self._queue_entries_cache
            )
        )

    @staticmethod
    def _candidate_search_backoff_seconds(consecutive_misses: int) -> float:
        exponent = min(4, max(0, consecutive_misses - 1))
        return min(30.0, 2.0 * float(2**exponent))

    def _start_background_preload(
        self,
        candidate: tuple[QueueEntry, DeckController, Track],
    ) -> None:
        if self._preload_in_progress or self._closed:
            return
        self._no_safe_candidate_warning_active = False
        entry, deck, track = candidate
        self._preload_in_progress = True
        self._preload_generation += 1
        generation = self._preload_generation
        started_at = monotonic()
        self._transition.preload_started(deck.model.deck_id)
        self._view.schedule(
            int(self._preparation_timeout_seconds * 1000),
            lambda: self._handle_preload_timeout(generation, entry, deck),
        )

        def worker() -> None:
            nonlocal track
            media: object | None = None
            try:
                validated_track, availability = self._queue_service.revalidate_candidate(
                    entry.queue_id,
                    cancelled=lambda: (generation != self._preload_generation or self._closed),
                )
                if not availability.accepted:

                    def unavailable() -> None:
                        if generation != self._preload_generation:
                            return
                        self._preload_in_progress = False
                        current_entry = self._queue_service.entry(entry.queue_id)
                        if current_entry is None or current_entry.status not in {
                            QueueStatus.WAITING,
                            QueueStatus.PREPARING,
                        }:
                            self._logger.info(
                                "Verspätetes Preload-Ergebnis wird wegen Zustandsänderung verworfen"
                            )
                            self._transition.reset()
                            self._refresh_queue()
                            self._auto_load()
                            return
                        self._queue_service.reject_candidate(entry.queue_id, availability)
                        self._transition.preload_failed(
                            deck.model.deck_id,
                            OSError(availability.code),
                        )
                        self._view.show_queue_warning(
                            f"{track.artist} – {track.title}: {availability.reason}"
                        )
                        self._refresh_queue()
                        self._auto_load()

                    self._publish_gui_callback(unavailable, "preload")
                    return
                assert validated_track is not None
                track = validated_track
                self._queue_service.mark_preparing(entry.queue_id, deck.model.deck_id)
                media = deck.prepare(track)
                with self._performance.measure(
                    "worker.preload.resolve_loudness", warning_threshold_ms=500.0
                ):
                    resolved_loudness = self._resolve_loudness(track)
                resolved_boundaries = (
                    self._cue_points.resolve(track, self.AUTOMATIC_OVERLAP_SECONDS, entry)
                    if self._cue_points is not None
                    else None
                )
                resolved_equalizer = self._resolve_equalizer(deck, track, entry.queue_id)
                result = PreparedPreloadResult(
                    media,
                    resolved_loudness,
                    resolved_boundaries,
                    resolved_equalizer,
                )
            except Exception as error:
                if media is not None:
                    deck.discard_prepared(media)

                def failed(captured_error: Exception = error) -> None:
                    if generation != self._preload_generation:
                        return
                    self._preload_in_progress = False
                    current_entry = self._queue_service.entry(entry.queue_id)
                    if current_entry is None or current_entry.status not in {
                        QueueStatus.WAITING,
                        QueueStatus.PREPARING,
                    }:
                        self._logger.info(
                            "Verspäteter Preload-Fehler wird wegen Zustandsänderung verworfen"
                        )
                        self._transition.reset()
                        self._refresh_queue()
                        self._auto_load()
                        return
                    self._queue_service.mark_error(entry.queue_id)
                    self._transition.preload_failed(deck.model.deck_id, captured_error)
                    self._logger.warning(
                        "Deck %s: Preload fehlgeschlagen: %s",
                        deck.model.deck_id,
                        captured_error,
                    )
                    self._view.show_queue_warning(
                        f"{track.artist} – {track.title}: Vorbereitung fehlgeschlagen"
                    )
                    self._refresh_queue()
                    self._auto_load()

                self._publish_gui_callback(failed, "preload")
                return

            def complete() -> None:
                """Apply prepared media, then dispatch non-essential GUI follow-up.

                Prepared media must be adopted before another deck candidate can be
                selected. Queue rendering, cover scheduling and the next automatic
                action are separated and individually measured so a future delay is
                attributable without changing playback semantics.
                """

                with (
                    self._callback_state.track("preload_result"),
                    self._performance.measure("gui_event.preload.total", warning_threshold_ms=25.0),
                ):
                    if generation != self._preload_generation:
                        deck.discard_prepared(result.media)
                        return
                    with self._performance.measure(
                        "gui_event.preload.apply_result", warning_threshold_ms=25.0
                    ):
                        current_entry = next(
                            (
                                item
                                for item in self._queue_entries_cache
                                if item.queue_id == entry.queue_id
                            ),
                            None,
                        )
                        if (
                            current_entry is None
                            or current_entry.status != QueueStatus.WAITING
                            or deck.model.loaded_track is not None
                            or deck.backend.is_playing()
                        ):
                            self._logger.info(
                                "Vorbereitetes Medium wird wegen Zustandsänderung verworfen"
                            )
                            deck.discard_prepared(result.media)
                            # This generation still owns the preload flag. Leaving
                            # it set would permanently suppress every later autoload.
                            self._preload_in_progress = False
                            self._transition.reset()
                            return
                    adopted_media = False
                    try:
                        with self._performance.measure(
                            "gui_event.preload.update_deck_view", warning_threshold_ms=25.0
                        ):
                            deck.load_prepared(track, result.media)
                            adopted_media = True
                            self._apply_resolved_equalizer(deck, result.equalizer)
                        self._deck_queue_ids[deck.model.deck_id] = entry.queue_id
                        with (
                            self._callback_state.track("apply_loudness"),
                            self._performance.measure(
                                "gui_event.preload.apply_loudness", warning_threshold_ms=25.0
                            ),
                        ):
                            if result.boundaries is not None:
                                self._apply_resolved_boundaries(deck, result.boundaries)
                            else:
                                self._apply_cue_points(deck)
                            self._apply_resolved_loudness(deck, result.loudness)
                        self._transition.preload_ready(deck.model.deck_id, monotonic() - started_at)
                        with (
                            self._callback_state.track("schedule_cover"),
                            self._performance.measure(
                                "gui_event.preload.schedule_cover", warning_threshold_ms=25.0
                            ),
                        ):
                            self._load_cover_async(
                                deck.model.deck_id,
                                track,
                                operation_id=f"preload-{generation}-{deck.model.deck_id}",
                            )

                        def followup() -> None:
                            """Refresh derived views after the prepared deck is stable."""
                            if generation != self._preload_generation:
                                return
                            self._preload_in_progress = False
                            with self._performance.measure(
                                "gui_event.preload.update_queue_view",
                                warning_threshold_ms=25.0,
                            ):
                                self._refresh_queue()
                            with self._performance.measure(
                                "gui_event.preload.update_catalog_view",
                                warning_threshold_ms=25.0,
                            ):
                                self._refresh_all()
                            with self._performance.measure(
                                "gui_event.preload.schedule_followup",
                                warning_threshold_ms=25.0,
                            ):
                                self._automatic_playback_tick()
                                self._auto_load()

                        def persist_preload() -> None:
                            if generation != self._preload_generation or self._closed:
                                return
                            try:
                                self._queue_service.release_deck_assignments(
                                    deck.model.deck_id, entry.queue_id
                                )
                                if generation != self._preload_generation or self._closed:
                                    return
                                self._queue_service.mark_loaded(entry.queue_id, deck.model.deck_id)
                            except Exception as error:

                                def persistence_failed(
                                    captured_error: Exception = error,
                                ) -> None:
                                    self._preload_in_progress = False
                                    self._transition.preload_failed(
                                        deck.model.deck_id, captured_error
                                    )
                                    self._handle_queue_error(
                                        f"Deck {deck.model.deck_id}: Queue-Aktualisierung fehlgeschlagen",
                                        captured_error,
                                    )

                                self._publish_gui_callback(
                                    persistence_failed, "preload_persistence_failed"
                                )
                                return
                            self._publish_gui_callback(followup, "preload_followup")

                        self._start_worker(
                            persist_preload,
                            f"preload-persist-{deck.model.deck_id}",
                            "preload_persist",
                            executor=self._preload_executor,
                        )
                    except Exception as error:
                        if not adopted_media:
                            deck.discard_prepared(result.media)
                        self._preload_in_progress = False
                        self._transition.preload_failed(deck.model.deck_id, error)
                        self._handle_queue_error(
                            f"Deck {deck.model.deck_id}: Laden fehlgeschlagen", error
                        )

            self._publish_gui_callback(complete, "preload")

        self._start_worker(
            worker,
            f"preload-deck-{deck.model.deck_id}",
            "preload",
            executor=self._preload_executor,
        )

    def _handle_preload_timeout(
        self,
        generation: int,
        entry: QueueEntry,
        deck: DeckController,
    ) -> None:
        """Invalidate a late worker without waiting for its backend call."""
        if generation != self._preload_generation or not self._preload_in_progress:
            return
        self._preload_generation += 1
        self._preload_in_progress = False
        self._queue_service.mark_error(entry.queue_id, "PREPARATION_TIMEOUT")
        self._transition.preload_failed(
            deck.model.deck_id,
            TimeoutError("Vorbereitung hat das Zeitlimit überschritten"),
        )
        track = self._queue_service.track(entry.track_id)
        title = f"{track.artist} – {track.title}" if track is not None else "Queue-Titel"
        self._view.show_queue_warning(f"{title}: Vorbereitung wegen Zeitüberschreitung beendet")
        self._refresh_queue()
        self._auto_load()

    def _show_no_safe_candidate_warning(self) -> None:
        if self._queue_service.automatic_selection_stage != "NO_SAFE_CANDIDATE":
            return
        if self._no_safe_candidate_warning_active:
            return
        self._no_safe_candidate_warning_active = True
        self._logger.warning(
            "Keine sichere automatische Auswahl verfügbar; laufender Titel endet regulär"
        )
        self._view.show_queue_warning(
            "Keine sichere automatische Auswahl – laufender Titel endet regulär"
        )

    def _apply_loudness(self, deck: DeckController) -> ResolvedLoudnessSettings | None:
        track = deck.model.loaded_track
        resolved = self._resolve_loudness(track) if track is not None else None
        self._apply_resolved_loudness(deck, resolved)
        return resolved

    def _resolve_equalizer(
        self,
        deck: DeckController,
        track: Track,
        queue_id: int | None = None,
    ) -> ResolvedEqualizerPreset:
        with self._performance.measure("equalizer.resolve", warning_threshold_ms=25.0):
            frequencies = deck.equalizer_band_frequencies()
            if not frequencies:
                resolved = ResolvedEqualizerPreset.disabled("UNSUPPORTED")
            elif self._equalizer_resolver is not None:
                queue_context = (
                    QueueEqualizerContext(
                        transient_preset_id=self._queue_equalizer_preset_id,
                        saved_queue_id=(
                            self._session.selected_playlist if self._session is not None else None
                        ),
                    )
                    if queue_id is not None
                    else None
                )
                preset, source = self._equalizer_resolver.resolve(
                    track,
                    queue_context,
                    self._default_equalizer_preset,
                )
                resolved = self._equalizer.resolve(preset, frequencies, source=source)
            else:
                resolved = self._equalizer.builtin(
                    self._default_equalizer_preset,
                    frequencies,
                    source="GLOBAL",
                )
        self._logger.info(
            "equalizer.resolve deck=%s preset=%s source=%s enabled=%s",
            deck.model.deck_id,
            resolved.name,
            resolved.source,
            resolved.enabled,
        )
        self._performance.record(
            f"equalizer_resolution_{resolved.source.lower()}_total",
            1.0,
            100.0,
        )
        return resolved

    def set_queue_equalizer_preset(self, preset_id: int | None) -> None:
        """Set a session-only queue preset; ``None`` restores inheritance."""
        self._queue_equalizer_preset_id = preset_id

    def equalizer_presets(self) -> tuple[tuple[str, str], ...]:
        if self._equalizer_resolver is None:
            return tuple(
                (preset.preset_id, preset.name) for preset in BUILTIN_EQUALIZER_PRESETS.values()
            )
        return tuple(
            (preset.preset_id, preset.name)
            for preset in self._equalizer_resolver.list_presets()
            if preset.preset_id != "disabled"
        )

    def equalizer_band_frequencies(self, deck_id: str) -> tuple[float, ...]:
        return self._deck(deck_id).equalizer_band_frequencies()

    def preview_equalizer(self, deck_id: str, preset_key: str) -> None:
        self._ensure_equalizer_change_allowed()
        deck = self._deck(deck_id)
        if deck.model.loaded_track is None:
            raise ValueError(f"Deck {deck_id} enthält keinen Titel")
        previous = self._deck_equalizer_snapshots.get(deck_id)
        if previous is not None:
            self._equalizer_preview_previous.setdefault(deck_id, previous)
        resolved = (
            self._resolve_equalizer(
                deck, deck.model.loaded_track, self._deck_queue_ids.get(deck_id)
            )
            if preset_key == "inherit"
            else self._resolve_selected_equalizer(deck, preset_key, "PREVIEW")
        )
        self._apply_resolved_equalizer(deck, resolved)
        self._view.show_deck(deck.model)

    def discard_equalizer_preview(self, deck_id: str) -> None:
        previous = self._equalizer_preview_previous.pop(deck_id, None)
        if previous is None:
            return
        deck = self._deck(deck_id)
        self._apply_resolved_equalizer(deck, previous)
        self._view.show_deck(deck.model)

    def save_track_equalizer(self, deck_id: str, preset_key: str | None) -> None:
        self._ensure_equalizer_change_allowed()
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        deck = self._deck(deck_id)
        track = deck.model.loaded_track
        if track is None:
            raise ValueError(f"Deck {deck_id} enthält keinen Titel")
        self._equalizer_resolver.assign_track(track.id, preset_key)
        self._equalizer_preview_previous.pop(deck_id, None)
        resolved = self._resolve_equalizer(deck, track, self._deck_queue_ids.get(deck_id))
        self._apply_resolved_equalizer(deck, resolved)
        self._view.show_deck(deck.model)

    def save_track_equalizer_by_id(self, track_id: int, preset_key: str | None) -> None:
        """Persist a catalog/queue title assignment without requiring a loaded deck."""
        self._ensure_equalizer_change_allowed()
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        if self._library_service.get_track(track_id) is None:
            raise ValueError("Titel nicht gefunden")
        self._equalizer_resolver.assign_track(track_id, preset_key)
        self._refresh_loaded_equalizers()

    def save_playlist_equalizer(self, preset_key: str | None) -> None:
        self._ensure_equalizer_change_allowed()
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        saved_queue_id = self._session.selected_playlist if self._session is not None else None
        if saved_queue_id is None:
            raise ValueError("Keine gespeicherte Playlist ausgewählt")
        self._equalizer_resolver.assign_saved_queue(saved_queue_id, preset_key)
        self._refresh_saved_queues()
        self._refresh_loaded_equalizers()

    def save_saved_queue_equalizer(self, saved_queue_id: int, preset_key: str | None) -> None:
        self._ensure_equalizer_change_allowed()
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        self._equalizer_resolver.assign_saved_queue(saved_queue_id, preset_key)
        self._refresh_saved_queues()
        self._refresh_loaded_equalizers()

    def saved_queue_equalizer_key(self, saved_queue_id: int) -> str | None:
        if self._equalizer_resolver is None:
            return None
        return self._equalizer_resolver.saved_queue_assignment_key(saved_queue_id)

    def save_genre_equalizer(self, deck_id: str, preset_key: str | None) -> None:
        self._ensure_equalizer_change_allowed()
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        track = self._deck(deck_id).model.loaded_track
        if track is None:
            raise ValueError(f"Deck {deck_id} enthält keinen Titel")
        if not track.genre.strip():
            raise ValueError("Der geladene Titel besitzt kein Genre")
        self._equalizer_resolver.assign_genre(track.genre, preset_key)
        self._refresh_loaded_equalizers()

    def equalizer_dialog_state(self, deck_id: str) -> EqualizerDialogState:
        deck = self._deck(deck_id)
        track = deck.model.loaded_track
        if track is None:
            raise ValueError(f"Deck {deck_id} enthält keinen Titel")
        resolver = self._equalizer_resolver
        saved_queue_id = self._session.selected_playlist if self._session is not None else None
        queue_key = None
        if resolver is not None and self._queue_equalizer_preset_id is not None:
            queue_key = next(
                (
                    preset.preset_id
                    for preset in resolver.list_presets()
                    if preset.database_id == self._queue_equalizer_preset_id
                ),
                None,
            )
        return EqualizerDialogState(
            deck_id,
            track.title,
            track.genre,
            deck.model.equalizer_preset_name,
            deck.model.equalizer_source,
            resolver.track_assignment_key(track.id) if resolver is not None else None,
            queue_key,
            (
                resolver.saved_queue_assignment_key(saved_queue_id)
                if resolver is not None and saved_queue_id is not None
                else None
            ),
            resolver.genre_assignment_key(track.genre) if resolver is not None else None,
            saved_queue_id,
        )

    def set_current_queue_equalizer(self, preset_key: str | None) -> None:
        self._ensure_equalizer_change_allowed()
        if preset_key is None or preset_key == "inherit":
            self._queue_equalizer_preset_id = None
            self._refresh_loaded_equalizers()
            return
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        preset = self._equalizer_resolver.preset_by_key(preset_key)
        if preset is None or preset.database_id is None:
            raise ValueError("Unbekanntes Equalizer-Preset")
        self._queue_equalizer_preset_id = preset.database_id
        self._refresh_loaded_equalizers()

    def _refresh_loaded_equalizers(self) -> None:
        for deck in (self.deck_a, self.deck_b):
            track = deck.model.loaded_track
            if track is None:
                continue
            self._apply_resolved_equalizer(
                deck,
                self._resolve_equalizer(
                    deck,
                    track,
                    self._deck_queue_ids.get(deck.model.deck_id),
                ),
            )
            self._view.show_deck(deck.model)

    def save_custom_equalizer(
        self,
        name: str,
        preamp_db: float,
        frequencies: tuple[float, ...],
        gains_db: tuple[float, ...],
    ) -> str:
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        if len(frequencies) != len(gains_db) or not frequencies:
            raise ValueError("Equalizer-Bänder sind unvollständig")
        highest_boost = max((0.0, *gains_db))
        if preamp_db > -highest_boost:
            raise ValueError(
                f"Clipping-Gefahr: Preamp muss höchstens {-highest_boost:.1f} dB betragen"
            )
        key = f"custom-{uuid4().hex}"
        preset = EqualizerPreset(
            key,
            name.strip(),
            preamp_db,
            tuple(zip(frequencies, gains_db, strict=True)),
        )
        self._equalizer.resolve(preset, frequencies, source="EDITOR")
        self._equalizer_resolver.save_custom(preset)
        return key

    def equalizer_editor_values(
        self, deck_id: str, preset_key: str
    ) -> tuple[str, float, tuple[float, ...]]:
        deck = self._deck(deck_id)
        frequencies = deck.equalizer_band_frequencies()
        if preset_key in {"inherit", "disabled"}:
            return ("Neutral", 0.0, tuple(0.0 for _ in frequencies))
        preset = (
            self._equalizer_resolver.preset_by_key(preset_key)
            if self._equalizer_resolver is not None
            else BUILTIN_EQUALIZER_PRESETS.get(preset_key)
        )
        if preset is None:
            raise ValueError("Unbekanntes Equalizer-Preset")
        resolved = self._equalizer.resolve(preset, frequencies, source="EDITOR")
        return preset.name, resolved.preamp_db, resolved.band_gains_db

    def rename_custom_equalizer(self, preset_key: str, name: str) -> None:
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        preset = self._equalizer_resolver.preset_by_key(preset_key)
        if preset is None:
            raise ValueError("Unbekanntes Equalizer-Preset")
        self._equalizer_resolver.save_custom(
            EqualizerPreset(
                preset.preset_id,
                name.strip(),
                preset.preamp_db,
                preset.curve,
                preset.database_id,
            )
        )

    def reset_custom_equalizer(self, preset_key: str) -> None:
        """Persist a neutral curve for a user preset without changing its identity."""
        if self._equalizer_resolver is None:
            raise RuntimeError("Equalizer-Persistenz ist nicht verfügbar")
        preset = self._equalizer_resolver.preset_by_key(preset_key)
        if preset is None:
            raise ValueError("Unbekanntes Equalizer-Preset")
        if not preset_key.startswith("custom-"):
            raise ValueError("Eingebaute Equalizer-Presets können nicht zurückgesetzt werden")
        self._equalizer_resolver.save_custom(
            EqualizerPreset(
                preset.preset_id,
                preset.name,
                0.0,
                tuple((frequency, 0.0) for frequency, _gain in preset.curve),
                preset.database_id,
            )
        )
        self._refresh_loaded_equalizers()

    def _resolve_selected_equalizer(
        self, deck: DeckController, preset_key: str, source: str
    ) -> ResolvedEqualizerPreset:
        if preset_key == "disabled":
            return ResolvedEqualizerPreset.disabled(source)
        preset = (
            self._equalizer_resolver.preset_by_key(preset_key)
            if self._equalizer_resolver is not None
            else BUILTIN_EQUALIZER_PRESETS.get(preset_key)
        )
        if preset is None:
            raise ValueError("Unbekanntes Equalizer-Preset")
        return self._equalizer.resolve(preset, deck.equalizer_band_frequencies(), source=source)

    def _ensure_equalizer_change_allowed(self) -> None:
        if self._transition.state == TransitionState.CROSSFADE:
            raise RuntimeError("Equalizer-Änderungen sind während eines Crossfades gesperrt")

    def _apply_resolved_equalizer(
        self,
        deck: DeckController,
        resolved: ResolvedEqualizerPreset,
    ) -> None:
        with self._performance.measure("equalizer.apply", warning_threshold_ms=10.0):
            previous_error = deck.model.equalizer_error
            changed = deck.apply_equalizer(resolved)
        self._deck_equalizer_snapshots[deck.model.deck_id] = resolved
        if deck.model.equalizer_error and deck.model.equalizer_error != previous_error:
            self._performance.record("equalizer_apply_failed_total", 1.0, 100.0)
            self._view.show_queue_warning(
                f"Deck {deck.model.deck_id}: Equalizer deaktiviert – "
                f"{deck.model.equalizer_error}"
            )
        elif changed:
            operation = "equalizer_apply_total" if resolved.enabled else "equalizer_disable_total"
            self._performance.record(operation, 1.0, 100.0)
        else:
            self._performance.record("equalizer_apply_skipped_total", 1.0, 100.0)

    def _resolve_loudness(self, track: Track) -> ResolvedLoudnessSettings | None:
        """Resolve cached gain and schedule missing tag reads without blocking the GUI."""
        if self._loudness is None:
            return None
        stored = self._loudness.get(track.id)
        if (
            self._background_analysis_enabled
            and stored.replaygain_scanned_at is None
            and self._replaygain_cache is not None
        ):
            self._replaygain_cache.request(track)
        return self._loudness.resolve(track.id)

    def _apply_resolved_loudness(
        self, deck: DeckController, resolved: ResolvedLoudnessSettings | None
    ) -> None:
        """Apply one prepared gain through the backend's non-blocking volume path."""
        with self._performance.measure("audio.apply_gain_command", warning_threshold_ms=10.0):
            self._loudness_playback.apply_resolved_loudness(
                deck.model.deck_id,
                resolved,
            )

    def _apply_cue_points(self, deck: DeckController) -> None:
        track = deck.model.loaded_track
        if track is None:
            return
        if self._cue_points is None:
            duration = max(0.0, deck.model.duration)
            boundaries = ResolvedTrackBoundaries(
                0.0,
                duration,
                min(self.AUTOMATIC_OVERLAP_SECONDS, duration),
                "FILE_BOUNDARY",
                "FILE_BOUNDARY",
                "GLOBAL",
            )
        else:
            queue_id = self._deck_queue_ids.get(deck.model.deck_id)
            queue_entry = self._queue_service.entry(queue_id) if queue_id is not None else None
            boundaries = self._cue_points.resolve(
                track, self.AUTOMATIC_OVERLAP_SECONDS, queue_entry
            )
        self._apply_resolved_boundaries(deck, boundaries)

    def _apply_resolved_boundaries(
        self, deck: DeckController, boundaries: ResolvedTrackBoundaries
    ) -> None:
        """Copy worker-resolved Cue boundaries without repository access."""
        deck.model.cue_in = boundaries.cue_in
        deck.model.cue_out = boundaries.cue_out
        deck.model.cue_fade_duration = boundaries.fade_duration
        deck.model.cue_in_source = boundaries.cue_in_source
        deck.model.cue_out_source = boundaries.cue_out_source
        deck.model.cue_fade_source = boundaries.fade_source
        deck.model.cue_warning = boundaries.warning
        deck.model.automatic_crossfade_allowed = boundaries.automatic_crossfade_allowed
        deck.model.cue_boundaries_ready = True
        self._cue_timing_warning.pop(deck.model.deck_id, None)

    def _warn_cue_timing_once(
        self,
        deck: DeckController,
        track_id: int,
        message: str,
        *,
        fallback_reason: str | None = None,
    ) -> None:
        if self._cue_timing_warning.get(deck.model.deck_id) == track_id:
            return
        self._cue_timing_warning[deck.model.deck_id] = track_id
        self._logger.warning("Deck %s: %s", deck.model.deck_id, message)
        if fallback_reason is not None:
            incoming = self.deck_b if deck is self.deck_a else self.deck_a
            self._queue_service.record_audit_event(
                "CUE_FALLBACK_ARMED",
                entity_type="QUEUE",
                entity_id=self._deck_queue_ids.get(deck.model.deck_id),
                details={
                    "reason": fallback_reason,
                    "fallback": "NATURAL_END_DIRECT_START",
                    "outgoing_deck": deck.model.deck_id,
                    "incoming_deck": incoming.model.deck_id,
                    "outgoing_track_id": track_id,
                    "incoming_track_id": (
                        incoming.model.loaded_track.id
                        if incoming.model.loaded_track is not None
                        else None
                    ),
                    "incoming_queue_id": self._deck_queue_ids.get(incoming.model.deck_id),
                },
            )

    def _status_tick(self) -> None:
        if self._status_tick_running:
            self._logger.warning("Überlappender Status-Tick wurde verhindert")
            return
        self._status_tick_running = True
        try:
            with self._performance.measure(
                "status_tick.total",
                warning_threshold_ms=self._performance_settings.gui_operation_warning_ms,
            ):
                self._run_status_tick()
        finally:
            self._status_tick_running = False

    def _run_status_tick(self) -> None:
        if self._closed:
            return
        self._check_audio_device_health()
        if self._queue_render_pending and not self._transition.is_transitioning:
            pending_update = self._pending_queue_view_update
            if pending_update is not None:
                self._deliver_queue_view_update(pending_update)
            else:
                self._view.show_queue(self._queue_entries_cache, self._queue_tracks_cache)
            self._pending_queue_view_update = None
            self._queue_render_pending = False
        if (
            self.player_mode == PlayerMode.AUTOMATIC
            and not self._automatic_run_active
            and not self._automatic_run_paused
        ):
            self._logger.warning("Automatik-Runner war inaktiv und wird wiederhergestellt")
            self._automatic_run_active = True
            self._view.show_automatic_playback(True)
        with self._performance.measure(
            "status_tick.background_callbacks",
            warning_threshold_ms=self._performance_settings.gui_step_warning_ms,
        ):
            self._drain_background_callbacks()
        queue_state_started = monotonic()
        for deck in (self.deck_a, self.deck_b):
            previous_state = deck.model.state
            with self._performance.measure(
                f"status_tick.deck_{deck.model.deck_id.lower()}_status",
                warning_threshold_ms=100.0,
                context={"deck": deck.model.deck_id},
            ):
                deck.update_status()
                if self._deck_health_monitor is not None:
                    self._deck_health_monitor.observe(deck)
            if previous_state == DeckState.PLAYING and deck.model.state == DeckState.FINISHED:
                self._schedule_finished_deck_completion(deck)
        self._publish_recovery_return_requirements()
        self._publish_emergency_dashboard()
        self._schedule_source_availability_checks()
        self._performance.record(
            "status_tick.queue_state",
            (monotonic() - queue_state_started) * 1000.0,
            self._performance_settings.gui_step_warning_ms,
        )
        with self._performance.measure(
            "status_tick.crossfader",
            warning_threshold_ms=self._performance_settings.gui_step_warning_ms,
        ):
            self.crossfader.apply()
        with self._performance.measure(
            "status_tick.render",
            warning_threshold_ms=self._performance_settings.gui_step_warning_ms,
        ):
            self._refresh_all()
        with self._performance.measure(
            "status_tick.queue_statistics",
            warning_threshold_ms=100.0,
        ):
            self._refresh_queue_stats()
        with self._performance.measure(
            "status_tick.autoload",
            warning_threshold_ms=self._performance_settings.gui_step_warning_ms,
        ):
            self._auto_load()
        with self._performance.measure(
            "status_tick.automatic_playback",
            warning_threshold_ms=self._performance_settings.gui_step_warning_ms,
        ):
            self._automatic_playback_tick()
        self._show_automatic_status()
        self._view.schedule(self._status_interval_ms(), self._status_tick)

    def _schedule_finished_deck_completion(self, deck: DeckController) -> None:
        """Let the status tick signal completion without doing repository work inline."""
        deck_id = deck.model.deck_id
        if deck_id in self._deck_completion_pending:
            return
        self._deck_completion_pending.add(deck_id)

        def finish_deck() -> None:
            if deck_id not in self._deck_completion_pending:
                return
            try:
                queue_id = self._deck_queue_ids[deck_id]
                request = (
                    self._history.prepare_finish(
                        deck_id,
                        CompletionStatus.COMPLETED,
                        deck.model.position,
                        transition_id=str(uuid4()),
                    )
                    if self._history is not None
                    else None
                )
                if request is not None:
                    self._enqueue_history_persist(request)
                queue_ids = (
                    {queue_id}
                    if queue_id is not None
                    else {
                        entry.queue_id
                        for entry in self._queue_entries_cache
                        if entry.loaded_deck == deck_id
                        and entry.status in {QueueStatus.LOADED, QueueStatus.PLAYING}
                    }
                )
                for index, entry in enumerate(self._queue_entries_cache):
                    if entry.queue_id in queue_ids:
                        updated_entry = replace(
                            entry, status=QueueStatus.PLAYED, played_at=datetime.now()
                        )
                        self._queue_entries_cache[index] = updated_entry
                        self._queue_entries_by_id_cache[entry.queue_id] = updated_entry
                if queue_ids:
                    self._queue_stats_dirty = True
                for completed_queue_id in queue_ids:
                    self._enqueue_queue_persist(completed_queue_id, deck_id)
                for entry in self._queue_entries_cache:
                    if entry.queue_id in queue_ids:
                        self._view.show_queue_entry(
                            entry, self._queue_tracks_cache.get(entry.track_id)
                        )
                if self._automatic_run_active and not self._transition.is_transitioning:
                    deck.eject()
                    self._deck_queue_ids[deck_id] = None
                    self._view.show_deck_cover(deck_id, None)
                    self._view.schedule(0, self._auto_load)
            finally:
                self._deck_completion_pending.discard(deck_id)
                self._one_deck_fade_pending.discard(deck_id)

        finish_deck.__name__ = f"finish_deck_{deck_id.lower()}"
        self._view.schedule(0, finish_deck)

    def _heartbeat_tick(self) -> None:
        if self._closed:
            return
        if not self._heartbeat_started:
            layout = self._callback_state.snapshot()
            startup_pending = (
                layout.pending_layout_refreshes
                or layout.pending_focus_request
                or layout.pending_catalog_chunks
                or layout.pending_queue_chunks
            )
            if startup_pending:
                self._callback_state.heartbeat()
                self._view.schedule(
                    self._performance_settings.gui_heartbeat_interval_ms,
                    self._heartbeat_tick,
                )
                return
            self._heartbeat_started = True
            self._callback_state.heartbeat()
            if self._heartbeat_watchdog_enabled:
                self._heartbeat_watchdog.start()
            self._heartbeat.start()
            self._view.schedule(
                self._performance_settings.gui_heartbeat_interval_ms,
                self._heartbeat_tick,
            )
            return
        self._callback_state.heartbeat()
        delay_ms = self._heartbeat.beat()
        if delay_ms >= self._performance_settings.gui_heartbeat_warning_ms:
            snapshot = self._callback_state.snapshot()
            self._logger.warning(
                "GUI-Heartbeat %.1f ms; zuletzt gestartet=%s; zuletzt abgeschlossen=%s; "
                "aktiv=%s; Katalog-Render=%s; Queue-Render=%s",
                delay_ms,
                snapshot.last_started_gui_callback,
                snapshot.last_completed_gui_callback,
                snapshot.active_gui_callback,
                snapshot.active_catalog_render,
                snapshot.active_queue_render,
            )
        self._view.schedule(
            self._performance_settings.gui_heartbeat_interval_ms, self._heartbeat_tick
        )

    def _memory_tick(self) -> None:
        """Capture one bounded sample on the GUI thread every five seconds."""
        if self._closed or not self._performance_settings.enabled:
            return
        self._sample_memory()
        self._view.schedule(5000, self._memory_tick)

    def _sample_memory(self, *, query_view: bool = True) -> None:
        if query_view:
            self._memory_gauges_cache = self._view.memory_gauges()
        gauges = self._memory_gauges_cache
        self._memory_monitor.sample(
            gui_event_queue_size=self._gui_dispatcher.statistics().pending,
            active_worker_count=len(self._worker_registry.active()),
            cover_cache_size=gauges.get("cover_cache_size", 0),
            registered_widget_count=gauges.get("registered_widget_count", 0),
            active_preview_count=gauges.get("active_preview_count", 0),
            active_vlc_player_count=VlcAudioBackend.active_player_count(),
        )

    def _status_interval_ms(self) -> int:
        active = (
            self._automatic_run_active
            or self._transition.is_transitioning
            or self.deck_a.is_fading
            or self.deck_b.is_fading
            or self.deck_a.model.state == DeckState.PLAYING
            or self.deck_b.model.state == DeckState.PLAYING
        )
        return 200 if active else 750

    def _automatic_playback_tick(self) -> None:
        if not self._automatic_run_active or self._transition.is_transitioning:
            return
        if self._preload_in_progress or self._one_deck_start_pending is not None:
            return
        if self._one_deck_fade_pending:
            return
        playing = [
            deck for deck in (self.deck_a, self.deck_b) if deck.model.state == DeckState.PLAYING
        ]
        if not playing:
            candidate = next(
                (
                    deck
                    for deck in (self.deck_a, self.deck_b)
                    if deck.model.loaded_track is not None
                    and deck.model.state in {DeckState.LOADED, DeckState.STOPPED}
                    and self._deck_is_ready_for_automatic_playback(deck)
                ),
                None,
            )
            if candidate is None:
                pending_queue = any(
                    entry.status in {QueueStatus.WAITING, QueueStatus.LOADED}
                    for entry in self._queue_entries_cache
                )
                if self._preload_in_progress or pending_queue:
                    return
                self._logger.info("Automatik beendet: keine weiteren Queue-Titel vorhanden")
                self.stop_automatic_queue(
                    reason_code="QUEUE_EXHAUSTED",
                    completed=True,
                )
                return
            candidate.set_transition_muted(False)
            self.crossfader.set_position(0.0 if candidate.model.deck_id == "A" else 1.0)
            if not self._one_deck_mode.crossfade_allowed():
                candidate.set_fade_level_immediately(0.0)
            self.deck_action(candidate.model.deck_id, "play", automatic=True)
            if not self._one_deck_mode.crossfade_allowed():
                self._begin_one_deck_playback_confirmation(candidate)
            return
        if not self._one_deck_mode.crossfade_allowed():
            if len(playing) == 1:
                self._arm_one_deck_fade_out(playing[0])
            return
        if len(playing) == 2:
            # Manual playback may have started the second deck while automatic
            # playback was paused. Resume with a defined transition instead of
            # leaving the automatic runner active but idle forever.
            outgoing = self.deck_a if self.crossfader.position <= 0.5 else self.deck_b
            incoming = self.deck_b if outgoing is self.deck_a else self.deck_a
            self._transition.begin(
                outgoing,
                incoming,
                self._deck_queue_ids[outgoing.model.deck_id],
                self._resolved_boundaries(outgoing),
            )
            return
        if len(playing) != 1:
            return
        outgoing = playing[0]
        incoming = self.deck_b if outgoing is self.deck_a else self.deck_a
        boundaries = self._resolved_boundaries(outgoing)
        outgoing_track = outgoing.model.loaded_track
        assert outgoing_track is not None
        if not boundaries.automatic_crossfade_allowed:
            self._warn_cue_timing_once(
                outgoing,
                outgoing_track.id,
                boundaries.warning
                or "Cue-Werte erlauben keinen sicheren Crossfade; am Dateiende wird fortgesetzt",
                fallback_reason="AUTOMATIC_CROSSFADE_NOT_ALLOWED",
            )
            return
        time_to_cue_out = boundaries.cue_out - outgoing.model.position
        if time_to_cue_out <= 0:
            self._warn_cue_timing_once(
                outgoing,
                outgoing_track.id,
                "Cue Out wurde bereits überschritten; am natürlichen Dateiende wird "
                "direkt mit dem vorbereiteten Titel fortgesetzt",
                fallback_reason="CUE_OUT_ALREADY_PASSED",
            )
            return
        if time_to_cue_out < self.MINIMUM_TRANSITION_PREPARATION_SECONDS:
            self._warn_cue_timing_once(
                outgoing,
                outgoing_track.id,
                "Bis Cue Out bleibt keine sichere Vorbereitungszeit; am natürlichen "
                "Dateiende wird direkt mit dem vorbereiteten Titel fortgesetzt",
                fallback_reason="INSUFFICIENT_PREPARATION_TIME",
            )
            return
        reached_crossfade_start = outgoing.model.position >= boundaries.crossfade_start
        if (
            reached_crossfade_start
            and outgoing.model.duration > 0
            and incoming.model.loaded_track is not None
            and incoming.model.state in {DeckState.LOADED, DeckState.STOPPED}
            and self._deck_is_ready_for_automatic_playback(incoming)
        ):
            self.crossfader.set_position(0.0 if outgoing is self.deck_a else 1.0)
            incoming.set_transition_muted(True)
            self.crossfader.apply()
            incoming_boundaries = self._resolved_boundaries(incoming)
            incoming.seek(incoming_boundaries.cue_in)
            self.deck_action(incoming.model.deck_id, "play", automatic=True)
            self._transition.begin(
                outgoing,
                incoming,
                self._deck_queue_ids[outgoing.model.deck_id],
                boundaries,
            )

    def _arm_one_deck_fade_out(self, deck: DeckController) -> None:
        deck_id = deck.model.deck_id
        if deck_id in self._one_deck_fade_pending or deck.is_fading:
            return
        boundaries = self._resolved_boundaries(deck)
        end = boundaries.cue_out if boundaries.cue_out > 0.0 else deck.model.duration
        if end <= 0.0 or end - deck.model.position > self.ONE_DECK_FADE_SECONDS:
            return
        self._one_deck_fade_pending.add(deck_id)
        deck.start_fade(
            0.0,
            self.ONE_DECK_FADE_SECONDS,
            self._view.schedule,
            stop_after=True,
        )
        self._queue_service.record_audit_event(
            "ONE_DECK_SEQUENTIAL_FADE",
            entity_type="QUEUE",
            entity_id=self._deck_queue_ids.get(deck_id),
            details={"deck_id": deck_id, "duration": self.ONE_DECK_FADE_SECONDS},
        )
        self._view.schedule(
            round(self.ONE_DECK_FADE_SECONDS * 1000) + 50,
            lambda: self._schedule_finished_deck_completion(deck),
        )

    def _begin_one_deck_playback_confirmation(self, deck: DeckController) -> None:
        """Keep a one-deck start muted until VLC shows real forward progress."""
        self._one_deck_start_generation += 1
        generation = self._one_deck_start_generation
        deck_id = deck.model.deck_id
        queue_id = self._deck_queue_ids.get(deck_id)
        start_position = deck.backend.get_position()
        self._one_deck_start_pending = deck_id
        self._confirm_one_deck_playback(
            deck,
            queue_id,
            start_position,
            generation,
            0,
        )

    def _confirm_one_deck_playback(
        self,
        deck: DeckController,
        queue_id: int | None,
        start_position: float,
        generation: int,
        step: int,
    ) -> None:
        if generation != self._one_deck_start_generation:
            return
        current_queue_id = self._deck_queue_ids.get(deck.model.deck_id)
        if (
            self._one_deck_start_pending != deck.model.deck_id
            or current_queue_id != queue_id
            or deck.model.loaded_track is None
            or deck.model.state != DeckState.PLAYING
        ):
            self._one_deck_start_pending = None
            return
        actual_position = deck.backend.get_position()
        if deck.backend.is_playing() and actual_position >= start_position + 0.1:
            self._one_deck_start_pending = None
            self._logger.info(
                "Ein-Deck-Wiedergabe auf Deck %s bei %.2f Sekunden bestätigt",
                deck.model.deck_id,
                actual_position,
            )
            deck.start_fade(1.0, self.ONE_DECK_FADE_SECONDS, self._view.schedule)
            return
        if step == self.ONE_DECK_START_RETRY_STEP:
            try:
                deck.play()
                self._logger.warning(
                    "Ein-Deck-Wiedergabestart auf Deck %s wird einmalig wiederholt",
                    deck.model.deck_id,
                )
            except Exception as exc:
                self._logger.warning(
                    "Wiederholung des Ein-Deck-Wiedergabestarts auf Deck %s fehlgeschlagen: %s",
                    deck.model.deck_id,
                    exc,
                )
        if step >= self.ONE_DECK_START_WAIT_STEPS:
            self._handle_unconfirmed_one_deck_playback(deck, queue_id)
            return

        self._view.schedule(
            self.ONE_DECK_START_WAIT_INTERVAL_MS,
            lambda: self._confirm_one_deck_playback(
                deck,
                queue_id,
                start_position,
                generation,
                step + 1,
            ),
        )

    def _handle_unconfirmed_one_deck_playback(
        self,
        deck: DeckController,
        queue_id: int | None,
    ) -> None:
        """Skip silent media and let the same deck try the next queue entry."""
        self._one_deck_start_generation += 1
        self._one_deck_start_pending = None
        deck_id = deck.model.deck_id
        if queue_id is not None:
            if self._history is not None:
                self._history.finish(
                    deck_id,
                    CompletionStatus.FAILED,
                    deck.model.position,
                    error_message="ONE_DECK_PLAYBACK_NOT_CONFIRMED",
                    skip_code=HistoryReasonCode.PLAYBACK_ERROR,
                )
            entry = self._queue_service.entry(queue_id)
            if entry is not None and entry.status == QueueStatus.PLAYING:
                self._queue_service.mark_skipped(
                    queue_id,
                    "Ein-Deck-Wiedergabe nicht bestätigt",
                    code="ONE_DECK_PLAYBACK_NOT_CONFIRMED",
                )
        deck.eject()
        self._deck_queue_ids[deck_id] = None
        self._view.show_deck_cover(deck_id, None)
        self._logger.warning(
            "Deck %s meldet im Ein-Deck-Betrieb keinen Wiedergabefortschritt; Titel übersprungen",
            deck_id,
        )
        self._view.show_queue_warning(
            f"Titel auf Deck {deck_id} ohne bestätigte Wiedergabe übersprungen; "
            "der nächste Titel wird vorbereitet."
        )
        self._refresh_queue()
        self._auto_load()

    def _deck_is_ready_for_automatic_playback(self, deck: DeckController) -> bool:
        """Do not start adopted media before its queue READY commit completes."""
        queue_id = self._deck_queue_ids.get(deck.model.deck_id)
        if queue_id is None:
            return True
        entry = self._queue_service.entry(queue_id)
        return entry is not None and entry.status in {QueueStatus.READY, QueueStatus.PLAYING}

    def _resolved_boundaries(self, deck: DeckController) -> ResolvedTrackBoundaries:
        track = deck.model.loaded_track
        if track is None or self._cue_points is None:
            duration = max(0.0, deck.model.duration)
            fade = min(self.AUTOMATIC_OVERLAP_SECONDS, duration)
            return ResolvedTrackBoundaries(
                0.0, duration, fade, "FILE_BOUNDARY", "FILE_BOUNDARY", "GLOBAL"
            )
        if not deck.model.cue_boundaries_ready:
            queue_id = self._deck_queue_ids.get(deck.model.deck_id)
            queue_entry = self._queue_service.entry(queue_id) if queue_id is not None else None
            boundaries = self._cue_points.resolve(
                track, self.AUTOMATIC_OVERLAP_SECONDS, queue_entry
            )
            deck.model.cue_in = boundaries.cue_in
            deck.model.cue_out = boundaries.cue_out
            deck.model.cue_fade_duration = boundaries.fade_duration
            deck.model.cue_in_source = boundaries.cue_in_source
            deck.model.cue_out_source = boundaries.cue_out_source
            deck.model.cue_fade_source = boundaries.fade_source
            deck.model.cue_warning = boundaries.warning
            deck.model.automatic_crossfade_allowed = boundaries.automatic_crossfade_allowed
            deck.model.cue_boundaries_ready = True
        return ResolvedTrackBoundaries(
            deck.model.cue_in,
            deck.model.cue_out,
            deck.model.cue_fade_duration,
            deck.model.cue_in_source,
            deck.model.cue_out_source,
            deck.model.cue_fade_source,
            deck.model.automatic_crossfade_allowed,
            deck.model.cue_warning,
        )

    def _complete_automatic_transition(
        self,
        outgoing: DeckController,
        outgoing_track_id: int | None,
        outgoing_queue_id: int | None,
    ) -> None:
        with self._performance.measure("transition_completion.total", warning_threshold_ms=50.0):
            self._complete_automatic_transition_impl(outgoing, outgoing_track_id, outgoing_queue_id)

    def _complete_automatic_transition_impl(
        self,
        outgoing: DeckController,
        outgoing_track_id: int | None,
        outgoing_queue_id: int | None,
    ) -> None:
        if self._transition_completion_pending:
            self._logger.warning("Doppelter Transition-Abschluss wurde verhindert")
            return
        self._transition_completion_pending = True
        # A status tick can observe the outgoing deck's natural end while the
        # crossfade completion callback is already queued.  The transition owns
        # that completion, so invalidate the older natural-end callback.
        self._deck_completion_pending.discard(outgoing.model.deck_id)
        transition_id = str(uuid4())
        history_request = None
        outgoing_position = outgoing.model.position
        outgoing_cleanup: Callable[[], None] | None = None
        try:
            with self._performance.measure(
                "transition_completion.stop_outgoing", warning_threshold_ms=25.0
            ):
                current_track = outgoing.model.loaded_track
                if current_track is not None and current_track.id == outgoing_track_id:
                    with self._performance.measure(
                        "transition_completion.stop_outgoing.detach",
                        warning_threshold_ms=10.0,
                    ):
                        outgoing_cleanup = outgoing.detach_for_cleanup()
                    if outgoing_queue_id is not None:
                        with self._performance.measure(
                            "transition_completion.stop_outgoing.release_assignment",
                            warning_threshold_ms=10.0,
                        ):
                            released = self._queue_service.release_playing_deck_assignment(
                                outgoing_queue_id,
                                outgoing.model.deck_id,
                            )
                        if not released:
                            self._logger.info(
                                "Veraltete Deckfreigabe für Queue-Eintrag %s "
                                "auf Deck %s übersprungen",
                                outgoing_queue_id,
                                outgoing.model.deck_id,
                            )
                    self._deck_queue_ids[outgoing.model.deck_id] = None
                    with self._performance.measure(
                        "transition_completion.stop_outgoing.clear_cover",
                        warning_threshold_ms=10.0,
                    ):
                        self._view.show_deck_cover(outgoing.model.deck_id, None)
            with self._performance.measure(
                "transition_completion.enqueue_history", warning_threshold_ms=10.0
            ):
                if self._history is not None:
                    history_request = self._history.prepare_finish(
                        outgoing.model.deck_id,
                        CompletionStatus.COMPLETED,
                        outgoing_position,
                        transition_id=transition_id,
                    )
                    if history_request is not None:
                        self._enqueue_history_persist(history_request)
            with self._performance.measure(
                "transition_completion.queue.total", warning_threshold_ms=10.0
            ):
                with self._performance.measure(
                    "transition_completion.queue.remove_played",
                    warning_threshold_ms=5.0,
                ):
                    # Completed entries remain visible as PLAYED; no structural
                    # removal or position normalization is required here.
                    pass
                with self._performance.measure(
                    "transition_completion.queue.update_memory", warning_threshold_ms=5.0
                ):
                    queue_ids = (
                        {outgoing_queue_id}
                        if outgoing_queue_id is not None
                        else {
                            entry.queue_id
                            for entry in self._queue_entries_cache
                            if entry.loaded_deck == outgoing.model.deck_id
                            and entry.status in {QueueStatus.LOADED, QueueStatus.PLAYING}
                        }
                    )
                    changed_indices: list[int] = []
                    for index, entry in enumerate(self._queue_entries_cache):
                        if entry.queue_id in queue_ids:
                            updated_entry = replace(
                                entry, status=QueueStatus.PLAYED, played_at=datetime.now()
                            )
                            self._queue_entries_cache[index] = updated_entry
                            self._queue_entries_by_id_cache[entry.queue_id] = updated_entry
                            changed_indices.append(index)
                    if changed_indices:
                        self._queue_stats_dirty = True
                with self._performance.measure(
                    "transition_completion.queue.publish_dirty_rows",
                    warning_threshold_ms=10.0,
                ):
                    if changed_indices:
                        self._queue_revision += 1
                        changed_entries = tuple(
                            self._queue_entries_cache[index] for index in changed_indices
                        )
                        render_update = QueueViewUpdate(
                            tuple(
                                QueueViewEvent(
                                    QueueViewEventType.ENTRY_STATUS_CHANGED,
                                    entry.queue_id,
                                    self._queue_revision,
                                    index,
                                )
                                for index, entry in zip(
                                    changed_indices, changed_entries, strict=True
                                )
                            ),
                            tuple(self._queue_entries_cache),
                            dict(self._queue_tracks_cache),
                        )

                        def render_completed_queue_rows() -> None:
                            self._deliver_queue_view_update(render_update)

                        self._view.schedule(
                            0,
                            render_completed_queue_rows,
                        )
            with self._performance.measure(
                "transition_completion.prepare_next", warning_threshold_ms=5.0
            ):
                if outgoing_cleanup is None:
                    self._view.schedule(0, self._auto_load)
                else:

                    def cleanup_outgoing_deck() -> None:
                        assert outgoing_cleanup is not None
                        with self._performance.measure(
                            "worker.transition_cleanup.backend_stop",
                            warning_threshold_ms=250.0,
                            context={"deck_id": outgoing.model.deck_id},
                        ):
                            outgoing_cleanup()
                        if not self._closed:
                            self._publish_gui_callback(
                                self._auto_load,
                                "transition_cleanup_autoload",
                                coalesce_key=f"autoload-{outgoing.model.deck_id}",
                            )

                    self._start_worker(
                        cleanup_outgoing_deck,
                        f"transition-cleanup-{outgoing.model.deck_id}",
                        "transition_cleanup",
                        executor=self._preload_executor,
                    )

                def refresh_transition_statistics() -> None:
                    with self._performance.measure(
                        "transition_completion.queue.statistics",
                        warning_threshold_ms=10.0,
                    ):
                        self._refresh_queue_stats(background_rebuild=True)

                self._view.schedule(0, refresh_transition_statistics)
            with self._performance.measure(
                "transition_completion.enqueue_queue_persist",
                warning_threshold_ms=10.0,
            ):
                for queue_id in queue_ids:
                    self._enqueue_queue_persist(queue_id, outgoing.model.deck_id)
            self._diagnostic_scenario.transition_completed()
        finally:
            self._transition_completion_pending = False

    def _enqueue_history_persist(self, request: HistoryPersistRequest) -> None:
        """Submit ordered, retryable history I/O without delaying audio state."""
        if self._history is None:
            return
        history = self._history
        self._diagnostic_scenario.persistence_submitted()

        def persist() -> None:
            try:
                with (
                    self._performance.measure(
                        "worker.playback_persist", warning_threshold_ms=1500.0
                    ),
                    self._performance.measure(
                        "database.history.total", warning_threshold_ms=1500.0
                    ),
                ):
                    with self._performance.measure(
                        "database.injected_delay", warning_threshold_ms=1500.0
                    ):
                        self._diagnostic_scenario.inject_database_delay()
                    with (
                        self._performance.measure(
                            "database.history.commit", warning_threshold_ms=250.0
                        ),
                        self._performance.measure(
                            "history_persist.repository", warning_threshold_ms=250.0
                        ),
                    ):
                        self._retry_persistence(lambda: history.persist(request))
                self._diagnostic_scenario.persistence_completed()
            except Exception:
                self._diagnostic_scenario.persistence_failed()
                raise

        accepted = self._start_worker(
            persist,
            f"history-persist-{getattr(request, 'transition_id', 'unknown')}",
            "history_persist",
            str(getattr(request, "transition_id", "unknown")),
            executor=self._persistence_executor,
        )
        if not accepted:
            self._diagnostic_scenario.persistence_failed()

    def _enqueue_queue_persist(self, queue_id: int, deck_id: str) -> None:
        """Persist the already-applied in-memory queue completion serially."""
        self._diagnostic_scenario.persistence_submitted()
        playback_generation = self._queue_playback_generations.get(queue_id, 0)

        def persist() -> None:
            try:
                with (
                    self._performance.measure(
                        "worker.playback_persist", warning_threshold_ms=1500.0
                    ),
                    self._performance.measure("database.queue.total", warning_threshold_ms=1500.0),
                ):
                    with self._performance.measure(
                        "database.injected_delay", warning_threshold_ms=1500.0
                    ):
                        self._diagnostic_scenario.inject_database_delay()
                    with (
                        self._performance.measure(
                            "database.queue.commit", warning_threshold_ms=250.0
                        ),
                        self._performance.measure(
                            "transition_completion.queue.persist",
                            warning_threshold_ms=250.0,
                        ),
                    ):
                        with self._queue_playback_generation_lock:
                            if (
                                self._queue_playback_generations.get(queue_id, 0)
                                == playback_generation
                            ):
                                self._retry_persistence(
                                    lambda: self._queue_service.mark_finished(
                                        queue_id, QueueStatus.PLAYED
                                    )
                                )
                            else:
                                self._logger.info(
                                    "Veralteter Queue-Abschluss für Eintrag %s verworfen",
                                    queue_id,
                                )
                self._diagnostic_scenario.persistence_completed()
            except Exception:
                self._diagnostic_scenario.persistence_failed()
                raise

        accepted = self._start_worker(
            persist,
            f"queue-persist-{queue_id}",
            "queue_persist",
            f"queue-{queue_id}-{deck_id}",
            executor=self._persistence_executor,
        )
        if not accepted:
            self._diagnostic_scenario.persistence_failed()

    @staticmethod
    def _retry_persistence(operation: Callable[[], object], attempts: int = 3) -> None:
        """Retry short transient database failures without affecting playback."""
        for attempt in range(attempts):
            try:
                operation()
                return
            except sqlite3.OperationalError:
                if attempt + 1 >= attempts:
                    raise
                sleep(0.05 * (attempt + 1))

    def _recover_deck_queue_ids(self) -> None:
        """Rebuild the explicit deck/queue relation after session restoration."""
        for entry in self._queue_service.entries():
            if entry.loaded_deck in self._deck_queue_ids and entry.status in {
                QueueStatus.LOADED,
                QueueStatus.PLAYING,
            }:
                deck = self._deck(entry.loaded_deck)
                if (
                    deck.model.loaded_track is not None
                    and deck.model.loaded_track.id == entry.track_id
                ):
                    self._deck_queue_ids[entry.loaded_deck] = entry.queue_id

    def _refresh_queue(self, *, refresh_stats: bool = True) -> None:
        entries = self._queue_service.entries()
        track_ids = {entry.track_id for entry in entries}
        tracks = {
            track_id: track
            for track_id, track in self._queue_tracks_cache.items()
            if track_id in track_ids
        }
        missing_ids = list(track_ids - tracks.keys())
        if missing_ids:
            tracks.update(self._library_service.get_tracks(missing_ids))
        previous_entries = self._queue_entries_cache
        if entries != previous_entries:
            self._next_preload_candidate_search_at = 0.0
            self._preload_candidate_misses = 0
        stats_signature = self._queue_statistics_signature(entries, tracks)
        if stats_signature != self._queue_stats_signature:
            self._queue_stats_dirty = True
            self._queue_stats_signature = stats_signature
            self._queue_stats_generation += 1
        if entries != previous_entries:
            self._queue_revision += 1
        events = queue_view_events(previous_entries, entries, self._queue_revision)
        current_ids = {entry.queue_id for entry in entries}
        if (
            self._selected_queue_entry_id is not None
            and self._selected_queue_entry_id not in current_ids
        ):
            events += (
                QueueViewEvent(
                    QueueViewEventType.SELECTION_CHANGED,
                    self._selected_queue_entry_id,
                    self._queue_revision,
                    selected=False,
                ),
            )
            self._selected_queue_entry_id = None
        self._queue_entries_cache = entries
        self._queue_entries_by_id_cache = {entry.queue_id: entry for entry in entries}
        self._queue_tracks_cache = tracks
        self._view.show_queue_origin(derive_queue_origin(entries).label)
        cue_warnings: dict[int, str] = {}
        if self._cue_points is not None:
            for entry in entries:
                if not entry.has_cue_overrides:
                    continue
                track = tracks.get(entry.track_id)
                if track is None:
                    continue
                boundaries = self._cue_points.resolve(track, self.AUTOMATIC_OVERLAP_SECONDS, entry)
                if boundaries.warning:
                    cue_warnings[entry.queue_id] = boundaries.warning
                elif not boundaries.automatic_crossfade_allowed:
                    cue_warnings[entry.queue_id] = (
                        "Diese Cue-Werte erlauben keinen sicheren automatischen Übergang"
                    )
        self._view.show_queue_cue_warnings(cue_warnings)
        self._show_automatic_status()
        self._capture_memory_stress_cycle(entries)
        if self._transition.is_transitioning and events:
            structural_types = {
                QueueViewEventType.ENTRY_ADDED,
                QueueViewEventType.ENTRY_REMOVED,
                QueueViewEventType.ENTRY_MOVED,
                QueueViewEventType.PAGE_CHANGED,
                QueueViewEventType.RESET,
            }
            structural_events = tuple(
                event for event in events if event.event_type in structural_types
            )
            incremental_events = tuple(
                event for event in events if event.event_type not in structural_types
            )
            if incremental_events:
                self._deliver_queue_view_update(
                    QueueViewUpdate(incremental_events, tuple(entries), dict(tracks))
                )
            if structural_events:
                self._queue_render_pending = True
                self._pending_queue_view_update = QueueViewUpdate(
                    (
                        QueueViewEvent(
                            QueueViewEventType.RESET,
                            None,
                            self._queue_revision,
                        ),
                    ),
                    tuple(entries),
                    dict(tracks),
                )
        elif events:
            self._deliver_queue_view_update(QueueViewUpdate(events, tuple(entries), dict(tracks)))
        elif not self._transition.is_transitioning:
            self._view.show_queue(entries, tracks)
        if refresh_stats:
            self._refresh_queue_stats()

    def _capture_memory_stress_cycle(self, entries: list[QueueEntry]) -> None:
        """Record a cycle when a diagnostic memory-stress queue becomes empty."""
        if self._diagnostic_context != "memory_stress":
            return
        if entries:
            self._memory_stress_was_populated = True
            self._memory_stress_peak_queue_size = max(
                self._memory_stress_peak_queue_size, len(entries)
            )
            return
        if not self._memory_stress_was_populated:
            return
        self._memory_stress_was_populated = False
        self._memory_stress_cycle_number += 1
        gc.collect()
        self._sample_memory()
        self._memory_monitor.record_stress_cycle(
            self._memory_stress_cycle_number,
            self._memory_stress_peak_queue_size,
            self._view.widget_diagnostics(),
        )
        self._memory_stress_peak_queue_size = 0

    def _refresh_catalog_page(self) -> None:
        total = self._library_service.count(self._catalog_query)
        page_count = max(1, (total + self.CATALOG_PAGE_SIZE - 1) // self.CATALOG_PAGE_SIZE)
        self._catalog_page = min(self._catalog_page, page_count - 1)
        offset = self._catalog_page * self.CATALOG_PAGE_SIZE
        self._catalog = (
            self._library_service.search(self._catalog_query, self.CATALOG_PAGE_SIZE, offset)
            if self._catalog_query
            else self._library_service.page(self.CATALOG_PAGE_SIZE, offset)
        )
        summary = (
            f"{total} Treffer für „{self._catalog_query}“"
            if self._catalog_query
            else f"{total:,} Titel im Partykatalog".replace(",", ".")
        )
        self._view.show_catalog(self._catalog, summary)
        self._view.show_catalog_paging(self._catalog_page + 1, page_count)

    def _refresh_queue_stats(self, *, background_rebuild: bool = False) -> None:
        if self._queue_stats_dirty and not self._queue_stats_rebuild_pending:
            if self._status_tick_running or background_rebuild:
                self._schedule_queue_stats_rebuild()
            else:
                result = self._build_queue_stats_cache(
                    tuple(self._queue_entries_cache),
                    dict(self._queue_tracks_cache),
                    effective_cues=self.queue_stats_use_effective_cues,
                )
                self._apply_queue_stats_cache(result)
        with self._performance.measure(
            "status_tick.queue_statistics.dynamic",
            warning_threshold_ms=5.0,
        ):
            remaining_duration = self._queue_stats_remaining_duration
            for queue_id in self._queue_stats_playing_ids:
                entry = self._queue_entries_by_id_cache.get(queue_id)
                if entry is None or entry.loaded_deck not in {"A", "B"}:
                    continue
                deck = self._deck(entry.loaded_deck)
                if deck.model.loaded_track is None or deck.model.loaded_track.id != entry.track_id:
                    continue
                track = self._queue_tracks_cache.get(entry.track_id)
                end = (
                    deck.model.cue_out
                    if self.queue_stats_use_effective_cues and deck.model.cue_boundaries_ready
                    else float(track.duration_seconds or 0.0) if track is not None else 0.0
                )
                remaining_duration -= self._queue_stats_durations.get(queue_id, 0.0)
                remaining_duration += max(0.0, end - deck.model.position)
        self._view.show_queue_stats(
            QueueStats(
                total_tracks=len(self._queue_entries_cache),
                total_duration=self._queue_stats_total_duration,
                remaining_tracks=len(self._queue_stats_remaining_ids),
                remaining_duration=remaining_duration,
            )
        )

    def _schedule_queue_stats_rebuild(self) -> None:
        entries = tuple(self._queue_entries_cache)
        tracks = dict(self._queue_tracks_cache)
        generation = self._queue_stats_generation
        effective_cues = self.queue_stats_use_effective_cues
        self._queue_stats_rebuild_pending = True

        def rebuild() -> None:
            try:
                with self._performance.measure(
                    "worker.queue_statistics.rebuild",
                    warning_threshold_ms=500.0,
                ):
                    result = self._build_queue_stats_cache(
                        entries,
                        tracks,
                        effective_cues=effective_cues,
                    )
            except Exception:
                self._publish_gui_callback(
                    lambda: self._finish_queue_stats_rebuild(None, generation, effective_cues),
                    "queue_statistics_failed",
                    coalesce_key="queue_statistics",
                )
                raise
            self._publish_gui_callback(
                lambda: self._finish_queue_stats_rebuild(result, generation, effective_cues),
                "queue_statistics_ready",
                coalesce_key="queue_statistics",
            )

        if not self._start_worker(
            rebuild,
            "queue-statistics",
            "statistics",
            executor=self._statistics_executor,
        ):
            self._queue_stats_rebuild_pending = False

    def _build_queue_stats_cache(
        self,
        entries: tuple[QueueEntry, ...],
        tracks: dict[int, Track],
        *,
        effective_cues: bool,
    ) -> tuple[dict[int, float], float, tuple[int, ...], tuple[int, ...], float]:
        remaining_statuses = {
            QueueStatus.WAITING,
            QueueStatus.LOADED,
            QueueStatus.PLAYING,
        }
        durations: dict[int, float] = {}
        remaining_ids: list[int] = []
        playing_ids: list[int] = []
        total_duration = 0.0
        remaining_duration = 0.0
        for entry in entries:
            track = tracks.get(entry.track_id)
            if track is None:
                duration = 0.0
            elif not effective_cues or self._cue_points is None:
                duration = float(track.duration_seconds or 0.0)
            else:
                boundaries = self._cue_points.resolve(track, queue_entry=entry)
                duration = max(0.0, boundaries.cue_out - boundaries.cue_in)
            durations[entry.queue_id] = duration
            total_duration += duration
            if entry.status in remaining_statuses:
                remaining_ids.append(entry.queue_id)
                remaining_duration += duration
            if entry.status == QueueStatus.PLAYING:
                playing_ids.append(entry.queue_id)
        return (
            durations,
            total_duration,
            tuple(remaining_ids),
            tuple(playing_ids),
            remaining_duration,
        )

    def _finish_queue_stats_rebuild(
        self,
        result: tuple[dict[int, float], float, tuple[int, ...], tuple[int, ...], float] | None,
        generation: int,
        effective_cues: bool,
    ) -> None:
        self._queue_stats_rebuild_pending = False
        if (
            result is None
            or generation != self._queue_stats_generation
            or effective_cues != self.queue_stats_use_effective_cues
        ):
            return
        self._apply_queue_stats_cache(result)

    def _apply_queue_stats_cache(
        self,
        result: tuple[dict[int, float], float, tuple[int, ...], tuple[int, ...], float],
    ) -> None:
        (
            self._queue_stats_durations,
            self._queue_stats_total_duration,
            self._queue_stats_remaining_ids,
            self._queue_stats_playing_ids,
            self._queue_stats_remaining_duration,
        ) = result
        self._queue_stats_dirty = False

    def _queue_statistics_signature(
        self, entries: list[QueueEntry], tracks: dict[int, Track]
    ) -> tuple[object, ...]:
        """Describe only values that can alter aggregate queue statistics."""
        remaining = {QueueStatus.WAITING, QueueStatus.LOADED, QueueStatus.PLAYING}
        return (
            self.queue_stats_use_effective_cues,
            tuple(
                (
                    entry.queue_id,
                    entry.track_id,
                    entry.status in remaining,
                    entry.status == QueueStatus.PLAYING,
                    entry.cue_in_override,
                    entry.cue_out_override,
                    entry.cue_override_source,
                    tracks[entry.track_id].duration_seconds if entry.track_id in tracks else None,
                )
                for entry in entries
            ),
        )

    def _queue_entry_duration(self, entry: QueueEntry, track: Track | None) -> float:
        if track is None:
            return 0.0
        physical_duration = float(track.duration_seconds or 0.0)
        if not self.queue_stats_use_effective_cues or self._cue_points is None:
            return physical_duration
        boundaries = self._cue_points.resolve(track, queue_entry=entry)
        return max(0.0, boundaries.cue_out - boundaries.cue_in)

    def _refresh_saved_queues(self) -> None:
        self._view.show_saved_queues(self._saved_queues.list() if self._saved_queues else [])

    def _refresh_all(self) -> None:
        with self._performance.measure("status_render.total", warning_threshold_ms=30.0):
            current = self._playback_status_view_model()
            previous = self._last_playback_status
            requested = 7
            changed = 0
            for deck, field, operation in (
                (self.deck_a, "deck_a", "status_render.deck_a"),
                (self.deck_b, "deck_b", "status_render.deck_b"),
            ):
                with self._performance.measure(operation, warning_threshold_ms=15.0):
                    if previous is None or getattr(previous, field) != getattr(current, field):
                        self._view.show_deck(deck.model)
                        changed += 1
            with self._performance.measure("status_render.crossfade", warning_threshold_ms=10.0):
                mixer = (current.crossfade_percent, current.master_percent)
                previous_mixer = (
                    (previous.crossfade_percent, previous.master_percent)
                    if previous is not None
                    else None
                )
                if not self._transition.is_transitioning and mixer != previous_mixer:
                    self._view.show_mixer(self.crossfader.position, self.crossfader.master_volume)
                    changed += 1
            for operation, differs in (
                (
                    "status_render.queue_summary",
                    previous is None or previous.queue_size != current.queue_size,
                ),
                (
                    "status_render.mode",
                    previous is None or previous.automatic_run != current.automatic_run,
                ),
                (
                    "status_render.transition",
                    previous is None or previous.transition_state != current.transition_state,
                ),
                ("status_render.buttons", False),
            ):
                with self._performance.measure(operation, warning_threshold_ms=5.0):
                    changed += int(differs)
            self._performance.record("status_render_requested_fields", requested, 100.0)
            self._performance.record("status_render_changed_fields", changed, 100.0)
            self._performance.record("status_render_skipped_fields", requested - changed, 100.0)
            self._last_playback_status = current

    def _playback_status_view_model(self) -> PlaybackStatusViewModel:
        def deck_status(deck: DeckController) -> DeckStatusViewModel:
            model = deck.model
            return DeckStatusViewModel(
                (
                    model.loaded_track.id if model.loaded_track is not None else None,
                    model.state,
                    round(model.volume, 3),
                    round(model.position, 1),
                    round(model.duration, 1),
                    model.error_message,
                    model.is_on_air,
                    round(model.cue_in, 2),
                    round(model.cue_out, 2),
                    round(model.cue_fade_duration, 2),
                    model.cue_in_source,
                    model.cue_out_source,
                    model.cue_warning,
                    round(model.loudness_requested_gain_db, 2),
                    round(model.loudness_effective_gain_db, 2),
                    model.loudness_source,
                    model.loudness_peak_limited,
                    model.equalizer_preset_name,
                    model.equalizer_source,
                    round(model.equalizer_preamp_db, 2),
                    model.equalizer_band_count,
                    model.equalizer_applied,
                    model.equalizer_error,
                )
            )

        return PlaybackStatusViewModel(
            deck_status(self.deck_a),
            deck_status(self.deck_b),
            round(self.crossfader.position * 100),
            round(self.crossfader.master_volume * 100),
            len(self._queue_entries_cache),
            self._automatic_run_active,
            self._transition.state.value,
        )

    def _refresh_crossfade_display(self) -> None:
        """Crossfade tick owns only the time-critical slider/percentage display."""
        self._view.show_crossfader(self.crossfader.position)

    def _mixer_changed(self) -> None:
        self.crossfader.apply()

    def _deck(self, deck_id: str) -> DeckController:
        if deck_id == "A":
            return self.deck_a
        if deck_id == "B":
            return self.deck_b
        raise ValueError("Unbekanntes Deck")

    def _manual_override(self, reason: str) -> None:
        self._preload_generation += 1
        self._preload_in_progress = False
        if not self._automatic_run_active and not self._transition.is_transitioning:
            return
        self._queue_service.record_audit_event(
            "MANUAL_OVERRIDE",
            details={"reason": reason},
        )
        self._automatic_run_active = False
        self._transition.abort(reason)
        if self.player_mode == PlayerMode.AUTOMATIC:
            self.player_mode = PlayerMode.SEMI_AUTOMATIC
            self.automatic_deck_loading = True
            if self._settings is not None:
                self._settings.set_player_mode(self.player_mode)
            self._view.show_player_mode(self.player_mode.value)
        self._logger.info("Automatik durch manuellen Eingriff beendet: %s", reason)
        self._view.show_automatic_playback(False)
        self._show_automatic_status("stopped", reason)

    def _pause_automatic_queue(self, reason: str, *, pause_audio: bool = False) -> None:
        """Pause automatic progression and optionally its currently playing audio."""
        if not self._automatic_run_active and not self._transition.is_transitioning:
            return
        self._preload_generation += 1
        self._preload_in_progress = False
        self._automatic_run_active = False
        self._automatic_run_paused = True
        self._automatic_pause_reason = reason
        self._transition.abort(reason)
        if pause_audio:
            self._automatic_audio_paused_decks.clear()
            for deck in (self.deck_a, self.deck_b):
                if deck.model.state != DeckState.PLAYING:
                    continue
                try:
                    deck.pause()
                except Exception as exc:
                    self._logger.exception(
                        "Queue-Pause auf Deck %s fehlgeschlagen", deck.model.deck_id
                    )
                    self._view.show_queue_warning(
                        f"Deck {deck.model.deck_id} konnte nicht pausiert werden: {exc}"
                    )
                else:
                    self._automatic_audio_paused_decks.add(deck.model.deck_id)
        self._queue_service.record_audit_event(
            "AUTOMATIC_PAUSED",
            details={"reason": reason},
        )
        self._logger.info("Automatik pausiert: %s", reason)
        self._view.show_automatic_playback(False)
        self._show_automatic_status("paused", reason)
        self._view.show_queue_warning(
            f"Automatik pausiert: {reason}. Mit ▶ kann sie fortgesetzt werden."
        )

    def _resume_automatic_queue_audio(self) -> None:
        """Resume only decks paused by the explicit queue-pause operation."""
        paused_decks = tuple(self._automatic_audio_paused_decks)
        self._automatic_audio_paused_decks.clear()
        for deck_id in paused_decks:
            deck = self._deck(deck_id)
            if deck.model.state != DeckState.PAUSED:
                continue
            try:
                deck.resume()
            except Exception as exc:
                self._logger.exception("Queue-Fortsetzung auf Deck %s fehlgeschlagen", deck_id)
                self._view.show_queue_warning(
                    f"Deck {deck_id} konnte nicht fortgesetzt werden: {exc}"
                )

    def _show_automatic_status(
        self,
        state: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Render an explanatory automatic state from the cached queue snapshot."""
        if state is not None:
            self._automatic_status_state = state
        if reason is not None:
            self._automatic_status_reason = reason
        elif state is not None:
            self._automatic_status_reason = ""

        pending_statuses = {
            QueueStatus.WAITING,
            QueueStatus.PREPARING,
            QueueStatus.READY,
            QueueStatus.PLAYING,
        }
        remaining = [
            entry for entry in self._queue_entries_cache if entry.status in pending_statuses
        ]
        next_entry = next(
            (
                entry
                for entry in remaining
                if entry.status in {QueueStatus.WAITING, QueueStatus.PREPARING, QueueStatus.READY}
            ),
            None,
        )
        details: list[str] = []
        operating_mode = self._one_deck_mode.snapshot()
        if operating_mode.mode == AudioOperatingMode.ONE_DECK:
            details.append(f"EIN-DECK-BETRIEB: Deck {operating_mode.active_deck_id}")
        if self._automatic_status_reason:
            details.append(self._automatic_status_reason)
        if next_entry is not None:
            track = self._queue_tracks_cache.get(next_entry.track_id)
            if track is not None:
                details.append(f"Nächster: {track.title}")
        elif remaining:
            details.append("Kein nächster Titel")
        if remaining:
            details.append(f"{len(remaining)} Titel")
        elif not any("Queue ist leer" in detail for detail in details):
            details.append("Queue leer")
        skipped = [
            entry for entry in self._queue_entries_cache if entry.status == QueueStatus.SKIPPED
        ]
        if skipped:
            repetition_skips = sum(
                entry.skip_code in {"TRACK_REPETITION", "ARTIST_REPETITION"} for entry in skipped
            )
            skip_detail = f"{len(skipped)} übersprungen"
            if repetition_skips:
                skip_detail += f" ({repetition_skips} Wiederholungsschutz)"
            details.append(skip_detail)
        rendered_state = (
            "transition"
            if self._automatic_status_state == "running" and self._transition.is_transitioning
            else self._automatic_status_state
        )
        rendered_detail = " · ".join(details)
        rendered = (rendered_state, rendered_detail)
        if rendered == self._last_automatic_status_render:
            return
        self._last_automatic_status_render = rendered
        self._view.show_automatic_status(rendered_state, rendered_detail)

    def _handle_error(self, title: str, error: Exception) -> None:
        self._logger.exception("%s: %s", title, error)
        self._view.show_error(title, self._safe_error_message(error))

    def _handle_queue_error(self, title: str, error: Exception) -> None:
        """Report an automatic queue failure without focus grab or playback pause."""
        self._logger.exception("%s: %s", title, error)
        self._view.show_queue_warning(f"{title}: {self._safe_error_message(error)}")

    @staticmethod
    def _safe_error_message(error: Exception) -> str:
        if isinstance(error, sqlite3.Error):
            return (
                "Die Änderung konnte nicht in der Datenbank gespeichert werden. "
                "Bitte erneut versuchen; technische Details stehen im Protokoll."
            )
        if isinstance(error, FileNotFoundError):
            return "Die ausgewählte Audiodatei oder das Laufwerk ist nicht verfügbar."
        if isinstance(error, OSError):
            return "Auf die benötigte Datei oder das Laufwerk konnte nicht zugegriffen werden."
        if isinstance(error, ValueError):
            return str(error)
        if isinstance(error, RuntimeError):
            return "Die Wiedergabeaktion konnte nicht ausgeführt werden."
        return "Ein unerwarteter Fehler ist aufgetreten. Details stehen im Protokoll."

    def _load_cover_async(
        self,
        deck_id: str,
        track: Track,
        operation_id: str | None = None,
    ) -> None:
        """Submit a complete cover lookup/prepare job to the bounded cover executor."""

        def worker() -> None:
            try:
                image_data = self._library_service.cover_data(Path(track.file_path))
                prepared_cover = prepare_cover_canvas(image_data)
            except Exception:
                self._logger.exception("Deck %s: Cover konnte nicht geladen werden", deck_id)
                prepared_cover = None

            def render() -> None:
                deck = self._deck(deck_id)
                if deck.model.loaded_track is not None and deck.model.loaded_track.id == track.id:
                    self._view.show_deck_cover(deck_id, prepared_cover)

            if not self._closed:
                self._publish_gui_callback(render, "cover")

        self._start_worker(
            worker,
            f"cover-deck-{deck_id}",
            "cover",
            operation_id,
            executor=self._cover_executor,
        )

    def _start_worker(
        self,
        target: Callable[[], None],
        name: str,
        category: str,
        operation_id: str | None = None,
        executor: Executor | None = None,
    ) -> bool:
        worker_id = str(uuid4())
        operation = operation_id or worker_id
        self._worker_registry.started(
            WorkerInfo(worker_id, name, category, monotonic(), True, operation)
        )

        def tracked_target() -> None:
            state = "completed"
            try:
                with self._performance.measure(
                    f"worker.{category}",
                    warning_threshold_ms=3000.0,
                    context={"operation_id": operation, "worker": name},
                ):
                    target()
            except Exception:
                state = "failed"
                self._logger.exception("Worker %s ist fehlgeschlagen", name)
            finally:
                self._worker_registry.finished(worker_id, state)

        if executor is not None:
            try:
                executor.submit(tracked_target)
            except RuntimeError:
                self._worker_registry.finished(worker_id, "discarded")
                self._logger.error("Worker %s konnte nicht eingereiht werden", name)
                return False
        else:
            Thread(target=tracked_target, name=name, daemon=True).start()
        return True

    def _publish_gui_callback(
        self,
        callback: Callable[[], None],
        source: str,
        *,
        coalesce_key: str | None = None,
    ) -> None:
        self._gui_dispatcher.publish(
            GuiEvent(
                GuiEventType.CALLBACK,
                source,
                callback,
                coalesce_key=coalesce_key,
            )
        )

    def _handle_gui_event(self, event: GuiEvent) -> None:
        callback_name = f"gui_event.{event.source}"
        self._callback_state.mark_started(callback_name)
        try:
            with self._performance.measure(
                f"gui_event.dispatch.{event.event_type.value}",
                warning_threshold_ms=25.0,
                context={"source": event.source},
            ):
                if event.event_type == GuiEventType.QUEUE_CHANGED and isinstance(
                    event.payload, QueueViewUpdate
                ):
                    self._apply_queue_view_update(event.payload)
                elif callable(event.payload):
                    event.payload()
        finally:
            self._callback_state.mark_completed(callback_name)

    def _deliver_queue_view_update(self, update: QueueViewUpdate) -> None:
        """Apply on the GUI thread or enqueue worker-originated queue changes."""
        if current_thread() is main_thread():
            self._apply_queue_view_update(update)
            return
        self._gui_dispatcher.publish(GuiEvent(GuiEventType.QUEUE_CHANGED, "queue_view", update))

    def _apply_queue_view_update(self, update: QueueViewUpdate) -> None:
        """Apply one immutable queue snapshot exclusively through the view contract."""
        self._view.show_queue_events(
            update.events,
            list(update.entries),
            update.tracks,
        )

    def _drain_background_callbacks(self) -> None:
        """Run a bounded worker-result budget exclusively on the Tk main thread."""
        self._gui_dispatcher.process_pending_events(self._handle_gui_event)

    def diagnostic_report(self, test_context: str = "idle") -> str:
        if not self._performance_settings.enabled:
            raise RuntimeError("Performance-Diagnostik ist im Produktionsbetrieb deaktiviert")
        if test_context not in self.DIAGNOSTIC_CONTEXTS:
            raise ValueError("Unbekannter Diagnosekontext")
        self._sample_memory()
        heartbeat = self._heartbeat.statistics()
        dispatcher = self._gui_dispatcher.statistics()
        threads = collect_thread_snapshot()
        workers = self._worker_registry.active()
        runtimes = self._worker_registry.runtimes()
        worker_history = self._worker_registry.history()
        now = datetime.now().astimezone()
        widget_diagnostics = self._view.widget_diagnostics()
        overlay_provider = getattr(self._view, "overlay_diagnostics", None)
        overlay_diagnostics: dict[str, object] = (
            overlay_provider() if callable(overlay_provider) else {}
        )
        performance_statistics = self._performance.statistics()
        data_operation_counters = self._performance.counters()
        missing_queue_instrumentation = tuple(
            operation
            for operation in self.QUEUE_INSTRUMENTATION_OPERATIONS
            if operation not in performance_statistics
        )
        queue_counter_names = (
            "created_widget_count",
            "destroyed_widget_count",
            "configured_widget_count",
            "rebound_row_count",
            "updated_row_count",
        )
        implausible_queue_counters = tuple(
            name for name in queue_counter_names if widget_diagnostics.get(name, 0) < 0
        )
        timing_validation = self._performance.validation_counters()
        scenario = self._diagnostic_scenario.snapshot()
        memory = self._memory_monitor.latest()
        memory_growth = self._memory_monitor.growth()
        memory_stress_cycles = self._memory_monitor.stress_cycles()
        lines = [
            f"{PRODUCT_NAME} diagnostic report",
            f"Version: {__version__}",
            f"Timestamp: {now.isoformat(timespec='seconds')}",
            f"Test context: {test_context}",
            "Backup/restore/maintenance:",
            *(f"  {item}" for item in self._database_diagnostic_status()),
            "Scenario:",
            f"  name: {scenario.name if scenario is not None else test_context}",
            "  started_at: "
            f"{scenario.started_at.isoformat(timespec='seconds') if scenario is not None else 'not_started'}",
            "  ended_at: "
            f"{scenario.ended_at.isoformat(timespec='seconds') if scenario is not None and scenario.ended_at is not None else 'not_ended'}",
            "  injected_database_delay_ms: "
            f"{scenario.injected_database_delay_ms if scenario is not None else 0}",
            "  statistics_reset_at_start: "
            f"{str(scenario.statistics_reset_at_start).lower() if scenario is not None else 'false'}",
            "  transitions_completed: "
            f"{scenario.transitions_completed if scenario is not None else 0}",
            "  persistence_jobs_submitted: "
            f"{scenario.persistence_jobs_submitted if scenario is not None else 0}",
            "  persistence_jobs_completed: "
            f"{scenario.persistence_jobs_completed if scenario is not None else 0}",
            "  persistence_jobs_failed: "
            f"{scenario.persistence_jobs_failed if scenario is not None else 0}",
            "  acceptance_data_present: "
            f"{str(scenario.is_meaningful_database_test).lower() if scenario is not None and test_context == 'database_delay' else 'not_applicable'}",
            f"Operating mode: {self.player_mode.value}",
            "Playback state:",
            *(
                f"  Deck {deck.model.deck_id}: state={deck.model.state.value}, "
                f"track={deck.model.loaded_track.title if deck.model.loaded_track else 'none'}, "
                f"on_air={deck.model.is_on_air}"
                for deck in (self.deck_a, self.deck_b)
            ),
            "Equalizer state:",
            *(
                f"  Deck {deck.model.deck_id}: preset={deck.model.equalizer_preset_name}, "
                f"source={deck.model.equalizer_source}, "
                f"preamp_db={deck.model.equalizer_preamp_db:.1f}, "
                f"bands={deck.model.equalizer_band_count}, "
                f"applied={str(deck.model.equalizer_applied).lower()}, "
                f"error={deck.model.equalizer_error or 'none'}"
                for deck in (self.deck_a, self.deck_b)
            ),
            f"  transition={self._transition.state.value}",
            f"  automatic_run={self._automatic_run_active}",
            "  emergency=not_implemented",
            "Overlay state:",
            *(
                f"  {name}: {value if value not in ('', None) else 'none'}"
                for name, value in overlay_diagnostics.items()
            ),
            f"Queue size: {len(self._queue_entries_cache)}",
            "Memory:",
            "  process_rss_bytes: "
            f"{memory.process_rss_bytes if memory is not None and memory.process_rss_bytes is not None else 'unavailable'}",
            f"  process_rss_status: {memory.process_rss_status if memory is not None else 'unavailable'}",
            f"  tracemalloc_enabled: {str(tracemalloc.is_tracing()).lower()}",
            "  python_traced_bytes: " f"{memory.python_traced_bytes if memory is not None else 0}",
            f"  python_peak_bytes: {memory.python_peak_bytes if memory is not None else 0}",
            "  active_thread_count: " f"{memory.active_thread_count if memory is not None else 0}",
            "  gui_event_queue_size: "
            f"{memory.gui_event_queue_size if memory is not None else 0}",
            "  active_worker_count: " f"{memory.active_worker_count if memory is not None else 0}",
            f"  cover_cache_size: {memory.cover_cache_size if memory is not None else 0}",
            "  registered_widget_count: "
            f"{memory.registered_widget_count if memory is not None else 0}",
            "  active_preview_count: "
            f"{memory.active_preview_count if memory is not None else 0}",
            "  active_vlc_player_count: "
            f"{memory.active_vlc_player_count if memory is not None else 0}",
            f"  retained_sample_count: {self._memory_monitor.sample_count()}",
            "Tracemalloc growth:",
            *(
                f"  {growth.filename}:{growth.line_number}: "
                f"object_count={growth.object_count}, total_size_bytes={growth.total_size_bytes}"
                for growth in memory_growth
            ),
            "Memory stress cycles:",
            *(
                f"  cycle_number={cycle.cycle_number}, queue_size={cycle.queue_size}, "
                f"queue_row_views={cycle.queue_row_views}, tk_widget_count={cycle.tk_widget_count}, "
                f"tooltip_instances_current={cycle.tooltip_instances_current}, "
                f"python_traced_bytes={cycle.python_traced_bytes}, "
                f"process_rss_bytes={cycle.process_rss_bytes if cycle.process_rss_bytes is not None else 'unavailable'}, "
                f"widgets_created_delta={cycle.widgets_created_delta}, "
                f"widgets_destroyed_delta={cycle.widgets_destroyed_delta}"
                for cycle in memory_stress_cycles
            ),
            "GUI heartbeat:",
            f"  current delay: {heartbeat.last_delay_ms:.1f} ms",
            f"  maximum delay: {heartbeat.maximum_delay_ms:.1f} ms",
            f"  average delay: {heartbeat.average_delay_ms:.1f} ms",
            f"  warnings: {heartbeat.warning_count}",
            f"  critical: {heartbeat.critical_count}",
            "GUI event dispatcher:",
            f"  current_queue_size: {dispatcher.pending}",
            f"  maximum_queue_size: {dispatcher.maximum_pending}",
            f"  published_count: {dispatcher.published}",
            f"  processed_count: {dispatcher.processed}",
            f"  coalesced_count: {dispatcher.coalesced}",
            f"  discarded_count: {dispatcher.discarded}",
            f"  critical_overflow_count: {dispatcher.critical_overflow}",
            "  maximum_items_processed_per_cycle: "
            f"{dispatcher.maximum_items_processed_per_cycle}",
            f"  maximum_dispatch_duration_ms: {dispatcher.maximum_dispatch_duration_ms:.1f}",
            f"  average_dispatch_duration_ms: {dispatcher.average_dispatch_duration_ms:.1f}",
            "Threads:",
            f"  active: {len(threads)}",
            f"  daemon: {sum(thread.daemon for thread in threads)}",
            *(
                f"  {thread.name}: identifier={thread.identifier}, daemon={thread.daemon}, "
                f"alive={thread.alive}"
                for thread in threads
            ),
            "Registered workers:",
            *(
                f"  {worker.name}: category={worker.category}, "
                f"operation_id={worker.operation_id}, running_duration={runtimes[worker.worker_id]:.1f}s, "
                f"daemon={worker.daemon}, state=running"
                for worker in workers
            ),
            "Recently completed workers:",
            *(
                f"  {worker.name}: category={worker.category}, "
                f"operation_id={worker.operation_id}, running_duration={worker.running_duration:.1f}s, "
                f"daemon={worker.daemon}, state={worker.state}"
                for worker in worker_history
            ),
            "Queue instrumentation:",
            "  complete: " f"{str(not missing_queue_instrumentation).lower()}",
            "  missing: "
            f"{', '.join(missing_queue_instrumentation) if missing_queue_instrumentation else 'none'}",
            "  counters_plausible: " f"{str(not implausible_queue_counters).lower()}",
            "  implausible_counters: "
            f"{', '.join(implausible_queue_counters) if implausible_queue_counters else 'none'}",
            "Timings:",
        ]
        diagnostic_counters = {
            "status_render_requested_fields",
            "status_render_changed_fields",
            "status_render_skipped_fields",
        }
        for name, stats in sorted(performance_statistics.items()):
            if name.endswith(("_count", "_total")) or name in diagnostic_counters:
                continue
            lines.append(
                f"  {name}: count={stats.count}, "
                f"average_duration_ms={stats.average_duration_ms:.1f}, "
                f"maximum_duration_ms={stats.maximum_duration_ms:.1f}, "
                f"minimum_value_ms={stats.minimum_value_ms:.1f}, "
                f"maximum_absolute_value_ms={stats.maximum_absolute_value_ms:.1f}, "
                f"slow_count={stats.slow_operation_count}"
            )
        lines.append("Crossfade level samples:")
        samples = self._transition.level_samples()
        level_diagnostic = self._transition.level_diagnostic()
        completion_delay = performance_statistics.get("crossfade.completion_detection_delay_ms")
        if not samples:
            crossfade_result = "NOT_MEASURED"
        elif not level_diagnostic.audio_ramp_complete:
            crossfade_result = "AUDIO_RAMP_PROBLEM"
        elif (
            completion_delay is not None
            and completion_delay.maximum_duration_ms
            > self._performance_settings.gui_heartbeat_warning_ms
        ):
            crossfade_result = "VISUAL_ONLY_DELAY_SUSPECTED"
        else:
            crossfade_result = "COMPLETE"
        lines.extend(
            (
                "Crossfade level diagnosis:",
                f"  result: {crossfade_result}",
                f"  sample_count: {level_diagnostic.sample_count}",
                f"  direction: {level_diagnostic.direction}",
                f"  duration_ms: {level_diagnostic.duration_ms:.1f}",
                f"  maximum_sample_gap_ms: {level_diagnostic.maximum_sample_gap_ms:.1f}",
                f"  position_monotonic: {str(level_diagnostic.position_monotonic).lower()}",
                f"  reached_target: {str(level_diagnostic.reached_target).lower()}",
                f"  audio_ramp_complete: {str(level_diagnostic.audio_ramp_complete).lower()}",
            )
        )
        if not samples:
            lines.append("  none")
        else:
            lines.append(
                "  elapsed_ms,position,normalization_a,normalization_b,"
                "backend_volume_a,backend_volume_b"
            )
            for sample in samples:
                lines.append(
                    f"  {sample.elapsed_ms:.1f},{sample.position:.5f},"
                    f"{sample.normalization_a:.5f},{sample.normalization_b:.5f},"
                    f"{sample.backend_volume_a:.5f},{sample.backend_volume_b:.5f}"
                )
        lines.append("Counters:")
        lines.extend(
            f"  {name}: {value}" for name, value in sorted(data_operation_counters.items())
        )
        lines.extend(
            f"  {name}: {int(stats.total_duration_ms)}"
            for name, stats in sorted(performance_statistics.items())
            if name.endswith(("_count", "_total")) or name in diagnostic_counters
        )
        lines.extend(f"  {name}: {value}" for name, value in timing_validation.items())
        lines.append("Invalid timing samples:")
        lines.extend(
            f"  {name}: value_ms={value:.1f}, measurement_status=invalid, "
            f"measurement_reason={reason}"
            for name, value, reason in self._performance.invalid_samples()
        )
        lines.extend(
            f"  {name}: {value}"
            for name, value in widget_diagnostics.items()
            if name.endswith("_total")
        )
        lines.append("Gauges:")
        lines.extend(
            f"  {name}: {value}"
            for name, value in widget_diagnostics.items()
            if not name.endswith("_total")
        )
        report = "\n".join(lines)
        self._logger.info("Performance-Diagnose:\n%s", report)
        return report

    def save_diagnostic_report(
        self, test_context: str = "idle", directory: Path | None = None
    ) -> Path:
        directory = directory or self._diagnostics_directory
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = directory / f"{PRODUCT_SLUG}-diagnostic-{timestamp}.txt"
        sequence = 1
        while target.exists():
            target = directory / f"{PRODUCT_SLUG}-diagnostic-{timestamp}-{sequence}.txt"
            sequence += 1
        target.write_text(self.diagnostic_report(test_context), encoding="utf-8")
        retain_latest(directory, f"{PRODUCT_SLUG}-diagnostic-*.txt", 500)
        return target
