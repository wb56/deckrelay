from pathlib import Path
from threading import Event, enumerate as enumerate_threads
from time import monotonic
from collections.abc import Iterable, Sequence
from unittest.mock import Mock

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.controllers.cue_point_controller import CuePointController, CuePointEditorState
from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.deck_controller import DeckController
from party_player.repositories.track_repository import TrackRepository
from party_player.services.library_service import LibraryService
from party_player.gui_event_dispatcher import GuiEventDispatcher
from party_player.analysis import (
    AnalysisSegment,
    AudioFileInfo,
    CancellationToken,
    CueAnalysisResult,
    CueAnalysisService,
    PcmChunk,
    SignalDetectionSettings,
)


class EditorAnalysisBackend:
    name = "editor-fake"

    def is_available(self) -> bool:
        return True

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".mp3"})

    def probe(self, file_path: Path) -> AudioFileInfo:
        del file_path
        return AudioFileInfo(250.0, 10, 1)

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        del file_path
        for segment in segments:
            if cancellation.is_set():
                return
            yield PcmChunk(
                segment.start_seconds,
                10,
                1,
                (0.5,) * round(segment.duration_seconds * 10),
            )


class BlockingEditorAnalysisBackend(EditorAnalysisBackend):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.decode_calls = 0

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        self.decode_calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        yield from super().decode_segments(file_path, segments, cancellation)


class UnavailableAnalysisBackend(EditorAnalysisBackend):
    def is_available(self) -> bool:
        return False


class AlwaysFailingAnalysisBackend(EditorAnalysisBackend):
    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        del file_path, segments, cancellation
        raise RuntimeError("simulierter Decoderfehler")
        yield  # pragma: no cover


def _controller(tmp_path: Path) -> tuple[CuePointController, DeckController]:
    database = Database(tmp_path / "cue-controller.db")
    migrate(database)
    tracks = TrackRepository(database)
    track = tracks.upsert_file("song.mp3", "Song", "Artist", "", 250.0)
    service = CuePointService(CuePointRepository(database), 7.0)
    deck_a = DeckController("A", FakeAudioBackend(duration=250))
    deck_b = DeckController("B", FakeAudioBackend(duration=250))
    controller = CuePointController(service, LibraryService(tracks), deck_a, deck_b, 7.0)
    deck_a.load(track, validate_file=False)
    return controller, deck_a


def test_editor_state_uses_german_source_names(tmp_path: Path) -> None:
    controller, _deck = _controller(tmp_path)

    initial = controller.state(1)
    saved = controller.save(1, 1.8, 242.0, 7.0)

    assert initial.cue_in_source_text == "Dateigrenze"
    assert saved.cue_in_source_text == "Manuell festgelegt"
    assert saved.cue_out_source_text == "Manuell festgelegt"
    assert saved.resolved.crossfade_start == 235.0
    assert _deck.model.cue_in == 1.8
    assert _deck.model.cue_out == 242.0
    assert _deck.model.cue_fade_duration == 7.0
    assert _deck.model.cue_in_source == "MANUAL"
    assert _deck.model.cue_warning == ""


def test_current_deck_position_can_be_used_by_editor(tmp_path: Path) -> None:
    controller, deck = _controller(tmp_path)
    deck.seek(17.25)

    assert controller.current_position(1) == 17.25


def test_editor_reset_is_persisted_only_when_saved(tmp_path: Path) -> None:
    controller, _deck = _controller(tmp_path)
    controller.save(1, 2.0, 240.0, 6.0)

    reset = controller.save(1, None, None, None)

    assert reset.manual_cue_in is None
    assert reset.manual_cue_out is None
    assert reset.manual_fade_duration is None
    assert reset.cue_in_source_text == "Dateigrenze"


