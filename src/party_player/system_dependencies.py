"""Shared runtime-dependency models, codes and version compatibility rules."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re


MINIMUM_VLC_VERSION = "3.0"
MINIMUM_FFMPEG_VERSION = "4.4"
VLC_DOWNLOAD_URL = "https://www.videolan.org/vlc/"
# FFmpeg publishes source code itself and links this provider for ready-to-use
# Windows binaries from its official download page.
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/"
DEPENDENCY_PROBE_TIMEOUT_SECONDS = 5.0
DEPENDENCY_MAXIMUM_OUTPUT_BYTES = 64 * 1024
VLC_STANDARD_DIRECTORIES = (
    Path("C:/Program Files/VideoLAN/VLC"),
    Path("C:/Program Files (x86)/VideoLAN/VLC"),
)
FFMPEG_STANDARD_DIRECTORIES = (
    Path("C:/ffmpeg/bin"),
    Path("C:/Program Files/ffmpeg/bin"),
)


class DependencyStatus(StrEnum):
    AVAILABLE = "available"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    INCOMPATIBLE = "incompatible"
    ERROR = "error"


class DependencySelectionMode(StrEnum):
    """Choose automatic discovery or an explicitly persisted directory."""

    AUTO = "AUTO"
    USER = "USER"


class VersionStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class DependencyErrorCode(StrEnum):
    VLC_NOT_FOUND = "DEP_VLC_NOT_FOUND"
    VLC_LIBVLC_MISSING = "DEP_VLC_LIBVLC_MISSING"
    VLC_PLUGINS_MISSING = "DEP_VLC_PLUGINS_MISSING"
    VLC_LOAD_FAILED = "DEP_VLC_LOAD_FAILED"
    VLC_ARCHITECTURE_MISMATCH = "DEP_VLC_ARCHITECTURE_MISMATCH"
    VLC_VERSION_UNSUPPORTED = "DEP_VLC_VERSION_UNSUPPORTED"
    FFMPEG_NOT_FOUND = "DEP_FFMPEG_NOT_FOUND"
    FFPROBE_NOT_FOUND = "DEP_FFPROBE_NOT_FOUND"
    FFMPEG_EXEC_FAILED = "DEP_FFMPEG_EXEC_FAILED"
    FFMPEG_VERSION_UNSUPPORTED = "DEP_FFMPEG_VERSION_UNSUPPORTED"
    FFMPEG_VERSION_UNKNOWN = "DEP_FFMPEG_VERSION_UNKNOWN"
    AUDIO_NO_DEVICE = "DEP_AUDIO_NO_DEVICE"
    DATABASE_UNAVAILABLE = "DEP_DATABASE_UNAVAILABLE"
    DATABASE_INTEGRITY_FAILED = "DEP_DATABASE_INTEGRITY_FAILED"
    PROCESS_TIMEOUT = "DEP_PROCESS_TIMEOUT"
    PROCESS_OUTPUT_LIMIT = "DEP_PROCESS_OUTPUT_LIMIT"


@dataclass(frozen=True, slots=True)
class DependencyInfo:
    name: str
    status: DependencyStatus
    executable_path: Path | None = None
    version: str | None = None
    source: str | None = None
    message: str | None = None
    error_code: str | None = None
    version_status: VersionStatus = VersionStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class VlcDependencyInfo:
    status: DependencyStatus
    installation_directory: Path | None = None
    executable_path: Path | None = None
    libvlc_path: Path | None = None
    plugin_directory: Path | None = None
    version: str | None = None
    source: str | None = None
    message: str | None = None
    error_code: str | None = None
    version_status: VersionStatus = VersionStatus.UNKNOWN
    libvlc_loaded: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    playback_available: bool
    cue_analysis_available: bool
    loudness_analysis_available: bool
    ffprobe_available: bool
    metadata_analysis_available: bool = False

    @classmethod
    def from_dependencies(
        cls,
        vlc: VlcDependencyInfo,
        ffmpeg: DependencyInfo,
        ffprobe: DependencyInfo,
    ) -> "RuntimeCapabilities":
        playback = vlc.status == DependencyStatus.AVAILABLE and vlc.libvlc_loaded
        ffmpeg_available = ffmpeg.status == DependencyStatus.AVAILABLE
        ffprobe_available = ffprobe.status == DependencyStatus.AVAILABLE
        analysis = ffmpeg_available and ffprobe_available
        return cls(playback, analysis, analysis, ffprobe_available, analysis)


@dataclass(frozen=True, slots=True)
class SystemDiagnosticSnapshot:
    checked_at: str
    vlc: VlcDependencyInfo
    ffmpeg: DependencyInfo
    ffprobe: DependencyInfo
    capabilities: RuntimeCapabilities
    operating_system: str = ""
    application_version: str = ""


@dataclass(frozen=True, slots=True, order=True)
class ParsedVersion:
    parts: tuple[int, ...]

    def normalized(self, width: int) -> tuple[int, ...]:
        return self.parts + (0,) * max(0, width - len(self.parts))


@dataclass(frozen=True, slots=True)
class VersionAssessment:
    status: VersionStatus
    detected: ParsedVersion | None
    minimum: ParsedVersion | None


_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)+)(?!\d)")


def parse_version(value: str | None) -> ParsedVersion | None:
    """Extract a numeric dotted version without guessing from arbitrary integers."""
    if not value:
        return None
    match = _VERSION_PATTERN.search(value)
    if match is None:
        return None
    try:
        parts = tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None
    return ParsedVersion(parts) if parts else None


def assess_version(detected: str | None, minimum: str) -> VersionAssessment:
    parsed_detected = parse_version(detected)
    parsed_minimum = parse_version(minimum)
    if parsed_detected is None or parsed_minimum is None:
        return VersionAssessment(VersionStatus.UNKNOWN, parsed_detected, parsed_minimum)
    width = max(len(parsed_detected.parts), len(parsed_minimum.parts))
    status = (
        VersionStatus.SUPPORTED
        if parsed_detected.normalized(width) >= parsed_minimum.normalized(width)
        else VersionStatus.UNSUPPORTED
    )
    return VersionAssessment(status, parsed_detected, parsed_minimum)
