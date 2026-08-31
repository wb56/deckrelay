"""FFmpeg analysis backend tests without requiring a local FFmpeg installation."""

from io import BytesIO
import json
from pathlib import Path
import struct
import subprocess
from threading import Event

import pytest

from party_player.analysis import (
    AnalysisBackendUnavailableError,
    AnalysisSegment,
    AudioDecodeError,
    FfmpegAudioAnalysisBackend,
    UnsupportedAudioFormatError,
)


class CompletedProbe:
    returncode = 0
    stderr = b""
    stdout = json.dumps(
        {
            "streams": [
                {
                    "codec_name": "mp3",
                    "sample_rate": "48000",
                    "channels": 2,
                    "duration": "120.0",
                }
            ],
            "format": {"duration": "120.0"},
        }
    ).encode()


class FakeProcess:
    def __init__(self, pcm: bytes, returncode: int = 0) -> None:
        self.stdout = BytesIO(pcm)
        self.stderr = BytesIO(b"decoder failed" if returncode else b"")
        self.returncode: int | None = returncode
        self.terminated = False

    def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def backend_with_commands() -> FfmpegAudioAnalysisBackend:
    return FfmpegAudioAnalysisBackend("C:/tools/ffmpeg.exe", "C:/tools/ffprobe.exe")