def test_async_save_persists_then_applies_loaded_deck_state(tmp_path: Path) -> None:
    controller, deck = _controller(tmp_path)
    completed = Event()
    result: list[object] = []
    errors: list[Exception] = []

    future = controller.save_async(
        1,
        2.5,
        240.0,
        6.0,
        lambda state: (result.append(state), completed.set()),
        errors.append,
    )

    future.result(timeout=2)
    assert completed.wait(timeout=2)
    assert not errors
    assert result
    assert deck.model.cue_in == 2.5
    assert deck.model.cue_out == 240.0
    assert deck.model.cue_fade_duration == 6.0
    controller.close()


def test_close_forwards_non_blocking_shutdown_to_analysis_workers(tmp_path: Path) -> None:
    controller, _deck = _controller(tmp_path)
    original_preview_executor = controller._preview_executor
    original_preview_executor.shutdown(wait=True, cancel_futures=True)
    preview_executor = Mock()
    analysis_service = Mock()
    controller._preview_executor = preview_executor
    controller._analysis_service = analysis_service

    controller.close(wait=False)

    preview_executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    analysis_service.close.assert_called_once_with(wait=False)


def test_saved_cues_survive_controller_restart_and_individual_reset(tmp_path: Path) -> None:
    controller, _deck = _controller(tmp_path)
    controller.save(1, 2.0, 240.0, 6.0)

    restarted, _restarted_deck = _controller(tmp_path)
    persisted = restarted.state(1)
    assert (persisted.manual_cue_in, persisted.manual_cue_out) == (2.0, 240.0)
    assert persisted.manual_fade_duration == 6.0

    restarted.save(1, None, persisted.manual_cue_out, persisted.manual_fade_duration)
    after_second_restart, _deck_after_second_restart = _controller(tmp_path)
    reset = after_second_restart.state(1)
    assert reset.manual_cue_in is None
    assert reset.manual_cue_out == 240.0
    assert reset.manual_fade_duration == 6.0


def test_current_position_requires_loaded_track(tmp_path: Path) -> None:
    controller, deck = _controller(tmp_path)
    deck.eject()

    with pytest.raises(ValueError, match="keinem Deck"):
        controller.current_position(1)


def test_preview_uses_dedicated_backend_without_party_services(tmp_path: Path) -> None:
    base, deck = _controller(tmp_path)
    service = base._service
    library = base._library
    preview_backends: list[FakeAudioBackend] = []

    def backend_factory() -> FakeAudioBackend:
        backend = FakeAudioBackend(duration=250)
        preview_backends.append(backend)
        return backend

    controller = CuePointController(
        service,
        library,
        deck,
        DeckController("B", FakeAudioBackend()),
        7.0,
        preview_backend_factory=backend_factory,
        schedule=lambda _delay, callback: callback(),
    )
    controller.save(1, 1.8, 242.0, 7.0)
    started = Event()

    controller.preview_cue_in(
        1, lambda message: started.set() if message.startswith("Vorhören ab") else None
    )

    assert started.wait(timeout=1)
    assert preview_backends[0] is not deck.backend
    assert preview_backends[0].position == pytest.approx(1.8)
    assert preview_backends[0].playing
    controller.stop_preview()


def test_preview_close_restores_resources_without_changing_on_air_deck(
    tmp_path: Path,
) -> None:
    base, deck = _controller(tmp_path)
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        preview_backend_factory=lambda: FakeAudioBackend(duration=250),
        schedule=lambda _delay, callback: callback(),
    )
    controller.save(1, 1.0, 245.0, 6.0)
    deck.play()
    deck.seek(33.0)
    on_air_state = (
        deck.model.loaded_track,
        deck.model.state,
        deck.model.position,
        deck.backend.is_playing(),
    )
    existing_preview_threads = {
        thread.ident for thread in enumerate_threads() if thread.name.startswith("cue-preview")
    }
    started = Event()

    controller.preview_cue_in(
        1,
        lambda message: started.set() if message.startswith("Vorhören ab") else None,
    )
    assert started.wait(timeout=1)
    controller.close()

    assert (
        deck.model.loaded_track,
        deck.model.state,
        deck.model.position,
        deck.backend.is_playing(),
    ) == on_air_state
    leaked = [
        thread
        for thread in enumerate_threads()
        if thread.name.startswith("cue-preview") and thread.ident not in existing_preview_threads
    ]
    assert leaked == []


