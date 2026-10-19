"""Shared support for main-controller tests."""

from concurrent.futures import Executor
from pathlib import Path
from typing import Callable

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.controllers.main_controller import (
    EmergencyDashboardViewModel,
    MainController,
    RecoveryReturnRequirement,
)
from party_player.crossfader_service import CrossfaderService
from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.deck_controller import DeckController
from party_player.equalizer_resolver import EqualizerResolver
from party_player.loudness import LoudnessRepository, LoudnessService
from party_player.models import Deck, QueueEntry, QueueStats, SavedQueue, Track
from party_player.performance_monitor import PerformanceSettings
from party_player.playback_history_service import PlaybackHistoryService
from party_player.queue_service import QueueService
from party_player.queue_view_events import QueueViewEvent, QueueViewEventType
from party_player.repositories.equalizer_repository import (
    EqualizerAssignmentRepository,
    EqualizerPresetRepository,
)
from party_player.repositories.saved_queue_repository import SavedQueueRepository
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.saved_queue_service import SavedQueueService
from party_player.services.library_service import LibraryService


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
