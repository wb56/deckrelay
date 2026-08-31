"""Functional validation of located VLC and FFmpeg dependency candidates."""

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
import struct
import sys
import tempfile

from party_player.dependency_locator import DependencyCandidate
from party_player.external_process import ExternalProcessRunner, diagnostic_output_excerpt
from party_player.system_dependencies import (
    DependencyErrorCode,
    DependencyInfo,
    DependencyStatus,
    MINIMUM_FFMPEG_VERSION,
    MINIMUM_VLC_VERSION,
    VersionStatus,
    VlcDependencyInfo,
    assess_version,
)


@dataclass(frozen=True, slots=True)
class VlcProbeResult:
    success: bool
    version: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class FfmpegValidationResult:
    installation_directory: Path
    ffmpeg: DependencyInfo
    ffprobe: DependencyInfo

    @property
    def available(self) -> bool:
        return (
            self.ffmpeg.status == DependencyStatus.AVAILABLE
            and self.ffprobe.status == DependencyStatus.AVAILABLE
        )


VlcProbe = Callable[[Path], VlcProbeResult]


class DependencyValidator:
    def __init__(
        self,
        *,
        process_runner: ExternalProcessRunner | None = None,
        vlc_probe: VlcProbe | None = None,
        minimum_vlc_version: str = MINIMUM_VLC_VERSION,
        minimum_ffmpeg_version: str = MINIMUM_FFMPEG_VERSION,
        process_bitness: int | None = None,
    ) -> None:
        self._runner = process_runner or ExternalProcessRunner()
        self._vlc_probe = vlc_probe or self._probe_vlc_isolated
        self._minimum_vlc = minimum_vlc_version
        self._minimum_ffmpeg = minimum_ffmpeg_version
        self._process_bitness = process_bitness or struct.calcsize("P") * 8

    def validate_vlc(self, candidate: DependencyCandidate) -> VlcDependencyInfo:
        return self._validate_vlc(candidate, include_version_check=True)

    def validate_vlc_quick(self, candidate: DependencyCandidate) -> VlcDependencyInfo:
        """Check startup-critical VLC state without a fallback version process."""
        return self._validate_vlc(candidate, include_version_check=False)

    def _validate_vlc(
        self,
        candidate: DependencyCandidate,
        *,
        include_version_check: bool,
    ) -> VlcDependencyInfo:
        directory = candidate.installation_directory
        executable = directory / "vlc.exe"
        libvlc = directory / "libvlc.dll"
        plugins = directory / "plugins"
        base = VlcDependencyInfo(
            DependencyStatus.ERROR,
            directory,
            executable if executable.is_file() else None,
            libvlc if libvlc.is_file() else None,
            plugins if plugins.is_dir() else None,
            source=candidate.source.value,
        )
        if not executable.is_file():
            return replace(
                base,
                status=DependencyStatus.NOT_FOUND,
                message="vlc.exe wurde nicht gefunden",
                error_code=DependencyErrorCode.VLC_NOT_FOUND.value,
            )
        if not libvlc.is_file():
            return replace(
                base,
                status=DependencyStatus.INVALID,
                message="libvlc.dll fehlt in der VLC-Installation",
                error_code=DependencyErrorCode.VLC_LIBVLC_MISSING.value,
            )
        if not plugins.is_dir():
            return replace(
                base,
                status=DependencyStatus.INVALID,
                message="Das VLC-Pluginverzeichnis fehlt",
                error_code=DependencyErrorCode.VLC_PLUGINS_MISSING.value,
            )
        architecture = self._pe_bitness(libvlc)
        if architecture is not None and architecture != self._process_bitness:
            return replace(
                base,
                status=DependencyStatus.INCOMPATIBLE,
                message=(
                    f"VLC ist {architecture}-Bit, DeckRelay benötigt {self._process_bitness}-Bit"
                ),
                error_code=DependencyErrorCode.VLC_ARCHITECTURE_MISMATCH.value,
            )
        probe = self._vlc_probe(directory)
        if not probe.success:
            return replace(
                base,
                status=DependencyStatus.ERROR,
                version=probe.version,
                message=probe.message or "libVLC konnte nicht geladen werden",
                error_code=DependencyErrorCode.VLC_LOAD_FAILED.value,
            )
        detected_version = probe.version
        if not detected_version and include_version_check:
            fallback = self._runner.run([executable, "--version"])
            if fallback.succeeded:
                detected_version = self._first_line(fallback.stdout or fallback.stderr) or None
        version = assess_version(detected_version, self._minimum_vlc)
        if version.status == VersionStatus.UNSUPPORTED:
            return replace(
                base,
                status=DependencyStatus.INCOMPATIBLE,
                version=detected_version,
                version_status=version.status,
                libvlc_loaded=True,
                message=f"VLC {detected_version} ist älter als {self._minimum_vlc}",
                error_code=DependencyErrorCode.VLC_VERSION_UNSUPPORTED.value,
            )
        return replace(
            base,
            status=DependencyStatus.AVAILABLE,
            version=detected_version,
            version_status=version.status,
            libvlc_loaded=True,
            message=(
                "VLC-Version konnte nicht sicher interpretiert werden"
                if version.status == VersionStatus.UNKNOWN
                else None
            ),
        )

    def validate_ffmpeg(self, candidate: DependencyCandidate) -> FfmpegValidationResult:
        directory = candidate.installation_directory
        ffmpeg_path = directory / "ffmpeg.exe"
        ffprobe_path = directory / "ffprobe.exe"
        ffmpeg = self._validate_ffmpeg_program(
            "FFmpeg",
            ffmpeg_path,
            candidate.source.value,
            DependencyErrorCode.FFMPEG_NOT_FOUND,
        )
        ffprobe = self._validate_ffmpeg_program(
            "FFprobe",
            ffprobe_path,
            candidate.source.value,
            DependencyErrorCode.FFPROBE_NOT_FOUND,
        )
        return FfmpegValidationResult(directory, ffmpeg, ffprobe)

    def validate_ffmpeg_quick(self, candidate: DependencyCandidate) -> FfmpegValidationResult:
        """Check the persisted executable pair without spawning either program."""
        directory = candidate.installation_directory
        return FfmpegValidationResult(
            directory,
            self._quick_program_info(
                "FFmpeg",
                directory / "ffmpeg.exe",
                candidate.source.value,
                DependencyErrorCode.FFMPEG_NOT_FOUND,
            ),
            self._quick_program_info(
                "FFprobe",
                directory / "ffprobe.exe",
                candidate.source.value,
                DependencyErrorCode.FFPROBE_NOT_FOUND,
            ),
        )

    @staticmethod
    def _quick_program_info(
        name: str,
        executable: Path,
        source: str,
        missing_code: DependencyErrorCode,
    ) -> DependencyInfo:
        if executable.is_file():
            return DependencyInfo(
                name,
                DependencyStatus.AVAILABLE,
                executable,
                source=source,
                message="Schnellprüfung: Programmdatei vorhanden",
            )
        return DependencyInfo(
            name,
            DependencyStatus.NOT_FOUND,
            source=source,
            message=f"{executable.name} wurde nicht gefunden",
            error_code=missing_code.value,
        )

    def _validate_ffmpeg_program(
        self,
        name: str,
        executable: Path,
        source: str,
        missing_code: DependencyErrorCode,
    ) -> DependencyInfo:
        if not executable.is_file():
            return DependencyInfo(
                name,
                DependencyStatus.NOT_FOUND,
                source=source,
                message=f"{executable.name} wurde nicht gefunden",
                error_code=missing_code.value,
            )
        result = self._runner.run([executable, "-version"])
        if not result.succeeded:
            message = (
                "Versionsabfrage hat das Zeitlimit überschritten"
                if result.timed_out
                else result.error
                or diagnostic_output_excerpt(result.stderr)
                or "Versionsabfrage fehlgeschlagen"
            )
            return DependencyInfo(
                name,
                DependencyStatus.ERROR,
                executable,
                source=source,
                message=message,
                error_code=DependencyErrorCode.FFMPEG_EXEC_FAILED.value,
            )
        first_line = self._first_line(result.stdout or result.stderr)
        version = assess_version(first_line, self._minimum_ffmpeg)
        if version.status == VersionStatus.UNSUPPORTED:
            return DependencyInfo(
                name,
                DependencyStatus.INCOMPATIBLE,
                executable,
                first_line or None,
                source,
                f"{name} ist älter als {self._minimum_ffmpeg}",
                DependencyErrorCode.FFMPEG_VERSION_UNSUPPORTED.value,
                version.status,
            )
        return DependencyInfo(
            name,
            DependencyStatus.AVAILABLE,
            executable,
            first_line or None,
            source,
            (
                "Version konnte nicht sicher interpretiert werden"
                if version.status == VersionStatus.UNKNOWN
                else None
            ),
            (
                DependencyErrorCode.FFMPEG_VERSION_UNKNOWN.value
                if version.status == VersionStatus.UNKNOWN
                else None
            ),
            version.status,
        )

    def _probe_vlc_isolated(self, directory: Path) -> VlcProbeResult:
        script = (
            "import ctypes,json,os,sys;"
            "root=sys.argv[1];os.environ['VLC_PLUGIN_PATH']=os.path.join(root,'plugins');"
            "_dll=os.add_dll_directory(root) if hasattr(os,'add_dll_directory') else None;"
            "lib=ctypes.CDLL(os.path.join(root,'libvlc.dll'));"
            "lib.libvlc_get_version.restype=ctypes.c_char_p;"
            "lib.libvlc_new.restype=ctypes.c_void_p;"
            "instance=lib.libvlc_new(0,None);"
            "assert instance,'libvlc_new failed';"
            "version=lib.libvlc_get_version().decode(errors='replace');"
            "lib.libvlc_release(ctypes.c_void_p(instance));"
            "print(json.dumps({'version':version}))"
        )
        probe_output: Path | None = None
        if getattr(sys, "frozen", False):
            # In a PyInstaller build sys.executable is DeckRelay.exe. Passing
            # Python's ``-c`` option would start a second GUI instance, which
            # remains alive until the dependency probe times out. The frozen
            # entry point handles this private command before importing the UI.
            with tempfile.NamedTemporaryFile(
                prefix="partyplayer-vlc-", suffix=".json", delete=False
            ) as temporary_output:
                probe_output = Path(temporary_output.name)
            command: list[str | Path] = [
                sys.executable,
                "--internal-vlc-probe",
                directory,
                probe_output,
            ]
        else:
            command = [sys.executable, "-c", script, directory]
        try:
            result = self._runner.run(command)
            probe_text = (
                probe_output.read_text(encoding="utf-8")
                if probe_output is not None and probe_output.stat().st_size
                else result.stdout
            )
        except OSError as exc:
            return VlcProbeResult(
                False, message=f"libVLC-Probeergebnis fehlt: {type(exc).__name__}"
            )
        finally:
            if probe_output is not None:
                probe_output.unlink(missing_ok=True)
        if not result.succeeded:
            return VlcProbeResult(
                False,
                message=(
                    "libVLC-Probe hat das Zeitlimit überschritten"
                    if result.timed_out
                    else result.error
                    or diagnostic_output_excerpt(result.stderr)
                    or "libVLC-Probe fehlgeschlagen"
                ),
            )
        try:
            payload = json.loads(probe_text.strip())
            if payload.get("error"):
                return VlcProbeResult(False, message=str(payload["error"]))
            version = str(payload["version"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return VlcProbeResult(False, message="Ungültige Ausgabe der libVLC-Probe")
        return VlcProbeResult(True, version)

    @staticmethod
    def _first_line(output: str) -> str:
        return next((line.strip() for line in output.splitlines() if line.strip()), "")

    @staticmethod
    def _pe_bitness(path: Path) -> int | None:
        try:
            with path.open("rb") as binary:
                if binary.read(2) != b"MZ":
                    return None
                binary.seek(0x3C)
                pe_offset_data = binary.read(4)
                if len(pe_offset_data) != 4:
                    return None
                pe_offset = int.from_bytes(pe_offset_data, "little")
                binary.seek(pe_offset)
                if binary.read(4) != b"PE\0\0":
                    return None
                machine_data = binary.read(2)
                if len(machine_data) != 2:
                    return None
        except OSError:
            return None
        machine = int.from_bytes(machine_data, "little")
        return {0x014C: 32, 0x8664: 64}.get(machine)
