"""Controller integration tests without Tkinter or real audio output."""

from pathlib import Path
from dataclasses import replace
from typing import Callable
from concurrent.futures import Executor, Future
import sqlite3
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from pytest import MonkeyPatch

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.audio_recovery import (
    AudioRecoveryPolicy,
    AudioRecoveryResult,
    DeckRestartAssessment,
    GlobalAudioRecoveryResult,
)
from party_player.controllers.main_controller import (
    MainController,
    EmergencyDashboardViewModel,
    QueueViewUpdate,
    RecoveryReturnRequirement,
)
from party_player.crossfader_service import CrossfaderService
from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.deck_controller import DeckController
from party_player.deck_health_monitor import DeckHealthMonitor
from party_player.emergency_state import DeckHealth, EmergencyStateService, EmergencySystemState
from party_player.emergency_persistence import EmergencyIncident
from party_player.emergency_actions import EmergencyActionProfile
from party_player.emergency_playback import EmergencyPlaybackResult
from party_player.source_availability_monitor import SourceAvailabilityMonitor
from party_player.equalizer_resolver import EqualizerResolver
from party_player.enums import DeckState, PlayerMode, QueueStatus, SessionStatus
from party_player.loudness import LoudnessRepository, LoudnessService
from party_player.models import Deck, QueueEntry, QueueStats, SavedQueue, Track
from party_player.one_deck_mode import AudioOperatingMode
from party_player.queue_service import QueueService
from party_player.queue_view_events import QueueViewEvent, QueueViewEventType
from party_player.playback_history_service import PlaybackHistoryService
from party_player.performance_monitor import PerformanceSettings
from party_player.repositories.saved_queue_repository import SavedQueueRepository
from party_player.repositories.equalizer_repository import (
    EqualizerAssignmentRepository,
    EqualizerPresetRepository,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.services.library_service import LibraryService
from party_player.saved_queue_service import SavedQueueService
from party_player.transition_controller import TransitionState
from party_player.track_selection import SelectionDecision


class GainObservingAudioBackend(FakeAudioBackend):
    """Capture deck normalization immediately before audio playback can start."""

    def __init__(self, duration: float = 120.0) -> None:
        super().__init__(duration)
        self.normalization_factor: Callable[[], float] = lambda: 1.0
        self.gain_at_play: list[float] = []

    def play(self) -> None:
        self.gain_at_play.append(self.normalization_factor())
        super().play()


class FakeView:
    def __init__(self) -> None:
        self.catalog: list[Track] = []
        self.queue: list[QueueEntry] = []
        self.queue_render_count = 0
        self.decks: dict[str, Deck] = {}
        self.scheduled: list[Callable[[], None]] = []
        self.errors: list[str] = []
        self.queue_warnings: list[str] = []
        self.queue_stats: QueueStats | None = None
        self.queue_origin = ""
        self.catalog_page = (1, 1)
        self.playlist: tuple[SavedQueue, list[Track]] | None = None
        self.confirm_queue_cues = True
        self.deck_render_count = 0
        self.mixer_render_count = 0
        self.crossfader_render_count = 0
        self.queue_events: list[QueueViewEvent] = []
        self.catalog_entry_updates: list[tuple[int, bool]] = []
        self.queue_entry_updates: list[int] = []
        self.restored_queue_ids: set[int] = set()
        self.queue_cue_warnings: dict[int, str] = {}
        self.audio_device_recovery: tuple[str, str] | None = None
        self.recovery_return_requirements: (
            tuple[tuple[RecoveryReturnRequirement, ...], bool] | None
        ) = None
        self.unresolved_emergency_incident: tuple[int, str] | None = None
        self.emergency_dashboard: EmergencyDashboardViewModel | None = None

    def show_catalog(self, tracks: list[Track], summary: str) -> None:
        self.catalog = tracks

    def show_track_cues_changed(self, track_id: int, has_manual_cues: bool) -> None:
        self.catalog_entry_updates.append((track_id, has_manual_cues))
        for entry in self.queue:
            if entry.track_id == track_id:
                self.show_queue_entry(entry, None)

    def show_catalog_paging(self, page: int, page_count: int) -> None:
        self.catalog_page = (page, page_count)

    def show_session(self, session: object) -> None:
        pass

    def show_start_settings(self, restore_session: bool, fullscreen: bool) -> None:
        pass

    def show_file_browser_setting(self, enabled: bool) -> None:
        pass

    def show_production_mode(self, enabled: bool) -> None:
        self.production_mode = enabled

    def show_diagnostic_saved(self, path: Path) -> None:
        self.diagnostic_path = path

    def show_diagnostic_state(self, state: str, context: str) -> None:
        self.diagnostic_state = (state, context)

    def widget_diagnostics(self) -> dict[str, int]:
        return {
            "catalog_row_views": 0,
            "queue_row_views": 0,
            "tooltip_instances_current": 0,
        }

    def memory_gauges(self) -> dict[str, int]:
        return {
            "cover_cache_size": 0,
            "registered_widget_count": 0,
            "active_preview_count": 0,
        }

    def show_audio_devices(self, devices: list[tuple[str, str]], selected_device: str) -> None:
        self.audio_devices = (devices, selected_device)

    def show_audio_device_recovery(self, state: str, message: str) -> None:
        self.audio_device_recovery = (state, message)

    def show_recovery_return_requirements(
        self, requirements: tuple[RecoveryReturnRequirement, ...], visible: bool
    ) -> None:
        self.recovery_return_requirements = (requirements, visible)

    def show_unresolved_emergency_incident(self, incident_id: int, summary: str) -> None:
        self.unresolved_emergency_incident = (incident_id, summary)

    def hide_unresolved_emergency_incident(self) -> None:
        self.unresolved_emergency_incident = None

    def show_emergency_dashboard(self, dashboard: EmergencyDashboardViewModel) -> None:
        self.emergency_dashboard = dashboard

    def show_queue(self, entries: list[QueueEntry], tracks: dict[int, Track]) -> None:
        self.queue = entries
        self.queue_render_count += 1

    def show_restored_queue_entries(self, queue_ids: set[int]) -> None:
        self.restored_queue_ids = set(queue_ids)

    def show_queue_cue_warnings(self, warnings: dict[int, str]) -> None:
        self.queue_cue_warnings = dict(warnings)

    def show_queue_entry(self, entry: QueueEntry, track: Track | None) -> None:
        self.queue_entry_updates.append(entry.queue_id)
        self.queue = [entry if item.queue_id == entry.queue_id else item for item in self.queue]

    def show_queue_events(
        self,
        events: tuple[QueueViewEvent, ...],
        entries: list[QueueEntry],
        tracks: dict[int, Track],
    ) -> None:
        self.queue_events.extend(events)
        structural = {
            QueueViewEventType.ENTRY_ADDED,
            QueueViewEventType.ENTRY_REMOVED,
            QueueViewEventType.ENTRY_MOVED,
            QueueViewEventType.RESET,
        }
        if any(event.event_type in structural for event in events):
            self.show_queue(entries, tracks)
            return
        entries_by_id = {entry.queue_id: entry for entry in entries}
        for event in events:
            entry = entries_by_id.get(event.queue_entry_id)
            if entry is not None:
                self.show_queue_entry(entry, tracks.get(entry.track_id))

    def show_queue_stats(self, stats: QueueStats) -> None:
        self.queue_stats = stats

    def show_queue_origin(self, text: str) -> None:
        self.queue_origin = text

    def show_deck(self, deck: Deck) -> None:
        self.decks[deck.deck_id] = deck
        self.deck_render_count += 1

    def show_deck_cover(self, deck_id: str, image_data: object | None) -> None:
        pass

    def show_mixer(self, crossfader: float, master: float) -> None:
        self.mixer_render_count += 1

    def show_crossfader(self, crossfader: float) -> None:
        self.crossfader_render = crossfader
        self.crossfader_render_count += 1

    def show_fade_settings(self, duration: float, stop_after: bool) -> None:
        pass

    def show_player_mode(self, mode: str) -> None:
        pass

    def show_queue_duplicate_policy(self, policy: str) -> None:
        pass

    def show_queue_duration_mode(self, use_effective_cues: bool) -> None:
        self.queue_duration_uses_cues = use_effective_cues

    def show_queue_artist_repetition(self, enabled: bool) -> None:
        self.queue_artist_repetition = enabled

    def show_directory_import_result(self, added: int, skipped: int, failed: int) -> None:
        pass

    def show_catalog_import_result(self, created: int, updated: int, failed: int) -> None:
        self.catalog_import_result = (created, updated, failed)

    def show_directory_import_progress(
        self, processed: int, total: int | None, active: bool
    ) -> None:
        pass

    def show_saved_queues(self, queues: list[SavedQueue]) -> None:
        pass

    def select_saved_queue(self, saved_queue_id: int) -> None:
        self.selected_saved_queue = saved_queue_id

    def show_saved_queue_load_result(self, added: int, skipped: int) -> None:
        pass

    def show_playlist(self, playlist: SavedQueue, tracks: list[Track]) -> None:
        self.playlist = (playlist, tracks)

    def show_queue_shuffle_result(self, shuffled: int) -> None:
        pass

    def show_automatic_playback(self, active: bool) -> None:
        pass

    def show_automatic_status(self, state: str, detail: str = "") -> None:
        self.automatic_status = (state, detail)

    def show_error(self, title: str, message: str) -> None:
        self.errors.append(message)

    def show_queue_warning(self, message: str) -> None:
        self.queue_warnings.append(message)

    def confirm_replace(self, deck_id: str) -> bool:
        return False

    def confirm_queue_cue_change(self, status: str) -> bool:
        return self.confirm_queue_cues

    def schedule(self, delay_ms: int, callback: object) -> object:
        assert callable(callback)
        self.scheduled.append(callback)
        return callback


class ManualExecutor(Executor):
    """Keep submitted persistence work pending until a test explicitly runs it."""

    def __init__(self) -> None:
        self.tasks: list[tuple[Callable[[], object], Future[object]]] = []

    def submit(
        self, fn: Callable[..., object], /, *args: object, **kwargs: object
    ) -> Future[object]:
        future: Future[object] = Future()
        self.tasks.append((lambda: fn(*args, **kwargs), future))
        return future

    def run_all(self) -> None:
        while self.tasks:
            task, future = self.tasks.pop(0)
            try:
                future.set_result(task())
            except Exception as exc:
                future.set_exception(exc)


def build_controller(
    tmp_path: Path,
    track_count: int = 1,
    *,
    background_preload: bool = False,
    persistence_executor: Executor | None = None,
    with_history: bool = False,
    performance_settings: PerformanceSettings | None = None,
    replaygain_db: float | None = None,
    diagnostics_directory: Path = Path("diagnostics"),
    default_equalizer_preset: str | None = None,
    track_genre: str = "",
) -> tuple[MainController, FakeView]:
    audio_file = tmp_path / "song.mp3"
    audio_file.touch()
    for index in range(2, track_count + 1):
        (tmp_path / f"song-{index}.mp3").touch()
    database = Database(tmp_path / "test.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks (file_path, title, artist, duration_seconds, genre)
               VALUES (?, ?, ?, ?, ?)""",
            (str(audio_file), "Song", "Artist", 120.0, track_genre),
        )
        connection.executemany(
            """INSERT INTO tracks (file_path, title, artist, duration_seconds)
               VALUES (?, ?, ?, ?)""",
            [
                (str(tmp_path / f"song-{index}.mp3"), f"Song {index:03d}", "Artist", 120.0)
                for index in range(2, track_count + 1)
            ],
        )
    tracks = TrackRepository(database)
    loudness_repository = LoudnessRepository(database)
    if replaygain_db is not None:
        loudness_repository.save_replaygain(1, replaygain_db, 0.5, None, None)
    party_repository = PartyPlayerRepository(database)
    session = party_repository.create_session("Test")
    view = FakeView()
    backend_a = GainObservingAudioBackend()
    backend_b = GainObservingAudioBackend()
    deck_a = DeckController("A", backend_a)
    deck_b = DeckController("B", backend_b)
    backend_a.normalization_factor = lambda: deck_a.normalization_factor
    backend_b.normalization_factor = lambda: deck_b.normalization_factor
    queue_service = QueueService(party_repository, tracks, session.session_id)
    controller = MainController(
        view,
        LibraryService(tracks, loudness_repository),
        queue_service,
        deck_a,
        deck_b,
        CrossfaderService(deck_a, deck_b),
        history_service=(
            PlaybackHistoryService(party_repository, session.session_id) if with_history else None
        ),
        saved_queue_service=SavedQueueService(SavedQueueRepository(database), queue_service),
        background_preload=background_preload,
        heartbeat_watchdog_enabled=False,
        cue_points=CuePointService(CuePointRepository(database), 7.0),
        loudness=LoudnessService(loudness_repository),
        persistence_executor=persistence_executor,
        performance_settings=performance_settings,
        diagnostics_directory=diagnostics_directory,
        default_equalizer_preset=default_equalizer_preset,
        equalizer_resolver=EqualizerResolver(
            EqualizerPresetRepository(database),
            EqualizerAssignmentRepository(database),
        ),
    )
    return controller, view


def pop_scheduled(view: FakeView, callback_name: str) -> Callable[[], None]:
    index = next(
        index
        for index, callback in enumerate(view.scheduled)
        if getattr(callback, "__name__", "") == callback_name
    )
    return view.scheduled.pop(index)


def test_stable_status_snapshot_skips_unchanged_deck_and_mixer_widgets(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller._last_playback_status = None
    view.deck_render_count = 0
    view.mixer_render_count = 0

    controller._refresh_all()
    first_counts = (view.deck_render_count, view.mixer_render_count)
    controller._refresh_all()

    assert first_counts == (2, 1)
    assert (view.deck_render_count, view.mixer_render_count) == first_counts


def test_initialize_marks_only_queue_entries_present_in_recovered_session(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path, track_count=2)
    controller.add_catalog_track_to_queue(1)
    existing = controller._queue_service.entries()[0]
    controller._session = SimpleNamespace(  # type: ignore[assignment]
        status=SessionStatus.RECOVERED,
        selected_playlist=None,
    )

    controller.initialize()

    assert view.restored_queue_ids == {existing.queue_id}
    controller.add_catalog_track_to_queue(2)
    added_later = controller._queue_service.entries()[-1]
    assert added_later.queue_id not in view.restored_queue_ids


def test_unresolved_emergency_incident_is_shown_during_initialization(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller._unresolved_emergency_incident = EmergencyIncident(
        17,
        4,
        "ACTIVE",
        "DEGRADED",
        "Audiogerät getrennt",
        "HEALTHY",
        "FAILED",
        "usb-dac",
        "AUDIO_OUTPUT_DEVICE_LOST",
        {"device_id": "usb-dac"},
        "2026-08-06 20:00:00",
        "2026-08-06 20:01:00",
        None,
    )

    controller.initialize()

    assert view.unresolved_emergency_incident is not None
    incident_id, summary = view.unresolved_emergency_incident
    assert incident_id == 17
    assert "DEGRADED" in summary
    assert "Audiogerät getrennt" in summary
    assert "Deck B: FAILED" in summary
    assert "usb-dac" in summary
    assert "Ungelöster Audiovorfall #17" in view.queue_warnings[-1]


def test_operator_can_persistently_close_startup_incident(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    incident = EmergencyIncident(
        18,
        4,
        "ACTIVE",
        "WARNING",
        "Kurze Audiowarnung",
        "HEALTHY",
        "HEALTHY",
        "",
        "EMERGENCY_STATE_CHANGED",
        {},
        "2026-08-06 20:00:00",
        "2026-08-06 20:01:00",
        None,
    )
    stored: list[tuple[int, dict[str, object]]] = []
    controller._unresolved_emergency_incident = incident
    controller._resolve_emergency_incident = lambda incident_id, details: (
        stored.append((incident_id, details)) or True
    )
    controller.initialize()

    assert controller.resolve_unresolved_emergency_incident()

    assert stored[0][0] == 18
    assert stored[0][1]["review"] == "OPERATOR_CONFIRMED"
    assert stored[0][1]["previous_system_state"] == "WARNING"
    assert view.unresolved_emergency_incident is None
    assert "als geprüft geschlossen" in view.queue_warnings[-1]
    assert not controller.resolve_unresolved_emergency_incident()


def test_mute_all_emergency_profile_immediately_engages_panic_mute(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller._automatic_run_active = True

    assert controller.start_emergency_action(EmergencyActionProfile.MUTE_ALL)

    assert controller.crossfader.panic_muted
    assert controller.is_automatic_queue_paused()
    assert "Gesamte Audioausgabe" in view.queue_warnings[-1]
    assert view.emergency_dashboard is not None
    assert view.emergency_dashboard.last_result == "MUTE_ALL · OK"
    assert view.emergency_dashboard.current_action == "Keine"


def test_safe_reset_stops_decks_and_keeps_panic_mute_active(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    controller.deck_action("A", "play")
    controller.deck_action("B", "play")
    controller._transition.state = TransitionState.CROSSFADE

    assert controller.start_emergency_action(EmergencyActionProfile.SAFE_RESET)

    assert controller.deck_a.model.state == DeckState.STOPPED
    assert controller.deck_b.model.state == DeckState.STOPPED
    assert controller.crossfader.panic_muted
    assert controller.crossfader.position == 0.5
    assert controller._transition.state == TransitionState.IDLE
    assert "Panic-Mute bleibt aktiv" in view.queue_warnings[-1]


def test_vlc_installation_change_gate_rejects_active_playback(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)

    assert controller.can_change_vlc_installation()

    controller.deck_action("A", "play")

    assert not controller.can_change_vlc_installation()


@pytest.mark.parametrize(
    "blocked_state",
    ["transition", "deck_recovery", "emergency_action", "global_recovery", "mute_review"],
)
def test_vlc_installation_change_gate_rejects_audio_safety_activity(
    tmp_path: Path, blocked_state: str
) -> None:
    controller, _view = build_controller(tmp_path)
    if blocked_state == "transition":
        controller._transition.state = TransitionState.CROSSFADE
    elif blocked_state == "deck_recovery":
        controller._deck_recovery_action_active = True
    elif blocked_state == "emergency_action":
        controller._emergency_action_active = True
    elif blocked_state == "global_recovery":
        controller._global_audio_recovery_requested = True
    else:
        controller._global_audio_recovery_ready_for_release = True

    assert not controller.can_change_vlc_installation()


def test_vlc_installation_change_gate_rejects_overlay_and_backend_recovery(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.bind_overlay_activity(lambda: True)

    assert not controller.can_change_vlc_installation()

    controller.bind_overlay_activity(lambda: False)
    controller._emergency = SimpleNamespace(recovery_active=lambda: True)  # type: ignore[assignment]

    assert not controller.can_change_vlc_installation()


def test_play_emergency_profile_runs_asynchronously_and_reports_result(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    release = Event()

    def prepare() -> EmergencyPlaybackResult:
        release.wait(timeout=1)
        return EmergencyPlaybackResult(True, "PREPARED", deck_id="B")

    controller._emergency = SimpleNamespace(
        prepare=prepare,
        activate=lambda: EmergencyPlaybackResult(True, "PLAYING", deck_id="B"),
    )

    assert controller.start_emergency_action(EmergencyActionProfile.PLAY_EMERGENCY)
    assert controller._emergency_action_active
    assert not controller.start_emergency_action(EmergencyActionProfile.PLAY_EMERGENCY)
    release.set()
    for _attempt in range(100):
        if view.scheduled:
            break
        sleep(0.01)
    assert view.scheduled

    view.scheduled.pop()()

    assert not controller._emergency_action_active
    assert "bestätigt gestartet" in view.queue_warnings[-1]
    assert view.emergency_dashboard is not None
    assert view.emergency_dashboard.last_result == "PLAYING · OK"


def test_typed_emergency_media_action_runs_asynchronously(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    requests: list[tuple[object, bool]] = []
    controller._emergency = SimpleNamespace(
        play_media=lambda media_type, loop=False: (
            requests.append((media_type, loop))
            or EmergencyPlaybackResult(True, "PLAYING", deck_id="A")
        )
    )

    assert controller.start_emergency_media_action("BREAK_MUSIC", loop=True)
    for _attempt in range(100):
        if view.scheduled:
            break
        sleep(0.01)
    assert view.scheduled
    view.scheduled.pop()()

    assert requests and requests[0][0].value == "BREAK_MUSIC"
    assert requests[0][1] is True
    assert "Pausenmusik wurde bestätigt gestartet" in view.queue_warnings[-1]
    assert view.emergency_dashboard is not None
    assert view.emergency_dashboard.last_result == "BREAK_MUSIC · PLAYING · OK"


def test_immediate_replace_action_pauses_automation_and_reports_result(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    requests: list[str] = []
    controller._emergency = SimpleNamespace(
        immediate_replace=lambda deck_id: (
            requests.append(deck_id) or EmergencyPlaybackResult(True, "PLAYING", deck_id="B")
        ),
        recovery_active=lambda: False,
    )

    assert controller.start_immediate_replace_action("A")
    assert controller.deck_a.emergency_muted
    assert controller._emergency_action_active
    assert not controller.start_immediate_replace_action("B")
    for _attempt in range(100):
        if view.scheduled:
            break
        sleep(0.01)
    assert view.scheduled
    view.scheduled.pop()()

    assert requests == ["A"]
    assert not controller._emergency_action_active
    assert "sofort stummgeschaltet" in view.queue_warnings[-1]
    assert view.emergency_dashboard is not None
    assert "IMMEDIATE_REPLACE A · PLAYING · OK" == view.emergency_dashboard.last_result


def test_emergency_dashboard_uses_cached_media_validation(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller._emergency = SimpleNamespace(
        playlist_validation=lambda: SimpleNamespace(
            ready=True,
            primary_track_id=91,
            validated_at="2026-08-06T20:15:00+02:00",
            issues=(),
        )
    )

    controller._publish_emergency_dashboard(force=True)

    assert view.emergency_dashboard is not None
    assert view.emergency_dashboard.system_state == "NORMAL"
    assert view.emergency_dashboard.deck_a_health == "HEALTHY"
    assert view.emergency_dashboard.deck_b_health == "HEALTHY"
    assert view.emergency_dashboard.audio_state == "normal"
    assert view.emergency_dashboard.media_ready
    assert "Primärtitel #91" in view.emergency_dashboard.media_summary


def test_emergency_dashboard_labels_local_network_and_empty_sources(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.deck_a.load(
        Track(70, r"\\nas\party\song.mp3", "NAS-Titel", "", "", 180),
        validate_file=False,
    )

    dashboard = controller.emergency_dashboard()

    assert dashboard.deck_a_source.startswith("NAS/NETZWERK")
    assert "NAS-Titel" in dashboard.deck_a_source
    assert dashboard.deck_b_source == "LEER · keine Quelldatei geladen"


def test_source_reachability_is_checked_in_worker_and_cached_in_dashboard(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    audio = tmp_path / "reachable.mp3"
    audio.write_bytes(b"audio")
    controller.deck_a.load(Track(71, str(audio), "Erreichbar", "", "", 180), validate_file=False)
    controller._source_availability_monitor = SourceAvailabilityMonitor()

    controller._schedule_source_availability_checks()

    assert view.emergency_dashboard is not None
    assert "PRÜFUNG LÄUFT" in view.emergency_dashboard.deck_a_source
    for _attempt in range(100):
        controller._drain_background_callbacks()
        if view.emergency_dashboard and "ERREICHBAR" in view.emergency_dashboard.deck_a_source:
            break
        sleep(0.01)

    assert view.emergency_dashboard is not None
    assert "LOKAL · ERREICHBAR" in view.emergency_dashboard.deck_a_source
    assert "geprüft" in view.emergency_dashboard.deck_a_source


def test_confirmed_single_deck_recovery_runs_off_thread_and_updates_dashboard(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    release = Event()

    def recover(_deck_id: str, _policy: object) -> AudioRecoveryResult:
        release.wait(timeout=1)
        return AudioRecoveryResult(
            True,
            "RECOVERED",
            "B",
            attempt=1,
            attempts_remaining=3,
        )

    controller._emergency = SimpleNamespace(
        can_restart_deck_independently=lambda _deck_id: DeckRestartAssessment(True),
        recover_deck=recover,
        recovery_active=lambda: False,
    )

    assert controller.start_deck_recovery_action("B")
    assert controller._deck_recovery_action_active
    assert not controller.start_deck_recovery_action("A")
    assert view.emergency_dashboard is not None
    assert view.emergency_dashboard.current_action == "Deck-Recovery B"
    release.set()
    for _attempt in range(100):
        if view.scheduled:
            break
        sleep(0.01)
    assert view.scheduled

    view.scheduled.pop()()

    assert not controller._deck_recovery_action_active
    assert view.emergency_dashboard is not None
    assert "DECK B · RECOVERED · OK · Versuch 1" == view.emergency_dashboard.last_result
    assert "bleibt sicher stumm" in view.queue_warnings[-1]


def test_transition_render_callback_only_updates_crossfader_display(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    view.deck_render_count = 0
    view.mixer_render_count = 0
    view.crossfader_render_count = 0

    controller._refresh_crossfade_display()

    assert view.crossfader_render_count == 1
    assert view.deck_render_count == 0
    assert view.mixer_render_count == 0


def test_failed_transition_restores_outgoing_and_isolates_incoming_deck(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    queue_a = controller._deck_queue_ids["A"]
    queue_b = controller._deck_queue_ids["B"]
    assert queue_a is not None and queue_b is not None
    controller.deck_a.play()
    controller.deck_b.play()
    controller._queue_service.mark_playing(queue_a)
    controller._queue_service.mark_playing(queue_b)
    controller._automatic_run_active = True
    controller._transition.state = TransitionState.FAILED
    controller.crossfader.set_position(1.0)

    controller._handle_transition_failure(
        "INCOMING_PLAYBACK_LOST", controller.deck_a, controller.deck_b
    )

    assert controller.crossfader.position == 0.0
    assert not controller.deck_a.emergency_muted
    assert controller.deck_b.emergency_muted
    assert controller._queue_service.entry(queue_b).status == QueueStatus.READY  # type: ignore[union-attr]
    assert controller.audio_operating_mode().active_deck_id == "A"
    assert controller.emergency_snapshot().deck_b == DeckHealth.FAILED
    assert controller.is_automatic_queue_paused()
    assert "Deck A bleibt hörbar" in view.queue_warnings[-1]
    assert "Deck B reparieren" in view.queue_warnings[-1]
    assert view.recovery_return_requirements[1]


def test_unconfirmed_incoming_track_is_skipped_and_deck_is_preloaded_again(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path, track_count=3, with_history=True)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    controller.add_catalog_track_to_queue(3)
    queue_a = controller._deck_queue_ids["A"]
    queue_b = controller._deck_queue_ids["B"]
    assert queue_a is not None and queue_b is not None
    controller.deck_a.play()
    controller.deck_b.play()
    controller._queue_service.mark_playing(queue_a)
    controller._queue_service.mark_playing(queue_b)
    controller._automatic_run_active = True
    controller._transition.state = TransitionState.FAILED

    controller._handle_transition_failure(
        "INCOMING_PLAYBACK_NOT_CONFIRMED", controller.deck_a, controller.deck_b
    )

    failed_entry = controller._queue_service.entry(queue_b)
    assert failed_entry is not None
    assert failed_entry.status == QueueStatus.SKIPPED
    assert failed_entry.skip_code == "INCOMING_PLAYBACK_NOT_CONFIRMED"
    assert controller.deck_b.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track.id == 3
    assert controller._deck_queue_ids["B"] is not None
    assert controller.audio_operating_mode().mode == AudioOperatingMode.TWO_DECK
    assert controller.emergency_snapshot().deck_b == DeckHealth.HEALTHY
    assert controller._transition.state == TransitionState.IDLE
    assert controller._automatic_run_active
    assert not controller.is_automatic_queue_paused()
    assert "nächsten Preload bereit" in view.queue_warnings[-1]


@pytest.mark.parametrize(("outgoing_id", "incoming_id"), (("A", "B"), ("B", "A")))
def test_unconfirmed_incoming_failure_is_symmetric_and_history_is_finished_once(
    tmp_path: Path,
    outgoing_id: str,
    incoming_id: str,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=3, with_history=True)
    controller.initialize()
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    decks = {"A": controller.deck_a, "B": controller.deck_b}
    outgoing = decks[outgoing_id]
    incoming = decks[incoming_id]
    outgoing_queue_id = controller._deck_queue_ids[outgoing_id]
    incoming_queue_id = controller._deck_queue_ids[incoming_id]
    assert outgoing_queue_id is not None and incoming_queue_id is not None
    assert incoming.model.loaded_track is not None
    outgoing.play()
    incoming.play()
    controller._queue_service.mark_playing(outgoing_queue_id)
    controller._queue_service.mark_playing(incoming_queue_id)
    assert controller._history is not None
    controller._history.start(
        incoming_id,
        incoming.model.loaded_track,
        incoming_queue_id,
    )
    controller._automatic_run_active = True
    controller._transition.state = TransitionState.FAILED

    controller._handle_transition_failure(
        "INCOMING_PLAYBACK_NOT_CONFIRMED",
        outgoing,
        incoming,
    )
    # A repeated failure callback must be harmless and must not duplicate history.
    controller._handle_transition_failure(
        "INCOMING_PLAYBACK_NOT_CONFIRMED",
        outgoing,
        incoming,
    )

    failed = controller._queue_service.entry(incoming_queue_id)
    assert failed is not None
    assert failed.status == QueueStatus.SKIPPED
    assert failed.skip_code == "INCOMING_PLAYBACK_NOT_CONFIRMED"
    assert controller._deck_queue_ids[incoming_id] is not None
    assert incoming.model.loaded_track is not None
    assert incoming.model.loaded_track.id == 3
    assert incoming_id not in controller._auto_load_suppressed_decks
    assert outgoing_id not in controller._auto_load_suppressed_decks
    assert controller.audio_operating_mode().mode == AudioOperatingMode.TWO_DECK
    assert controller._automatic_run_active
    assert not controller.is_automatic_queue_paused()
    with controller._queue_service._repository._database.connect() as connection:
        history = connection.execute(
            """SELECT completion_status, error_message, skip_code
               FROM play_history WHERE queue_id = ? ORDER BY id""",
            (incoming_queue_id,),
        ).fetchall()
    assert len(history) == 1
    assert history[0]["completion_status"] == "FAILED"
    assert history[0]["error_message"] == "INCOMING_PLAYBACK_NOT_CONFIRMED"
    assert history[0]["skip_code"] == "PLAYBACK_ERROR"


def test_manual_load_after_unconfirmed_start_allows_automatic_reactivation(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=4)
    controller.initialize()
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    queue_a = controller._deck_queue_ids["A"]
    queue_b = controller._deck_queue_ids["B"]
    assert queue_a is not None and queue_b is not None
    controller.deck_b.play()
    controller.deck_a.play()
    controller._queue_service.mark_playing(queue_b)
    controller._queue_service.mark_playing(queue_a)
    controller._automatic_run_active = True
    controller._transition.state = TransitionState.FAILED

    controller._handle_transition_failure(
        "INCOMING_PLAYBACK_NOT_CONFIRMED", controller.deck_b, controller.deck_a
    )
    controller._preload_in_progress = True
    stale_generation = controller._preload_generation

    controller.load_catalog_track(4, "A")

    assert not controller._automatic_run_active
    assert controller._transition.state == TransitionState.IDLE
    assert not controller._preload_in_progress
    assert controller._preload_generation > stale_generation
    controller._transition.state = TransitionState.FAILED

    controller.start_automatic_queue()

    assert controller.player_mode == PlayerMode.AUTOMATIC
    assert controller._automatic_run_active
    assert not controller.is_automatic_queue_paused()
    assert controller._transition.state == TransitionState.IDLE
    assert not controller._preload_in_progress
    assert not controller._auto_load_suppressed_decks


def test_manual_load_without_queue_entry_can_restart_automatic_playback(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=1)
    controller.initialize()
    controller.set_player_mode("manual")
    controller.load_catalog_track(1, "B")

    assert controller._queue_service.entries() == []
    assert "Starttitel: Song" in controller.automatic_start_summary()
    assert "Voraussichtlich spielbar: 1" in controller.automatic_start_summary()

    controller.start_automatic_queue()

    assert controller.player_mode == PlayerMode.AUTOMATIC
    assert controller._automatic_run_active
    assert controller.deck_b.model.state == DeckState.PLAYING


def test_consecutive_unconfirmed_one_deck_tracks_do_not_loop_or_use_failed_deck(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=3, with_history=True)
    controller.initialize()
    controller.enter_one_deck_mode("A", "Deck B ausgefallen")
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    controller._automatic_run_active = True
    controller.ONE_DECK_START_WAIT_STEPS = 0

    controller._automatic_playback_tick()
    controller._automatic_playback_tick()

    entries = controller._queue_service.entries()
    skipped = [entry for entry in entries if entry.status == QueueStatus.SKIPPED]
    assert len(skipped) == 2
    assert all(entry.skip_code == "ONE_DECK_PLAYBACK_NOT_CONFIRMED" for entry in skipped)
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 3
    assert controller.deck_b.model.loaded_track is None
    assert "B" in controller._auto_load_suppressed_decks
    assert controller._automatic_run_active
    assert controller._one_deck_start_pending is None


def test_one_deck_mode_suppresses_failed_deck_preload_and_restores_it(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=2)
    controller.initialize()

    snapshot = controller.enter_one_deck_mode("A", "Deck B ausgefallen")
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)

    assert snapshot.active_deck_id == "A"
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track is None
    assert controller.deck_b.emergency_muted

    restored = controller.return_to_two_deck_mode()

    assert restored.active_deck_id is None
    assert not controller.deck_b.emergency_muted
    assert controller.deck_b.model.loaded_track is not None


def test_one_deck_automatic_playback_uses_short_fade_in_and_arms_fade_out(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.enter_one_deck_mode("A", "Deck B ausgefallen")
    controller.add_catalog_track_to_queue(1)
    controller._automatic_run_active = True
    view.scheduled.clear()

    controller._automatic_playback_tick()

    assert controller.deck_a.model.state == DeckState.PLAYING
    assert not controller.deck_a.is_fading
    assert controller.deck_a.fade_level == 0.0
    assert not controller._transition.is_transitioning
    assert controller._one_deck_start_pending == "A"

    controller.deck_a.backend.position = 0.2
    view.scheduled.pop(0)()

    assert controller.deck_a.is_fading
    assert controller._one_deck_start_pending is None

    controller.deck_a.cancel_fade()
    controller.deck_a.set_fade_level_immediately(1.0)
    controller.deck_a.model.position = 119.5
    view.scheduled.clear()
    controller._automatic_playback_tick()

    assert controller._one_deck_fade_pending == {"A"}
    assert controller.deck_a.is_fading
    assert not controller._transition.is_transitioning


def test_one_deck_unconfirmed_playback_skips_track_and_loads_next(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path, track_count=2, with_history=True)
    controller.initialize()
    controller.enter_one_deck_mode("A", "Deck B ausgefallen")
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    first_queue_id = controller._deck_queue_ids["A"]
    assert first_queue_id is not None
    controller._automatic_run_active = True
    controller.ONE_DECK_START_WAIT_STEPS = 0

    controller._automatic_playback_tick()

    failed = controller._queue_service.entry(first_queue_id)
    assert failed is not None
    assert failed.status == QueueStatus.SKIPPED
    assert failed.skip_code == "ONE_DECK_PLAYBACK_NOT_CONFIRMED"
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 2
    assert controller.deck_a.model.state == DeckState.LOADED
    assert controller._deck_queue_ids["A"] != first_queue_id
    assert controller._one_deck_start_pending is None
    assert controller._automatic_run_active
    assert "nächste Titel" in view.queue_warnings[-1]


def test_one_deck_does_not_start_while_preload_commit_is_pending(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.enter_one_deck_mode("A", "Deck B ausgefallen")
    controller.add_catalog_track_to_queue(1)
    controller._automatic_run_active = True
    controller._preload_in_progress = True

    controller._automatic_playback_tick()

    assert controller.deck_a.model.state == DeckState.LOADED
    assert not controller.deck_a.backend.is_playing()


def test_two_deck_return_is_blocked_while_controller_reports_recovery(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.enter_one_deck_mode("A", "Deck B ausgefallen")
    controller._emergency = SimpleNamespace(recovery_active=lambda: True)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="Recovery läuft"):
        controller.return_to_two_deck_mode()


def test_playlist_details_keep_order_and_tracks_can_be_used(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(2)
    controller.add_catalog_track_to_queue(1)
    controller.save_current_queue("Tanzabend")
    playlist_id = controller._saved_queues.list()[0].saved_queue_id  # type: ignore[union-attr]

    controller.show_saved_queue(playlist_id)

    assert view.playlist is not None
    assert [track.id for track in view.playlist[1]] == [2, 1]
    controller.load_playlist_track(2, "B")
    assert controller.deck_b.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track.id == 2
    controller.add_playlist_track_to_queue(3)
    assert [entry.track_id for entry in controller._queue_service.entries()] == [2, 1, 3]


def test_saved_queue_play_all_overrides_repetition_for_loaded_entries(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    controller.save_current_queue("Album")
    saved_queue_id = controller._saved_queues.list()[0].saved_queue_id  # type: ignore[union-attr]
    controller.clear_waiting_queue()

    class RepetitionOverrideSpy:
        def __init__(self) -> None:
            self.queue_ids: list[int] = []

        def allow_queue_entry(self, queue_id: int) -> None:
            self.queue_ids.append(queue_id)

    repetition = RepetitionOverrideSpy()
    controller._repetition = repetition  # type: ignore[assignment]

    controller.load_saved_queue(
        saved_queue_id,
        replace_waiting=False,
        play_all_in_order=True,
    )

    loaded_ids = [entry.queue_id for entry in controller._queue_service.entries()]
    assert repetition.queue_ids == loaded_ids
    assert len(loaded_ids) == 2
    assert controller._queue_service._repository.repetition_override_queue_ids(  # noqa: SLF001
        controller._queue_service.session_id
    ) == set(loaded_ids)


def test_replace_and_play_all_plays_saved_queue_once_in_saved_order(tmp_path: Path) -> None:
    executor = ManualExecutor()
    controller, view = build_controller(
        tmp_path,
        track_count=4,
        persistence_executor=executor,
        with_history=True,
    )
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    saved_order = [3, 1, 4, 2]
    for track_id in saved_order:
        controller.add_catalog_track_to_queue(track_id)
    controller.save_current_queue("Vollständiges Album")
    saved_queue_id = controller._saved_queues.list()[0].saved_queue_id  # type: ignore[union-attr]
    controller.clear_waiting_queue()
    controller.add_catalog_track_to_queue(2)

    class RepetitionOverrideSpy:
        def allow_queue_entry(self, _queue_id: int) -> None:
            pass

    controller._repetition = RepetitionOverrideSpy()  # type: ignore[assignment]
    controller.load_saved_queue(
        saved_queue_id,
        replace_waiting=True,
        play_all_in_order=True,
    )
    assert [entry.track_id for entry in controller._queue_service.entries()] == saved_order

    controller.start_automatic_queue()
    played_track_ids: list[int] = []
    for expected_track_id in saved_order:
        playing = [
            deck
            for deck in (controller.deck_a, controller.deck_b)
            if deck.model.state == DeckState.PLAYING
        ]
        assert len(playing) == 1
        deck = playing[0]
        assert deck.model.loaded_track is not None
        played_track_ids.append(deck.model.loaded_track.id)
        assert deck.model.loaded_track.id == expected_track_id
        backend = deck.backend
        assert isinstance(backend, FakeAudioBackend)
        backend.playing = False
        backend.finished = True
        backend.position = backend.duration
        deck.update_status()
        controller._schedule_finished_deck_completion(deck)
        controller._automatic_playback_tick()
        pop_scheduled(view, f"finish_deck_{deck.model.deck_id.lower()}")()
        executor.run_all()
        if expected_track_id != saved_order[-1]:
            pop_scheduled(view, "_auto_load")()

    assert played_track_ids == saved_order
    assert view.automatic_status[0] == "completed"
    assert all(entry.status == QueueStatus.PLAYED for entry in controller._queue_service.entries())


def test_operator_can_return_repetition_skip_to_waiting_queue(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    controller._queue_service.mark_skipped(
        entry.queue_id,
        "Titel wurde vor Kurzem gespielt",
        code="TRACK_REPETITION",
    )

    class RepetitionOverrideSpy:
        def __init__(self) -> None:
            self.queue_ids: list[int] = []

        def allow_queue_entry(self, queue_id: int) -> None:
            self.queue_ids.append(queue_id)

    repetition = RepetitionOverrideSpy()
    controller._repetition = repetition  # type: ignore[assignment]

    controller.play_repetition_skipped_queue_track(entry.queue_id)

    restored = controller._queue_service.entry(entry.queue_id)
    assert restored is not None
    assert restored.status == QueueStatus.WAITING
    assert restored.skip_code is None
    assert restored.skip_reason is None
    assert repetition.queue_ids == [entry.queue_id]
    assert controller._queue_service._repository.repetition_override_queue_ids(  # noqa: SLF001
        controller._queue_service.session_id
    ) == {entry.queue_id}


def test_replacing_saved_queue_discards_old_inactive_prepared_deck(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(2)
    controller.add_catalog_track_to_queue(3)
    controller.save_current_queue("Neue CD")
    saved_queue_id = controller._saved_queues.list()[0].saved_queue_id  # type: ignore[union-attr]
    controller.clear_complete_queue()

    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    playing_entry, prepared_entry = controller._queue_service.entries()
    controller.load_queue_track(playing_entry.queue_id, "A")
    controller.deck_action("A", "play")
    controller.load_queue_track(prepared_entry.queue_id, "B")
    assert controller.deck_b.model.loaded_track is not None

    controller.load_saved_queue(saved_queue_id, replace_waiting=True)

    assert controller.deck_a.model.state == DeckState.PLAYING
    assert controller.deck_b.model.loaded_track is None
    assert controller._deck_queue_ids["B"] is None
    entries = controller._queue_service.entries()
    assert [entry.track_id for entry in entries] == [1, 2, 3]
    assert entries[0].status == QueueStatus.PLAYING
    assert all(entry.queue_id != prepared_entry.queue_id for entry in entries)


def test_background_preload_finishes_on_controller_callback_queue(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, background_preload=True)
    controller.initialize()

    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    assert controller._automatic_run_active
    for _ in range(100):
        controller._drain_background_callbacks()
        if controller.deck_a.model.state.value == "playing":
            break
        sleep(0.01)

    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.state.value == "playing"
    assert controller._automatic_run_active
    timings = controller._performance.statistics()
    assert "worker.preload.resolve_loudness" in timings
    assert "audio.apply_gain_command" in timings
    assert "gui_event.preload.total" in timings
    assert "gui_event.preload.apply_result" in timings
    assert "gui_event.preload.update_deck_view" in timings
    assert "gui_event.preload.apply_loudness" in timings
    assert "gui_event.preload.schedule_cover" in timings
    assert "gui_event.preload.update_queue_view" in timings
    assert "gui_event.preload.update_catalog_view" in timings
    assert "gui_event.preload.schedule_followup" in timings


def test_background_automatic_start_fills_both_decks_when_two_tracks_are_playable(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(
        tmp_path,
        track_count=2,
        background_preload=True,
    )
    controller.initialize()
    controller.set_player_mode("manual")
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)

    controller.start_automatic_queue()
    for _ in range(400):
        controller._drain_background_callbacks()
        if (
            controller.deck_a.model.loaded_track is not None
            and controller.deck_b.model.loaded_track is not None
        ):
            break
        sleep(0.01)

    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track is not None
    assert {controller.deck_a.model.loaded_track.id, controller.deck_b.model.loaded_track.id} == {
        1,
        2,
    }
    assert (
        sum(
            deck.model.state == DeckState.PLAYING for deck in (controller.deck_a, controller.deck_b)
        )
        == 1
    )


def test_background_candidate_search_does_not_block_gui_thread(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, _view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entered = Event()
    release = Event()
    original = controller._queue_service.next_load_candidate

    def slow_search(*args: object, **kwargs: object) -> object:
        entered.set()
        release.wait(2.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(controller._queue_service, "next_load_candidate", slow_search)

    started = monotonic()
    controller._auto_load_in_background()
    elapsed = monotonic() - started

    assert elapsed < 0.1
    assert entered.wait(1.0)
    assert controller._preload_in_progress
    release.set()


def test_background_candidate_search_respects_empty_result_cooldown(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, _view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller._queue_service.add(1)
    controller._refresh_queue()
    controller._next_preload_candidate_search_at = monotonic() + 60.0

    def unexpected_search(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("candidate search ignored its cooldown")

    monkeypatch.setattr(controller._queue_service, "next_load_candidate", unexpected_search)

    controller._auto_load_in_background()

    assert not controller._preload_in_progress


def test_recovery_replacement_search_pauses_without_creating_automatic_candidates(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller._queue_service.add(1)
    controller._refresh_queue()
    controller.automatic_deck_loading = True
    controller._automatic_run_active = True
    calls: list[bool] = []

    def no_replacement(
        *_args: object,
        allow_empty_queue_selection: bool = True,
        **_kwargs: object,
    ) -> None:
        calls.append(allow_empty_queue_selection)
        return None

    monkeypatch.setattr(controller._queue_service, "next_load_candidate", no_replacement)

    controller._auto_load_in_background(recovery_replacement=True)
    deadline = monotonic() + 2.0
    while controller._preload_in_progress and monotonic() < deadline:
        controller._drain_background_callbacks()
        sleep(0.01)

    assert calls == [False]
    assert controller.is_automatic_queue_paused()
    assert not controller._automatic_run_active
    assert "Kein sicherer Ersatztitel" in view.queue_warnings[-1]


def test_recovery_replacement_pauses_when_no_waiting_entry_remains(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller._queue_entries_cache = []
    controller._automatic_run_active = True

    controller._auto_load_in_background(recovery_replacement=True)

    assert controller.is_automatic_queue_paused()
    assert not controller._automatic_run_active
    assert "Kein sicherer Ersatztitel" in view.queue_warnings[-1]


def test_large_playing_queue_does_not_search_when_inactive_deck_is_ready(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, _view = build_controller(tmp_path, track_count=2, background_preload=True)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    track_a = controller._library_service.get_track(1)
    track_b = controller._library_service.get_track(2)
    assert track_a is not None and track_b is not None
    controller.deck_a.load(track_a, validate_file=False)
    controller.deck_a.model.state = DeckState.PLAYING
    controller.deck_b.load(track_b, validate_file=False)
    controller._deck_queue_ids = {"A": 1, "B": 2}
    controller._queue_entries_cache = [
        QueueEntry(1, 1, 0, QueueStatus.PLAYING, loaded_deck="A"),
        QueueEntry(2, 2, 1, QueueStatus.READY, loaded_deck="B"),
        *(QueueEntry(queue_id, 1, queue_id - 1, QueueStatus.WAITING) for queue_id in range(3, 399)),
    ]
    searches = 0

    def counted_search(*_args: object, **_kwargs: object) -> object:
        nonlocal searches
        searches += 1
        return None

    monkeypatch.setattr(controller._queue_service, "next_load_candidate", counted_search)

    for _ in range(71):
        controller._auto_load_in_background()

    assert searches == 0
    assert not controller._preload_in_progress


def test_candidate_search_misses_use_exponential_backoff(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller.set_automatic_deck_loading(False)

    assert [controller._candidate_search_backoff_seconds(miss) for miss in range(1, 7)] == [
        2,
        4,
        8,
        16,
        30,
        30,
    ]


def test_irrelevant_queue_metadata_does_not_dirty_statistics(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    controller._refresh_queue_stats()
    assert not controller._queue_stats_dirty
    entry = controller._queue_entries_cache[0]
    controller._queue_entries_cache = [replace(entry, request_count=entry.request_count + 1)]
    signature = controller._queue_statistics_signature(
        controller._queue_entries_cache, controller._queue_tracks_cache
    )

    assert signature == controller._queue_stats_signature


def test_background_preload_reconciles_stale_empty_deck_assignment(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=2, background_preload=True)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    stale = controller._queue_service.add(1)
    waiting = controller._queue_service.add(2)
    controller._queue_service.mark_preparing(stale.queue_id, "A")
    controller._queue_service.mark_loaded(stale.queue_id, "A")
    controller._refresh_queue()

    controller._auto_load_in_background()
    for _ in range(200):
        controller._drain_background_callbacks()
        if controller.deck_a.model.loaded_track is not None:
            break
        sleep(0.01)

    entries = controller._queue_service.entries()
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 1
    assert not any(entry.status == QueueStatus.FAILED for entry in entries)
    assert controller._queue_service.entry(waiting.queue_id).status == QueueStatus.WAITING  # type: ignore[union-attr]


def test_played_queue_track_cannot_be_loaded_into_deck(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    entry = controller._queue_service.add(1)
    controller._queue_service.mark_played(entry.queue_id)

    controller.load_queue_track(entry.queue_id, "A")

    assert controller.deck_a.model.loaded_track is None
    assert controller._queue_service.entry(entry.queue_id).status == QueueStatus.PLAYED  # type: ignore[union-attr]
    assert "zuerst wieder auf wartend setzen" in view.queue_warnings[-1]


def test_played_queue_track_can_be_reset_and_loaded_into_deck(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    entry = controller._queue_service.add(1)
    controller._queue_service.mark_played(entry.queue_id)

    controller.reset_played_queue_track(entry.queue_id)
    controller.load_queue_track(entry.queue_id, "B")

    assert controller.deck_b.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track.id == 1
    restored = controller._queue_service.entry(entry.queue_id)
    assert restored is not None
    assert restored.status == QueueStatus.READY
    assert restored.loaded_deck == "B"


def test_manual_load_applies_gain_before_first_playback_sample(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, replaygain_db=-6.0)
    controller.initialize()

    controller.load_catalog_track(1, "A")

    expected_factor = 10 ** (-6 / 20)
    assert controller.deck_a.normalization_factor == pytest.approx(expected_factor)
    assert controller.deck_a.model.loudness_effective_gain_db == -6.0
    controller.deck_action("A", "play")
    backend = controller.deck_a.backend
    assert isinstance(backend, GainObservingAudioBackend)
    assert backend.gain_at_play == pytest.approx([expected_factor])


def test_synchronous_autoload_applies_gain_before_automatic_playback(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path, replaygain_db=-6.0)
    controller.initialize()

    controller.add_catalog_track_to_queue(1)
    expected_factor = 10 ** (-6 / 20)
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.normalization_factor == pytest.approx(expected_factor)
    controller.start_automatic_queue()

    backend = controller.deck_a.backend
    assert isinstance(backend, GainObservingAudioBackend)
    assert backend.gain_at_play == pytest.approx([expected_factor])


def test_automatic_start_clears_stale_transition_mute(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_a.set_transition_muted(True)

    controller.start_automatic_queue()

    assert not controller.deck_a.transition_muted
    assert controller.deck_a.backend.is_playing()
    assert controller.deck_a.backend.volume > 0


def test_background_preload_applies_gain_before_automatic_playback(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(
        tmp_path,
        background_preload=True,
        replaygain_db=-6.0,
    )
    controller.initialize()

    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    for _ in range(100):
        controller._drain_background_callbacks()
        if controller.deck_a.model.state.value == "playing":
            break
        sleep(0.01)

    expected_factor = 10 ** (-6 / 20)
    backend = controller.deck_a.backend
    assert isinstance(backend, GainObservingAudioBackend)
    assert controller.deck_a.normalization_factor == pytest.approx(expected_factor)
    assert backend.gain_at_play == pytest.approx([expected_factor])


def test_background_preload_applies_equalizer_once_before_ready(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, _view = build_controller(
        tmp_path,
        background_preload=True,
        default_equalizer_preset="rock",
    )
    controller.initialize()
    backend = controller.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    events: list[str] = []
    original_apply = backend.apply_equalizer
    original_ready = controller._transition.preload_ready
    monkeypatch.setattr(
        backend,
        "apply_equalizer",
        lambda preset: (events.append("apply"), original_apply(preset))[1],
    )
    monkeypatch.setattr(
        controller._transition,
        "preload_ready",
        lambda deck_id, elapsed: (
            events.append("ready"),
            original_ready(deck_id, elapsed),
        )[1],
    )

    controller.add_catalog_track_to_queue(1)
    for _ in range(200):
        controller._drain_background_callbacks()
        if controller.deck_a.model.loaded_track is not None:
            break
        sleep(0.01)

    assert events[:2] == ["apply", "ready"]
    assert backend.equalizer_apply_count == 1
    assert controller.deck_a.model.equalizer_preset_name == "Rock"


def test_equalizer_failure_does_not_block_direct_track_load(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, view = build_controller(tmp_path, default_equalizer_preset="dance")
    controller.initialize()
    backend = controller.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    original_apply = backend.apply_equalizer

    def fail_enabled_equalizer(preset: object) -> bool:
        if getattr(preset, "enabled", False):
            raise RuntimeError("Test-EQ-Fehler")
        return original_apply(preset)  # type: ignore[arg-type]

    monkeypatch.setattr(backend, "apply_equalizer", fail_enabled_equalizer)

    controller.load_catalog_track(1, "A")

    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.state == DeckState.LOADED
    assert controller.deck_a.model.equalizer_source == "ERROR"
    assert any("Equalizer deaktiviert" in warning for warning in view.queue_warnings)
    assert "Test-EQ-Fehler" in controller.deck_a.model.equalizer_error
    assert any("Equalizer deaktiviert" in warning for warning in view.queue_warnings)


def test_equalizer_failure_does_not_block_background_preload(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, view = build_controller(
        tmp_path,
        background_preload=True,
        default_equalizer_preset="dance",
    )
    controller.initialize()
    backend = controller.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    original_apply = backend.apply_equalizer

    def fail_enabled_equalizer(preset: object) -> bool:
        if getattr(preset, "enabled", False):
            raise RuntimeError("Preload-EQ-Fehler")
        return original_apply(preset)  # type: ignore[arg-type]

    monkeypatch.setattr(backend, "apply_equalizer", fail_enabled_equalizer)
    controller.add_catalog_track_to_queue(1)
    for _ in range(200):
        controller._drain_background_callbacks()
        if controller.deck_a.model.loaded_track is not None:
            break
        sleep(0.01)

    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.state == DeckState.LOADED
    assert controller.deck_a.model.equalizer_source == "ERROR"


def test_equalizer_preview_can_be_discarded_exactly(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, default_equalizer_preset="neutral")
    controller.initialize()
    controller.load_catalog_track(1, "A")
    backend = controller.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    previous = backend.equalizer

    controller.preview_equalizer("A", "rock")
    assert backend.equalizer is not None
    assert backend.equalizer.preset_id == "rock"

    controller.discard_equalizer_preview("A")
    assert backend.equalizer == previous


def test_equalizer_title_assignment_survives_reload(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, default_equalizer_preset="neutral")
    controller.initialize()
    controller.load_catalog_track(1, "A")

    controller.save_track_equalizer("A", "dance")
    controller.load_catalog_track(1, "B")

    assert controller.deck_b.model.equalizer_preset_name == "Dance"
    assert controller.deck_b.model.equalizer_source == "TITLE"


def test_equalizer_title_assignment_can_be_changed_without_loading_track(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    resolver = controller._equalizer_resolver
    assert resolver is not None

    controller.save_track_equalizer_by_id(1, "rock")
    assert resolver.track_assignment_key(1) == "rock"

    controller.save_track_equalizer_by_id(1, None)
    assert resolver.track_assignment_key(1) is None


def test_equalizer_title_assignment_rejects_unknown_track(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)

    with pytest.raises(ValueError, match="Titel nicht gefunden"):
        controller.save_track_equalizer_by_id(999, "rock")


def test_equalizer_changes_are_blocked_during_crossfade(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.load_catalog_track(1, "A")
    controller._transition.state = TransitionState.CROSSFADE

    with pytest.raises(RuntimeError, match="Crossfades gesperrt"):
        controller.preview_equalizer("A", "rock")


@pytest.mark.parametrize(
    "state",
    [
        TransitionState.WAIT_FOR_ACTUAL_PLAYBACK,
        TransitionState.VERIFY_COMPLETION,
        TransitionState.STOP_FIRST_DECK,
        TransitionState.UNLOAD_FIRST_DECK,
        TransitionState.LOAD_NEXT_TRACK,
    ],
)
def test_equalizer_changes_are_not_blocked_outside_actual_crossfade(
    tmp_path: Path,
    state: TransitionState,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.load_catalog_track(1, "A")
    controller._transition.state = state

    controller.preview_equalizer("A", "rock")

    assert controller.deck_a.model.equalizer_source == "PREVIEW"


def test_custom_equalizer_rejects_clipping_and_becomes_selectable(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)

    with pytest.raises(ValueError, match="Clipping-Gefahr"):
        controller.save_custom_equalizer("Unsicher", -1.0, (60.0, 170.0), (3.0, 1.0))

    key = controller.save_custom_equalizer("Sanft", -3.0, (60.0, 170.0), (2.0, -1.0))

    assert (key, "Sanft") in controller.equalizer_presets()


def test_custom_equalizer_can_be_renamed_and_reset_without_changing_key(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    key = controller.save_custom_equalizer(
        "Sanft",
        -3.0,
        (60.0, 170.0),
        (2.0, -1.0),
    )
    resolver = controller._equalizer_resolver
    assert resolver is not None

    controller.rename_custom_equalizer(key, "Neutralisiert")
    controller.reset_custom_equalizer(key)

    preset = resolver.preset_by_key(key)
    assert preset is not None
    assert preset.name == "Neutralisiert"
    assert preset.preset_id == key
    assert preset.preamp_db == 0.0
    assert preset.curve == ((60.0, 0.0), (170.0, 0.0))


def test_builtin_equalizer_cannot_be_renamed_or_reset(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)

    with pytest.raises(ValueError, match="nicht verändert"):
        controller.rename_custom_equalizer("rock", "Mein Rock")
    with pytest.raises(ValueError, match="nicht zurückgesetzt"):
        controller.reset_custom_equalizer("rock")


def test_equalizer_dialog_state_and_genre_assignment(tmp_path: Path) -> None:
    controller, _view = build_controller(
        tmp_path,
        default_equalizer_preset="neutral",
        track_genre="Blues Rock",
    )
    controller.initialize()
    controller.load_catalog_track(1, "A")

    initial = controller.equalizer_dialog_state("A")
    assert initial.effective_name == "Neutral"
    assert initial.genre == "Blues Rock"
    assert initial.genre_preset_key is None

    controller.save_genre_equalizer("A", "bluesrock")

    resolved = controller.equalizer_dialog_state("A")
    assert resolved.genre_preset_key == "bluesrock"
    assert controller.deck_a.model.equalizer_source == "GENRE"


def test_reading_equalizer_dialog_state_does_not_apply_equalizer(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path, default_equalizer_preset="neutral")
    controller.initialize()
    controller.load_catalog_track(1, "A")
    backend = controller.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    applications_before_open = backend.equalizer_apply_count

    state = controller.equalizer_dialog_state("A")

    assert state.effective_name == "Neutral"
    assert backend.equalizer_apply_count == applications_before_open


def test_status_tick_does_not_reapply_equalizer(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, default_equalizer_preset="rock")
    controller.initialize()
    controller.load_catalog_track(1, "A")
    backend = controller.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    applications_before_tick = backend.equalizer_apply_count

    controller._run_status_tick()

    assert backend.equalizer_apply_count == applications_before_tick


def test_saved_queue_equalizer_can_be_changed_without_loading_playlist(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.save_current_queue("Abend")
    saved = controller._saved_queues.list()[0]  # type: ignore[union-attr]

    controller.save_saved_queue_equalizer(saved.saved_queue_id, "rock")

    assert controller.saved_queue_equalizer_key(saved.saved_queue_id) == "rock"


def test_discarded_preload_releases_autoload_guard(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    for _ in range(100):
        if controller._gui_dispatcher.statistics().pending:
            break
        sleep(0.01)
    assert controller._preload_in_progress
    controller._drain_background_callbacks()
    for _ in range(100):
        if controller._gui_dispatcher.statistics().pending:
            break
        sleep(0.01)
    controller._queue_entries_cache[0] = replace(
        controller._queue_entries_cache[0], status=QueueStatus.PLAYED
    )

    controller._drain_background_callbacks()

    assert not controller._preload_in_progress
    assert controller._transition.state == TransitionState.IDLE
    backend = controller.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    assert backend.equalizer_apply_count == 0


def test_stale_preload_result_cannot_restore_a_skipped_queue_entry(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    added = controller._queue_service.add(1)
    controller._refresh_queue()
    controller._auto_load_in_background()
    for _ in range(100):
        if controller._gui_dispatcher.statistics().pending:
            break
        sleep(0.01)
    controller._drain_background_callbacks()
    for _ in range(100):
        if controller._gui_dispatcher.statistics().pending:
            break
        sleep(0.01)
    assert controller._preload_in_progress

    controller._queue_service.mark_skipped(
        added.queue_id,
        "Wiedergabe auf dem eingehenden Deck nicht bestätigt",
        code="INCOMING_PLAYBACK_NOT_CONFIRMED",
    )
    controller._refresh_queue()
    controller._drain_background_callbacks()

    skipped = controller._queue_service.entry(added.queue_id)
    assert skipped is not None
    assert skipped.status == QueueStatus.SKIPPED
    assert skipped.skip_code == "INCOMING_PLAYBACK_NOT_CONFIRMED"
    assert controller.deck_a.model.loaded_track is None
    assert controller.deck_b.model.loaded_track is None
    assert added.queue_id not in controller._deck_queue_ids.values()
    assert not controller._preload_in_progress
    assert controller._transition.state == TransitionState.IDLE


def test_background_preload_waits_for_pending_gui_startup_work(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller._callback_state.update_layout_state(
        pending_layout_refreshes=0,
        pending_focus_request=True,
        pending_catalog_chunks=0,
        pending_queue_chunks=0,
        catalog_rows_created=0,
        queue_rows_created=0,
    )

    controller.add_catalog_track_to_queue(1)

    assert not controller._preload_in_progress
    controller._callback_state.update_layout_state(
        pending_layout_refreshes=0,
        pending_focus_request=False,
        pending_catalog_chunks=0,
        pending_queue_chunks=0,
        catalog_rows_created=10,
        queue_rows_created=10,
    )
    controller._auto_load()
    assert controller._preload_in_progress


def test_first_heartbeat_retries_preload_deferred_by_startup_work(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller._callback_state.update_layout_state(
        pending_layout_refreshes=0,
        pending_focus_request=True,
        pending_catalog_chunks=0,
        pending_queue_chunks=0,
        catalog_rows_created=0,
        queue_rows_created=0,
    )
    controller.add_catalog_track_to_queue(1)
    assert not controller._preload_in_progress

    controller._callback_state.update_layout_state(
        pending_layout_refreshes=0,
        pending_focus_request=False,
        pending_catalog_chunks=0,
        pending_queue_chunks=0,
        catalog_rows_created=1,
        queue_rows_created=1,
    )
    view.scheduled.clear()
    controller._heartbeat_tick()

    retry = next(
        callback for callback in view.scheduled if getattr(callback, "__name__", "") == "_auto_load"
    )
    retry()
    assert controller._preload_in_progress


def test_background_preload_ignores_render_work_after_startup(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller._heartbeat_started = True
    controller._callback_state.update_layout_state(
        pending_layout_refreshes=0,
        pending_focus_request=False,
        pending_catalog_chunks=3,
        pending_queue_chunks=2,
        catalog_rows_created=10,
        queue_rows_created=10,
    )

    controller.add_catalog_track_to_queue(1)

    assert controller._preload_in_progress


def test_preload_timeout_fails_owned_generation_and_releases_guard(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path, background_preload=True)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    entry = controller._queue_service.add(1)
    controller._queue_service.mark_preparing(entry.queue_id, "A")
    prepared = controller._queue_service.entry(entry.queue_id)
    assert prepared is not None
    controller._preload_in_progress = True
    controller._preload_generation = 7
    controller._transition.preload_started("A")

    controller._handle_preload_timeout(7, prepared, controller.deck_a)

    failed = controller._queue_service.entry(entry.queue_id)
    assert failed is not None
    assert failed.status == QueueStatus.FAILED
    assert failed.failure_code == "PREPARATION_TIMEOUT"
    assert controller._preload_generation == 8
    assert not controller._preload_in_progress
    assert controller._transition.state == TransitionState.FAILED
    assert view.queue_warnings
    assert not view.errors


def test_no_safe_candidate_warning_is_non_blocking_and_shown_once(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller._queue_service._automatic_selection = SimpleNamespace(  # type: ignore[assignment]
        last_relaxation_stage="NO_SAFE_CANDIDATE"
    )

    controller._show_no_safe_candidate_warning()
    controller._show_no_safe_candidate_warning()

    assert len(view.queue_warnings) == 1
    assert "endet regulär" in view.queue_warnings[0]
    assert not view.errors


def test_controller_initializes_one_status_loop(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    assert len(view.catalog) == 1
    assert {callback.__name__ for callback in view.scheduled} == {
        "_status_tick",
        "_heartbeat_tick",
        "_memory_tick",
    }


def test_runtime_heartbeat_waits_until_gui_startup_work_is_complete(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller._callback_state.update_layout_state(
        pending_layout_refreshes=0,
        pending_focus_request=True,
        pending_catalog_chunks=2,
        pending_queue_chunks=1,
        catalog_rows_created=0,
        queue_rows_created=0,
    )
    controller.initialize()

    pop_scheduled(view, "_heartbeat_tick")()

    assert not controller._heartbeat_started
    controller._callback_state.update_layout_state(
        pending_layout_refreshes=0,
        pending_focus_request=False,
        pending_catalog_chunks=0,
        pending_queue_chunks=0,
        catalog_rows_created=10,
        queue_rows_created=10,
    )
    pop_scheduled(view, "_heartbeat_tick")()
    assert controller._heartbeat_started


def test_diagnostic_report_is_saved_with_context_and_without_file_paths(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.bind_database_diagnostic_status(
        lambda: (
            "last_manual_backup_at: 2026-08-10T12:00:00+00:00",
            "last_data_operation: MAINTENANCE",
            "last_data_operation_error: none",
        )
    )
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller._status_tick()

    report_path = controller.save_diagnostic_report("normal_playback", tmp_path / "diagnostics")
    report = report_path.read_text(encoding="utf-8")

    assert report_path.name.startswith("deckrelay-diagnostic-")
    assert report.startswith("DeckRelay diagnostic report")
    assert "Version: 2.0.0-beta.1" in report
    assert "Test context: normal_playback" in report
    assert "Operating mode:" in report
    assert "status_tick.total:" in report
    assert "GUI event dispatcher:" in report
    assert "published_count:" in report
    assert "Registered workers:" in report
    assert "Queue instrumentation:" in report
    assert "  complete: false" in report
    assert "gui.queue_render.total" in report
    assert "  counters_plausible: true" in report
    assert "Timings:" in report
    assert "Counters:" in report
    assert "Gauges:" in report
    assert "tracemalloc_enabled:" in report
    assert "process_rss_status:" in report
    assert "catalog_row_views: 0" in report
    assert "Backup/restore/maintenance:" in report
    assert "last_data_operation: MAINTENANCE" in report
    assert "song.mp3" not in report


def test_diagnostic_report_confirms_complete_queue_instrumentation(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    for operation in controller.QUEUE_INSTRUMENTATION_OPERATIONS:
        controller._performance.record(operation, 1.0, 100.0)

    report_path = controller.save_diagnostic_report("queue_stress", tmp_path)
    report = report_path.read_text(encoding="utf-8")

    assert "Queue instrumentation:" in report
    assert "  complete: true" in report
    assert "  missing: none" in report
    assert "  counters_plausible: true" in report


def test_diagnostic_report_classifies_track_editor_timings_and_counters(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller._performance.record(
        "track_editor.build_view_model",
        4.0,
        100.0,
        {"track_id": 1},
    )
    controller._performance.record("track_editor_open_total", 1.0, 100.0)

    report_path = controller.save_diagnostic_report("idle", tmp_path)
    report = report_path.read_text(encoding="utf-8")

    timings = report.split("Timings:", 1)[1].split("Counters:", 1)[0]
    counters = report.split("Counters:", 1)[1].split("Gauges:", 1)[0]
    assert "track_editor.build_view_model" in timings
    assert "track_editor_open_total" not in timings
    assert "track_editor_open_total" in counters
    assert "song.mp3" not in report


def test_save_performance_diagnostic_reports_saved_path(tmp_path: Path, monkeypatch) -> None:
    controller, view = build_controller(tmp_path)
    report_path = tmp_path / "diagnostic.txt"
    report_path.write_text("diagnostic", encoding="utf-8")
    monkeypatch.setattr(controller, "save_diagnostic_report", lambda _context: report_path)

    controller.save_performance_diagnostic("crossfade")

    assert view.diagnostic_path == report_path.resolve()
    assert view.diagnostic_state == ("stopped", "crossfade")


def test_diagnostic_report_uses_configured_runtime_directory(tmp_path: Path) -> None:
    diagnostic_dir = tmp_path / "runtime" / "diagnostics"
    controller, _view = build_controller(
        tmp_path,
        diagnostics_directory=diagnostic_dir,
    )

    report_path = controller.save_diagnostic_report("idle")

    assert report_path.parent == diagnostic_dir
    assert report_path.is_file()


def test_save_performance_diagnostic_explains_disabled_mode(tmp_path: Path, monkeypatch) -> None:
    controller, view = build_controller(tmp_path)

    def reject_report(_context: str) -> Path:
        raise RuntimeError("Performance-Diagnostik ist im Produktionsbetrieb deaktiviert")

    monkeypatch.setattr(controller, "save_diagnostic_report", reject_report)

    controller.save_performance_diagnostic("idle")

    assert view.errors == ["Performance-Diagnostik ist im Produktionsbetrieb deaktiviert"]


def test_diagnostic_report_rejects_unknown_context(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    with pytest.raises(ValueError, match="Diagnosekontext"):
        controller.save_diagnostic_report("free text", tmp_path)


def test_status_tick_does_not_render_unchanged_queue(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    initial_render_count = view.queue_render_count

    controller._status_tick()

    assert view.queue_render_count == initial_render_count


def test_queue_render_is_deferred_during_crossfade(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    initial_render_count = view.queue_render_count
    controller._transition.state = TransitionState.CROSSFADE
    controller._queue_service.add(1)

    controller._refresh_queue()

    assert view.queue_render_count == initial_render_count
    assert controller._queue_render_pending


def test_crossfade_coalesces_structural_queue_changes_to_latest_snapshot(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    initial_render_count = view.queue_render_count
    controller._transition.state = TransitionState.CROSSFADE

    controller._queue_service.add(1)
    controller._refresh_queue()
    controller._queue_service.add(1)
    controller._refresh_queue()

    assert view.queue_render_count == initial_render_count
    assert controller._pending_queue_view_update is not None
    assert len(controller._pending_queue_view_update.entries) == 2

    controller._transition.state = TransitionState.IDLE
    controller._run_status_tick()

    assert view.queue_render_count == initial_render_count + 1
    assert len(view.queue) == 2
    assert not controller._queue_render_pending


def test_crossfade_applies_status_event_without_waiting_for_structural_render(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entry = view.queue[0]
    initial_render_count = view.queue_render_count
    controller._transition.state = TransitionState.CROSSFADE

    controller._queue_service.mark_loaded(entry.queue_id, "A")
    controller._refresh_queue()

    assert view.queue[0].status == QueueStatus.LOADED
    assert view.queue_render_count == initial_render_count
    assert not controller._queue_render_pending


def test_gui_event_handler_is_measured_individually(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    called: list[bool] = []
    controller._publish_gui_callback(lambda: called.append(True), "test")

    controller._drain_background_callbacks()

    assert called == [True]
    assert controller._performance.statistics()["gui_event.dispatch.callback"].count == 1


def test_catalog_and_search_results_can_be_paginated(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path, track_count=105)
    controller.initialize()
    assert len(view.catalog) == 50
    assert view.catalog_page == (1, 3)

    controller.change_catalog_page(1)
    assert len(view.catalog) == 50
    assert view.catalog_page == (2, 3)

    controller.search("Song")
    assert len(view.catalog) == 50
    assert view.catalog_page == (1, 3)


def test_adding_to_queue_loads_free_deck_without_starting(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    assert controller.deck_a.model.loaded_track is not None
    assert not controller.deck_a.backend.is_playing()
    assert view.queue[0].loaded_deck == "A"
    assert view.queue_stats is not None
    assert view.queue_stats.total_tracks == 1
    assert view.queue_stats.total_duration == 120.0
    assert view.queue_stats.remaining_tracks == 1
    assert view.queue_stats.remaining_duration == 120.0


def test_explicit_prepared_removal_unloads_inactive_deck_before_queue_change(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    entry = view.queue[0]
    assert entry.status == QueueStatus.READY
    assert controller.deck_a.model.loaded_track is not None

    controller.remove_prepared_queue_track(entry.queue_id)

    assert controller.deck_a.model.loaded_track is None
    assert controller._queue_service.entry(entry.queue_id).status == QueueStatus.REMOVED  # type: ignore[union-attr]
    assert controller._deck_queue_ids["A"] is None


def test_explicit_preparing_removal_cancels_owned_preload_generation(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    controller._queue_service.mark_preparing(entry.queue_id, "A")
    controller._preload_in_progress = True
    generation = controller._preload_generation

    controller.remove_prepared_queue_track(entry.queue_id)

    assert controller._preload_generation == generation + 1
    assert not controller._preload_in_progress
    assert controller._queue_service.entry(entry.queue_id).status == QueueStatus.REMOVED  # type: ignore[union-attr]


def test_explicit_preparing_move_cancels_preload_and_reorders_as_waiting(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    first, prepared = controller._queue_service.entries()
    controller._queue_service.mark_preparing(prepared.queue_id, "A")
    controller._preload_in_progress = True
    generation = controller._preload_generation

    controller.move_prepared_queue_track(prepared.queue_id, -1)

    entries = controller._queue_service.entries()
    assert controller._preload_generation == generation + 1
    assert not controller._preload_in_progress
    assert [entry.queue_id for entry in entries] == [prepared.queue_id, first.queue_id]
    assert entries[0].status == QueueStatus.WAITING
    assert entries[0].loaded_deck is None


def test_complete_queue_clear_unloads_prepared_deck(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    entry = view.queue[0]
    assert entry.status == QueueStatus.READY
    assert controller.deck_a.model.loaded_track is not None

    controller.clear_complete_queue()

    assert controller.deck_a.model.loaded_track is None
    assert controller._deck_queue_ids["A"] is None
    assert controller._queue_service.entries() == []


def test_complete_queue_clear_removes_failed_entries(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    controller._queue_service.mark_error(entry.queue_id)

    controller.clear_complete_queue()

    assert controller._queue_service.entries() == []


def test_complete_queue_clear_keeps_actually_playing_stale_ready_entry(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    assert entry.status == QueueStatus.READY
    controller.deck_a.backend.play()
    controller.deck_a.model.state = DeckState.PLAYING

    controller.clear_complete_queue()

    remaining = controller._queue_service.entries()
    assert len(remaining) == 1
    assert remaining[0].queue_id == entry.queue_id
    assert remaining[0].status == QueueStatus.PLAYING


def test_queue_stats_can_use_effective_cue_duration(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    controller.save_queue_cues(entry.queue_id, 10.0, 100.0, 7.0)

    controller.set_queue_stats_use_effective_cues(True)

    assert view.queue_stats is not None
    assert view.queue_stats.total_duration == 90.0
    assert view.queue_stats.remaining_duration == 90.0


def test_track_cue_save_targets_only_matching_catalog_and_queue_rows(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    full_queue_renders = view.queue_render_count
    view.catalog_entry_updates.clear()
    view.queue_entry_updates.clear()

    controller.track_cues_changed(1, True)

    assert view.catalog_entry_updates == [(1, True)]
    assert view.queue_entry_updates == [view.queue[0].queue_id]
    assert view.queue_render_count == full_queue_renders


def test_track_editor_equalizer_state_uses_existing_resolver_snapshot(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()

    key, name, source = controller.track_editor_equalizer_state(view.catalog[0])

    assert source in {"TITLE", "GENRE", "GLOBAL", "DISABLED"}
    assert (key is None and name == "Aus") or (key is not None and name is not None)


def test_track_editor_view_model_is_loaded_before_gui_callback(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    worker_finished = Event()
    completed: list[str] = []

    accepted = controller.load_track_editor_view_model(
        lambda: (worker_finished.set(), "editor-model")[1],
        completed.append,
        lambda error: pytest.fail(str(error)),
    )

    assert accepted
    assert worker_finished.wait(timeout=2)
    assert completed == []
    controller._gui_dispatcher.process_pending_events(
        lambda event: event.payload() if callable(event.payload) else None
    )
    assert completed == ["editor-model"]
    controller.close()


def test_track_editor_worker_accepts_tempo_and_technical_follow_up(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    release = Event()
    first_started = Event()

    def first() -> str:
        first_started.set()
        assert release.wait(timeout=2)
        return "tempo"

    assert controller.load_track_editor_view_model(first, lambda _value: None, pytest.fail)
    assert first_started.wait(timeout=2)
    assert controller.load_track_editor_view_model(
        lambda: "technical", lambda _value: None, pytest.fail
    )
    release.set()
    assert controller._track_editor_executor.drain(2.0)
    controller.close()


def test_unchanged_queue_stats_reuse_resolved_durations(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, _view = build_controller(tmp_path, track_count=20)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    for track_id in range(1, 21):
        controller.add_catalog_track_to_queue(track_id)
    controller.set_queue_stats_use_effective_cues(True)
    assert controller._cue_points is not None
    resolve_calls = 0
    original_resolve = controller._cue_points.resolve

    def counted_resolve(*args: object, **kwargs: object) -> object:
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(controller._cue_points, "resolve", counted_resolve)
    controller._queue_stats_dirty = True

    controller._refresh_queue_stats()
    first_refresh_calls = resolve_calls
    controller._refresh_queue_stats()
    controller._refresh_queue_stats()

    assert first_refresh_calls == 20
    assert resolve_calls == first_refresh_calls


def test_dynamic_queue_stats_do_not_rescan_full_queue(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    for track_id in (1, 2, 3):
        controller.add_catalog_track_to_queue(track_id)
    controller._refresh_queue_stats()
    first_entry = controller._queue_entries_cache[0]
    controller._queue_stats_playing_ids = (first_entry.queue_id,)

    class NoIterationList(list[QueueEntry]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("dynamic statistics scanned the complete queue")

    controller._queue_entries_cache = NoIterationList(controller._queue_entries_cache)

    controller._refresh_queue_stats()


def test_automatic_deck_loading_requests_cover(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    requested: list[tuple[str, int]] = []
    controller._load_cover_async = (  # type: ignore[method-assign]
        lambda deck_id, track: requested.append((deck_id, track.id))
    )

    controller.add_catalog_track_to_queue(1)

    assert requested == [("A", 1)]


def test_closing_without_finishing_keeps_loaded_queue_for_recovery(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    assert view.queue[0].status.value == "ready"

    controller.close(finish_session=False)

    assert controller._queue_service.entries()[0].status.value == "ready"


def test_closing_finished_session_skips_preloaded_unplayed_track(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    assert view.queue[0].status.value == "ready"

    controller.close(finish_session=True)

    entry = controller._queue_service.entries()[0]
    assert entry.status.value == "skipped"
    assert entry.skip_reason == "Session beendet, bevor der Titel gestartet wurde"


def test_close_does_not_wait_for_pending_owned_persistence_worker(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    release_worker = Event()
    controller._persistence_executor.submit(lambda: release_worker.wait(2.0))

    started = monotonic()
    controller.close(finish_session=True)
    elapsed = monotonic() - started
    release_worker.set()

    assert elapsed < 0.5


def test_skipping_active_queue_entry_ejects_deck_and_keeps_reason(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")

    controller.mark_queue_track_skipped(view.queue[0].queue_id, "Nicht gewünscht")

    assert controller.deck_a.model.loaded_track is None
    assert view.queue[0].status.value == "skipped"
    assert view.queue[0].skip_reason == "Nicht gewünscht"


def test_audio_start_error_marks_assigned_queue_entry_as_error(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    backend = controller.deck_a.backend
    backend.play = lambda: (_ for _ in ()).throw(RuntimeError("Decoderfehler"))  # type: ignore[method-assign]

    controller.deck_action("A", "play")

    assert view.queue[0].status.value == "failed"
    assert controller.deck_a.model.state.value == "error"


def test_keyboard_play_pause_action_toggles_deck_state(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)

    controller.toggle_deck_play_pause("A")
    assert controller.deck_a.model.state.value == "playing"
    controller.toggle_deck_play_pause("A")
    assert controller.deck_a.model.state.value == "paused"
    controller.toggle_deck_play_pause("A")
    assert controller.deck_a.model.state.value == "playing"  # type: ignore[comparison-overlap]


def test_audio_output_device_is_applied_to_decks_and_overlay(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    overlay_devices: list[str] = []
    controller.bind_overlay_output_device(overlay_devices.append)

    controller.set_audio_output_device("device-42")

    assert isinstance(controller.deck_a.backend, FakeAudioBackend)
    assert isinstance(controller.deck_b.backend, FakeAudioBackend)
    assert controller.deck_a.backend.output_device == "device-42"
    assert controller.deck_b.backend.output_device == "device-42"
    assert overlay_devices == ["device-42"]


def test_lost_audio_device_mutes_until_same_device_is_confirmed(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    state = EmergencyStateService()
    controller._deck_health_monitor = DeckHealthMonitor(state)
    controller._settings = SimpleNamespace(audio_output_device=lambda: "usb-dac")
    overlay_devices: list[str] = []
    overlay_mutes: list[bool] = []
    controller.bind_overlay_output_device(overlay_devices.append)
    controller.bind_overlay_master_mute(overlay_mutes.append)
    controller._automatic_run_active = True
    controller.deck_a.backend.output_devices = [("speakers", "Lautsprecher")]

    controller._check_audio_device_health()

    assert controller.audio_output_device_recovery_state() == "device_lost"
    assert controller.is_automatic_queue_paused()
    assert controller.deck_a.emergency_muted
    assert controller.deck_b.emergency_muted
    assert overlay_mutes[-1] is True
    assert view.audio_device_recovery == (
        "device_lost",
        "Audiogerät getrennt · Ausgabe gesperrt",
    )
    assert not controller.retry_audio_output_device()
    assert overlay_devices == []
    assert view.audio_device_recovery == (
        "device_lost",
        "Gerät weiterhin nicht verfügbar",
    )

    controller.deck_a.backend.output_devices = [("usb-dac", "USB DAC")]
    assert controller.retry_audio_output_device()
    assert controller.audio_output_device_recovery_state() == "ready_for_confirmation"
    assert controller.deck_a.emergency_muted
    assert controller.deck_b.emergency_muted
    assert overlay_devices == ["usb-dac"]
    assert view.audio_device_recovery == (
        "ready_for_confirmation",
        "Gerät angewendet · Ausgabe noch gesperrt",
    )

    assert controller.confirm_audio_output_device_recovered()
    assert controller.audio_output_device_recovery_state() == "normal"
    assert not controller.deck_a.emergency_muted
    assert not controller.deck_b.emergency_muted
    assert controller.is_automatic_queue_paused()
    assert "Automatik bleibt" in view.queue_warnings[-1]
    assert view.audio_device_recovery == ("normal", "Audioausgabe bereit")


def test_master_mute_is_applied_to_decks_and_overlay(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    overlay_mutes: list[bool] = []
    controller.bind_overlay_master_mute(overlay_mutes.append)

    controller.toggle_mute()
    assert controller.crossfader.master_muted
    assert overlay_mutes[-1] is True

    controller.toggle_mute()
    assert not controller.crossfader.master_muted
    assert overlay_mutes[-1] is False


def test_global_audio_recovery_requires_explicit_mute_release(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    overlay_mutes: list[bool] = []
    controller.bind_overlay_master_mute(overlay_mutes.append)
    controller._emergency = SimpleNamespace(
        recovery_active=lambda: False,
        recover_all_audio_backends=lambda: GlobalAudioRecoveryResult(
            True,
            "RECOVERED_MUTED",
            attempt=1,
            attempts_remaining=2,
            recovered_decks=("A", "B"),
        ),
    )
    controller._automatic_run_active = True

    result = controller.recover_all_audio_backends()

    assert result.success
    assert controller.deck_a.emergency_muted
    assert controller.deck_b.emergency_muted
    assert controller.is_automatic_queue_paused()
    assert controller.global_audio_recovery_ready_for_release()
    assert overlay_mutes[-1] is True
    assert view.recovery_return_requirements is not None
    requirements, visible = view.recovery_return_requirements
    assert visible
    assert not next(item.fulfilled for item in requirements if item.code == "GLOBAL_MUTE_RELEASED")

    assert controller.release_global_audio_recovery_mute()
    assert not controller.deck_a.emergency_muted
    assert not controller.deck_b.emergency_muted
    assert not controller.global_audio_recovery_ready_for_release()
    assert controller.is_automatic_queue_paused()
    assert overlay_mutes[-1] is False
    assert "bewusst freigegeben" in view.queue_warnings[-1]
    assert view.recovery_return_requirements is not None
    requirements, visible = view.recovery_return_requirements
    assert visible
    assert all(item.fulfilled for item in requirements)


def test_global_audio_recovery_runs_off_thread_and_rejects_duplicate_start(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    entered = Event()
    release = Event()

    def recover() -> GlobalAudioRecoveryResult:
        entered.set()
        release.wait(timeout=1)
        return GlobalAudioRecoveryResult(True, "RECOVERED_MUTED")

    controller._emergency = SimpleNamespace(
        recovery_active=lambda: controller._global_audio_recovery_requested,
        recover_all_audio_backends=recover,
    )

    assert controller.start_global_audio_recovery()
    assert entered.wait(timeout=0.5)
    assert controller.global_audio_recovery_active()
    assert not controller.start_global_audio_recovery()
    release.set()
    for _attempt in range(100):
        if view.scheduled:
            break
        sleep(0.01)
    assert view.scheduled

    view.scheduled.pop()()

    assert not controller.global_audio_recovery_active()
    assert controller.global_audio_recovery_ready_for_release()


def test_recovery_return_assessment_reports_each_safety_gate(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)

    assert controller.assess_recovery_return().allowed

    controller._global_audio_recovery_ready_for_release = True
    assert controller.assess_recovery_return().error_code == "GLOBAL_MUTE_NOT_RELEASED"
    controller._global_audio_recovery_ready_for_release = False

    controller._audio_device_loss_active = True
    assert controller.assess_recovery_return().error_code == "OUTPUT_DEVICE_NOT_CONFIRMED"
    controller._audio_device_loss_active = False

    controller._emergency_state.set_deck_health("B", DeckHealth.FAILED, "Test")
    assert controller.assess_recovery_return().error_code == "DECKS_NOT_HEALTHY"
    controller._emergency_state.set_deck_health("B", DeckHealth.HEALTHY, "Test beendet")

    controller._transition.state = TransitionState.CROSSFADE
    assert controller.assess_recovery_return().error_code == "TRANSITION_STATE_UNSAFE"
    controller._transition.reset()

    controller._deck_queue_ids = {"A": 99999, "B": None}
    assert controller.assess_recovery_return().error_code == "QUEUE_DECK_ASSIGNMENT_INCONSISTENT"


def test_recovery_return_checklist_reports_all_blockers_at_once(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller._global_audio_recovery_requested = True
    controller._global_audio_recovery_ready_for_release = True
    controller._audio_device_loss_active = True
    controller._emergency_state.set_deck_health("A", DeckHealth.FAILED, "Test")
    controller._transition.state = TransitionState.CROSSFADE
    controller._deck_queue_ids = {"A": 12345, "B": None}

    requirements = controller.recovery_return_requirements()

    assert len(requirements) == 6
    assert {item.code for item in requirements if not item.fulfilled} == {
        "RECOVERY_FINISHED",
        "GLOBAL_MUTE_RELEASED",
        "OUTPUT_DEVICE_CONFIRMED",
        "DECKS_HEALTHY",
        "TRANSITION_STABLE",
        "QUEUE_CONSISTENT",
    }


def test_automatic_start_is_blocked_until_recovery_return_is_safe(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    controller, view = build_controller(tmp_path)
    controller.add_catalog_track_to_queue(1)
    controller._global_audio_recovery_ready_for_release = True

    controller.start_automatic_queue()

    assert not controller._automatic_run_active
    assert controller.player_mode != "automatic"
    assert "Sicherheits-Stummschaltung" in view.queue_warnings[-1]
    assert "code=GLOBAL_MUTE_NOT_RELEASED" in caplog.text
    assert "transition=idle" in caplog.text
    assert "preload_aktiv=False" in caplog.text


def test_explicit_recovery_resume_rechecks_gates_and_starts_automation(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.add_catalog_track_to_queue(1)
    controller._recovery_return_validation_required = True
    controller._automatic_run_paused = True

    assert controller.resume_automatic_after_recovery()

    assert not controller._recovery_return_validation_required
    assert controller._automatic_run_active
    assert controller.player_mode == "automatic"
    assert view.recovery_return_requirements == ((), False)


def test_explicit_recovery_resume_returns_to_two_deck_operation(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.add_catalog_track_to_queue(1)
    controller.enter_one_deck_mode("A", "Deck B vorübergehend ausgefallen")
    controller._emergency_state.set_deck_health("B", DeckHealth.HEALTHY, "Repariert")

    assert controller.resume_automatic_after_recovery()

    assert controller.audio_operating_mode().mode == AudioOperatingMode.TWO_DECK
    assert not controller.deck_b.emergency_muted
    assert "B" not in controller._auto_load_suppressed_decks
    assert controller._automatic_run_active
    assert any(
        deck.model.state == DeckState.PLAYING for deck in (controller.deck_a, controller.deck_b)
    )


def test_explicit_recovery_resume_keeps_gate_when_safety_check_fails(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller._recovery_return_validation_required = True
    controller._emergency_state.set_deck_health("A", DeckHealth.FAILED, "Test")

    assert not controller.resume_automatic_after_recovery()

    assert controller._recovery_return_validation_required
    assert not controller._automatic_run_active
    assert "Beide Decks müssen gesund sein" in view.queue_warnings[-1]


def test_aborted_transition_is_reset_when_automatic_restart_is_safe(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.add_catalog_track_to_queue(1)
    controller._transition.state = TransitionState.ABORTED

    controller.start_automatic_queue()

    assert controller._transition.state == TransitionState.IDLE
    assert controller._automatic_run_active


def test_database_error_is_shown_without_technical_details(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()

    def fail(_track_id: int) -> None:
        raise sqlite3.OperationalError("database is locked at internal/path")

    monkeypatch.setattr(controller._queue_service, "add", fail)
    controller.add_catalog_track_to_queue(1)

    assert view.errors
    assert "Datenbank" in view.errors[-1]
    assert "locked" not in view.errors[-1]
    assert "internal/path" not in view.errors[-1]


def test_automatic_deck_error_uses_non_blocking_queue_warning(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)

    def fail() -> None:
        raise RuntimeError("backend detail")

    monkeypatch.setattr(controller.deck_a.backend, "play", fail)
    controller.deck_action("A", "play", automatic=True)

    assert view.queue_warnings
    assert "Wiedergabeaktion" in view.queue_warnings[-1]
    assert not view.errors


@pytest.mark.parametrize("deck_id", ("A", "B"))
def test_automatic_start_failure_frees_deck_and_keeps_other_deck_playing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    deck_id: str,
) -> None:
    controller, view = build_controller(tmp_path, track_count=3, with_history=True)
    controller.initialize()
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    failed = controller._deck(deck_id)
    other = controller.deck_b if failed is controller.deck_a else controller.deck_a
    other.play()
    other_queue_id = controller._deck_queue_ids[other.model.deck_id]
    assert other_queue_id is not None
    controller._queue_service.mark_playing(other_queue_id)
    failed_queue_id = controller._deck_queue_ids[deck_id]
    assert failed_queue_id is not None
    controller._automatic_run_active = True

    def fail() -> None:
        raise RuntimeError("decoder rejected media")

    monkeypatch.setattr(failed.backend, "play", fail)
    controller.deck_action(deck_id, "play", automatic=True)
    deadline = monotonic() + 2.0
    while deck_id in controller._automatic_recovering_decks and monotonic() < deadline:
        controller._drain_background_callbacks()
        sleep(0.01)

    entry = controller._queue_service.entry(failed_queue_id)
    assert entry is not None
    assert entry.status == QueueStatus.FAILED
    assert entry.failure_code == "AUTOMATIC_PLAYBACK_COMMAND_FAILED"
    assert other.model.state == DeckState.PLAYING
    assert other.backend.is_playing()
    assert failed.model.loaded_track is not None
    assert failed.model.loaded_track.id == 3
    assert controller._automatic_run_active
    assert "Ersatztitel" in view.queue_warnings[-1]


def test_three_consecutive_automatic_failures_pause_without_consuming_forever(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, view = build_controller(tmp_path, track_count=5)
    controller.initialize()
    for track_id in range(1, 6):
        controller.add_catalog_track_to_queue(track_id)
    controller._automatic_run_active = True

    for _attempt in range(controller.MAXIMUM_CONSECUTIVE_AUTOMATIC_FAILURES):
        deck = next(
            item
            for item in (controller.deck_a, controller.deck_b)
            if item.model.loaded_track is not None and item.model.state == DeckState.LOADED
        )

        def fail() -> None:
            raise RuntimeError("broken media")

        monkeypatch.setattr(deck.backend, "play", fail)
        controller.deck_action(deck.model.deck_id, "play", automatic=True)
        deadline = monotonic() + 2.0
        while (
            deck.model.deck_id in controller._automatic_recovering_decks and monotonic() < deadline
        ):
            controller._drain_background_callbacks()
            sleep(0.01)

    entries = controller._queue_service.entries()
    assert len([entry for entry in entries if entry.status == QueueStatus.FAILED]) == 3
    assert (
        len(
            [entry for entry in entries if entry.status in {QueueStatus.WAITING, QueueStatus.READY}]
        )
        == 2
    )
    assert controller.is_automatic_queue_paused()
    assert not controller._automatic_run_active
    assert "manueller Eingriff" in view.queue_warnings[-2]


def test_manual_load_invalidates_late_automatic_cleanup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller._automatic_run_active = True
    release = Event()
    original_stop = controller.deck_a.backend.stop

    def delayed_stop() -> None:
        release.wait(1.0)
        original_stop()

    monkeypatch.setattr(controller.deck_a.backend, "stop", delayed_stop)

    def fail() -> None:
        raise RuntimeError("broken media")

    monkeypatch.setattr(controller.deck_a.backend, "play", fail)
    controller.deck_action("A", "play", automatic=True)
    controller._emergency_state.set_deck_health("A", DeckHealth.FAILED, "Testfehler")
    controller._emergency_state.transition(EmergencySystemState.DEGRADED, "Testfehler")
    controller.load_catalog_track(2, "A")
    release.set()
    sleep(0.02)
    controller._drain_background_callbacks()

    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 2
    assert not controller._automatic_run_active
    assert not controller._preload_in_progress
    assert controller.emergency_snapshot().deck_a == DeckHealth.HEALTHY
    assert controller.emergency_snapshot().system == EmergencySystemState.NORMAL


def test_confirmed_runtime_failure_uses_existing_isolated_recovery(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=2, with_history=True)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    controller.deck_a.play()
    controller.deck_b.play()
    queue_a = controller._deck_queue_ids["A"]
    queue_b = controller._deck_queue_ids["B"]
    assert queue_a is not None and queue_b is not None
    controller._queue_service.mark_playing(queue_a)
    controller._queue_service.mark_playing(queue_b)
    controller._automatic_run_active = True
    requested: list[tuple[str, AudioRecoveryPolicy, bool]] = []

    def request(
        deck_id: str,
        policy: AudioRecoveryPolicy = AudioRecoveryPolicy.RESUME_POSITION,
        *,
        automatic: bool = False,
    ) -> bool:
        requested.append((deck_id, policy, automatic))
        return True

    monkeypatch.setattr(controller, "start_deck_recovery_action", request)
    controller._handle_runtime_deck_failure(controller.deck_a, "Backend meldet ERROR")

    assert requested == [("A", AudioRecoveryPolicy.SKIP_TRACK, True)]
    failed = controller._queue_service.entry(queue_a)
    assert failed is not None and failed.status == QueueStatus.FAILED
    assert failed.failure_code == "PLAYBACK_ABORTED"
    assert controller.deck_b.model.state == DeckState.PLAYING
    assert controller.deck_b.backend.is_playing()
    assert controller.audio_operating_mode().mode == AudioOperatingMode.ONE_DECK


def test_second_simultaneous_runtime_failure_falls_back_to_manual_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    controller.deck_a.play()
    controller.deck_b.play()
    controller._automatic_run_active = True
    calls = 0

    def request(
        _deck_id: str,
        _policy: AudioRecoveryPolicy = AudioRecoveryPolicy.RESUME_POSITION,
        *,
        automatic: bool = False,
    ) -> bool:
        nonlocal calls
        assert automatic
        calls += 1
        return calls == 1

    monkeypatch.setattr(controller, "start_deck_recovery_action", request)
    controller._handle_runtime_deck_failure(controller.deck_a, "Deck A ausgefallen")
    controller._handle_runtime_deck_failure(controller.deck_b, "Deck B ausgefallen")

    assert calls == 2
    assert controller.is_automatic_queue_paused()
    assert not controller._automatic_run_active
    assert "manuellen Eingriff" in view.queue_warnings[-1]


def test_shutdown_invalidates_pending_automatic_cleanup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller._automatic_run_active = True
    release = Event()
    original_stop = controller.deck_a.backend.stop

    def delayed_stop() -> None:
        release.wait(1.0)
        original_stop()

    def fail() -> None:
        raise RuntimeError("broken media")

    monkeypatch.setattr(controller.deck_a.backend, "stop", delayed_stop)
    monkeypatch.setattr(controller.deck_a.backend, "play", fail)
    controller.deck_action("A", "play", automatic=True)
    generation = controller._automatic_failure_generations["A"]

    controller.close()
    release.set()

    assert controller._closed
    assert controller._automatic_failure_generations["A"] > generation
    assert not controller._automatic_recovering_decks


def test_failed_only_track_falls_back_to_stable_paused_automatic_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller._automatic_run_active = True

    def fail() -> None:
        raise RuntimeError("broken media")

    monkeypatch.setattr(controller.deck_a.backend, "play", fail)
    controller.deck_action("A", "play", automatic=True)
    deadline = monotonic() + 2.0
    while controller._automatic_recovering_decks and monotonic() < deadline:
        controller._drain_background_callbacks()
        sleep(0.01)

    assert controller.is_automatic_queue_paused()
    assert not controller._automatic_run_active
    assert controller.deck_a.model.loaded_track is None
    assert "Kein Ersatztitel" in view.queue_warnings[-1]


def test_explicit_eject_leaves_deck_empty_even_with_waiting_queue(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)

    controller.deck_action("A", "eject")

    assert controller.deck_a.model.loaded_track is None


def test_controller_clamps_configurable_fade_duration(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.set_fade_duration(0)
    assert controller.fade_duration == 1
    controller.set_fade_duration(45)
    assert controller.fade_duration == 30
    controller.set_fade_out_stops_deck(True)
    assert controller.fade_out_stops_deck


def test_disabled_automatic_loading_leaves_queue_waiting(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)

    assert controller.deck_a.model.loaded_track is None
    assert view.queue[0].status.value == "waiting"


def test_reenabling_automatic_loading_fills_free_deck(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)

    controller.set_automatic_deck_loading(True)

    assert controller.deck_a.model.loaded_track is not None


def test_queue_status_updates_when_finished_track_is_restarted(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")
    assert f"{view.queue[0].status}" == "playing"
    timings = controller._performance.statistics()
    assert "queue_service.mark_playing.repository" in timings
    assert "queue_service.mark_playing.log" in timings
    backend = controller.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    backend.playing = False
    backend.finished = True
    backend.position = backend.duration

    status_tick = pop_scheduled(view, "_status_tick")
    status_tick()
    # The status tick now only signals completion; repository work is detached.
    pop_scheduled(view, "finish_deck_a")()
    assert f"{view.queue[0].status}" == "played"

    controller.deck_action("A", "play")
    assert f"{view.queue[0].status}" == "playing"


def test_transition_completion_does_not_wait_for_persistence_and_preloads_next(
    tmp_path: Path,
) -> None:
    executor = ManualExecutor()
    controller, view = build_controller(
        tmp_path,
        track_count=3,
        persistence_executor=executor,
        with_history=True,
    )
    controller.initialize()
    for track_id in (1, 2, 3):
        controller.add_catalog_track_to_queue(track_id)
    controller.deck_action("A", "play")
    outgoing_track = controller.deck_a.model.loaded_track
    assert outgoing_track is not None
    queue_id = controller._deck_queue_ids["A"]
    assert queue_id is not None

    controller._complete_automatic_transition(controller.deck_a, outgoing_track.id, queue_id)

    assert controller.deck_a.model.loaded_track is None
    assert (
        next(
            entry for entry in controller._queue_entries_cache if entry.queue_id == queue_id
        ).status
        == QueueStatus.PLAYED
    )
    pop_scheduled(view, "render_completed_queue_rows")()
    assert (
        next(entry for entry in view.queue if entry.queue_id == queue_id).status
        == QueueStatus.PLAYED
    )
    assert len(executor.tasks) == 2
    assert controller._queue_service.entry(queue_id).status == QueueStatus.PLAYING  # type: ignore[union-attr]

    for _ in range(100):
        controller._drain_background_callbacks()
        if controller.deck_a.model.loaded_track is not None:
            break
        sleep(0.01)
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 3
    executor.run_all()
    assert controller._queue_service.entry(queue_id).status == QueueStatus.PLAYED  # type: ignore[union-attr]


def test_slow_deck_cleanup_cannot_delay_crossfade_target_or_incoming_audio(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    controller.deck_action("B", "play", automatic=True)
    controller.deck_a.model.position = 119.75
    cleanup_started = Event()
    release_cleanup = Event()

    def detach_for_slow_cleanup() -> Callable[[], None]:
        def cleanup() -> None:
            cleanup_started.set()
            release_cleanup.wait(timeout=2)

        return cleanup

    monkeypatch.setattr(controller.deck_a, "detach_for_cleanup", detach_for_slow_cleanup)
    controller._automatic_playback_tick()
    assert controller._transition.is_transitioning

    for _ in range(20):
        sleep(0.05)
        render = next(
            (
                callback
                for callback in view.scheduled
                if getattr(callback, "__name__", "") == "render_tick"
            ),
            None,
        )
        if render is not None:
            view.scheduled.remove(render)
            render()
        if any(
            getattr(callback, "__name__", "") == "finish_transition" for callback in view.scheduled
        ):
            break

    assert controller.crossfader.position == pytest.approx(1.0)
    assert view.crossfader_render == pytest.approx(1.0)
    level_diagnostic = controller._transition.level_diagnostic()
    assert level_diagnostic.direction == "A_TO_B"
    assert level_diagnostic.position_monotonic
    assert level_diagnostic.reached_target
    assert level_diagnostic.audio_ramp_complete
    assert "  result: COMPLETE" in controller.diagnostic_report("crossfade")
    finish = pop_scheduled(view, "finish_transition")
    started = monotonic()
    finish()
    completion_duration = monotonic() - started

    assert cleanup_started.wait(timeout=1)
    assert completion_duration < 0.15
    assert controller.deck_b.backend.is_playing()
    assert controller.crossfader.position == pytest.approx(1.0)
    sleep(0.3)
    assert controller.deck_b.backend.is_playing()
    release_cleanup.set()
    for _ in range(100):
        cleanup_timing = controller._performance.statistics().get(
            "worker.transition_cleanup.backend_stop"
        )
        if cleanup_timing is not None:
            break
        sleep(0.01)
    assert cleanup_timing is not None
    assert cleanup_timing.maximum_duration_ms >= 200.0


def test_transition_completion_cancels_queued_natural_end_completion(tmp_path: Path) -> None:
    executor = ManualExecutor()
    controller, view = build_controller(
        tmp_path,
        persistence_executor=executor,
        with_history=True,
    )
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")
    track = controller.deck_a.model.loaded_track
    queue_id = controller._deck_queue_ids["A"]
    assert track is not None and queue_id is not None

    controller._schedule_finished_deck_completion(controller.deck_a)
    controller._complete_automatic_transition(controller.deck_a, track.id, queue_id)

    assert len(executor.tasks) == 2
    pop_scheduled(view, "finish_deck_a")()
    assert len(executor.tasks) == 2


def test_slow_queue_render_and_persistence_do_not_block_transition_completion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    executor = ManualExecutor()
    controller, view = build_controller(
        tmp_path,
        persistence_executor=executor,
        with_history=True,
    )
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")
    track = controller.deck_a.model.loaded_track
    queue_id = controller._deck_queue_ids["A"]
    assert track is not None and queue_id is not None
    original_show_queue_events = view.show_queue_events

    def slow_show_queue_events(*args, **kwargs) -> None:
        sleep(0.1)
        original_show_queue_events(*args, **kwargs)

    monkeypatch.setattr(view, "show_queue_events", slow_show_queue_events)

    started = monotonic()
    controller._complete_automatic_transition(controller.deck_a, track.id, queue_id)
    elapsed = monotonic() - started

    assert elapsed < 0.05
    assert controller.deck_a.model.loaded_track is None
    assert len(executor.tasks) == 2
    assert view.queue[0].status == QueueStatus.PLAYING

    pop_scheduled(view, "render_completed_queue_rows")()

    assert view.queue[0].status == QueueStatus.PLAYED


def test_transition_statistics_rebuild_is_dispatched_to_worker(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")
    track = controller.deck_a.model.loaded_track
    queue_id = controller._deck_queue_ids["A"]
    assert track is not None and queue_id is not None
    background_flags: list[bool] = []
    original_refresh = controller._refresh_queue_stats

    def observed_refresh(*, background_rebuild: bool = False) -> None:
        background_flags.append(background_rebuild)
        original_refresh(background_rebuild=background_rebuild)

    monkeypatch.setattr(controller, "_refresh_queue_stats", observed_refresh)

    controller._complete_automatic_transition(controller.deck_a, track.id, queue_id)
    pop_scheduled(view, "refresh_transition_statistics")()

    assert background_flags == [True]


def test_slow_outgoing_stop_does_not_block_incoming_audio_or_completion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    controller.deck_action("A", "play")
    controller.deck_action("B", "play")
    outgoing_track = controller.deck_a.model.loaded_track
    outgoing_queue_id = controller._deck_queue_ids["A"]
    incoming_backend = controller.deck_b.backend
    assert outgoing_track is not None and outgoing_queue_id is not None
    assert incoming_backend.is_playing()
    original_stop = controller.deck_a.backend.stop

    def slow_stop() -> None:
        sleep(0.1)
        original_stop()

    monkeypatch.setattr(controller.deck_a.backend, "stop", slow_stop)

    started = monotonic()
    controller._complete_automatic_transition(
        controller.deck_a,
        outgoing_track.id,
        outgoing_queue_id,
    )
    elapsed = monotonic() - started

    assert elapsed < 0.05
    assert controller.deck_a.model.loaded_track is None
    assert incoming_backend.is_playing()


def test_delayed_completion_does_not_overwrite_restarted_queue_entry(tmp_path: Path) -> None:
    executor = ManualExecutor()
    controller, _view = build_controller(tmp_path, persistence_executor=executor)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")
    track = controller.deck_a.model.loaded_track
    queue_id = controller._deck_queue_ids["A"]
    assert track is not None and queue_id is not None

    controller._complete_automatic_transition(controller.deck_a, track.id, queue_id)
    controller.deck_a.load(track)
    controller._deck_queue_ids["A"] = queue_id
    controller.deck_action("A", "play")
    executor.run_all()

    entry = controller._queue_service.entry(queue_id)
    assert entry is not None
    assert entry.status == QueueStatus.PLAYING


def test_transition_completion_guard_rejects_duplicate_execution(tmp_path: Path) -> None:
    executor = ManualExecutor()
    controller, _view = build_controller(tmp_path, persistence_executor=executor)
    controller.initialize()
    controller._transition_completion_pending = True

    controller._complete_automatic_transition(controller.deck_a, None, None)

    assert executor.tasks == []


def test_history_persistence_failure_does_not_reverse_audio_completion(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    executor = ManualExecutor()
    controller, _view = build_controller(tmp_path, persistence_executor=executor, with_history=True)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")
    track = controller.deck_a.model.loaded_track
    queue_id = controller._deck_queue_ids["A"]
    assert track is not None and queue_id is not None and controller._history is not None
    monkeypatch.setattr(
        controller._history,
        "persist",
        lambda _request: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )

    controller._complete_automatic_transition(controller.deck_a, track.id, queue_id)
    executor.run_all()

    assert controller.deck_a.model.loaded_track is None
    assert controller._queue_service.entry(queue_id).status == QueueStatus.PLAYED  # type: ignore[union-attr]


def test_database_delay_scenario_isolated_to_persistence_and_report(tmp_path: Path) -> None:
    executor = ManualExecutor()
    controller, _view = build_controller(tmp_path, persistence_executor=executor, with_history=True)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")
    track = controller.deck_a.model.loaded_track
    queue_id = controller._deck_queue_ids["A"]
    assert track is not None and queue_id is not None
    controller._performance.record("measurement.before_scenario", 99.0, 1000.0)
    controller.begin_diagnostic_scenario("database_delay", 20)
    assert _view.diagnostic_state == ("running", "database_delay")

    started = monotonic()
    controller._complete_automatic_transition(controller.deck_a, track.id, queue_id)
    gui_elapsed = monotonic() - started

    assert gui_elapsed < 0.05
    assert controller.deck_a.model.loaded_track is None
    executor.run_all()
    controller._diagnostic_scenario.end()
    snapshot = controller._diagnostic_scenario.snapshot()
    assert snapshot is not None
    assert snapshot.transitions_completed == 1
    assert snapshot.persistence_jobs_submitted == 2
    assert snapshot.persistence_jobs_completed == 2
    report = controller.diagnostic_report("database_delay")
    assert "acceptance_data_present: true" in report
    assert "injected_database_delay_ms: 20" in report
    assert "measurement.before_scenario" not in report
    for operation in (
        "database.injected_delay",
        "database.history.total",
        "database.history.commit",
        "database.queue.total",
        "database.queue.commit",
        "transition_completion.total",
        "worker.history_persist",
        "worker.queue_persist",
        "worker.playback_persist",
    ):
        assert operation in report


def test_database_delay_cannot_be_enabled_in_production_mode(tmp_path: Path) -> None:
    controller, view = build_controller(
        tmp_path, performance_settings=PerformanceSettings(enabled=False)
    )

    controller.begin_diagnostic_scenario("database_delay", 1000)

    assert controller._diagnostic_scenario.snapshot() is None
    assert view.errors
    assert "Produktionsmodus" in view.errors[-1]
    assert "neu starten" in view.errors[-1]
    assert "Administratorrechte sind nicht erforderlich" in view.errors[-1]


def test_restarting_stopped_duplicate_updates_only_its_queue_entry(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)

    controller.deck_action("A", "play")
    controller.deck_action("B", "play")
    controller.deck_action("A", "stop")
    assert [f"{entry.status}" for entry in view.queue] == ["played", "playing"]

    controller.deck_action("A", "play")
    assert [f"{entry.status}" for entry in view.queue] == ["playing", "playing"]


def test_stopping_loaded_deck_keeps_queue_ready_and_is_idempotent(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)

    assert controller.deck_a.model.state == DeckState.LOADED
    assert [f"{entry.status}" for entry in view.queue] == ["ready"]

    controller.deck_action("A", "stop")
    controller.deck_action("A", "stop")

    assert controller.deck_a.model.state == DeckState.STOPPED
    assert [f"{entry.status}" for entry in view.queue] == ["ready"]
    assert not view.errors


def test_stopping_paused_deck_finishes_played_queue_entry(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.deck_action("A", "play")
    controller.deck_action("A", "pause")

    controller.deck_action("A", "stop")

    assert controller.deck_a.model.state == DeckState.STOPPED
    assert [f"{entry.status}" for entry in view.queue] == ["played"]
    assert not view.errors


def test_automatic_queue_starts_and_begins_crossfade_near_track_end(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)

    controller.start_automatic_queue()
    assert controller.deck_a.model.state.value == "playing"
    backend_a = controller.deck_a.backend
    assert isinstance(backend_a, FakeAudioBackend)
    backend_a.position = backend_a.duration - 8
    status_tick = pop_scheduled(view, "_status_tick")
    status_tick()
    assert controller.deck_b.model.state.value == "loaded"

    backend_a.position = backend_a.duration - 7
    status_tick = pop_scheduled(view, "_status_tick")
    status_tick()

    assert controller.deck_b.model.state.value == "playing"
    assert controller.player_mode.value == "automatic"


def test_automatic_start_ignores_first_skipped_entry_and_loads_next_waiting_track(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    first, second = controller._queue_service.entries()
    controller.mark_queue_track_skipped(first.queue_id, "Operator")

    controller.start_automatic_queue()

    skipped = controller._queue_service.entry(first.queue_id)
    next_entry = controller._queue_service.entry(second.queue_id)
    assert skipped is not None and skipped.status == QueueStatus.SKIPPED
    assert next_entry is not None and next_entry.status in {
        QueueStatus.READY,
        QueueStatus.PLAYING,
    }
    assert any(
        deck.model.loaded_track is not None and deck.model.loaded_track.id == 2
        for deck in (controller.deck_a, controller.deck_b)
    )


def test_changed_future_cue_out_reschedules_automatic_transition(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    track = controller.deck_a.model.loaded_track
    assert track is not None and controller._cue_points is not None
    controller._cue_points.save_manual(track, None, 70.0, 7.0)
    controller._apply_cue_points(controller.deck_a)

    controller.deck_a.model.position = 62.0
    controller._automatic_playback_tick()
    assert controller.deck_b.model.state.value == "loaded"

    controller.deck_a.model.position = 63.0
    controller._automatic_playback_tick()
    assert controller.deck_b.model.state.value == "playing"


def test_loaded_deck_resolves_cues_from_its_assigned_queue_entry(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    queue_id = controller._deck_queue_ids["A"]
    assert queue_id is not None
    controller._queue_service.set_cue_overrides(queue_id, 3.0, 100.0, 8.0, "snapshot")

    controller._apply_cue_points(controller.deck_a)

    assert controller.deck_a.model.cue_in == 3.0
    assert controller.deck_a.model.cue_out == 100.0
    assert controller.deck_a.model.cue_fade_duration == 8.0
    assert controller.deck_a.model.cue_in_source == "QUEUE_SNAPSHOT"


def test_waiting_queue_cues_are_edited_without_changing_global_track_cues(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]

    saved = controller.save_queue_cues(entry.queue_id, 2.0, 110.0, 6.0)

    restored = controller._queue_service.entry(entry.queue_id)
    assert restored is not None
    assert (restored.cue_in_override, restored.cue_out_override) == (2.0, 110.0)
    assert restored.fade_duration_override == 6.0
    assert restored.cue_override_source == "queue"
    assert saved.resolved.cue_in_source == "QUEUE_OVERRIDE"
    assert controller._cue_points is not None
    global_cues = controller._cue_points.get(entry.track_id)
    assert global_cues.manual_cue_in is None
    assert global_cues.manual_cue_out is None
    assert global_cues.manual_fade_duration is None


def test_non_waiting_queue_cues_cannot_be_edited(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    controller._queue_service.mark_played(entry.queue_id)

    with pytest.raises(ValueError, match="nicht mehr"):
        controller.save_queue_cues(entry.queue_id, 2.0, 110.0, 6.0)


def test_loaded_queue_cue_change_requires_confirmation_and_updates_deck(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    assert entry.status == QueueStatus.LOADED

    controller.save_queue_cues(entry.queue_id, 2.0, 110.0, 6.0)

    assert controller.deck_a.model.cue_in == 2.0
    assert controller.deck_a.model.cue_out == 110.0
    view.confirm_queue_cues = False
    with pytest.raises(ValueError, match="abgebrochen"):
        controller.save_queue_cues(entry.queue_id, 3.0, 100.0, 5.0)


def test_too_late_running_queue_cue_change_is_saved_only_for_next_playback(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    entry = controller._queue_service.entries()[0]
    controller.deck_a.model.position = 119.0
    previous_cue_out = controller.deck_a.model.cue_out

    controller.save_queue_cues(entry.queue_id, None, 110.0, 6.0)

    stored = controller._queue_service.entry(entry.queue_id)
    assert stored is not None and stored.cue_out_override == 110.0
    assert controller.deck_a.model.cue_out == previous_cue_out
    assert "nächsten Wiedergabe" in controller.deck_a.model.cue_warning


def test_queue_editor_can_adopt_title_values_and_reset_to_inheritance(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    assert controller._cue_points is not None
    track = controller._queue_service.track(entry.track_id)
    assert track is not None
    controller._cue_points.save_manual(track, 2.0, 110.0, 6.0)

    adopted = controller.adopt_title_cues_for_queue(entry.queue_id)

    assert (adopted.cue_in_override, adopted.cue_out_override) == (2.0, 110.0)
    assert adopted.fade_duration_override == 6.0
    stored = controller._queue_service.entry(entry.queue_id)
    assert stored is not None and stored.cue_override_source == "queue"

    reset = controller.reset_queue_cues(entry.queue_id)

    assert reset.cue_in_override is None
    assert reset.cue_out_override is None
    assert reset.fade_duration_override is None
    assert reset.resolved.cue_in == 2.0
    assert reset.resolved.cue_in_source == "MANUAL"
    stored = controller._queue_service.entry(entry.queue_id)
    assert stored is not None and stored.cue_override_source == "inherited"


def test_automatic_transition_uses_deck_snapshot_without_cue_database_query(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    assert controller._cue_points is not None
    controller.deck_a.model.position = 113.0
    monkeypatch.setattr(
        controller._cue_points.repository,
        "get",
        lambda _track_id: (_ for _ in ()).throw(AssertionError("unerwartete DB-Abfrage")),
    )

    controller._automatic_playback_tick()

    assert controller._transition.is_transitioning


def test_automatic_tick_skips_incoming_queue_query_before_crossfade(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    controller.deck_a.model.position = 1.0
    original_entry = controller._queue_service.entry
    entry_calls = 0

    def counted_entry(queue_id: int):
        nonlocal entry_calls
        entry_calls += 1
        return original_entry(queue_id)

    monkeypatch.setattr(controller._queue_service, "entry", counted_entry)

    controller._automatic_playback_tick()

    assert entry_calls == 0
    assert not controller._transition.is_transitioning


def test_cue_change_during_running_transition_applies_only_next_time(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    track = controller.deck_a.model.loaded_track
    assert track is not None and controller._cue_points is not None
    controller._cue_points.save_manual(track, None, 70.0, 7.0)
    controller._apply_cue_points(controller.deck_a)
    controller.deck_a.model.position = 63.0
    controller._automatic_playback_tick()
    assert controller._transition.is_transitioning

    controller._cue_points.save_manual(track, None, 100.0, 5.0)
    controller._apply_cue_points(controller.deck_a)
    controller._automatic_playback_tick()

    assert controller._transition.is_transitioning
    assert controller._resolved_boundaries(controller.deck_a).cue_out == 100.0


def test_cue_out_already_passed_uses_natural_end_fallback(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    track = controller.deck_a.model.loaded_track
    assert track is not None and controller._cue_points is not None
    controller._cue_points.save_manual(track, None, 50.0, 7.0)
    controller._apply_cue_points(controller.deck_a)
    controller.deck_a.model.position = 60.0

    controller._automatic_playback_tick()
    controller._automatic_playback_tick()

    assert controller.deck_b.model.state.value == "loaded"
    assert controller._cue_timing_warning == {"A": track.id}

    backend_a = controller.deck_a.backend
    assert isinstance(backend_a, FakeAudioBackend)
    backend_a.playing = False
    backend_a.finished = True
    backend_a.position = backend_a.duration
    status_tick = pop_scheduled(view, "_status_tick")
    status_tick()

    assert controller.deck_b.model.state == DeckState.PLAYING
    assert controller._automatic_run_active


def test_automatic_queue_resumes_after_second_deck_was_started_manually(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    controller.stop_automatic_queue()

    controller.deck_action("B", "play")
    assert controller.deck_a.backend.is_playing()
    assert controller.deck_b.backend.is_playing()

    controller.start_automatic_queue()

    assert controller._automatic_run_active
    assert controller._transition.is_transitioning


def test_manual_crossfader_movement_pauses_and_can_resume_automatic_transition(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    controller.deck_action("B", "play", automatic=True)
    controller._automatic_playback_tick()
    assert controller._transition.is_transitioning

    controller.set_crossfader(0.4)

    assert not controller._transition.is_transitioning
    assert not controller._automatic_run_active
    assert controller.is_automatic_queue_paused()
    assert controller.player_mode.value == "automatic"
    assert view.automatic_status[0] == "paused"
    assert view.automatic_status[1].startswith("Crossfader manuell bewegt")

    controller.start_automatic_queue()

    assert controller._automatic_run_active
    assert not controller.is_automatic_queue_paused()
    assert view.automatic_status[0] == "transition"


def test_automatic_status_shows_next_remaining_and_repetition_skips(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    entries = controller._queue_service.entries()
    controller._queue_service.mark_skipped(
        entries[2].queue_id,
        "Titel liegt noch innerhalb des Wiederholungsschutzes",
        code="TRACK_REPETITION",
    )

    controller._refresh_queue()

    assert view.automatic_status[0] == "ready"
    assert "Nächster: Song" in view.automatic_status[1]
    assert "2 Titel" in view.automatic_status[1]
    assert "1 übersprungen (1 Wiederholungsschutz)" in view.automatic_status[1]


def test_automatic_status_identifies_running_transition(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    controller.deck_action("B", "play", automatic=True)

    controller._automatic_playback_tick()
    controller._show_automatic_status()

    assert controller._transition.is_transitioning
    assert view.automatic_status[0] == "transition"


def test_unchanged_crossfader_callback_does_not_pause_automatic_queue(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()

    controller.set_crossfader(controller.crossfader.position)

    assert controller._automatic_run_active
    assert not controller.is_automatic_queue_paused()


def test_explicit_pause_stops_audio_and_can_resume(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    assert controller.deck_a.backend.is_playing()

    controller.pause_automatic_queue()

    assert not controller.deck_a.backend.is_playing()
    assert controller.deck_a.model.state == DeckState.PAUSED
    assert controller.is_automatic_queue_paused()
    assert not controller._automatic_run_active
    assert view.automatic_status[0] == "paused"

    controller.start_automatic_queue()

    assert controller.deck_a.backend.is_playing()
    assert controller.deck_a.model.state == DeckState.PLAYING
    assert controller._automatic_run_active
    assert not controller.is_automatic_queue_paused()


def test_pause_resume_preserves_order_and_writes_each_history_once(tmp_path: Path) -> None:
    executor = ManualExecutor()
    controller, view = build_controller(
        tmp_path,
        track_count=2,
        persistence_executor=executor,
        with_history=True,
    )
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    controller.start_automatic_queue()
    controller.pause_automatic_queue()
    controller.start_automatic_queue()
    played: list[int] = []

    for expected_track_id in (1, 2):
        deck = next(
            deck
            for deck in (controller.deck_a, controller.deck_b)
            if deck.model.state == DeckState.PLAYING
        )
        assert deck.model.loaded_track is not None
        played.append(deck.model.loaded_track.id)
        assert deck.model.loaded_track.id == expected_track_id
        backend = deck.backend
        assert isinstance(backend, FakeAudioBackend)
        backend.playing = False
        backend.finished = True
        backend.position = backend.duration
        deck.update_status()
        controller._schedule_finished_deck_completion(deck)
        controller._automatic_playback_tick()
        pop_scheduled(view, f"finish_deck_{deck.model.deck_id.lower()}")()
        executor.run_all()
        if expected_track_id == 1:
            pop_scheduled(view, "_auto_load")()

    with controller._queue_service._repository._database.connect() as connection:  # noqa: SLF001
        history = connection.execute(
            """SELECT track_id, completion_status
               FROM play_history ORDER BY id"""
        ).fetchall()
    assert played == [1, 2]
    assert [row["track_id"] for row in history] == [1, 2]
    assert len(history) == len(played)
    assert all(row["completion_status"] == "PARTIALLY_PLAYED" for row in history)
    assert view.automatic_status[0] == "completed"


def test_regular_end_waits_until_rule_block_is_visible_and_audited(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)

    def evaluate(entry: QueueEntry, _track: Track | None) -> SelectionDecision:
        if entry.track_id == 2:
            return SelectionDecision.reject(
                "TRACK_REPETITION",
                reason="Titel liegt innerhalb des Wiederholungsschutzes",
            )
        return SelectionDecision.allow()

    monkeypatch.setattr(controller._queue_service._selection_service, "evaluate", evaluate)
    controller.start_automatic_queue()
    playing = next(
        deck
        for deck in (controller.deck_a, controller.deck_b)
        if deck.model.state == DeckState.PLAYING
    )
    backend = playing.backend
    assert isinstance(backend, FakeAudioBackend)
    backend.playing = False
    backend.finished = True
    backend.position = backend.duration
    playing.update_status()
    controller._schedule_finished_deck_completion(playing)
    controller._automatic_playback_tick()
    pop_scheduled(view, f"finish_deck_{playing.model.deck_id.lower()}")()

    entries = controller._queue_service.entries()
    blocked = next(entry for entry in entries if entry.track_id == 2)
    assert blocked.status == QueueStatus.SKIPPED
    assert blocked.skip_code == "TRACK_REPETITION"
    assert "Wiederholungsschutz" in (blocked.skip_reason or "")
    assert view.automatic_status[0] == "completed"
    assert "1 übersprungen" in view.automatic_status[1]
    assert "1 Wiederholungsschutz" in view.automatic_status[1]
    with controller._queue_service._repository._database.connect() as connection:  # noqa: SLF001
        audit = connection.execute(
            """SELECT details FROM session_audit_events
               WHERE event_code = 'QUEUE_SKIPPED' AND entity_id = ?""",
            (blocked.queue_id,),
        ).fetchone()
    assert audit is not None
    assert '"skip_code": "TRACK_REPETITION"' in audit["details"]


def test_deck_pause_pauses_and_deck_resume_continues_automatic_queue(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()

    controller.deck_action("A", "pause")

    assert controller.deck_a.model.state == DeckState.PAUSED
    assert controller.is_automatic_queue_paused()
    assert not controller._automatic_run_active
    assert controller.player_mode.value == "automatic"
    assert view.automatic_status[0] == "paused"
    assert view.automatic_status[1].startswith("Deck A pausiert")

    controller.deck_action("A", "resume")

    assert controller.deck_a.model.state == DeckState.PLAYING
    assert controller._automatic_run_active
    assert not controller.is_automatic_queue_paused()
    assert controller.player_mode.value == "automatic"


def test_explicit_stop_and_regular_queue_end_have_distinct_statuses(tmp_path: Path) -> None:
    stopped_path = tmp_path / "stopped"
    completed_path = tmp_path / "completed"
    stopped_path.mkdir()
    completed_path.mkdir()
    stopped, stopped_view = build_controller(stopped_path)
    stopped.initialize()
    stopped.add_catalog_track_to_queue(1)
    stopped.start_automatic_queue()

    stopped.stop_automatic_queue()

    assert stopped_view.automatic_status[0] == "stopped"
    assert "Vom Benutzer beendet" in stopped_view.automatic_status[1]

    completed, completed_view = build_controller(completed_path)
    completed.initialize()
    completed.add_catalog_track_to_queue(1)
    completed.start_automatic_queue()
    backend = completed.deck_a.backend
    assert isinstance(backend, FakeAudioBackend)
    backend.playing = False
    backend.finished = True
    backend.position = backend.duration

    status_tick = pop_scheduled(completed_view, "_status_tick")
    status_tick()

    assert completed_view.automatic_status[0] == "completed"
    assert "Queue vollständig abgespielt" in completed_view.automatic_status[1]


def test_automatic_start_with_empty_queue_stays_ready_and_explains_problem(
    tmp_path: Path,
) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()

    controller.start_automatic_queue()

    assert not controller._automatic_run_active
    assert controller.player_mode.value != "automatic"
    assert view.automatic_status[0] == "ready"
    assert view.automatic_status[1].startswith("Queue ist leer")
    assert view.automatic_status[1] == "Queue ist leer"
    assert "keinen Titel" in view.queue_warnings[-1]


def test_twelve_track_automatic_queue_plays_once_in_order_before_regular_end(
    tmp_path: Path,
) -> None:
    executor = ManualExecutor()
    controller, view = build_controller(
        tmp_path,
        track_count=12,
        persistence_executor=executor,
        with_history=True,
    )
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    for track_id in range(1, 13):
        controller.add_catalog_track_to_queue(track_id)
    controller.start_automatic_queue()
    played_track_ids: list[int] = []

    for expected_track_id in range(1, 13):
        playing = [
            deck
            for deck in (controller.deck_a, controller.deck_b)
            if deck.model.state == DeckState.PLAYING
        ]
        assert len(playing) == 1
        deck = playing[0]
        track = deck.model.loaded_track
        assert track is not None
        played_track_ids.append(track.id)
        assert track.id == expected_track_id
        if expected_track_id < 12:
            assert controller._automatic_run_active
            assert view.automatic_status[0] != "completed"

        backend = deck.backend
        assert isinstance(backend, FakeAudioBackend)
        backend.playing = False
        backend.finished = True
        backend.position = backend.duration
        deck.update_status()
        controller._schedule_finished_deck_completion(deck)
        controller._automatic_playback_tick()
        pop_scheduled(view, f"finish_deck_{deck.model.deck_id.lower()}")()
        executor.run_all()
        if expected_track_id < 12:
            pop_scheduled(view, "_auto_load")()

    assert played_track_ids == list(range(1, 13))
    assert not controller._automatic_run_active
    assert view.automatic_status[0] == "completed"
    assert "Queue vollständig abgespielt" in view.automatic_status[1]
    entries = controller._queue_service.entries()
    assert [entry.track_id for entry in entries] == list(range(1, 13))
    assert all(entry.status == QueueStatus.PLAYED for entry in entries)


def test_manual_seek_keeps_automatic_runner_active(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()

    controller.seek("A", 30)

    assert controller._automatic_run_active
    assert controller.player_mode.value == "automatic"


def test_automatic_runner_starts_loaded_second_deck_after_first_finishes(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    controller.start_automatic_queue()
    backend_a = controller.deck_a.backend
    assert isinstance(backend_a, FakeAudioBackend)
    backend_a.playing = False
    backend_a.finished = True
    backend_a.position = backend_a.duration

    status_tick = pop_scheduled(view, "_status_tick")
    status_tick()

    assert controller.deck_b.model.state.value == "playing"
    assert controller._automatic_run_active


def test_worker_queue_update_is_applied_only_by_gui_dispatcher(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    waiting = view.queue[0]
    playing = replace(waiting, status=QueueStatus.PLAYING)
    update = QueueViewUpdate(
        (
            QueueViewEvent(
                QueueViewEventType.ENTRY_STATUS_CHANGED,
                waiting.queue_id,
                controller._queue_revision + 1,
                0,
            ),
        ),
        (playing,),
        dict(controller._queue_tracks_cache),
    )

    worker = Thread(target=lambda: controller._deliver_queue_view_update(update))
    worker.start()
    worker.join()

    assert view.queue[0].status == QueueStatus.WAITING
    assert controller._gui_dispatcher.statistics().pending == 1

    controller._drain_background_callbacks()

    assert view.queue[0].status == QueueStatus.PLAYING


def test_queue_selection_publishes_targeted_revisioned_events(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.set_automatic_deck_loading(False)
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(1)
    first, second = view.queue
    view.queue_events.clear()

    controller.select_queue_entry(first.queue_id)
    first_revision = controller._queue_revision
    controller.select_queue_entry(second.queue_id)

    selection_events = [
        event
        for event in view.queue_events
        if event.event_type == QueueViewEventType.SELECTION_CHANGED
    ]
    assert [(event.queue_entry_id, event.selected) for event in selection_events] == [
        (first.queue_id, True),
        (first.queue_id, False),
        (second.queue_id, True),
    ]
    assert first_revision < controller._queue_revision


def test_switching_to_automatic_ignores_selected_waiting_track(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in (1, 2, 3):
        controller.add_catalog_track_to_queue(track_id)
    first, selected, following = controller._queue_service.entries()
    controller.select_queue_entry(selected.queue_id)

    controller.set_player_mode("automatic")

    assert controller._automatic_run_active
    assert controller.player_mode.value == "automatic"
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == first.track_id
    assert controller.deck_a.model.state == DeckState.PLAYING
    assert controller.deck_b.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track.id == selected.track_id
    first_entry = controller._queue_service.entry(first.queue_id)
    following_entry = controller._queue_service.entry(following.queue_id)
    assert first_entry is not None and first_entry.status == QueueStatus.PLAYING
    assert following_entry is not None and following_entry.status == QueueStatus.WAITING


def test_selected_automatic_start_replaces_earlier_preloaded_tracks(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    for track_id in (1, 2, 3):
        controller.add_catalog_track_to_queue(track_id)
    entries = controller._queue_service.entries()
    selected = entries[2]
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track is not None
    controller.select_queue_entry(selected.queue_id)

    controller.start_automatic_queue(from_selected=True)

    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == selected.track_id
    assert controller.deck_a.model.state == DeckState.PLAYING
    earlier = [controller._queue_service.entry(entry.queue_id) for entry in entries[:2]]
    assert all(entry is not None and entry.status == QueueStatus.SKIPPED for entry in earlier)


def test_selected_waiting_queue_track_can_be_deleted(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=2)
    controller.initialize()
    controller.set_player_mode("manual")
    controller.add_catalog_track_to_queue(1)
    controller.add_catalog_track_to_queue(2)
    first, selected = controller._queue_service.entries()
    controller.select_queue_entry(selected.queue_id)

    assert controller.remove_selected_queue_track()

    assert [entry.queue_id for entry in controller._queue_service.entries()] == [first.queue_id]
    assert controller._selected_queue_entry_id is None


def test_selected_prepared_queue_track_is_unloaded_and_deleted(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    selected = controller._queue_service.entries()[0]
    assert selected.status == QueueStatus.READY
    controller.select_queue_entry(selected.queue_id)

    assert controller.remove_selected_queue_track()

    assert controller._queue_service.entries() == []
    assert controller.deck_a.model.loaded_track is None
    assert controller._deck_queue_ids["A"] is None


def test_selected_playing_queue_track_remains_protected(tmp_path: Path) -> None:
    controller, view = build_controller(tmp_path)
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    selected = controller._queue_service.entries()[0]
    controller.deck_action("A", "play")
    controller.select_queue_entry(selected.queue_id)

    assert not controller.remove_selected_queue_track()

    remaining = controller._queue_service.entry(selected.queue_id)
    assert remaining is not None
    assert remaining.status == QueueStatus.PLAYING
    assert "aktuell spielende" in view.queue_warnings[-1]


def test_two_selected_automatic_restart_cycles_keep_refilling_free_deck(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=6)
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in range(1, 7):
        controller.add_catalog_track_to_queue(track_id)
    entries = controller._queue_service.entries()

    controller.start_automatic_queue()
    assert controller.deck_a.model.state == DeckState.PLAYING
    controller.select_queue_entry(entries[2].queue_id)
    controller.stop_automatic_queue()
    controller.start_automatic_queue(from_selected=True)
    assert controller.deck_b.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track.id == 3

    outgoing_track = controller.deck_a.model.loaded_track
    outgoing_queue_id = controller._deck_queue_ids["A"]
    assert outgoing_track is not None and outgoing_queue_id is not None
    controller.deck_action("B", "play", automatic=True)
    assert controller._deck_queue_ids["B"] == entries[2].queue_id
    controller._complete_automatic_transition(
        controller.deck_a,
        outgoing_track.id,
        outgoing_queue_id,
    )
    for _ in range(200):
        controller._drain_background_callbacks()
        if controller.deck_a.model.loaded_track is not None:
            break
        sleep(0.01)
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 4

    controller.select_queue_entry(entries[4].queue_id)
    controller.stop_automatic_queue()
    controller.start_automatic_queue(from_selected=True)
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 5

    outgoing_track = controller.deck_b.model.loaded_track
    outgoing_queue_id = controller._deck_queue_ids["B"]
    assert outgoing_track is not None and outgoing_queue_id is not None
    controller.deck_action("A", "play", automatic=True)
    controller._complete_automatic_transition(
        controller.deck_b,
        outgoing_track.id,
        outgoing_queue_id,
    )
    for _ in range(200):
        controller._drain_background_callbacks()
        if controller.deck_b.model.loaded_track is not None:
            break
        sleep(0.01)

    assert controller.deck_b.model.loaded_track is not None
    assert controller.deck_b.model.loaded_track.id == 6


def test_automatic_start_ignores_incidental_queue_selection(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    entries = controller._queue_service.entries()
    controller.select_queue_entry(entries[2].queue_id)

    controller.start_automatic_queue()

    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 1
    stored = {entry.queue_id: entry for entry in controller._queue_service.entries()}
    assert stored[entries[0].queue_id].status == QueueStatus.PLAYING
    assert stored[entries[1].queue_id].status == QueueStatus.READY
    assert stored[entries[2].queue_id].status == QueueStatus.WAITING


def test_automatic_start_from_selection_requires_explicit_request(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    entries = controller._queue_service.entries()
    controller.select_queue_entry(entries[2].queue_id)

    controller.start_automatic_queue(from_selected=True)

    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 3
    stored = {entry.queue_id: entry for entry in controller._queue_service.entries()}
    assert stored[entries[0].queue_id].status == QueueStatus.SKIPPED
    assert stored[entries[1].queue_id].status == QueueStatus.SKIPPED
    assert stored[entries[2].queue_id].status == QueueStatus.PLAYING


def test_automatic_start_from_selection_can_keep_earlier_titles_waiting(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    entries = controller._queue_service.entries()
    controller.select_queue_entry(entries[2].queue_id)

    controller.start_automatic_queue(from_selected=True, skip_earlier=False)

    current = controller._queue_service.entries()
    assert controller.deck_a.model.loaded_track is not None
    assert controller.deck_a.model.loaded_track.id == 3
    assert current[0].queue_id == entries[2].queue_id
    stored = {entry.queue_id: entry for entry in current}
    assert stored[entries[0].queue_id].status == QueueStatus.READY
    assert stored[entries[1].queue_id].status == QueueStatus.WAITING
    assert stored[entries[2].queue_id].status == QueueStatus.PLAYING
    assert all(entry.status != QueueStatus.SKIPPED for entry in current)


def test_automatic_start_reports_when_selection_would_skip_waiting_entries(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)
    entries = controller._queue_service.entries()

    assert not controller.automatic_start_has_earlier_waiting_entries()
    controller.select_queue_entry(entries[2].queue_id)
    assert controller.automatic_start_has_earlier_waiting_entries()
    controller.select_queue_entry(entries[0].queue_id)
    assert not controller.automatic_start_has_earlier_waiting_entries()


def test_automatic_start_summary_lists_start_counts_and_rule_blocks(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=3)
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in range(1, 4):
        controller.add_catalog_track_to_queue(track_id)

    def previews(
        entries: list[QueueEntry],
    ) -> dict[int, tuple[Track | None, SimpleNamespace]]:
        return {
            entry.queue_id: (
                controller._queue_service.track(entry.track_id),
                SimpleNamespace(
                    accepted=entry.track_id != 2,
                    reason=(
                        "Titel liegt noch innerhalb des Wiederholungsschutzes"
                        if entry.track_id == 2
                        else ""
                    ),
                    code="TRACK_REPETITION" if entry.track_id == 2 else "",
                ),
            )
            for entry in entries
        }

    monkeypatch.setattr(controller._queue_service, "preview_candidate_decisions", previews)

    summary = controller.automatic_start_summary()

    assert "Starttitel: Song" in summary
    assert "Wartend/vorbereitet: 3" in summary
    assert "Voraussichtlich spielbar: 2" in summary
    assert "Durch Regeln blockiert: 1" in summary
    assert "Song 002: Titel liegt noch innerhalb des Wiederholungsschutzes" in summary


def test_automatic_start_summary_reuses_one_connection_for_candidate_rules(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    controller, _view = build_controller(tmp_path, track_count=18)
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in range(1, 19):
        controller.add_catalog_track_to_queue(track_id)
    database = controller._queue_service._repository._database
    original_open = database._open_connection
    opened = 0

    def counted_open() -> object:
        nonlocal opened
        opened += 1
        return original_open()

    monkeypatch.setattr(database, "_open_connection", counted_open)

    summary = controller.automatic_start_summary()

    assert "Wartend/vorbereitet: 18" in summary
    assert opened == 2


def test_background_preload_survives_two_selected_automatic_restart_cycles(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(
        tmp_path,
        track_count=6,
        background_preload=True,
    )
    controller.initialize()
    controller.set_player_mode("manual")
    for track_id in range(1, 7):
        controller.add_catalog_track_to_queue(track_id)
    entries = controller._queue_service.entries()

    def drain_until(predicate: Callable[[], bool]) -> None:
        for _ in range(500):
            controller._drain_background_callbacks()
            if predicate():
                return
            sleep(0.01)
        pytest.fail("Hintergrund-Preload erreichte den erwarteten Zustand nicht")

    controller.start_automatic_queue()
    drain_until(
        lambda: (
            controller.deck_a.model.state == DeckState.PLAYING
            and controller.deck_b.model.loaded_track is not None
            and controller.deck_b.model.loaded_track.id == 2
            and controller._deck_queue_ids["B"] == entries[1].queue_id
            and not controller._preload_in_progress
            and (
                (stored := controller._queue_service.entry(entries[1].queue_id)) is not None
                and stored.status == QueueStatus.READY
            )
        )
    )
    controller.select_queue_entry(entries[2].queue_id)
    controller.stop_automatic_queue()
    controller.start_automatic_queue(from_selected=True)
    drain_until(
        lambda: (
            controller.deck_b.model.loaded_track is not None
            and controller.deck_b.model.loaded_track.id == 3
            and controller._deck_queue_ids["B"] == entries[2].queue_id
            and not controller._preload_in_progress
            and (
                (stored := controller._queue_service.entry(entries[2].queue_id)) is not None
                and stored.status == QueueStatus.READY
            )
        )
    )

    outgoing_track = controller.deck_a.model.loaded_track
    outgoing_queue_id = controller._deck_queue_ids["A"]
    assert outgoing_track is not None and outgoing_queue_id is not None
    controller.deck_action("B", "play", automatic=True)
    assert controller._deck_queue_ids["B"] == entries[2].queue_id
    controller._complete_automatic_transition(
        controller.deck_a,
        outgoing_track.id,
        outgoing_queue_id,
    )
    drain_until(
        lambda: (
            controller.deck_a.model.loaded_track is not None
            and controller.deck_a.model.loaded_track.id == 4
            and controller._deck_queue_ids["A"] == entries[3].queue_id
            and not controller._preload_in_progress
            and (
                (stored := controller._queue_service.entry(entries[3].queue_id)) is not None
                and stored.status == QueueStatus.READY
            )
        )
    )

    controller.select_queue_entry(entries[4].queue_id)
    controller.stop_automatic_queue()
    controller.start_automatic_queue(from_selected=True)
    drain_until(
        lambda: (
            controller.deck_a.model.loaded_track is not None
            and controller.deck_a.model.loaded_track.id == 5
            and controller._deck_queue_ids["A"] == entries[4].queue_id
            and not controller._preload_in_progress
            and (
                (stored := controller._queue_service.entry(entries[4].queue_id)) is not None
                and stored.status == QueueStatus.READY
            )
        )
    )
    assert controller._deck_queue_ids["B"] == entries[2].queue_id

    outgoing_track = controller.deck_b.model.loaded_track
    outgoing_queue_id = controller._deck_queue_ids["B"]
    assert outgoing_track is not None and outgoing_queue_id is not None
    controller.deck_action("A", "play", automatic=True)
    controller._complete_automatic_transition(
        controller.deck_b,
        outgoing_track.id,
        outgoing_queue_id,
    )
    drain_until(
        lambda: (
            controller.deck_b.model.loaded_track is not None
            and controller.deck_b.model.loaded_track.id == 6
            and controller._deck_queue_ids["B"] == entries[5].queue_id
            and not controller._preload_in_progress
            and (
                (stored := controller._queue_service.entry(entries[5].queue_id)) is not None
                and stored.status == QueueStatus.READY
            )
        )
    )


def test_automatic_runner_waits_for_queue_ready_commit_before_starting_deck(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller.initialize()
    controller.set_player_mode("manual")
    controller.add_catalog_track_to_queue(1)
    entry = controller._queue_service.entries()[0]
    track = controller._queue_service.track(entry.track_id)
    assert track is not None
    controller.deck_a.load(track)
    controller._deck_queue_ids["A"] = entry.queue_id
    controller._automatic_run_active = True

    controller._automatic_playback_tick()

    assert controller.deck_a.model.state == DeckState.LOADED
    assert controller._deck_queue_ids["A"] == entry.queue_id

    controller._queue_service.mark_loaded(entry.queue_id, "A")
    controller._automatic_playback_tick()

    assert controller.deck_a.model.state == DeckState.PLAYING
    assert controller._deck_queue_ids["A"] == entry.queue_id
