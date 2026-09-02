"""Portable resolution of FFmpeg executables used by integration tests."""

from collections.abc import Callable, Mapping
import os
from pathlib import Path
import shutil


_BUNDLED_BIN = Path(".tools/ffmpeg/ffmpeg-8.1.2-essentials_build/bin")
_EXECUTABLE_NAMES = {"ffmpeg": "ffmpeg.exe", "ffprobe": "ffprobe.exe"}
_ENVIRONMENT_NAMES = {
    "ffmpeg": "DECKRELAY_TEST_FFMPEG",
    "ffprobe": "DECKRELAY_TEST_FFPROBE",
}


def resolve_ffmpeg_test_tool(
    tool: str,
    *,
    environment: Mapping[str, str] = os.environ,
    bundled_bin: Path = _BUNDLED_BIN,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """Resolve one test executable from an override, the bundle, or ``PATH``."""
    executable_name = _EXECUTABLE_NAMES[tool]
    explicit = environment.get(_ENVIRONMENT_NAMES[tool])
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())

    bundled = bundled_bin / executable_name
    if bundled.is_file():
        return str(bundled.resolve())

    installed = which(tool)
    return str(Path(installed).resolve()) if installed else None


def resolve_ffmpeg_test_tools() -> tuple[str | None, str | None]:
    """Resolve FFmpeg and FFprobe independently for test skip decisions."""
    return resolve_ffmpeg_test_tool("ffmpeg"), resolve_ffmpeg_test_tool("ffprobe")
