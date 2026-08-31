"""FFmpeg implementation of bounded offline PCM decoding."""

from array import array
from collections.abc import Iterable, Sequence
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from party_player.analysis.base import (
    AnalysisSegment,
    AudioFileInfo,
    CancellationToken,
    PcmChunk,
)


class AnalysisBackendUnavailableError(RuntimeError):
    """Raised when FFmpeg or FFprobe cannot be executed."""


class UnsupportedAudioFormatError(ValueError):
    """Raised before spawning a decoder for an unsupported file type."""


class AudioDecodeError(RuntimeError):
    """Raised for invalid probe output or a failed decoder process."""


class FfmpegAudioAnalysisBackend:
    """Decode selected file ranges as normalized interleaved float32 PCM."""

    _EXTENSIONS = frozenset(
        {".aac", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
    )

    def __init__(
        self,
        ffmpeg_command: str = "ffmpeg",
        ffprobe_command: str = "ffprobe",
        *,
        frames_per_chunk: int = 4096,
    ) -> None:
        if frames_per_chunk <= 0:
            raise ValueError("frames_per_chunk muss positiv sein")
        self._ffmpeg_command = ffmpeg_command
        self._ffprobe_command = ffprobe_command
        self._frames_per_chunk = frames_per_chunk

    @property
    def name(self) -> str:
        return "ffmpeg"

    def is_available(self) -> bool:
        return (
            self._resolve_command(self._ffmpeg_command) is not None
            and self._resolve_command(self._ffprobe_command) is not None
        )

    def supported_extensions(self) -> frozenset[str]:
        return self._EXTENSIONS

    def probe(self, file_path: Path) -> AudioFileInfo:
        self._validate_file(file_path, require_supported_extension=False)
        ffprobe = self._required_command(self._ffprobe_command)
        command = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            (
                "stream=index,codec_name,codec_long_name,profile,sample_rate,channels,"
                "channel_layout,duration,bit_rate,bits_per_sample,bits_per_raw_sample,"
                "sample_fmt:stream_disposition=default:stream_tags=encoder:"
                "format=duration,format_name,"
                "format_long_name,bit_rate:format_tags=encoder:packet=size,duration_time"
            ),
            "-show_packets",
            "-read_intervals",
            "0%+#64",
            "-of",
            "json",
            str(file_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=30.0,
                **self._process_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AnalysisBackendUnavailableError(
                f"FFprobe konnte nicht ausgeführt werden: {exc}"
            ) from exc
        if completed.returncode != 0:
            message = completed.stderr.decode(errors="replace").strip()
            raise AudioDecodeError(message or "FFprobe konnte die Audiodatei nicht lesen")
        try:
            payload = json.loads(completed.stdout)
            streams = payload["streams"]
            stream = next(
                (
                    candidate
                    for candidate in streams
                    if int((candidate.get("disposition") or {}).get("default") or 0) == 1
                ),
                streams[0],
            )
            selected_stream_index = int(stream.get("index") or 0)
            duration = float(stream.get("duration") or payload["format"]["duration"])
            sample_rate = int(stream["sample_rate"])
            channels = int(stream["channels"])
            codec_name = str(stream.get("codec_name") or "")
            codec_long_name = str(stream.get("codec_long_name") or "")
            format_data = payload.get("format") or {}
            format_name = str(format_data.get("format_name") or "")
            format_long_name = str(format_data.get("format_long_name") or "")
            bitrate = self._positive_int(stream.get("bit_rate") or format_data.get("bit_rate"))
            bits_per_sample = self._bit_depth(codec_name, stream)
            channel_layout = str(stream.get("channel_layout") or "")
            codec_profile = str(stream.get("profile") or "")
            encoder = str(
                (stream.get("tags") or {}).get("encoder")
                or (format_data.get("tags") or {}).get("encoder")
                or ""
            )
            packets = tuple(
                packet
                for packet in payload.get("packets") or ()
                if "stream_index" not in packet
                or int(packet.get("stream_index") or 0) == selected_stream_index
            )
            bitrate_mode = self._bitrate_mode(codec_name, packets)
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AudioDecodeError("FFprobe lieferte unvollständige Audiodaten") from exc
        if not math.isfinite(duration) or duration <= 0 or sample_rate <= 0 or channels <= 0:
            raise AudioDecodeError("FFprobe lieferte ungültige Audiodaten")
        return AudioFileInfo(
            duration_seconds=duration,
            sample_rate_hz=sample_rate,
            channels=channels,
            codec_name=codec_name,
            format_name=format_name,
            bitrate_bps=bitrate,
            bitrate_mode=bitrate_mode,
            bits_per_sample=bits_per_sample,
            channel_layout=channel_layout,
            codec_profile=codec_profile,
            encoder=encoder,
            codec_long_name=codec_long_name,
            format_long_name=format_long_name,
            audio_stream_count=len(streams),
            selected_stream_index=selected_stream_index,
        )

    @staticmethod
    def _positive_int(value: object) -> int | None:
        if not isinstance(value, (str, bytes, bytearray, int, float)):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @classmethod
    def _bit_depth(cls, codec_name: str, stream: dict[str, Any]) -> int | None:
        codec = codec_name.casefold()
        if codec not in {"flac", "alac"} and not codec.startswith("pcm_"):
            return None
        explicit = cls._positive_int(
            stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
        )
        if explicit is not None:
            return explicit
        match = re.fullmatch(
            r"[su](8|16|24|32|64)(?:p|le|be)?", str(stream.get("sample_fmt") or "")
        )
        return int(match.group(1)) if match else None

    @staticmethod
    def _bitrate_mode(codec_name: str, packets: Sequence[dict[str, Any]]) -> str | None:
        if codec_name.casefold() not in {"mp3", "mp2"}:
            return None
        rates: list[float] = []
        for packet in packets:
            try:
                size = float(packet["size"])
                duration = float(packet["duration_time"])
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0 and duration > 0 and math.isfinite(size) and math.isfinite(duration):
                rates.append(size * 8.0 / duration)
        if len(rates) < 8:
            return None
        median = sorted(rates)[len(rates) // 2]
        if median <= 0:
            return None
        return "VBR" if (max(rates) - min(rates)) / median > 0.08 else "CBR"

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        self._validate_file(file_path, require_supported_extension=True)
        info = self.probe(file_path)
        ffmpeg = self._required_command(self._ffmpeg_command)
        for segment in segments:
            if cancellation.is_set():
                return
            start, duration = self._validated_segment(segment, info.duration_seconds)
            if duration <= 0:
                continue
            yield from self._decode_segment(ffmpeg, file_path, start, duration, info, cancellation)

    def _decode_segment(
        self,
        ffmpeg: str,
        file_path: Path,
        start: float,
        duration: float,
        info: AudioFileInfo,
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]:
        command = [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            format(start, ".9g"),
            "-i",
            str(file_path),
            "-t",
            format(duration, ".9g"),
            "-vn",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ar",
            str(info.sample_rate_hz),
            "-ac",
            str(info.channels),
            "pipe:1",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **self._process_options(),
            )
        except OSError as exc:
            raise AnalysisBackendUnavailableError(
                f"FFmpeg konnte nicht ausgeführt werden: {exc}"
            ) from exc
        assert process.stdout is not None
        frame_offset = 0
        bytes_per_chunk = self._frames_per_chunk * info.channels * 4
        try:
            while True:
                if cancellation.is_set():
                    process.terminate()
                    return
                raw = process.stdout.read(bytes_per_chunk)
                if not raw:
                    break
                complete_bytes = len(raw) - (len(raw) % 4)
                samples = array("f")
                samples.frombytes(raw[:complete_bytes])
                if sys.byteorder != "little":
                    samples.byteswap()
                chunk = PcmChunk(
                    start + frame_offset / info.sample_rate_hz,
                    info.sample_rate_hz,
                    info.channels,
                    tuple(samples),
                )
                frame_offset += chunk.frame_count
                if chunk.frame_count:
                    yield chunk
            return_code = process.wait()
            if return_code != 0:
                assert process.stderr is not None
                message = process.stderr.read().decode(errors="replace").strip()
                raise AudioDecodeError(message or f"FFmpeg wurde mit Code {return_code} beendet")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def _validate_file(self, file_path: Path, *, require_supported_extension: bool) -> None:
        if require_supported_extension and file_path.suffix.lower() not in self._EXTENSIONS:
            raise UnsupportedAudioFormatError(
                f"Nicht unterstütztes Audioformat: {file_path.suffix or '(ohne Endung)'}"
            )
        if not file_path.is_file():
            raise FileNotFoundError(file_path)

    @staticmethod
    def _validated_segment(segment: AnalysisSegment, file_duration: float) -> tuple[float, float]:
        start = float(segment.start_seconds)
        duration = float(segment.duration_seconds)
        if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or duration <= 0:
            raise ValueError(
                "Analysesegmente benötigen einen gültigen Start und eine positive Dauer"
            )
        if start >= file_duration:
            return start, 0.0
        return start, min(duration, file_duration - start)

    @staticmethod
    def _resolve_command(command: str) -> str | None:
        candidate = Path(command)
        if candidate.parent != Path("."):
            return str(candidate) if candidate.is_file() else None
        installed = shutil.which(command)
        return installed

    def _required_command(self, command: str) -> str:
        resolved = self._resolve_command(command)
        if resolved is None:
            raise AnalysisBackendUnavailableError(
                f"{command} wurde nicht gefunden; FFmpeg muss installiert oder konfiguriert sein"
            )
        return resolved

    @staticmethod
    def _process_options() -> dict[str, Any]:
        if sys.platform == "win32":
            return {"creationflags": subprocess.CREATE_NO_WINDOW}
        return {}
