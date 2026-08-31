"""Bounded non-recursive reachability checks for configured UNC roots."""

from dataclasses import dataclass
from pathlib import Path
import shutil

from party_player.external_process import ExternalProcessRunner


@dataclass(frozen=True, slots=True)
class NetworkSourceProbeResult:
    source: str
    reachable: bool
    timed_out: bool = False
    message: str = ""


class NetworkSourceChecker:
    """Run Test-Path in a killable process so an UNC call cannot hang the caller."""

    def __init__(
        self,
        *,
        runner: ExternalProcessRunner | None = None,
        powershell_executable: str | Path | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        self._runner = runner or ExternalProcessRunner()
        self._powershell = str(
            powershell_executable
            or shutil.which("powershell.exe")
            or shutil.which("pwsh.exe")
            or "powershell.exe"
        )
        self._timeout = max(0.1, float(timeout_seconds))

    def __call__(self, source: str) -> NetworkSourceProbeResult:
        normalized = source.strip()
        if not normalized.startswith("\\\\"):
            return NetworkSourceProbeResult(
                normalized, False, message="Nur UNC-Netzwerkquellen werden geprüft"
            )
        script = (
            "$ok=Test-Path -LiteralPath $args[0] -PathType Container;if($ok){exit 0}else{exit 2}"
        )
        result = self._runner.run(
            [
                self._powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                normalized,
            ],
            timeout_seconds=self._timeout,
        )
        if result.timed_out:
            return NetworkSourceProbeResult(
                normalized, False, True, "Erreichbarkeitsprüfung hat das Zeitlimit überschritten"
            )
        if result.error:
            return NetworkSourceProbeResult(normalized, False, message=result.error)
        return NetworkSourceProbeResult(
            normalized,
            result.return_code == 0,
            message=(
                "erreichbar"
                if result.return_code == 0
                else "nicht erreichbar oder Zugriff verweigert"
            ),
        )
