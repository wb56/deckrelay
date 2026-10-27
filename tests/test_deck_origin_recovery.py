"""Persistent deck-load origin and crash recovery of manual deck reservations."""

from pathlib import Path
import json
import sqlite3
from zipfile import ZipFile

import pytest

from party_player.audio.fake_backend import FakeAudioBackend
from party_player.backup_service import (
    MANIFEST_ARCHIVE_PATH,
    BackupService,
    RestoreMaterializer,
)
from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.deck_controller import DeckController
from party_player.enums import DeckOrigin, QueueStatus
from party_player.models import QueueEntry
from party_player.queue_service import QueueService
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.session_service import PartySessionService


def make_database(path: Path) -> Database:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        for name in ("one", "two", "three"):
            audio_file = path.parent / f"{name}.mp3"
            audio_file.touch()
            connection.execute(
                "INSERT INTO tracks (file_path, title, artist) VALUES (?, ?, ?)",
                (str(audio_file), name.title(), "Artist"),
            )
    return database


def reopen(path: Path) -> tuple[PartyPlayerRepository, TrackRepository]:
    """Return fresh repository objects on a new connection factory, as after a restart."""
    database = Database(path)
    return PartyPlayerRepository(database), TrackRepository(database)


def new_decks() -> tuple[DeckController, DeckController]:
    return DeckController("A", FakeAudioBackend()), DeckController("B", FakeAudioBackend())


def start_session(tmp_path: Path) -> tuple[Path, int]:
    path = tmp_path / "recovery.db"
    database = make_database(path)
    session = PartySessionService(PartyPlayerRepository(database)).start("Absturz")
    return path, session.session_id


def service_for(path: Path, session_id: int) -> QueueService:
    repository, tracks = reopen(path)
    return QueueService(repository, tracks, session_id)


def crash_and_recover(path: Path) -> PartySessionService:
    repository, _tracks = reopen(path)
    service = PartySessionService(repository)
    service.restore_or_start()
    return service


def entry_of(path: Path, queue_id: int) -> QueueEntry:
    repository, _tracks = reopen(path)
    entry = repository.get_queue_entry(queue_id)
    assert entry is not None
    return entry


def manual_ready(queue: QueueService, track_id: int, deck_id: str) -> QueueEntry:
    entry = queue.add(track_id)
    queue.mark_loaded(entry.queue_id, deck_id, deck_origin=DeckOrigin.MANUAL)
    loaded = queue.entry(entry.queue_id)
    assert loaded is not None
    return loaded


# --- Migration -------------------------------------------------------------


def test_new_database_contains_deck_origin_and_latest_schema(tmp_path: Path) -> None:
    database = make_database(tmp_path / "new.db")
    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(party_queue)")}
        version = connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert "deck_origin" in columns
    assert version["v"] == LATEST_SCHEMA_VERSION == 46


