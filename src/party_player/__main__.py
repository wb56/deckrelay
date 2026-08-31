"""Application entry point."""

import ctypes
import json
import multiprocessing
import os
from pathlib import Path
import sys
from time import monotonic, sleep


def _run_internal_vlc_probe(directory: Path, output_path: Path) -> None:
    """Load and initialize external libVLC for the parent process's probe."""
    os.environ["VLC_PLUGIN_PATH"] = str(directory / "plugins")
    dll_directory = (
        os.add_dll_directory(str(directory)) if hasattr(os, "add_dll_directory") else None
    )
    try:
        try:
            libvlc = ctypes.CDLL(str(directory / "libvlc.dll"))
            libvlc.libvlc_get_version.restype = ctypes.c_char_p
            libvlc.libvlc_new.restype = ctypes.c_void_p
            instance = libvlc.libvlc_new(0, None)
            if not instance:
                raise RuntimeError("libvlc_new failed")
            try:
                raw_version = libvlc.libvlc_get_version()
                version = raw_version.decode(errors="replace") if raw_version else ""
            finally:
                libvlc.libvlc_release(ctypes.c_void_p(instance))
            payload = {"version": version}
        except Exception as exc:
            payload = {"error": f"{type(exc).__name__}: libVLC konnte nicht initialisiert werden"}
        output_path.write_text(json.dumps(payload), encoding="utf-8")
    finally:
        if dll_directory is not None:
            dll_directory.close()


def _run_internal_metadata_analysis_probe(
    input_path: Path,
    output_path: Path,
    ffmpeg: Path,
    ffprobe: Path,
    timeout_seconds: float,
    scope_name: str = "TRACK_FULL",
    cue_in: float | None = None,
    cue_out: float | None = None,
    fade_duration: float = 0.0,
    physical_duration: float | None = None,
    context_id: int | None = None,
) -> None:
    """Exercise the packaged spawn worker without composing the GUI or database."""
    from dataclasses import asdict
    from datetime import datetime, timezone
    from uuid import uuid4

    from party_player.metadata_analysis_contracts import (
        FileSnapshot,
        MetadataAnalysisBackendKind,
        MetadataAnalysisJob,
        MetadataAnalysisKind,
        TempoAnalysisRangeSnapshot,
        TempoAnalysisScope,
    )
    from party_player.metadata_analysis_profiles import ALGORITHM_VERSION, MetadataAnalysisProfile
    from party_player.metadata_analysis_supervisor import MetadataAnalysisProcessSupervisor

    snapshot = FileSnapshot.capture(str(input_path))
    scope = TempoAnalysisScope(scope_name)
    analysis_range = None
    signature = ""
    if cue_in is not None and cue_out is not None and physical_duration is not None:
        analysis_range = TempoAnalysisRangeSnapshot(
            cue_in,
            cue_out,
            fade_duration,
            physical_duration,
            datetime.now(timezone.utc).isoformat(),
            "packaged-probe",
            context_id if scope is TempoAnalysisScope.SAVED_QUEUE_ENTRY else None,
            context_id if scope is TempoAnalysisScope.PARTY_QUEUE_SNAPSHOT else None,
        )
        from party_player.tempo_context import tempo_range_signature

        signature = tempo_range_signature(scope, 1, snapshot, analysis_range, ALGORITHM_VERSION)
    cancel_after = abs(timeout_seconds) if timeout_seconds < 0 else None
    job_timeout = 60.0 if cancel_after is not None else timeout_seconds
    job = MetadataAnalysisJob(
        str(uuid4()),
        1,
        1,
        snapshot,
        MetadataAnalysisProfile.TEMPO_AND_ENERGY_EXPERIMENTAL.value,
        ALGORITHM_VERSION,
        (MetadataAnalysisKind.BPM, MetadataAnalysisKind.ENERGY),
        0,
        job_timeout,
        datetime.now(timezone.utc).isoformat(),
        MetadataAnalysisBackendKind.FFMPEG_TEMPO,
        (
            ("ffmpeg", str(ffmpeg.resolve())),
            ("ffprobe", str(ffprobe.resolve())),
            ("segment_strategy", "distributed"),
        ),
        scope,
        analysis_range,
        signature,
    )
    supervisor = MetadataAnalysisProcessSupervisor()
    try:
        supervisor.submit(job)
        started = monotonic()
        deadline = started + max(5.0, job_timeout + 5.0)
        result = None
        while result is None and monotonic() < deadline:
            if cancel_after is not None and monotonic() - started >= cancel_after:
                result = supervisor.cancel()
            else:
                result = supervisor.poll()
            sleep(0.02)
        if result is None:
            raise TimeoutError("Der gepackte Analyseprozess lieferte kein Ergebnis.")
        output_path.write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        supervisor.close()


def main() -> None:
    """Start the DeckRelay desktop application."""
    multiprocessing.freeze_support()
    if len(sys.argv) in {7, 13} and sys.argv[1] == "--internal-metadata-analysis-probe":
        _run_internal_metadata_analysis_probe(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            float(sys.argv[6]),
            *(
                (
                    sys.argv[7],
                    float(sys.argv[8]),
                    float(sys.argv[9]),
                    float(sys.argv[10]),
                    float(sys.argv[11]),
                    int(sys.argv[12]),
                )
                if len(sys.argv) == 13
                else ()
            ),
        )
        return
    if len(sys.argv) == 4 and sys.argv[1] == "--internal-vlc-probe":
        _run_internal_vlc_probe(Path(sys.argv[2]), Path(sys.argv[3]))
        return

    # Keep GUI imports out of the private dependency-probe subprocess.
    from party_player.app import PartyPlayerApplication

    PartyPlayerApplication().run()


if __name__ == "__main__":
    main()