def test_repeated_preview_cycles_reuse_one_worker_and_leave_no_active_job(
    tmp_path: Path,
) -> None:
    base, deck = _controller(tmp_path)
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        preview_backend_factory=lambda: FakeAudioBackend(duration=250),
        schedule=lambda _delay, callback: callback(),
    )

    for _cycle in range(5):
        started = Event()
        controller.preview_cue_in(
            1,
            lambda message: started.set() if message.startswith("Vorhören ab") else None,
        )
        assert started.wait(timeout=1)
        controller.stop_preview()
        deadline = monotonic() + 1.0
        while controller.active_preview_count and monotonic() < deadline:
            Event().wait(0.01)
        assert controller.active_preview_count == 0

    preview_threads = [
        thread for thread in enumerate_threads() if thread.name.startswith("cue-preview")
    ]
    assert len(preview_threads) <= 1
    controller.close()


def test_cue_out_preview_starts_ten_seconds_before_end_point(tmp_path: Path) -> None:
    base, deck = _controller(tmp_path)
    preview_backends: list[FakeAudioBackend] = []

    def backend_factory() -> FakeAudioBackend:
        backend = FakeAudioBackend(duration=250)
        preview_backends.append(backend)
        return backend

    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        7.0,
        preview_backend_factory=backend_factory,
        schedule=lambda _delay, callback: callback(),
    )
    controller.save(1, 1.8, 242.0, 7.0)
    started = Event()

    controller.preview_cue_out(
        1, lambda message: started.set() if message.startswith("Vorhören ab") else None
    )

    assert started.wait(timeout=1)
    assert preview_backends[0].position == pytest.approx(232.0)
    assert preview_backends[0].playing
    controller.stop_preview()


def test_preview_worker_publishes_status_without_calling_tk_scheduler(tmp_path: Path) -> None:
    base, deck = _controller(tmp_path)
    dispatcher = GuiEventDispatcher()
    scheduler_called = Event()
    status_received = Event()
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        preview_backend_factory=lambda: FakeAudioBackend(duration=250),
        schedule=lambda _delay, _callback: scheduler_called.set(),
        gui_dispatcher=dispatcher,
    )

    controller.preview_cue_in(
        1, lambda message: status_received.set() if message.startswith("Vorhören ab") else None
    )
    for _attempt in range(20):
        dispatcher.process_pending_events(
            lambda event: event.payload() if callable(event.payload) else None
        )
        if status_received.wait(0.05):
            break

    assert status_received.is_set()
    assert not scheduler_called.is_set()
    controller.stop_preview()


def test_editor_can_start_single_analysis_and_receive_result_via_dispatcher(
    tmp_path: Path,
) -> None:
    base, deck = _controller(tmp_path)
    dispatcher = GuiEventDispatcher()
    analysis = CueAnalysisService(
        EditorAnalysisBackend(),
        base._service,
        signal_settings=SignalDetectionSettings(
            minimum_signal_seconds=0.1,
            minimum_silence_seconds=0.1,
        ),
        level_window_seconds=0.1,
    )
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        gui_dispatcher=dispatcher,
        analysis_service=analysis,
    )
    received: list[CueAnalysisResult] = []
    editor_states: list[CuePointEditorState] = []
    statuses: list[str] = []

    controller.analyze(
        1,
        received.append,
        statuses.append,
        state_completed=editor_states.append,
    )
    for _attempt in range(40):
        dispatcher.process_pending_events(
            lambda event: event.payload() if callable(event.payload) else None
        )
        if received:
            break
        Event().wait(0.025)

    controller.close()
    assert len(received) == 1
    assert len(editor_states) == 1
    assert editor_states[0].automatic_cue_out == 250.0
    assert received[0].backend_name == "editor-fake"
    assert any(message.startswith("Automatisch erkannt") for message in statuses)
    state = controller.state(1)
    assert state.resolved.cue_in_source == "AUTOMATIC"
    assert (state.automatic_cue_in, state.automatic_cue_out) == (0.0, 250.0)
    assert state.automatic_fade_duration == 7.0
    assert state.minimum_level_dbfs is not None
    assert state.maximum_level_dbfs is not None
    assert state.peak == 0.5
    assert state.confidence is not None
    assert state.analysis_version == "silence-v1"
    assert state.analysis_backend == "editor-fake"
    assert state.analysed_at is not None


