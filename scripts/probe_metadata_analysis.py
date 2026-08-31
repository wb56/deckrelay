"""Run the productive tempo backend as a database-free acceptance probe."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import sleep
from uuid import uuid4

from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisKind,
)
from party_player.metadata_analysis_supervisor import MetadataAnalysisProcessSupervisor
from party_player.metadata_analysis_profiles import (
    ALGORITHM_VERSION,
    MetadataAnalysisProfile,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("files", nargs="+", type=Path)
    result.add_argument("--ffmpeg", default="ffmpeg")
    result.add_argument("--ffprobe", default="ffprobe")
    result.add_argument(
        "--strategy",
        choices=("full", "middle", "distributed", "begin_middle_end"),
        default="distributed",
    )
    result.add_argument("--timeout", type=float, default=180.0)
    return result


def main() -> int:
    arguments = parser().parse_args()
    supervisor = MetadataAnalysisProcessSupervisor()
    exit_code = 0
    try:
        for index, path in enumerate(arguments.files, start=1):
            snapshot = FileSnapshot.capture(str(path))
            job = MetadataAnalysisJob(
                str(uuid4()),
                index,
                index,
                snapshot,
                MetadataAnalysisProfile.TEMPO_AND_ENERGY_EXPERIMENTAL.value,
                ALGORITHM_VERSION,
                (MetadataAnalysisKind.BPM, MetadataAnalysisKind.ENERGY),
                0,
                arguments.timeout,
                datetime.now(timezone.utc).isoformat(),
                MetadataAnalysisBackendKind.FFMPEG_TEMPO,
                (
                    ("ffmpeg", str(Path(arguments.ffmpeg).resolve())),
                    ("ffprobe", str(Path(arguments.ffprobe).resolve())),
                    ("segment_strategy", arguments.strategy),
                ),
            )
            supervisor.submit(job)
            while True:
                result = supervisor.poll()
                if result is not None:
                    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
                    if result.outcome.value not in {"SUCCESS", "PARTIAL_SUCCESS"}:
                        exit_code = 1
                    break
                sleep(0.02)
    except KeyboardInterrupt:
        supervisor.cancel()
        print("Analyse abgebrochen.", file=sys.stderr)
        return 130
    finally:
        supervisor.close()
    return exit_code


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
