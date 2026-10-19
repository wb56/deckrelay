"""LibVLC adapter behavior without real audio output."""

import logging
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any

import pytest

from party_player.audio.vlc_backend import VlcAudioBackend
from party_player.equalizer import EqualizerService, ResolvedEqualizerPreset


class DelayedAudioPlayer:
    def __init__(self) -> None:
        self.volume_results = [-1, 0]
        self.volume_calls: list[int] = []
        self.applied = Event()

    def audio_set_volume(self, volume: int) -> int:
        self.volume_calls.append(volume)
        result = self.volume_results.pop(0)
        if result == 0:
            self.applied.set()
        return result

    def get_time(self) -> int:
        return 1000

    def is_playing(self) -> bool:
        return True


class StartingAudioPlayer:
    def __init__(self) -> None:
        self.volume_calls: list[int] = []

    def play(self) -> int:
        return 0

    def audio_set_volume(self, volume: int) -> int:
        self.volume_calls.append(volume)
        return 0


class BlockingAudioPlayer:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()

    def audio_set_volume(self, _volume: int) -> int:
        self.entered.set()
        self.release.wait(timeout=1)
        return 0


class SeekResetPlayer:
    def __init__(self) -> None:
        self.position_ms = 0
        self.seek_calls: list[int] = []

    def set_time(self, position_ms: int) -> None:
        self.seek_calls.append(position_ms)

    def get_time(self) -> int:
        return self.position_ms

    def is_playing(self) -> bool:
        return True


class ClosingPlayer:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.release_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1

    def release(self) -> None:
        self.release_calls += 1


class EqualizerPlayer:
    def __init__(self) -> None:
        self.equalizers: list[object | None] = []

    def set_equalizer(self, equalizer: object | None) -> int:
        self.equalizers.append(equalizer)
        return 0


class EqualizerHandle:
    def __init__(self) -> None:
        self.preamp: float | None = None
        self.bands: list[tuple[float, int]] = []

    def set_preamp(self, value: float) -> int:
        self.preamp = value
        return 0

    def set_amp_at_index(self, value: float, index: int) -> int:
        self.bands.append((value, index))
        return 0


class EqualizerVlc:
    FREQUENCIES = (60.0, 170.0, 1000.0)

    @classmethod
    def libvlc_audio_equalizer_get_band_count(cls) -> int:
        return len(cls.FREQUENCIES)

    @classmethod
    def libvlc_audio_equalizer_get_band_frequency(cls, index: int) -> float:
        return cls.FREQUENCIES[index]

    AudioEqualizer = EqualizerHandle


def test_volume_is_retried_when_audio_output_becomes_ready() -> None:
    backend = VlcAudioBackend.__new__(VlcAudioBackend)
    backend._player = DelayedAudioPlayer()
    backend._volume_percent = None
    backend._requested_volume_percent = None
    backend._logger = logging.getLogger(__name__)

    backend.set_volume(0.75)
    assert backend._volume_thread is not None
    player: Any = backend._player
    assert player.applied.wait(timeout=1)
    assert backend.get_position() == 1.0
    assert backend._volume_percent == 75
    assert player.volume_calls == [75, 75]
    backend._volume_stop.set()
    backend._volume_changed.set()


def test_play_reapplies_cached_volume_for_new_audio_output() -> None:
    backend = VlcAudioBackend.__new__(VlcAudioBackend)
    player = StartingAudioPlayer()
    backend._player = player
    backend._paused = False
    backend._requested_seek_seconds = None
    backend._output_device = ""
    backend._requested_volume_percent = 80
    backend._volume_percent = 80
    backend._volume_call_lock = Lock()
    backend._logger = logging.getLogger(__name__)

    backend.play()

    assert player.volume_calls == [80]
    assert backend._volume_percent == 80


def test_slow_vlc_volume_call_does_not_block_gui_caller() -> None:
    backend = VlcAudioBackend.__new__(VlcAudioBackend)
    player = BlockingAudioPlayer()
    backend._player = player
    backend._volume_percent = None
    backend._requested_volume_percent = None
    backend._logger = logging.getLogger(__name__)

    started = monotonic()
    backend.set_volume(0.5)
    elapsed = monotonic() - started

    assert elapsed < 0.1
    assert player.entered.wait(timeout=1)
    player.release.set()
    assert backend._volume_thread is not None
    backend._volume_stop.set()
    backend._volume_changed.set()
    backend._volume_thread.join(timeout=1)


def test_volume_request_is_clamped_to_vlc_200_percent_ceiling() -> None:
    backend = VlcAudioBackend.__new__(VlcAudioBackend)
    player = DelayedAudioPlayer()
    player.volume_results = [0]
    backend._player = player
    backend._volume_percent = None
    backend._requested_volume_percent = None
    backend._logger = logging.getLogger(__name__)

    backend.set_volume(3.5)

    assert player.applied.wait(timeout=1)
    assert player.volume_calls == [200]
    assert backend.maximum_volume_factor() == 2.0
    backend._volume_stop.set()
    backend._volume_changed.set()


