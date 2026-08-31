"""Dedicated loudness editing controller tests."""

from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.analysis.loudness_backend import LoudnessAnalysisResult
from party_player.analysis.loudness_service import OfflineLoudnessAnalysisService
from party_player.controllers.loudness_controller import LoudnessController
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.deck_controller import DeckController
from party_player.loudness import LoudnessRepository, LoudnessService
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.services.library_service import LibraryService
from party_player.settings_service import SettingsService


class ImmediateLoudnessBackend:
    name = "test-ebur128"

    def is_available(self) -> bool:
        return True

    def analyze(self, _file_path: Path) -> LoudnessAnalysisResult:
        return LoudnessAnalysisResult(-16.0, 4.0, -1.5, "EBU R128", self.name)


def test_controller_persists_gain_and_updates_only_matching_loaded_deck(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "loudness-controller.db")
    migrate(database)
    tracks = TrackRepository(database)
    first = tracks.upsert_file("one.mp3", "One", "Artist", "", 100.0)
    second = tracks.upsert_file("two.mp3", "Two", "Artist", "", 100.0)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    deck_a.load(first, validate_file=False)
    deck_b.load(second, validate_file=False)
    scheduled: list[object] = []
    controller = LoudnessController(
        LoudnessService(LoudnessRepository(database)),
        LibraryService(tracks),
        deck_a,
        deck_b,
        lambda _delay, callback: scheduled.append(callback),
    )

    initial = controller.state(first.id)
    saved = controller.save_manual_gain(first.id, -6.0)

    assert initial.manual_gain_db is None
    assert initial.source_text == "Keine Anpassung"
    assert initial.clip_protection_text == "Clip-Schutz nicht aktiv"
    assert initial.metadata_status_text == "Noch nicht geprüft"
    assert saved.manual_gain_db == -6.0
    assert saved.resolved.source == "MANUAL"
    assert saved.source_text == "Manuell angepasst"
    assert saved.resolved.linear_gain_factor == pytest.approx(10 ** (-6 / 20))
    assert scheduled
    assert deck_b.normalization_factor == 1.0


def test_controller_rejects_unknown_track(tmp_path: Path) -> None:
    database = Database(tmp_path / "loudness-controller.db")
    migrate(database)
    controller = LoudnessController(
        LoudnessService(LoudnessRepository(database)),
        LibraryService(TrackRepository(database)),
        DeckController("A", FakeAudioBackend()),
        DeckController("B", FakeAudioBackend()),
        lambda _delay, callback: callback(),
    )

    with pytest.raises(ValueError, match="nicht gefunden"):
        controller.save_manual_gain(999, 1.0)


def test_capability_gate_blocks_only_new_analysis(tmp_path: Path) -> None:
    database = Database(tmp_path / "loudness-capability.db")
    migrate(database)
    tracks = TrackRepository(database)
    track = tracks.upsert_file("stored.mp3", "Stored", "Artist", "", 100.0)
    service = LoudnessService(LoudnessRepository(database))
    service.save_manual_gain(track.id, -3.0)
    controller = LoudnessController(
        service,
        LibraryService(tracks),
        DeckController("A", FakeAudioBackend()),
        DeckController("B", FakeAudioBackend()),
        lambda _delay, callback: callback(),
        analysis_unavailable_reason="FFmpeg fehlt; neue Analyse deaktiviert.",
    )

    available, message = controller.analysis_availability()

    assert not available
    assert "FFmpeg fehlt" in message
    assert controller.state(track.id).manual_gain_db == -3.0
    with pytest.raises(RuntimeError, match="neue Analyse deaktiviert"):
        controller.analyze_track(track.id, lambda _result, _error: None)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("MANUAL", "Manuell angepasst"),
        ("REPLAYGAIN_TAG", "ReplayGain"),
        ("ANALYSIS", "Eigene Analyse"),
        ("NONE", "Keine Anpassung"),
        ("UNKNOWN", "Keine Anpassung"),
    ],
)
def test_loudness_sources_have_german_user_facing_names(source: str, expected: str) -> None:
    assert LoudnessController.source_text(source) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("NOT_ANALYSED", "Noch nicht geprüft"),
        ("INCOMPLETE", "Metadaten unvollständig"),
        ("COMPLETE", "Metadaten vollständig"),
        ("FAILED", "Metadaten konnten nicht gelesen werden"),
    ],
)
def test_metadata_statuses_have_german_user_facing_names(status: str, expected: str) -> None:
    assert LoudnessController.metadata_status_text(status) == expected


def test_controller_offers_single_and_serial_catalog_loudness_analysis(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "catalog-analysis.db")
    migrate(database)
    tracks = TrackRepository(database)
    first = tracks.upsert_file("one.flac", "One", "", "", 100.0)
    second = tracks.upsert_file("two.mp3", "Two", "", "", 100.0)
    repository = LoudnessRepository(database)
    analysis = OfflineLoudnessAnalysisService(ImmediateLoudnessBackend(), repository)
    controller = LoudnessController(
        LoudnessService(repository),
        LibraryService(tracks),
        DeckController("A", FakeAudioBackend()),
        DeckController("B", FakeAudioBackend()),
        lambda _delay, callback: callback(),
        analysis_service=analysis,
    )
    single_done = Event()
    single_result: list[tuple[LoudnessAnalysisResult | None, str | None]] = []

    def single_completed(result: LoudnessAnalysisResult | None, error: str | None) -> None:
        single_result.append((result, error))
        single_done.set()

    controller.analyze_track(first.id, single_completed)

    assert single_done.wait(2.0)
    assert single_result[0][0] is not None
    assert single_result[0][1] is None
    completed = Event()
    progress: list[tuple[int, int, int, int]] = []
    totals: list[tuple[int, int]] = []

    def batch_completed(succeeded: int, failed: int) -> None:
        totals.append((succeeded, failed))
        completed.set()

    controller.analyze_catalog(
        lambda *values: progress.append(values),
        batch_completed,
    )

    assert completed.wait(2.0)
    assert progress[0] == (0, 2, 0, 0)
    assert progress[-1] == (2, 2, 2, 0)
    assert totals == [(2, 0)]
    assert repository.get(second.id).analysis_status == "COMPLETE"
    controller.close()