def test_schema_45_database_gains_nullable_deck_origin_without_data_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    database = make_database(path)
    session = PartySessionService(PartyPlayerRepository(database)).start("Alt")
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO party_queue
               (session_id, track_id, position, status, loaded_deck, locked, lock_source)
               VALUES (?, 1, 1, 'ready', 'A', 1, 'MANUAL_SYSTEM')""",
            (session.session_id,),
        )
        connection.execute(
            """INSERT INTO party_queue
               (session_id, track_id, position, status, loaded_deck, locked, lock_source)
               VALUES (?, 2, 2, 'waiting', NULL, 0, 'NONE')""",
            (session.session_id,),
        )
        connection.execute("ALTER TABLE party_queue DROP COLUMN deck_origin")
        connection.execute("UPDATE schema_version SET version = 45")
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, status, loaded_deck, locked, lock_source FROM party_queue ORDER BY id"
            )
        ]

    migrate(Database(path))
    migrate(Database(path))

    with Database(path).connect() as connection:
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT id, status, loaded_deck, locked, lock_source FROM party_queue ORDER BY id"
            )
        ]
        origins = [row[0] for row in connection.execute("SELECT deck_origin FROM party_queue")]
        version = connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(party_queue)")
            if row["name"] == "deck_origin"
        ]
    assert after == before
    assert origins == [None, None]
    assert columns == ["deck_origin"]
    assert version["v"] == 46


def test_restored_schema_45_backup_is_migrated_to_46_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy-backup.db"
    database = make_database(path)
    session = PartySessionService(PartyPlayerRepository(database)).start("Alt")
    with database.connect() as connection:
        for position, track_id, status, deck, locked in (
            (1, 1, "ready", "A", 1),
            (2, 2, "waiting", None, 0),
        ):
            connection.execute(
                """INSERT INTO party_queue
                   (session_id, track_id, position, status, loaded_deck, locked, lock_source)
                   VALUES (?, ?, ?, ?, ?, ?, 'NONE')""",
                (session.session_id, track_id, position, status, deck, locked),
            )
        connection.execute("ALTER TABLE party_queue DROP COLUMN deck_origin")
        connection.execute("UPDATE schema_version SET version = 45")
    query = "SELECT id, track_id, status, loaded_deck, locked FROM party_queue ORDER BY id"
    with database.connect() as connection:
        expected = [tuple(row) for row in connection.execute(query)]

    backup = BackupService(Database(path)).create_backup(tmp_path / "backups")
    assert backup.backup_path is not None
    with ZipFile(backup.backup_path) as archive:
        assert json.loads(archive.read(MANIFEST_ARCHIVE_PATH))["database_schema_version"] == 45
    restored = tmp_path / "restored.db"
    result = RestoreMaterializer().materialize(backup.backup_path, restored)

    assert result.success
    with Database(restored).connect() as connection:
        version = connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        origins = [row[0] for row in connection.execute("SELECT deck_origin FROM party_queue")]
        actual = [tuple(row) for row in connection.execute(query)]
    assert version["v"] == 46
    assert actual == expected
    assert origins == [None, None]


def test_deck_origin_rejects_values_outside_the_contract(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    entry = queue.add(1)
    with Database(path).connect() as connection:
        connection.execute(
            "UPDATE party_queue SET deck_origin = 'MANUAL' WHERE id = ?", (entry.queue_id,)
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE party_queue SET deck_origin = 'BOGUS' WHERE id = ?", (entry.queue_id,)
            )


# --- Writing and resetting the origin --------------------------------------


@pytest.mark.parametrize("deck_id", ["A", "B"])
def test_manual_queue_load_persists_manual_origin(tmp_path: Path, deck_id: str) -> None:
    path, session_id = start_session(tmp_path)
    entry = manual_ready(service_for(path, session_id), 1, deck_id)

    stored = entry_of(path, entry.queue_id)
    assert stored.status is QueueStatus.READY
    assert stored.loaded_deck == deck_id
    assert stored.deck_origin is DeckOrigin.MANUAL


def test_automatic_queue_load_persists_auto_origin(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    waiting = queue.add(1)
    deck_a, deck_b = new_decks()

    result = queue.load_next_into_free_deck(deck_a, deck_b)

    assert result is not None
    assert entry_of(path, waiting.queue_id).deck_origin is DeckOrigin.AUTO
    assert entry_of(path, waiting.queue_id).status is QueueStatus.READY


def test_mark_loaded_without_origin_keeps_an_unset_origin_unset(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    entry = queue.add(1)
    queue.mark_preparing(entry.queue_id, "A")
    queue.mark_loaded(entry.queue_id, "A")
    assert entry_of(path, entry.queue_id).deck_origin is None


def test_returning_from_playing_to_ready_keeps_the_manual_origin(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    entry = manual_ready(queue, 1, "A")
    queue.mark_playing(entry.queue_id)
    queue.mark_loaded(entry.queue_id, "A")
    assert entry_of(path, entry.queue_id).deck_origin is DeckOrigin.MANUAL


def test_releasing_the_assignment_clears_origin_but_keeps_lock_semantics(
    tmp_path: Path,
) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    entry = manual_ready(queue, 1, "A")
    queue.toggle_lock(entry.queue_id)
    assert entry_of(path, entry.queue_id).lock_source == "MANUAL_SYSTEM"

    queue.release_deck_assignments("A")

    released = entry_of(path, entry.queue_id)
    assert released.loaded_deck is None
    assert released.deck_origin is None
    assert released.lock_source == "MANUAL"


@pytest.mark.parametrize("path_name", ["reset", "release_playing", "played_reset", "failed"])
def test_every_return_to_unassigned_state_clears_origin(tmp_path: Path, path_name: str) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    entry = manual_ready(queue, 1, "A")
    if path_name == "reset":
        queue._repository.reset_queue_entry_to_waiting(entry.queue_id)
    elif path_name == "release_playing":
        queue.mark_playing(entry.queue_id)
        assert queue.release_playing_deck_assignment(entry.queue_id, "A")
    elif path_name == "played_reset":
        queue.mark_playing(entry.queue_id)
        queue.mark_finished(entry.queue_id, QueueStatus.PLAYED)
        queue.reset_played(entry.queue_id)
    else:
        queue.mark_error(entry.queue_id)

    assert entry_of(path, entry.queue_id).deck_origin is None


# --- Crash recovery --------------------------------------------------------


@pytest.mark.parametrize("deck_id", ["A", "B"])
def test_manual_ready_reservation_survives_crash_on_the_same_deck(
    tmp_path: Path, deck_id: str
) -> None:
    path, session_id = start_session(tmp_path)
    entry = manual_ready(service_for(path, session_id), 1, deck_id)

    service = crash_and_recover(path)

    stored = entry_of(path, entry.queue_id)
    assert stored.status is QueueStatus.READY
    assert stored.loaded_deck == deck_id
    assert stored.deck_origin is DeckOrigin.MANUAL
    assert service.last_recovery_summary is not None
    assert service.last_recovery_summary.interrupted_playbacks == 0
    assert service.last_recovery_summary.preserved_deck_reservations == 1


def test_recovered_manual_reservation_loads_deck_without_playback(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    entry = manual_ready(service_for(path, session_id), 2, "B")
    crash_and_recover(path)
    deck_a, deck_b = new_decks()

    restored = service_for(path, session_id).restore_deck_assignments(deck_a, deck_b)

    assert restored == ["B"]
    assert deck_a.model.loaded_track is None
    assert deck_b.model.loaded_track is not None
    assert deck_b.model.loaded_track.id == 2
    assert not deck_a.backend.is_playing()
    assert not deck_b.backend.is_playing()
    stored = entry_of(path, entry.queue_id)
    assert stored.status is QueueStatus.READY
    assert stored.deck_origin is DeckOrigin.MANUAL
    assert stored.played_at is None


def test_automatic_ready_entry_returns_to_waiting_after_crash(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    entry = queue.add(1)
    queue.mark_preparing(entry.queue_id, "A")
    queue.mark_loaded(entry.queue_id, "A", deck_origin=DeckOrigin.AUTO)

    service = crash_and_recover(path)

    stored = entry_of(path, entry.queue_id)
    assert stored.status is QueueStatus.WAITING
    assert stored.loaded_deck is None
    assert stored.deck_origin is None
    assert not stored.locked
    assert service.last_recovery_summary is not None
    assert service.last_recovery_summary.preserved_deck_reservations == 0
    assert service.last_recovery_summary.reset_preparations == 1


def test_ready_entry_without_origin_keeps_legacy_recovery(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    entry = queue.add(1)
    with Database(path).connect() as connection:
        connection.execute(
            """UPDATE party_queue SET status = 'ready', loaded_deck = 'A',
                   locked = 1, lock_source = 'SYSTEM', deck_origin = NULL WHERE id = ?""",
            (entry.queue_id,),
        )

    crash_and_recover(path)

    stored = entry_of(path, entry.queue_id)
    assert stored.status is QueueStatus.WAITING
    assert stored.loaded_deck is None
    assert stored.deck_origin is None


def test_preparing_entry_is_never_kept_as_loaded_even_with_manual_origin(
    tmp_path: Path,
) -> None:
    path, session_id = start_session(tmp_path)
    entry = service_for(path, session_id).add(1)
    with Database(path).connect() as connection:
        connection.execute(
            """UPDATE party_queue SET status = 'preparing', loaded_deck = 'A',
                   deck_origin = 'MANUAL' WHERE id = ?""",
            (entry.queue_id,),
        )

    crash_and_recover(path)

    stored = entry_of(path, entry.queue_id)
    assert stored.status is QueueStatus.WAITING
    assert stored.loaded_deck is None
    assert stored.deck_origin is None


def test_playing_manual_entry_is_interrupted_and_not_requeued(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    entry = manual_ready(queue, 1, "A")
    queue.mark_playing(entry.queue_id)

    service = crash_and_recover(path)

    stored = entry_of(path, entry.queue_id)
    assert stored.status is QueueStatus.SKIPPED
    assert stored.skip_code == "RECOVERY_INTERRUPTED_PLAYBACK"
    assert stored.loaded_deck is None
    assert stored.deck_origin is None
    assert service.last_recovery_summary is not None
    assert service.last_recovery_summary.interrupted_playbacks == 1
    assert service.last_recovery_summary.preserved_deck_reservations == 0


def test_repeated_recovery_is_idempotent_for_reservations_and_history(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    reserved = manual_ready(queue, 1, "A")
    playing = manual_ready(queue, 2, "B")
    queue.mark_playing(playing.queue_id)

    crash_and_recover(path)
    first = [
        (e.queue_id, e.status, e.loaded_deck, e.deck_origin, e.locked, e.lock_source)
        for e in service_for(path, session_id).entries()
    ]
    crash_and_recover(path)
    second = [
        (e.queue_id, e.status, e.loaded_deck, e.deck_origin, e.locked, e.lock_source)
        for e in service_for(path, session_id).entries()
    ]

    assert first == second
    assert sum(1 for item in second if item[2] == "A") == 1
    assert entry_of(path, reserved.queue_id).status is QueueStatus.READY
    with Database(path).connect() as connection:
        history = connection.execute(
            "SELECT COUNT(*) FROM play_history WHERE queue_id = ?", (playing.queue_id,)
        ).fetchone()[0]
        total = connection.execute("SELECT COUNT(*) FROM party_queue").fetchone()[0]
    assert history == 1
    assert total == 2


def test_recovery_preserves_user_lock_priority_order_and_cue_overrides(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    first = queue.add(1)
    reserved = manual_ready(queue, 2, "A")
    last = queue.add(3)
    queue.toggle_lock(reserved.queue_id)
    queue.set_priority(reserved.queue_id, 600)
    queue.set_cue_overrides(reserved.queue_id, 5.0, 100.0, 3.0)
    order_before = [e.queue_id for e in queue.entries()]

    crash_and_recover(path)

    stored = entry_of(path, reserved.queue_id)
    assert stored.status is QueueStatus.READY
    assert stored.locked
    assert stored.lock_source in {"MANUAL", "MANUAL_SYSTEM"}
    assert stored.priority == 600
    assert (stored.cue_in_override, stored.cue_out_override) == (5.0, 100.0)
    assert stored.fade_duration_override == 3.0
    assert [e.queue_id for e in service_for(path, session_id).entries()] == order_before
    assert {first.queue_id, last.queue_id} <= set(order_before)


def test_automatic_loading_does_not_overwrite_recovered_manual_reservation(
    tmp_path: Path,
) -> None:
    path, session_id = start_session(tmp_path)
    queue = service_for(path, session_id)
    reserved = manual_ready(queue, 1, "A")
    waiting = queue.add(2)
    crash_and_recover(path)
    deck_a, deck_b = new_decks()
    recovered_queue = service_for(path, session_id)
    recovered_queue.restore_deck_assignments(deck_a, deck_b)

    result = recovered_queue.load_next_into_free_deck(deck_a, deck_b)

    assert result is not None
    assert result[0].queue_id == waiting.queue_id
    assert result[1] is deck_b
    kept = entry_of(path, reserved.queue_id)
    assert (kept.status, kept.loaded_deck, kept.deck_origin) == (
        QueueStatus.READY,
        "A",
        DeckOrigin.MANUAL,
    )
    assert deck_a.model.loaded_track is not None and deck_a.model.loaded_track.id == 1
    assert entry_of(path, waiting.queue_id).deck_origin is DeckOrigin.AUTO


def test_missing_media_of_reserved_track_fails_controlled_without_start(
    tmp_path: Path,
) -> None:
    path, session_id = start_session(tmp_path)
    entry = manual_ready(service_for(path, session_id), 1, "A")
    crash_and_recover(path)
    (tmp_path / "one.mp3").unlink()
    deck_a, deck_b = new_decks()

    def missing(_track: object) -> None:
        raise FileNotFoundError("Mediendatei fehlt")

    deck_a.load = missing  # type: ignore[method-assign]

    restored = service_for(path, session_id).restore_deck_assignments(deck_a, deck_b)

    assert restored == []
    stored = entry_of(path, entry.queue_id)
    assert stored.status is QueueStatus.FAILED
    assert stored.loaded_deck is None
    assert stored.deck_origin is None
    assert deck_a.model.loaded_track is None
    assert not deck_a.backend.is_playing()


def test_recovered_reservation_can_be_started_explicitly_by_the_user(tmp_path: Path) -> None:
    path, session_id = start_session(tmp_path)
    entry = manual_ready(service_for(path, session_id), 1, "A")
    crash_and_recover(path)
    deck_a, deck_b = new_decks()
    recovered_queue = service_for(path, session_id)
    recovered_queue.restore_deck_assignments(deck_a, deck_b)
    assert not deck_a.backend.is_playing()

    queue_id = recovered_queue.mark_playing_for_deck("A", 1)

    assert queue_id == entry.queue_id
    assert entry_of(path, entry.queue_id).status is QueueStatus.PLAYING