def test_seek_is_repeated_when_vlc_resets_to_file_start() -> None:
    backend = VlcAudioBackend.__new__(VlcAudioBackend)
    player = SeekResetPlayer()
    backend._player = player
    backend._requested_seek_seconds = None

    backend.seek(1.8)
    assert backend.get_position() == 0.0
    player.position_ms = 1810
    assert backend.get_position() == pytest.approx(1.81)

    assert player.seek_calls == [1800, 1800]
    assert backend._requested_seek_seconds is None


def test_close_is_serialized_and_idempotent() -> None:
    backend = VlcAudioBackend.__new__(VlcAudioBackend)
    player = ClosingPlayer()
    backend._player = player
    backend._closed = False
    backend._lifecycle_lock = Lock()
    backend._volume_call_lock = Lock()
    backend._volume_stop = Event()
    backend._volume_changed = Event()
    backend._logger = logging.getLogger(__name__)

    backend.close()
    backend.close()

    assert player.stop_calls == 1
    assert player.release_calls == 1
    assert backend._player is None


def test_unc_paths_are_detected_as_network_paths() -> None:
    assert VlcAudioBackend._is_network_path(Path(r"\\server\music\song.mp3"))
    assert not VlcAudioBackend._is_network_path(Path(r"C:\music\song.mp3"))


def test_audio_container_validation_rejects_invalid_files(tmp_path: Path) -> None:
    invalid_mp3 = tmp_path / "invalid.mp3"
    invalid_flac = tmp_path / "invalid.flac"
    invalid_mp3.write_bytes(b"not an mp3")
    invalid_flac.write_bytes(b"fLaC but without stream info")

    with pytest.raises(ValueError, match="beschädigt"):
        VlcAudioBackend._validate_audio_file(invalid_mp3)
    with pytest.raises(ValueError, match="beschädigt"):
        VlcAudioBackend._validate_audio_file(invalid_flac)


def test_audio_container_validation_accepts_mp3_and_flac_headers(tmp_path: Path) -> None:
    mp3 = tmp_path / "valid.mp3"
    flac = tmp_path / "valid.flac"
    mp3.write_bytes(b"\xff\xfb\x90\x64" + b"\0" * 64)
    flac.write_bytes(b"fLaC" + b"\x00\x00\x00\x22" + b"\0" * 34)

    VlcAudioBackend._validate_audio_file(mp3)
    VlcAudioBackend._validate_audio_file(flac)


def test_vlc_equalizer_uses_runtime_band_layout_and_skips_identical_snapshot() -> None:
    backend = VlcAudioBackend.__new__(VlcAudioBackend)
    backend._vlc = EqualizerVlc
    backend._player = EqualizerPlayer()
    backend._equalizer = None
    backend._equalizer_snapshot = None
    backend._logger = logging.getLogger(__name__)
    resolved = EqualizerService().builtin("rock", backend.equalizer_band_frequencies())

    assert backend.apply_equalizer(resolved)
    assert not backend.apply_equalizer(resolved)

    player: EqualizerPlayer = backend._player
    assert len(player.equalizers) == 1
    handle = player.equalizers[0]
    assert isinstance(handle, EqualizerHandle)
    assert handle.preamp == resolved.preamp_db
    assert handle.bands == list(zip(resolved.band_gains_db, range(3), strict=True))


def test_vlc_equalizer_is_disabled_with_none() -> None:
    backend = VlcAudioBackend.__new__(VlcAudioBackend)
    backend._vlc = EqualizerVlc
    backend._player = EqualizerPlayer()
    backend._equalizer = object()
    backend._equalizer_snapshot = None
    backend._logger = logging.getLogger(__name__)

    assert backend.apply_equalizer(ResolvedEqualizerPreset.disabled())
    assert backend._player.equalizers == [None]
    assert backend._equalizer is None


def test_shared_vlc_instance_cannot_be_released_while_player_is_active(
    monkeypatch,
) -> None:
    class SharedInstance:
        def __init__(self) -> None:
            self.released = False

        def release(self) -> None:
            self.released = True

    shared = SharedInstance()
    monkeypatch.setattr(VlcAudioBackend, "_shared_instance", shared)
    monkeypatch.setattr(VlcAudioBackend, "_active_players", 1)

    assert not VlcAudioBackend.release_shared_instance()
    assert VlcAudioBackend.shared_instance_identity() == id(shared)
    assert not shared.released


def test_shared_vlc_instance_is_released_only_after_last_player(
    monkeypatch,
) -> None:
    class SharedInstance:
        def __init__(self) -> None:
            self.released = False

        def release(self) -> None:
            self.released = True

    shared = SharedInstance()
    monkeypatch.setattr(VlcAudioBackend, "_shared_instance", shared)
    monkeypatch.setattr(VlcAudioBackend, "_active_players", 0)

    assert VlcAudioBackend.release_shared_instance()
    assert shared.released
    assert VlcAudioBackend.shared_instance_identity() is None
