"""End-to-end automatic cue service tests with a deterministic PCM backend."""

from collections.abc import Iterable, Sequence
from concurrent.futures import CancelledError
import logging
from pathlib import Path
from threading import Event

import pytest

from party_player.analysis import (
    AnalysisSegment,
    AudioFileInfo,
    CancellationToken,
    CueAnalysisService,
    PcmChunk,
    SignalDetectionSettings,
)
from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.models import Track


class EdgePcmBackend:
    name = "edge-fake"
    worker_started: Event | None = None

    def __init__(self, started: Event | None = None) -> None:
        self.worker_started = started

    def is_available(self) -> bool:
        return True

    def supported_extensions(self) -> frozenset[str]:
        return frozenset({".wav"})

    def probe(self, file_path: Path) -> AudioFileInfo:
        del file_path
        if self.worker_started is not None:
            self.worker_started.set()
        return AudioFileInfo(120.0, 10, 1, "pcm")

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
            samples = [0.0] * round(segment.duration_seconds * 10)
            if segment.start_seconds == 0:
                samples[20:] = [0.5] * (len(samples) - 20)
            else:
                samples[:-20] = [0.5] * (len(samples) - 20)
            yield PcmChunk(segment.start_seconds, 10, 1, tuple(samples))


class FailingOnceBackend(EdgePcmBackend):
    def __init__(self) -> None:
        self.calls = 0

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("defekte Audiodatei")
        yield from super().decode_segments(file_path, segments, cancellation)


class BlockingBackend(EdgePcmBackend):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        self.started.set()
        self.release.wait(timeout=2)
        yield from super().decode_segments(file_path, segments, cancellation)


def build_service(
    tmp_path: Path, *, worker_started: Event | None = None
) -> tuple[CueAnalysisService, CuePointService, Track]:
    database = Database(tmp_path / "analysis.db")
    migrate(database)
    audio = tmp_path / "song.wav"
    audio.touch()
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks
               (id, file_path, title, artist, album, duration_seconds)
               VALUES (1, ?, 'Song', 'Artist', '', 120)""",
            (str(audio),),
        )
    cue_points = CuePointService(CuePointRepository(database))
    service = CueAnalysisService(
        EdgePcmBackend(worker_started),
        cue_points,
        signal_settings=SignalDetectionSettings(
            minimum_signal_seconds=0.2,
            minimum_silence_seconds=0.2,
        ),
        level_window_seconds=0.1,
    )
    return service, cue_points, Track(1, str(audio), "Song", "Artist", "", 120.0)


def test_service_runs_complete_pipeline_and_persists_result(tmp_path: Path) -> None:
    worker_started = Event()
    service, cue_points, track = build_service(tmp_path, worker_started=worker_started)

    job = service.analyze(track)
    assert worker_started.wait(timeout=10)
    result = job.future.result(timeout=2)
    service.close()

    assert result.cue_in == 2.0
    assert result.cue_out == 118.0
    assert result.backend_name == "edge-fake"
    assert result.confidence > 0.8
    stored = cue_points.get(track.id)
    assert (stored.automatic_cue_in, stored.automatic_cue_out) == (2.0, 118.0)
    assert stored.analysis_version == "silence-v1"


def test_service_preserves_manual_values_while_updating_automatic_result(
    tmp_path: Path,
) -> None:
    service, cue_points, track = build_service(tmp_path)
    cue_points.save_manual(track, 3.0, 110.0, 6.0)

    service.analyze(track).future.result(timeout=2)
    service.close()

    stored = cue_points.get(track.id)
    assert (stored.manual_cue_in, stored.manual_cue_out, stored.manual_fade_duration) == (
        3.0,
        110.0,
        6.0,
    )
    resolved = cue_points.resolve(track)
    assert (resolved.cue_in, resolved.cue_out, resolved.fade_duration) == (3.0, 110.0, 6.0)


def test_failed_file_is_logged_and_next_analysis_still_succeeds(tmp_path: Path, caplog) -> None:
    unused_service, cue_points, track = build_service(tmp_path)
    unused_service.close()
    backend = FailingOnceBackend()
    service = CueAnalysisService(
        backend,
        cue_points,
        signal_settings=SignalDetectionSettings(
            minimum_signal_seconds=0.2,
            minimum_silence_seconds=0.2,
        ),
        level_window_seconds=0.1,
    )

    with caplog.at_level(logging.ERROR):
        first = service.analyze(track)
        try:
            first.future.result(timeout=2)
        except RuntimeError as exc:
            assert "defekte Audiodatei" in str(exc)
        second = service.analyze(track).future.result(timeout=2)
    service.close()

    assert second.cue_in == 2.0
    assert str(track.id) in caplog.text
    assert track.file_path in caplog.text
    assert "defekte Audiodatei" in caplog.text


def test_cancelled_analysis_persists_no_automatic_values(tmp_path: Path) -> None:
    unused_service, cue_points, track = build_service(tmp_path)
    unused_service.close()
    backend = BlockingBackend()
    service = CueAnalysisService(
        backend,
        cue_points,
        signal_settings=SignalDetectionSettings(minimum_signal_seconds=0.2),
        level_window_seconds=0.1,
    )
    job = service.analyze(track)
    assert backend.started.wait(timeout=1)

    job.cancel()
    backend.release.set()

    with pytest.raises(CancelledError):
        job.future.result(timeout=2)
    service.close()
    stored = cue_points.get(track.id)
    assert stored.automatic_cue_in is None
    assert stored.automatic_cue_out is None


def test_analysis_version_marks_missing_and_old_results_for_targeted_reanalysis(
    tmp_path: Path,
) -> None:
    service, cue_points, track = build_service(tmp_path)
    assert service.analysis_version == "silence-v1"
    assert service.needs_analysis(track.id)

    service.analyze(track).future.result(timeout=2)
    assert not service.needs_analysis(track.id)
    service.close()

    newer = CueAnalysisService(
        EdgePcmBackend(),
        cue_points,
        analysis_version="silence-v2",
        signal_settings=SignalDetectionSettings(
            minimum_signal_seconds=0.2,
            minimum_silence_seconds=0.2,
        ),
        level_window_seconds=0.1,
    )
    assert newer.needs_analysis(track.id)
    updated = newer.analyze(track).future.result(timeout=2)
    assert updated.analysis_version == "silence-v2"
    assert not newer.needs_analysis(track.id)
    newer.close()
