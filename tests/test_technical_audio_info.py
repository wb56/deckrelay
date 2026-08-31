"""Snapshot-cache tests for technical audio probing."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest

from party_player.analysis import AudioFileInfo
from party_player.technical_audio_info import (
    TechnicalAudioFileChangedError,
    TechnicalAudioInfoService,
)


class ProbeBackend:
    def __init__(self) -> None:
        self.calls = 0
        self.started = Event()
        self.release = Event()
        self.block = False
        self.change_during_probe = False
        self._lock = Lock()

    def probe(self, path: Path) -> AudioFileInfo:
        with self._lock:
            self.calls += 1
        self.started.set()
        if self.block:
            assert self.release.wait(2.0)
        if self.change_during_probe:
            path.write_bytes(path.read_bytes() + b"changed")
        return AudioFileInfo(60.0, 44_100, 2, "flac", bits_per_sample=16)


def test_unchanged_snapshot_is_served_from_cache(tmp_path: Path) -> None:
    source = tmp_path / "track.flac"
    source.write_bytes(b"first")
    backend = ProbeBackend()
    service = TechnicalAudioInfoService(backend)  # type: ignore[arg-type]

    first = service.probe(source)
    second = service.probe(source)

    assert first is second
    assert backend.calls == 1
    assert service.cached_entry_count == 1


def test_changed_snapshot_replaces_cached_result(tmp_path: Path) -> None:
    source = tmp_path / "track.flac"
    source.write_bytes(b"first")
    backend = ProbeBackend()
    service = TechnicalAudioInfoService(backend)  # type: ignore[arg-type]
    service.probe(source)

    source.write_bytes(b"second-version")
    service.probe(source)

    assert backend.calls == 2
    assert service.cached_entry_count == 1


def test_identical_concurrent_requests_share_one_probe(tmp_path: Path) -> None:
    source = tmp_path / "track.flac"
    source.write_bytes(b"same")
    backend = ProbeBackend()
    backend.block = True
    service = TechnicalAudioInfoService(backend)  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.probe, source)
        assert backend.started.wait(1.0)
        second = executor.submit(service.probe, source)
        backend.release.set()
        assert first.result() == second.result()

    assert backend.calls == 1


def test_result_is_rejected_when_file_changes_during_probe(tmp_path: Path) -> None:
    source = tmp_path / "track.flac"
    source.write_bytes(b"before")
    backend = ProbeBackend()
    backend.change_during_probe = True
    service = TechnicalAudioInfoService(backend)  # type: ignore[arg-type]

    with pytest.raises(TechnicalAudioFileChangedError):
        service.probe(source)

    assert service.cached_entry_count == 0
