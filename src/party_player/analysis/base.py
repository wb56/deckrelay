"""Backend-neutral contract for bounded offline PCM decoding."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AudioFileInfo:
    """Metadata needed to plan analysis without decoding a complete file."""

    duration_seconds: float
    sample_rate_hz: int
    channels: int
    codec_name: str = ""
    format_name: str = ""
    bitrate_bps: int | None = None
    bitrate_mode: str | None = None
    bits_per_sample: int | None = None
    channel_layout: str = ""
    codec_profile: str = ""
    encoder: str = ""
    codec_long_name: str = ""
    format_long_name: str = ""
    audio_stream_count: int = 1
    selected_stream_index: int = 0


@dataclass(frozen=True, slots=True)
class AnalysisSegment:
    """One half-open time range requested from an audio file."""

    start_seconds: float
    duration_seconds: float


def plan_edge_segments(
    file_duration_seconds: float,
    edge_window_seconds: float = 45.0,
) -> tuple[AnalysisSegment, ...]:
    """Plan bounded start/end ranges without decoding an overlap twice."""
    duration = float(file_duration_seconds)
    window = float(edge_window_seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Die Audiodauer muss positiv und endlich sein")
    if not math.isfinite(window) or not 1.0 <= window <= 60.0:
        raise ValueError("Das Analysefenster muss zwischen 1 und 60 Sekunden liegen")
    if duration <= 2 * window:
        return (AnalysisSegment(0.0, duration),)
    return (
        AnalysisSegment(0.0, window),
        AnalysisSegment(duration - window, window),
    )


@dataclass(frozen=True, slots=True)
class PcmChunk:
    """Interleaved normalized PCM samples and their position in the source."""

    start_seconds: float
    sample_rate_hz: int
    channels: int
    samples: Sequence[float]

    @property
    def frame_count(self) -> int:
        """Return complete sample frames, excluding an incomplete tail."""
        return len(self.samples) // self.channels if self.channels > 0 else 0


@runtime_checkable
class CancellationToken(Protocol):
    """Small contract implemented directly by ``threading.Event``."""

    def is_set(self) -> bool: ...


@runtime_checkable
class AudioAnalysisBackend(Protocol):
    """Probe files and decode selected ranges to backend-neutral PCM chunks.

    Implementations must not retain the complete decoded file in memory. They
    yield bounded chunks, honor cancellation between chunks and never modify
    the source file.
    """

    @property
    def name(self) -> str: ...

    def is_available(self) -> bool: ...

    def supported_extensions(self) -> frozenset[str]: ...

    def probe(self, file_path: Path) -> AudioFileInfo: ...

    def decode_segments(
        self,
        file_path: Path,
        segments: Sequence[AnalysisSegment],
        cancellation: CancellationToken,
    ) -> Iterable[PcmChunk]: ...