def test_selected_track_batch_runs_serially_and_reports_progress(tmp_path: Path) -> None:
    base, deck = _controller(tmp_path)
    second = base._library._repository.upsert_file("second.mp3", "Second", "Artist", "", 250.0)
    dispatcher = GuiEventDispatcher()
    analysis = CueAnalysisService(
        EditorAnalysisBackend(),
        base._service,
        signal_settings=SignalDetectionSettings(
            minimum_signal_seconds=0.1,
            minimum_silence_seconds=0.1,
        ),
        level_window_seconds=0.1,
    )
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        gui_dispatcher=dispatcher,
        analysis_service=analysis,
    )
    progress: list[tuple[int, int, int, int]] = []
    completed: list[tuple[int, int]] = []

    controller.analyze_tracks(
        [1, second.id, 1],
        lambda *values: progress.append(values),
        lambda succeeded, failed: completed.append((succeeded, failed)),
    )
    for _attempt in range(80):
        dispatcher.process_pending_events(
            lambda event: event.payload() if callable(event.payload) else None
        )
        if completed:
            break
        Event().wait(0.025)

    controller.close()
    assert progress[0] == (0, 2, 0, 0)
    assert progress[-1] == (2, 2, 2, 0)
    assert completed == [(2, 0)]
    assert base._service.get(second.id).analysis_version == "silence-v1"


def test_cancelled_batch_is_not_counted_as_failure_and_starts_no_next_track(
    tmp_path: Path,
) -> None:
    base, deck = _controller(tmp_path)
    second = base._library._repository.upsert_file("second.mp3", "Second", "Artist", "", 250.0)
    dispatcher = GuiEventDispatcher()
    backend = BlockingEditorAnalysisBackend()
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        gui_dispatcher=dispatcher,
        analysis_service=CueAnalysisService(
            backend,
            base._service,
            signal_settings=SignalDetectionSettings(minimum_signal_seconds=0.1),
            level_window_seconds=0.1,
        ),
    )
    completed: list[tuple[int, int]] = []
    controller.analyze_tracks(
        [1, second.id],
        lambda *_values: None,
        lambda succeeded, failed: completed.append((succeeded, failed)),
    )
    assert backend.started.wait(timeout=1)

    controller.cancel_batch_analysis()
    backend.release.set()
    for _attempt in range(40):
        dispatcher.process_pending_events(
            lambda event: event.payload() if callable(event.payload) else None
        )
        if completed:
            break
        Event().wait(0.025)

    controller.close()
    assert completed == [(0, 0)]
    assert backend.decode_calls == 1


