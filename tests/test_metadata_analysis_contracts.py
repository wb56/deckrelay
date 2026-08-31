from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import pickle
from pathlib import Path

import pytest

from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisKind,
)
from party_player.metadata_analysis_worker import metadata_analysis_worker_entry


def make_job(path: Path, **changes: object) -> MetadataAnalysisJob:
    values: dict[str, object] = {
        "job_id": "job-1",
        "run_id": 1,
        "track_id": 2,
        "input_snapshot": FileSnapshot.capture(str(path)),
        "analysis_profile": "diagnostic",
        "analysis_version": "diagnostic-v1",
        "requested_kinds": (MetadataAnalysisKind.BPM,),
        "priority": 0,
        "timeout_seconds": 5.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "backend": MetadataAnalysisBackendKind.FAKE,
    }
    values.update(changes)
    return MetadataAnalysisJob(**values)  # type: ignore[arg-type]


def test_job_is_frozen_and_pickle_serializable(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    job = make_job(path)
    assert pickle.loads(pickle.dumps(job)) == job
    with pytest.raises(FrozenInstanceError):
        job.priority = 2  # type: ignore[misc]
    assert pickle.loads(pickle.dumps(metadata_analysis_worker_entry)).__name__ == (
        "metadata_analysis_worker_entry"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": ""},
        {"requested_kinds": ()},
        {"timeout_seconds": 0.0},
        {"technical_options": tuple((f"key-{index}", index) for index in range(21))},
    ],
)
def test_job_rejects_invalid_or_unbounded_values(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"test")
    with pytest.raises(ValueError):
        make_job(path, **changes)


def test_snapshot_detects_file_change(tmp_path: Path) -> None:
    path = tmp_path / "one.mp3"
    path.write_bytes(b"first")
    snapshot = FileSnapshot.capture(str(path))
    path.write_bytes(b"changed size")
    assert not snapshot.matches_file()
