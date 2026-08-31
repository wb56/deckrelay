"""Import-safe child-process entry point without GUI or persistence imports."""

from datetime import datetime, timezone
import os
from pathlib import Path
from time import monotonic, sleep
from typing import Protocol

from party_player.metadata_analysis_contracts import (
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisResult,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AnalysisBackend(Protocol):
    def analyze(self, job: MetadataAnalysisJob) -> MetadataAnalysisResult: ...


class DiagnosticAnalysisBackend:
    """Check only reachability and the immutable input snapshot."""

    def analyze(self, job: MetadataAnalysisJob) -> MetadataAnalysisResult:
        started = _now()
        path = Path(job.input_snapshot.normalized_path)
        if not path.is_file():
            outcome = MetadataAnalysisOutcome.FILE_MISSING
            code = "FILE_MISSING"
            text = "Die Analysedatei ist nicht erreichbar."
        elif not job.input_snapshot.matches_file():
            outcome = MetadataAnalysisOutcome.FILE_CHANGED
            code = "FILE_CHANGED"
            text = "Die Analysedatei wurde seit Auftragserstellung verändert."
        else:
            outcome = MetadataAnalysisOutcome.SUCCESS
            code = text = ""
        return MetadataAnalysisResult(
            job.job_id,
            job.run_id,
            job.track_id,
            job.input_snapshot,
            job.analysis_profile,
            job.analysis_version,
            started,
            _now(),
            outcome,
            error_code=code,
            error_text=text,
            backend_name="snapshot-diagnostic",
            backend_version="1",
        )


class DeterministicFakeAnalysisBackend:
    """Test backend; it deliberately creates no musical metadata."""

    def analyze(self, job: MetadataAnalysisJob) -> MetadataAnalysisResult:
        started = _now()
        options = dict(job.technical_options)
        delay = float(options.get("test_delay_seconds", 0.0) or 0.0)
        if delay:
            sleep(min(delay, 30.0))
        outcome = MetadataAnalysisOutcome(str(options.get("test_outcome", "SUCCESS")))
        return MetadataAnalysisResult(
            job.job_id,
            job.run_id,
            job.track_id,
            job.input_snapshot,
            job.analysis_profile,
            job.analysis_version,
            started,
            _now(),
            outcome,
            error_code="TEST_FAILURE" if outcome is not MetadataAnalysisOutcome.SUCCESS else "",
            error_text=(
                "Deterministischer Testfehler"
                if outcome is not MetadataAnalysisOutcome.SUCCESS
                else ""
            ),
            backend_name="deterministic-fake",
            backend_version="1",
        )


def analyze_in_child(
    job: MetadataAnalysisJob, cancellation: object | None = None
) -> MetadataAnalysisResult:
    path = Path(job.input_snapshot.normalized_path)
    if not path.is_file() or not job.input_snapshot.matches_file():
        now = _now()
        missing = not path.is_file()
        return MetadataAnalysisResult(
            job.job_id,
            job.run_id,
            job.track_id,
            job.input_snapshot,
            job.analysis_profile,
            job.analysis_version,
            now,
            now,
            (
                MetadataAnalysisOutcome.FILE_MISSING
                if missing
                else MetadataAnalysisOutcome.FILE_CHANGED
            ),
            error_code="FILE_MISSING" if missing else "FILE_CHANGED",
            error_text=(
                "Die Analysedatei ist nicht erreichbar."
                if missing
                else "Die Analysedatei wurde seit Auftragserstellung verändert."
            ),
            backend_name="analysis-input-validation",
            backend_version=job.analysis_version,
        )
    backend: AnalysisBackend
    if job.backend is MetadataAnalysisBackendKind.FFMPEG_TEMPO:
        if cancellation is None:
            raise ValueError("Tempoanalyse benötigt ein Abbruchsignal")
        from party_player.metadata_tempo_backend import FfmpegTempoAnalysisBackend

        return FfmpegTempoAnalysisBackend().analyze(job, cancellation)
    if job.backend is MetadataAnalysisBackendKind.FAKE:
        backend = DeterministicFakeAnalysisBackend()
    else:
        backend = DiagnosticAnalysisBackend()
    return backend.analyze(job)


def metadata_analysis_worker_entry(
    connection: object, cancellation: object, job: MetadataAnalysisJob
) -> None:
    """Run one bounded command; ``connection`` is a multiprocessing pipe endpoint."""
    sender = connection
    try:
        sender.send(("STARTED", job.job_id))  # type: ignore[attr-defined]
        if job.backend is MetadataAnalysisBackendKind.FAKE and dict(job.technical_options).get(
            "test_crash"
        ):
            os._exit(17)
        options = dict(job.technical_options)
        delay = float(options.get("test_cooperative_delay_seconds", 0.0) or 0.0)
        deadline = monotonic() + min(delay, 30.0)
        while monotonic() < deadline:
            if cancellation.is_set():  # type: ignore[attr-defined]
                sender.send(("CANCELLED", job.job_id))  # type: ignore[attr-defined]
                return
            sleep(0.01)
        if cancellation.is_set():  # type: ignore[attr-defined]
            sender.send(("CANCELLED", job.job_id))  # type: ignore[attr-defined]
            return
        result = analyze_in_child(job, cancellation)
        result_job_id = options.get("test_result_job_id")
        if job.backend is MetadataAnalysisBackendKind.FAKE and isinstance(result_job_id, str):
            from dataclasses import replace

            result = replace(result, job_id=result_job_id)
        sender.send(("RESULT", result))  # type: ignore[attr-defined]
    except BaseException as exc:
        sender.send(("ERROR", type(exc).__name__, str(exc)[:500]))  # type: ignore[attr-defined]
    finally:
        sender.close()  # type: ignore[attr-defined]