def test_automatic_suggestion_can_be_adopted_corrected_and_discarded(
    tmp_path: Path,
) -> None:
    base, deck = _controller(tmp_path)
    dispatcher = GuiEventDispatcher()
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        gui_dispatcher=dispatcher,
        analysis_service=CueAnalysisService(
            EditorAnalysisBackend(),
            base._service,
            signal_settings=SignalDetectionSettings(minimum_signal_seconds=0.1),
            level_window_seconds=0.1,
        ),
    )
    completed = Event()
    controller.analyze(1, lambda _result: completed.set())
    for _attempt in range(40):
        dispatcher.process_pending_events(
            lambda event: event.payload() if callable(event.payload) else None
        )
        if completed.wait(0.025):
            break

    adopted = controller.adopt_automatic(1)
    corrected = controller.save(1, 1.5, 245.0, 6.0)
    discarded = controller.discard_automatic(1)
    controller.close()

    assert (
        adopted.manual_cue_in,
        adopted.manual_cue_out,
        adopted.manual_fade_duration,
    ) == (0.0, 250.0, 7.0)
    assert (
        corrected.manual_cue_in,
        corrected.manual_cue_out,
        corrected.manual_fade_duration,
    ) == (1.5, 245.0, 6.0)
    assert discarded.automatic_cue_in is None
    assert discarded.automatic_cue_out is None
    assert discarded.analysis_version is None
    assert (
        discarded.manual_cue_in,
        discarded.manual_cue_out,
        discarded.manual_fade_duration,
    ) == (1.5, 245.0, 6.0)
    assert discarded.resolved.cue_in_source == "MANUAL"


def test_unavailable_backend_is_rejected_before_large_catalog_is_read(
    tmp_path: Path, monkeypatch
) -> None:
    base, deck = _controller(tmp_path)
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        analysis_service=CueAnalysisService(
            UnavailableAnalysisBackend(),
            base._service,
        ),
    )
    monkeypatch.setattr(
        base._library,
        "count",
        lambda: pytest.fail("Katalog darf bei fehlendem FFmpeg nicht gelesen werden"),
    )

    available, message = controller.analysis_availability()
    assert not available
    assert "fehlen" in message
    with pytest.raises(RuntimeError, match="FFmpeg/FFprobe"):
        controller.analyze_catalog(lambda *_values: None, lambda *_values: None)
    controller.close()


def test_explicit_capability_reason_blocks_analysis_before_catalog_read(
    tmp_path: Path, monkeypatch
) -> None:
    base, deck = _controller(tmp_path)
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        analysis_unavailable_reason=("FFmpeg-Konfiguration fehlt; neue Analysen sind deaktiviert."),
    )
    monkeypatch.setattr(
        base._library,
        "count",
        lambda: pytest.fail("Katalog darf bei fehlender Capability nicht gelesen werden"),
    )

    available, message = controller.analysis_availability()

    assert not available
    assert "Konfiguration fehlt" in message
    with pytest.raises(RuntimeError, match="neue Analysen sind deaktiviert"):
        controller.analyze_catalog(lambda *_values: None, lambda *_values: None)
    controller.close()


def test_large_faulty_batch_stays_bounded_and_releases_all_jobs(tmp_path: Path) -> None:
    base, deck = _controller(tmp_path)
    track_ids = [1]
    for index in range(2, 102):
        track_ids.append(
            base._library._repository.upsert_file(
                f"song-{index}.mp3",
                f"Song {index}",
                "Artist",
                "",
                250.0,
            ).id
        )
    dispatcher = GuiEventDispatcher(capacity=1000)
    analysis = CueAnalysisService(AlwaysFailingAnalysisBackend(), base._service)
    controller = CuePointController(
        base._service,
        base._library,
        deck,
        DeckController("B", FakeAudioBackend()),
        gui_dispatcher=dispatcher,
        analysis_service=analysis,
    )
    completed: list[tuple[int, int]] = []

    controller.analyze_tracks(
        track_ids,
        lambda *_values: None,
        lambda succeeded, failed: completed.append((succeeded, failed)),
    )
    for _attempt in range(400):
        dispatcher.process_pending_events(
            lambda event: event.payload() if callable(event.payload) else None
        )
        if completed:
            break
        Event().wait(0.01)

    assert completed == [(0, 101)]
    assert analysis.active_job_count == 0
    assert not hasattr(base._service.get(1), "samples")
    controller.close()