def test_probe_reads_audio_stream_metadata(tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "song.mp3"
    audio.touch()
    backend = backend_with_commands()
    monkeypatch.setattr(backend, "_resolve_command", lambda command: command)
    monkeypatch.setattr(
        "party_player.analysis.ffmpeg_backend.subprocess.run", lambda *a, **k: CompletedProbe()
    )

    info = backend.probe(audio)

    assert (info.duration_seconds, info.sample_rate_hz, info.channels) == (120.0, 48_000, 2)
    assert info.codec_name == "mp3"


@pytest.mark.parametrize(
    ("codec", "stream_extra", "format_extra", "packet_sizes", "expected"),
    [
        (
            "flac",
            {"bits_per_raw_sample": "16", "channel_layout": "stereo"},
            {"format_name": "flac", "bit_rate": "987654"},
            (),
            (None, 16, "stereo"),
        ),
        (
            "mp3",
            {"bit_rate": "320000", "channel_layout": "stereo"},
            {"format_name": "mp3"},
            (1045,) * 16,
            ("CBR", None, "stereo"),
        ),
        (
            "mp3",
            {"bit_rate": "192000", "channel_layout": "stereo"},
            {"format_name": "mp3"},
            (417, 1045, 626, 835, 522, 1045, 417, 731) * 2,
            ("VBR", None, "stereo"),
        ),
        (
            "flac",
            {"bits_per_raw_sample": "24", "channel_layout": "mono"},
            {"format_name": "flac", "bit_rate": "1200000"},
            (),
            (None, 24, "mono"),
        ),
        (
            "mp3",
            {"bit_rate": "245000", "channel_layout": "stereo"},
            {"format_name": "mp3"},
            (626,) * 4,
            (None, None, "stereo"),
        ),
    ],
)
def test_probe_reports_real_technical_audio_properties(
    tmp_path: Path,
    monkeypatch,
    codec: str,
    stream_extra: dict[str, str],
    format_extra: dict[str, str],
    packet_sizes: tuple[int, ...],
    expected: tuple[str | None, int | None, str],
) -> None:
    audio = tmp_path / "misleading.extension.mp3"
    audio.touch()
    payload = {
        "streams": [
            {
                "codec_name": codec,
                "sample_rate": "44100",
                "channels": 2,
                "duration": "180.0",
                **stream_extra,
            }
        ],
        "format": {"duration": "180.0", **format_extra},
        "packets": [{"size": str(size), "duration_time": "0.026122"} for size in packet_sizes],
    }
    completed = CompletedProbe()
    completed.stdout = json.dumps(payload).encode()
    backend = backend_with_commands()
    monkeypatch.setattr(backend, "_resolve_command", lambda command: command)
    monkeypatch.setattr(
        "party_player.analysis.ffmpeg_backend.subprocess.run", lambda *a, **k: completed
    )

    info = backend.probe(audio)

    assert (info.bitrate_mode, info.bits_per_sample, info.channel_layout) == expected
    assert info.codec_name == codec
    assert info.sample_rate_hz == 44_100


def test_probe_does_not_infer_codec_from_misleading_extension(tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "actually-flac.mp3"
    audio.touch()
    completed = CompletedProbe()
    payload = json.loads(completed.stdout)
    payload["streams"][0]["codec_name"] = "flac"
    completed.stdout = json.dumps(payload).encode()
    backend = backend_with_commands()
    monkeypatch.setattr(backend, "_resolve_command", lambda command: command)
    monkeypatch.setattr(
        "party_player.analysis.ffmpeg_backend.subprocess.run", lambda *a, **k: completed
    )

    assert backend.probe(audio).codec_name == "flac"


def test_probe_selects_default_audio_stream_and_reports_stream_count(
    tmp_path: Path, monkeypatch
) -> None:
    audio = tmp_path / "multi.bin"
    audio.touch()
    payload = {
        "streams": [
            {
                "index": 2,
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 6,
                "duration": "60",
                "disposition": {"default": 0},
            },
            {
                "index": 4,
                "codec_name": "flac",
                "sample_rate": "44100",
                "channels": 2,
                "channel_layout": "stereo",
                "bits_per_raw_sample": "24",
                "duration": "60",
                "disposition": {"default": 1},
            },
        ],
        "format": {"duration": "60", "format_name": "matroska"},
        "packets": [],
    }
    completed = CompletedProbe()
    completed.stdout = json.dumps(payload).encode()
    backend = backend_with_commands()
    monkeypatch.setattr(backend, "_resolve_command", lambda command: command)
    monkeypatch.setattr(
        "party_player.analysis.ffmpeg_backend.subprocess.run", lambda *a, **k: completed
    )

    info = backend.probe(audio)

    assert info.codec_name == "flac"
    assert info.bits_per_sample == 24
    assert info.audio_stream_count == 2
    assert info.selected_stream_index == 4


def test_decode_yields_normalized_interleaved_pcm_chunks(tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "song.flac"
    audio.touch()
    backend = FfmpegAudioAnalysisBackend("ffmpeg", "ffprobe", frames_per_chunk=2)
    monkeypatch.setattr(backend, "_resolve_command", lambda command: command)
    monkeypatch.setattr(
        "party_player.analysis.ffmpeg_backend.subprocess.run", lambda *a, **k: CompletedProbe()
    )
    pcm = struct.pack("<8f", 0.0, 0.25, -0.5, 1.0, 0.1, 0.2, 0.3, 0.4)
    monkeypatch.setattr(
        "party_player.analysis.ffmpeg_backend.subprocess.Popen",
        lambda *a, **k: FakeProcess(pcm),
    )

    chunks = list(backend.decode_segments(audio, (AnalysisSegment(10.0, 1.0),), Event()))

    assert len(chunks) == 2
    assert chunks[0].frame_count == 2
    assert chunks[0].start_seconds == 10.0
    assert chunks[1].start_seconds == pytest.approx(10.0 + 2 / 48_000)
    assert tuple(chunks[0].samples) == pytest.approx((0.0, 0.25, -0.5, 1.0))


def test_missing_commands_and_unsupported_formats_fail_cleanly(tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "song.txt"
    audio.touch()
    backend = FfmpegAudioAnalysisBackend()
    monkeypatch.setattr(backend, "_resolve_command", lambda _command: None)

    assert not backend.is_available()
    with pytest.raises(AnalysisBackendUnavailableError):
        backend.probe(audio)

    monkeypatch.setattr(backend, "_resolve_command", lambda command: command)
    with pytest.raises(UnsupportedAudioFormatError):
        list(backend.decode_segments(audio, (AnalysisSegment(0.0, 1.0),), Event()))
    audio = tmp_path / "song.mp3"
    audio.touch()
    monkeypatch.setattr(backend, "_resolve_command", lambda _command: None)
    with pytest.raises(AnalysisBackendUnavailableError, match="nicht gefunden"):
        backend.probe(audio)


def test_project_local_ffmpeg_tools_are_not_discovered_implicitly(
    tmp_path: Path, monkeypatch
) -> None:
    bin_directory = tmp_path / ".tools" / "ffmpeg" / "ffmpeg-test" / "bin"
    bin_directory.mkdir(parents=True)
    ffmpeg = bin_directory / "ffmpeg.exe"
    ffprobe = bin_directory / "ffprobe.exe"
    ffmpeg.touch()
    ffprobe.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("party_player.analysis.ffmpeg_backend.shutil.which", lambda _name: None)
    backend = FfmpegAudioAnalysisBackend()

    assert not backend.is_available()
    assert backend._resolve_command("ffmpeg") is None
    assert backend._resolve_command("ffprobe") is None


def test_invalid_probe_output_is_reported(tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "song.mp3"
    audio.touch()
    backend = backend_with_commands()
    monkeypatch.setattr(backend, "_resolve_command", lambda command: command)
    invalid = CompletedProbe()
    invalid.stdout = b"{}"
    monkeypatch.setattr(
        "party_player.analysis.ffmpeg_backend.subprocess.run", lambda *a, **k: invalid
    )

    with pytest.raises(AudioDecodeError, match="unvollständige"):
        backend.probe(audio)


def test_probe_timeout_is_reported_without_blocking_indefinitely(
    tmp_path: Path, monkeypatch
) -> None:
    audio = tmp_path / "song.mp3"
    audio.touch()
    backend = backend_with_commands()
    monkeypatch.setattr(backend, "_resolve_command", lambda command: command)

    def time_out(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("ffprobe", 30.0)

    monkeypatch.setattr("party_player.analysis.ffmpeg_backend.subprocess.run", time_out)

    with pytest.raises(AnalysisBackendUnavailableError, match="FFprobe konnte"):
        backend.probe(audio)
