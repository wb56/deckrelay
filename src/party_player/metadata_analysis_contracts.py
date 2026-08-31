"""Serializable contracts for isolated catalog-metadata analysis."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
from pathlib import Path
from typing import TypeAlias


Primitive: TypeAlias = str | int | float | bool | None


class MetadataAnalysisKind(StrEnum):
    BPM = "BPM"
    ENERGY = "ENERGY"
    DANCEABILITY = "DANCEABILITY"
    MOOD = "MOOD"


class MetadataAnalysisOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    FILE_MISSING = "FILE_MISSING"
    FILE_CHANGED = "FILE_CHANGED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    ANALYSIS_ERROR = "ANALYSIS_ERROR"
    WORKER_CRASHED = "WORKER_CRASHED"


class MetadataAnalysisSource(StrEnum):
    AUDIO_ANALYSIS = "AUDIO_ANALYSIS"
    DIAGNOSTIC = "DIAGNOSTIC"


class MetadataAnalysisBackendKind(StrEnum):
    DIAGNOSTIC = "DIAGNOSTIC"
    FAKE = "FAKE"
    FFMPEG_TEMPO = "FFMPEG_TEMPO"


class TempoAnalysisScope(StrEnum):
    TRACK_FULL = "TRACK_FULL"
    TRACK_DEFAULT_CUES = "TRACK_DEFAULT_CUES"
    SAVED_QUEUE_ENTRY = "SAVED_QUEUE_ENTRY"
    PARTY_QUEUE_SNAPSHOT = "PARTY_QUEUE_SNAPSHOT"


@dataclass(frozen=True, slots=True)
class TempoAnalysisRangeSnapshot:
    """Immutable, main-process-resolved playback range passed to the worker."""

    cue_in: float
    cue_out: float
    fade_duration: float
    physical_duration: float
    resolved_at: str
    context_revision: str
    saved_queue_entry_id: int | None = None
    party_queue_id: int | None = None
    inherited_track_cues: bool = False

    def __post_init__(self) -> None:
        values = (self.cue_in, self.cue_out, self.fade_duration, self.physical_duration)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Analysebereich enthält keinen endlichen Wert")
        if (
            self.physical_duration <= 0
            or self.cue_in < 0
            or self.cue_out <= self.cue_in
            or self.cue_out > self.physical_duration + 0.25
            or self.fade_duration < 0
            or self.fade_duration > self.cue_out - self.cue_in
        ):
            raise ValueError("Analysebereich liegt außerhalb der Dateigrenzen")
        if not self.context_revision.strip():
            raise ValueError("Analysebereich benötigt eine Kontextrevision")
        try:
            datetime.fromisoformat(self.resolved_at)
        except ValueError as exc:
            raise ValueError("Auflösungszeitpunkt ist nicht ISO-8601-konform") from exc


@dataclass(frozen=True, slots=True)
class MetadataAnalysisRequest:
    track_id: int
    input_snapshot: "FileSnapshot"
    analysis_profile: str
    analysis_version: str
    requested_kinds: tuple[MetadataAnalysisKind, ...]
    priority: int = 0
    timeout_seconds: float = 300.0
    backend: MetadataAnalysisBackendKind = MetadataAnalysisBackendKind.DIAGNOSTIC
    technical_options: tuple[tuple[str, Primitive], ...] = ()
    scope: TempoAnalysisScope = TempoAnalysisScope.TRACK_FULL
    analysis_range: TempoAnalysisRangeSnapshot | None = None
    range_signature: str = ""

    def __post_init__(self) -> None:
        if (
            self.track_id <= 0
            or not self.analysis_profile.strip()
            or not self.analysis_version.strip()
        ):
            raise ValueError("Analyseanforderung ist unvollständig")
        if not self.requested_kinds or len(set(self.requested_kinds)) != len(self.requested_kinds):
            raise ValueError("Analysearten müssen eindeutig und nicht leer sein")
        if not 0.1 <= self.timeout_seconds <= 86_400.0:
            raise ValueError("Timeout liegt außerhalb des zulässigen Bereichs")
        keys = [key for key, _value in self.technical_options]
        if (
            len(keys) > 20
            or any(not key or len(key) > 80 for key in keys)
            or len(set(keys)) != len(keys)
        ):
            raise ValueError("Technische Optionen sind ungültig")
        if self.scope is not TempoAnalysisScope.TRACK_FULL and self.analysis_range is None:
            raise ValueError("Kontextbezogene Analyse benötigt einen aufgelösten Bereich")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    normalized_path: str
    size: int
    modified_ns: int
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.normalized_path or self.size < 0 or self.modified_ns < 0:
            raise ValueError("Dateisnapshot ist ungültig")

    @classmethod
    def capture(cls, path: str, fingerprint: str | None = None) -> "FileSnapshot":
        normalized = str(Path(path).resolve())
        stat = Path(normalized).stat()
        return cls(normalized, stat.st_size, stat.st_mtime_ns, fingerprint)

    def matches_file(self) -> bool:
        try:
            current = self.capture(self.normalized_path, self.fingerprint)
        except OSError:
            return False
        return current.size == self.size and current.modified_ns == self.modified_ns


@dataclass(frozen=True, slots=True)
class MetadataAnalysisJob:
    job_id: str
    run_id: int
    track_id: int
    input_snapshot: FileSnapshot
    analysis_profile: str
    analysis_version: str
    requested_kinds: tuple[MetadataAnalysisKind, ...]
    priority: int
    timeout_seconds: float
    created_at: str
    backend: MetadataAnalysisBackendKind = MetadataAnalysisBackendKind.DIAGNOSTIC
    technical_options: tuple[tuple[str, Primitive], ...] = ()
    scope: TempoAnalysisScope = TempoAnalysisScope.TRACK_FULL
    analysis_range: TempoAnalysisRangeSnapshot | None = None
    range_signature: str = ""

    def __post_init__(self) -> None:
        if not self.job_id.strip() or self.run_id <= 0 or self.track_id <= 0:
            raise ValueError("Job-, Run- und Track-ID müssen gültig sein")
        if not self.analysis_profile.strip() or not self.analysis_version.strip():
            raise ValueError("Analyseprofil und -version dürfen nicht leer sein")
        if not self.requested_kinds or len(set(self.requested_kinds)) != len(self.requested_kinds):
            raise ValueError("Analysearten müssen eindeutig und nicht leer sein")
        if not 0.1 <= self.timeout_seconds <= 86_400.0:
            raise ValueError("Timeout liegt außerhalb des zulässigen Bereichs")
        if len(self.technical_options) > 20:
            raise ValueError("Zu viele technische Optionen")
        keys = [key for key, _value in self.technical_options]
        if any(not key or len(key) > 80 for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("Technische Optionen benötigen eindeutige kurze Namen")
        if self.scope is not TempoAnalysisScope.TRACK_FULL and self.analysis_range is None:
            raise ValueError("Kontextbezogener Job benötigt einen aufgelösten Bereich")
        try:
            datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise ValueError("Erstellungszeitpunkt ist nicht ISO-8601-konform") from exc

    @classmethod
    def created_now(cls, **values: object) -> "MetadataAnalysisJob":
        return cls(created_at=datetime.now(timezone.utc).isoformat(), **values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MetadataFieldSuggestion:
    field_key: str
    canonical_value: Primitive | tuple[Primitive, ...]
    source: MetadataAnalysisSource
    confidence: float

    def __post_init__(self) -> None:
        if not self.field_key.strip() or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Metadatenvorschlag ist ungültig")


@dataclass(frozen=True, slots=True)
class AnalyzedAudioRange:
    start_seconds: float
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class TempoSegmentDiagnostic:
    """Bounded raw tempo decision for one actually decoded excerpt."""

    range_index: int
    start_seconds: float
    end_seconds: float
    raw_bpm: float
    alternative_bpm: float
    confidence: float
    harmonic_quality_score: float

    def __post_init__(self) -> None:
        values = (
            self.start_seconds,
            self.end_seconds,
            self.raw_bpm,
            self.alternative_bpm,
            self.confidence,
            self.harmonic_quality_score,
        )
        if (
            self.range_index < 0
            or self.end_seconds <= self.start_seconds
            or any(not math.isfinite(value) for value in values)
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("Tempo-Segmentdiagnose ist ungültig")


@dataclass(frozen=True, slots=True)
class TechnicalAudioMetric:
    name: str
    value: float
    unit: str = ""

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 80 or not math.isfinite(self.value):
            raise ValueError("Technischer Messwert ist ungültig")
        if len(self.unit) > 24:
            raise ValueError("Messwerteinheit ist zu lang")


@dataclass(frozen=True, slots=True)
class MetadataAnalysisResult:
    job_id: str
    run_id: int
    track_id: int
    input_snapshot: FileSnapshot
    analysis_profile: str
    analysis_version: str
    started_at: str
    finished_at: str
    outcome: MetadataAnalysisOutcome
    suggestions: tuple[MetadataFieldSuggestion, ...] = ()
    analyzed_ranges: tuple[AnalyzedAudioRange, ...] = ()
    technical_metrics: tuple[TechnicalAudioMetric, ...] = ()
    rhythm_stability: float = 0.0
    warnings: tuple[str, ...] = ()
    error_code: str = ""
    error_text: str = ""
    backend_name: str = ""
    backend_version: str = ""
    scope: TempoAnalysisScope = TempoAnalysisScope.TRACK_FULL
    analysis_range: TempoAnalysisRangeSnapshot | None = None
    range_signature: str = ""
    probed_duration_seconds: float | None = None
    segment_diagnostics: tuple[TempoSegmentDiagnostic, ...] = ()
    decision_reasons: tuple[str, ...] = ()
    effective_parameters: tuple[tuple[str, Primitive], ...] = ()
    aggregated_bpm: float | None = None
    aggregated_alternative_bpm: float | None = None
    aggregated_confidence: float | None = None
    confidence_components: tuple[tuple[str, Primitive], ...] = ()

    def __post_init__(self) -> None:
        if not self.job_id or self.run_id <= 0 or self.track_id <= 0:
            raise ValueError("Ergebnisidentität ist ungültig")
        if len(self.warnings) > 20 or any(len(item) > 500 for item in self.warnings):
            raise ValueError("Warnungen überschreiten die Vertragsgrenze")
        if not 0.0 <= self.rhythm_stability <= 1.0:
            raise ValueError("Rhythmusstabilität liegt außerhalb des Wertebereichs")
        if len(self.error_code) > 80 or len(self.error_text) > 500:
            raise ValueError("Fehlerangaben überschreiten die Vertragsgrenze")
        if len(self.segment_diagnostics) > 8 or len(self.decision_reasons) > 20:
            raise ValueError("Tempo-Diagnose überschreitet die Vertragsgrenze")
        if len(self.confidence_components) > 20:
            raise ValueError("Konfidenzdiagnose überschreitet die Vertragsgrenze")
        if (
            self.outcome
            not in {
                MetadataAnalysisOutcome.SUCCESS,
                MetadataAnalysisOutcome.PARTIAL_SUCCESS,
            }
            and self.suggestions
        ):
            raise ValueError("Fehlerhafte Ergebnisse dürfen keine Vorschläge enthalten")
