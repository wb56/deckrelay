"""Real local FFmpeg smoke tests for compressed and variable-bitrate formats."""

from pathlib import Path
import shutil
import subprocess
from threading import Event
import wave

import pytest

from party_player.analysis import AnalysisSegment, FfmpegAudioAnalysisBackend


_BUNDLED_BIN = Path(".tools/ffmpeg/ffmpeg-8.1.2-essentials_build/bin")
FFMPEG = (
    str(_BUNDLED_BIN / "ffmpeg.exe")
    if (_BUNDLED_BIN / "ffmpeg.exe").is_file()
    else shutil.which("ffmpeg")
)
FFPROBE = (
    str(_BUNDLED_BIN / "ffprobe.exe")
    if (_BUNDLED_BIN / "ffprobe.exe").is_file()
    else shutil.which("ffprobe")
)
pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="FFmpeg/FFprobe ist für echte Formattests nicht installiert",
)


def write_test_wave(path: Path) -> None:
    sample_rate = 44_100
    samples = (
        [0] * sample_rate
        + [round(10_000 * ((index % 40) / 20 - 1)) for index in range(sample_rate * 2)]
        + [0] * sample_rate
    )
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(
            b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples)
        )


@pytest.mark.parametrize(
    ("suffix", "codec_options"),
    [
        (".mp3", ["-codec:a", "libmp3lame", "-b:a", "320k"]),
        (".flac", ["-codec:a", "flac"]),
        (".vbr.mp3", ["-codec:a", "libmp3lame", "-q:a", "4"]),
    ],
)
def test_real_ffmpeg_probe_and_bounded_decode(
    tmp_path: Path,
    suffix: str,
    codec_options: list[str],
) -> None:
    source = tmp_path / "source.wav"
    target = tmp_path / f"encoded{suffix}"
    write_test_wave(source)
    assert FFMPEG is not None and FFPROBE is not None
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", str(source), *codec_options, str(target)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    backend = FfmpegAudioAnalysisBackend(FFMPEG, FFPROBE, frames_per_chunk=512)

    info = backend.probe(target)
    chunks = list(
        backend.decode_segments(
            target,
            (AnalysisSegment(0.0, min(2.0, info.duration_seconds)),),
            Event(),
        )
    )

    assert info.duration_seconds == pytest.approx(4.0, abs=0.2)
    assert info.sample_rate_hz > 0
    assert sum(chunk.frame_count for chunk in chunks) > 0
    if suffix == ".flac":
        assert info.codec_name == "flac"
        assert info.bits_per_sample == 16
        misleading = tmp_path / "claims-to-be-mp3.mp3"
        shutil.copyfile(target, misleading)
        assert backend.probe(misleading).codec_name == "flac"
    else:
        assert info.codec_name == "mp3"
        assert info.bitrate_mode == ("VBR" if suffix == ".vbr.mp3" else "CBR")
        if suffix == ".mp3":
            assert info.bitrate_bps == pytest.approx(320_000, rel=0.02)