def test_close_forwards_non_blocking_shutdown_to_analysis_service(tmp_path: Path) -> None:
    database = Database(tmp_path / "loudness-close.db")
    migrate(database)
    analysis_service = Mock()
    controller = LoudnessController(
        LoudnessService(LoudnessRepository(database)),
        LibraryService(TrackRepository(database)),
        DeckController("A", FakeAudioBackend()),
        DeckController("B", FakeAudioBackend()),
        lambda _delay, callback: callback(),
        analysis_service=analysis_service,
    )

    controller.close(wait=False)

    analysis_service.close.assert_called_once_with(wait=False)


def test_controller_reports_active_clip_protection(tmp_path: Path) -> None:
    database = Database(tmp_path / "clip-protection.db")
    migrate(database)
    tracks = TrackRepository(database)
    track = tracks.upsert_file("loud.mp3", "Loud", "", "", 100.0)
    repository = LoudnessRepository(database)
    repository.save_replaygain(track.id, 8.0, 0.9, None, None)
    controller = LoudnessController(
        LoudnessService(repository),
        LibraryService(tracks),
        DeckController("A", FakeAudioBackend()),
        DeckController("B", FakeAudioBackend()),
        lambda _delay, callback: callback(),
    )

    state = controller.state(track.id)

    assert state.source_text == "ReplayGain"
    assert state.clip_protection_text == "Clip-Schutz aktiv"


def test_runtime_setting_change_is_persisted_and_ramped_on_loaded_deck(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "runtime-loudness.db")
    migrate(database)
    tracks = TrackRepository(database)
    track = tracks.upsert_file("track.mp3", "Track", "", "", 100.0)
    repository = LoudnessRepository(database)
    repository.save_replaygain(track.id, 6.0, None, None, None)
    service = LoudnessService(repository)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    deck_a.load(track, validate_file=False)
    deck_a.set_resolved_loudness(service.resolve(track.id))
    scheduled: list[object] = []
    settings = SettingsService(PartyPlayerRepository(database))
    controller = LoudnessController(
        service,
        LibraryService(tracks),
        deck_a,
        deck_b,
        lambda _delay, callback: scheduled.append(callback),
        settings_service=settings,
    )

    controller.update_normalization_settings(
        clip_protection_enabled=False,
        maximum_positive_gain_db=1.0,
        smoothing_seconds=0.2,
    )

    assert deck_a.model.loudness_requested_gain_db == 6.0
    assert deck_a.model.loudness_effective_gain_db == 1.0
    assert deck_a.normalization_factor == pytest.approx(10 ** (3 / 20))
    while scheduled:
        callback = scheduled.pop(0)
        assert callable(callback)
        callback()
    assert deck_a.normalization_factor == pytest.approx(10 ** (1 / 20))
    restored = SettingsService(PartyPlayerRepository(database))
    assert restored.maximum_positive_gain() == 1.0
    assert not restored.clip_protection_enabled()
    assert restored.gain_smoothing_seconds() == 0.2
    assert not deck_b.model.loaded_track
    state = controller.settings_state()
    assert state.maximum_positive_gain_db == 1.0
    assert not state.clip_protection_enabled
    assert state.smoothing_seconds == 0.2


def test_album_mode_change_uses_album_gain_and_persists_for_restart(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "album-mode.db")
    migrate(database)
    tracks = TrackRepository(database)
    track = tracks.upsert_file("album-track.mp3", "Track", "", "Album", 100.0)
    repository = LoudnessRepository(database)
    repository.save_replaygain(track.id, -3.0, 0.9, -7.0, 0.8)
    service = LoudnessService(repository, mode="TRACK")
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    deck_a.load(track, validate_file=False)
    deck_a.set_resolved_loudness(service.resolve(track.id))
    scheduled: list[object] = []
    settings = SettingsService(PartyPlayerRepository(database))
    controller = LoudnessController(
        service,
        LibraryService(tracks),
        deck_a,
        deck_b,
        lambda _delay, callback: scheduled.append(callback),
        settings_service=settings,
    )

    controller.update_normalization_settings(mode="ALBUM", smoothing_seconds=0.1)
    while scheduled:
        callback = scheduled.pop(0)
        assert callable(callback)
        callback()

    assert deck_a.model.loudness_requested_gain_db == -7.0
    assert deck_a.normalization_factor == pytest.approx(10 ** (-7 / 20))
    assert controller.settings_state().mode == "ALBUM"
    assert SettingsService(PartyPlayerRepository(database)).normalization_mode() == "ALBUM"
