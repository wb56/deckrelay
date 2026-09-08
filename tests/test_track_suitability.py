"""Persistent event-suitability selection tests."""

from pathlib import Path

import pytest

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry, Track
from party_player.track_suitability import (
    TrackSuitabilityRepository,
    TrackSuitabilityService,
    TrackSuitabilityStatus,
)


def _database(path: Path) -> Database:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title, artist) VALUES ('song.mp3', 'Song', 'Artist')"
        )
    return database


def _entry(source: QueueSource) -> QueueEntry:
    return QueueEntry(7, 1, 1, QueueStatus.WAITING, source=source)


def test_suitability_survives_repository_restart(tmp_path: Path) -> None:
    database = _database(tmp_path / "suitability.db")
    TrackSuitabilityRepository(database).set(
        1,
        TrackSuitabilityStatus.MANUAL_ONLY,
        "Nur nach Rücksprache",
    )

    restored = TrackSuitabilityRepository(database).get(1)

    assert restored.status is TrackSuitabilityStatus.MANUAL_ONLY
    assert restored.reason == "Nur nach Rücksprache"


@pytest.mark.parametrize(
    "status",
    [TrackSuitabilityStatus.UNKNOWN, TrackSuitabilityStatus.MANUAL_ONLY],
)
def test_unknown_and_manual_only_require_operator_source(
    tmp_path: Path,
    status: TrackSuitabilityStatus,
) -> None:
    database = _database(tmp_path / f"{status.value}.db")
    repository = TrackSuitabilityRepository(database)
    repository.set(1, status)
    service = TrackSuitabilityService(repository)
    track = Track(1, "song.mp3", "Song", "Artist", "", 60)

    assert service.evaluate(_entry(QueueSource.AUTOMATIC), track) is not None
    assert service.evaluate(_entry(QueueSource.GUEST_REQUEST), track) is not None
    assert service.evaluate(_entry(QueueSource.MANUAL), track) is None


def test_unsuitable_track_requires_explicit_entry_override(tmp_path: Path) -> None:
    database = _database(tmp_path / "unsuitable.db")
    repository = TrackSuitabilityRepository(database)
    repository.set(1, TrackSuitabilityStatus.UNSUITABLE, "Unpassender Inhalt")
    service = TrackSuitabilityService(repository)
    entry = _entry(QueueSource.MANUAL)
    track = Track(1, "song.mp3", "Song", "Artist", "", 60)

    rejected = service.evaluate(entry, track)
    assert rejected is not None
    assert rejected.code == "UNSUITABLE_TRACK"

    service.allow_queue_entry(entry.queue_id)
    assert service.evaluate(entry, track) is None


def test_suitable_track_allows_automatic_and_guest_sources(tmp_path: Path) -> None:
    database = _database(tmp_path / "suitable.db")
    repository = TrackSuitabilityRepository(database)
    repository.set(1, TrackSuitabilityStatus.SUITABLE)
    service = TrackSuitabilityService(repository)
    track = Track(1, "song.mp3", "Song", "Artist", "", 60)

    assert service.evaluate(_entry(QueueSource.AUTOMATIC), track) is None
    assert service.evaluate(_entry(QueueSource.GUEST_REQUEST), track) is None


def test_bulk_suitability_is_atomic_on_database_failure(tmp_path: Path) -> None:
    database = _database(tmp_path / "atomic.db")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (file_path, title, artist) VALUES ('two.mp3', 'Two', 'Artist')"
        )
        connection.execute(
            """CREATE TRIGGER reject_second_suitability BEFORE INSERT ON track_suitability
               WHEN NEW.track_id = 2 BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"""
        )
    repository = TrackSuitabilityRepository(database)

    with pytest.raises(Exception, match="simulated failure"):
        repository.set_many((1, 2), TrackSuitabilityStatus.SUITABLE)

    assert repository.get(1).status is TrackSuitabilityStatus.UNKNOWN
    assert repository.get(2).status is TrackSuitabilityStatus.UNKNOWN
