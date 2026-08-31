"""Central immutable system diagnostics without mutating runtime state."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import platform
import sqlite3
from time import monotonic

from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION
from party_player.system_dependencies import SystemDiagnosticSnapshot
from party_player.system_dependency_service import SystemDependencyResolution
from party_player.network_source_check import NetworkSourceProbeResult
from party_player.performance_monitor import PerformanceMonitor


class DiagnosticStatus(StrEnum):
    AVAILABLE = "available"
    WARNING = "warning"
    ERROR = "error"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True, slots=True)
class DatabaseDiagnostic:
    status: DiagnosticStatus
    sqlite_version: str
    schema_version: int | None
    expected_schema_version: int
    integrity_result: str | None = None
    message: str = ""
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class AudioDeviceProbe:
    devices: tuple[tuple[str, str], ...]
    default_device_id: str | None = None


@dataclass(frozen=True, slots=True)
class AudioDeviceDiagnostic:
    status: DiagnosticStatus
    device_count: int
    default_device_id: str | None
    devices: tuple[tuple[str, str], ...]
    message: str = ""
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SystemDiagnosticReport:
    checked_at: str
    operating_system: str
    architecture: str
    application_version: str
    resolution: SystemDependencyResolution
    database: DatabaseDiagnostic
    audio: AudioDeviceDiagnostic
    network_sources: tuple[NetworkSourceProbeResult, ...]
    full_check: bool

    @property
    def dependencies(self) -> SystemDiagnosticSnapshot:
        return self.resolution.snapshot


AudioDeviceProvider = Callable[[], AudioDeviceProbe]
NetworkSourceProvider = Callable[[], tuple[str, ...]]
NetworkSourceProbe = Callable[[str], NetworkSourceProbeResult]


class SystemDiagnosticService:
    """Create a read-only diagnostic report from explicit probes and snapshots."""

    def __init__(
        self,
        database: Database,
        *,
        application_version: str,
        audio_device_provider: AudioDeviceProvider | None = None,
        network_source_provider: NetworkSourceProvider | None = None,
        network_source_probe: NetworkSourceProbe | None = None,
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        self._database = database
        self._application_version = application_version
        self._audio_device_provider = audio_device_provider
        self._network_source_provider = network_source_provider
        self._network_source_probe = network_source_probe
        self._performance = performance_monitor or PerformanceMonitor()

    def check(
        self,
        dependencies: SystemDependencyResolution,
        *,
        full: bool = False,
    ) -> SystemDiagnosticReport:
        return SystemDiagnosticReport(
            datetime.now().astimezone().isoformat(),
            f"{platform.system()} {platform.release()}".strip(),
            platform.machine(),
            self._application_version,
            dependencies,
            self._check_database(full=full),
            self._check_audio_devices(),
            self._check_network_sources() if full else (),
            full,
        )

    def _check_network_sources(self) -> tuple[NetworkSourceProbeResult, ...]:
        if self._network_source_provider is None or self._network_source_probe is None:
            return ()
        try:
            sources = self._network_source_provider()[:10]
        except Exception as exc:
            return (NetworkSourceProbeResult("", False, message=str(exc)),)
        results = []
        for source in sources:
            with self._performance.measure(
                "dependencies.network_source_check", warning_threshold_ms=3_000.0
            ):
                results.append(self._network_source_probe(source))
        return tuple(results)

    def _check_database(self, *, full: bool) -> DatabaseDiagnostic:
        started = monotonic()
        try:
            with self._database.connect() as connection:
                sqlite_version = str(connection.execute("SELECT sqlite_version()").fetchone()[0])
                row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
                schema_version = int(row[0]) if row and row[0] is not None else None
                integrity = (
                    str(connection.execute("PRAGMA quick_check").fetchone()[0]) if full else None
                )
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            result = DatabaseDiagnostic(
                DiagnosticStatus.ERROR,
                sqlite3.sqlite_version,
                None,
                LATEST_SCHEMA_VERSION,
                message=f"SQLite-Datenbank ist nicht verfügbar: {exc}",
                error_code="DEP_DATABASE_UNAVAILABLE",
            )
            self._record_probe("dependencies.database_check", started, result.status.value)
            return result
        if integrity is not None and integrity.casefold() != "ok":
            result = DatabaseDiagnostic(
                DiagnosticStatus.ERROR,
                sqlite_version,
                schema_version,
                LATEST_SCHEMA_VERSION,
                integrity,
                "SQLite quick_check meldet einen Integritätsfehler",
                "DEP_DATABASE_INTEGRITY_FAILED",
            )
            self._record_probe("dependencies.database_check", started, result.status.value)
            return result
        if schema_version != LATEST_SCHEMA_VERSION:
            result = DatabaseDiagnostic(
                DiagnosticStatus.WARNING,
                sqlite_version,
                schema_version,
                LATEST_SCHEMA_VERSION,
                integrity,
                "Datenbankschema entspricht nicht der Anwendungsversion",
            )
            self._record_probe("dependencies.database_check", started, result.status.value)
            return result
        result = DatabaseDiagnostic(
            DiagnosticStatus.AVAILABLE,
            sqlite_version,
            schema_version,
            LATEST_SCHEMA_VERSION,
            integrity,
        )
        self._record_probe("dependencies.database_check", started, result.status.value)
        return result

    def _check_audio_devices(self) -> AudioDeviceDiagnostic:
        started = monotonic()
        if self._audio_device_provider is None:
            result = AudioDeviceDiagnostic(
                DiagnosticStatus.NOT_CHECKED,
                0,
                None,
                (),
                "Keine read-only Audiogeräteprobe konfiguriert",
            )
            self._record_probe("dependencies.audio_devices", started, result.status.value)
            return result
        try:
            probe = self._audio_device_provider()
        except Exception as exc:
            result = AudioDeviceDiagnostic(
                DiagnosticStatus.ERROR,
                0,
                None,
                (),
                f"Audiogeräte konnten nicht gelesen werden: {exc}",
                "DEP_AUDIO_NO_DEVICE",
            )
            self._record_probe("dependencies.audio_devices", started, result.status.value)
            return result
        status = DiagnosticStatus.AVAILABLE if probe.devices else DiagnosticStatus.WARNING
        message = (
            ""
            if probe.default_device_id is not None
            else (
                "Standardgerät ist über die read-only Endpunktabfrage nicht zuverlässig bestimmbar"
                if probe.devices
                else "Es wurde kein Audiogerät gefunden"
            )
        )
        result = AudioDeviceDiagnostic(
            status,
            len(probe.devices),
            probe.default_device_id,
            probe.devices,
            message,
            None if probe.devices else "DEP_AUDIO_NO_DEVICE",
        )
        self._record_probe("dependencies.audio_devices", started, result.status.value)
        return result

    def _record_probe(self, operation: str, started: float, status: str) -> None:
        self._performance.record(
            operation,
            max(0.0, (monotonic() - started) * 1000.0),
            5_000.0,
            {"status": status},
        )
