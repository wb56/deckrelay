"""Queue persistence, ordering, recovery and history tests."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
from pathlib import Path
import random
from threading import Barrier

import pytest

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.audio.fake_backend import FakeAudioBackend
from party_player.deck_controller import DeckController
from party_player.enums import (
    CompletionStatus,
    GuestPriority,
    QueueSource,
    QueueStatus,
    SessionStatus,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.repositories.saved_queue_repository import SavedQueueRepository
from party_player.repository import PartyPlayerRepository
from party_player.queue_service import QueueService
from party_player.saved_queue_service import SavedQueueService
from party_player.session_service import PartySessionService
from party_player.models import SavedQueueEntry
from party_player.track_selection import SelectionDecision, TrackSelectionService
from party_player.selection_source import SelectionSourceClass


def database_with_tracks(path: Path) -> Database:
    database = Database(path)
    migrate(database)
    first_file = path.parent / "one.mp3"
    second_file = path.parent / "two.mp3"
    first_file.touch()
    second_file.touch()
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (file_path, title, artist) VALUES (?, ?, ?)",
            [
                (str(first_file), "One", "Artist"),
                (str(second_file), "Two", "Artist"),
            ],
        )
    return database


def test_concurrent_additions_receive_unique_next_positions(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "concurrent-positions.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Concurrent")

    def add_entry(index: int) -> int:
        return (
            PartyPlayerRepository(database)
            .add_queue_entry(
                session.session_id,
                1 if index % 2 == 0 else 2,
            )
            .queue_id
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        queue_ids = list(executor.map(add_entry, range(24)))

    assert len(set(queue_ids)) == 24
    with database.connect() as connection:
        positions = [
            int(row["position"])
            for row in connection.execute(
                """SELECT position FROM party_queue
                   WHERE session_id = ? ORDER BY position""",
                (session.session_id,),
            )
        ]
    assert positions == list(range(1, 25))


def test_concurrent_removal_and_preparation_use_status_compare_and_swap(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "concurrent-status.db")
    repository = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    session = repository.create_session("Concurrent")
    entry = QueueService(repository, tracks, session.session_id).add(1)
    barrier = Barrier(2)

    def remove() -> str:
        barrier.wait()
        try:
            QueueService(PartyPlayerRepository(database), tracks, session.session_id).remove(
                entry.queue_id
            )
        except ValueError:
            return "rejected"
        return "removed"

    def prepare() -> str:
        barrier.wait()
        try:
            QueueService(
                PartyPlayerRepository(database), tracks, session.session_id
            ).mark_preparing(entry.queue_id, "A")
        except ValueError:
            return "rejected"
        return "preparing"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(remove), executor.submit(prepare)]
        outcomes = [future.result() for future in futures]

    assert outcomes.count("rejected") == 1
    final = repository.get_queue_entry(entry.queue_id)
    assert final is not None
    assert final.status in {QueueStatus.REMOVED, QueueStatus.PREPARING}


def test_queue_operations_emit_complete_structured_log_fields(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    database = database_with_tracks(tmp_path / "structured-queue-log.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Logging")
    service = QueueService(repository, TrackRepository(database), session.session_id)

    entry = service.add(1, source=QueueSource.GUEST_REQUEST)
    service.mark_preparing(entry.queue_id, "A")
    service.mark_error(entry.queue_id, "FILE_MISSING")

    record = next(item for item in reversed(caplog.records) if item.event_code == "QUEUE_FAILED")
    assert record.session_id == session.session_id
    assert record.queue_id == entry.queue_id
    assert record.track_id == 1
    assert record.source == "GUEST_REQUEST"
    assert record.status == "failed"
    assert record.reason_code == "FILE_MISSING"


def test_queue_sources_are_normalized_to_stable_enum_values(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "sources.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Sources")

    manual = repository.add_queue_entry(session.session_id, 1, source="catalog")
    guest = repository.add_queue_entry(
        session.session_id,
        2,
        source=QueueSource.GUEST_REQUEST,
    )

    assert manual.source is QueueSource.MANUAL
    assert guest.source is QueueSource.GUEST_REQUEST
    with pytest.raises(ValueError, match="Unbekannte Queue-Quelle"):
        repository.add_queue_entry(session.session_id, 1, source="external")


@pytest.mark.parametrize(
    ("source", "expected_priority"),
    [
        (QueueSource.EMERGENCY, 999),
        (QueueSource.MANUAL, 700),
        (QueueSource.GUEST_REQUEST, 600),
        (QueueSource.PLAYLIST, 300),
        (QueueSource.AUTOMATIC, 100),
    ],
)
def test_queue_sources_receive_persistent_default_priorities(
    tmp_path: Path,
    source: QueueSource,
    expected_priority: int,
) -> None:
    database = database_with_tracks(tmp_path / f"{source.value}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Priorities")

    entry = repository.add_queue_entry(session.session_id, 1, source=source)

    assert entry.priority == expected_priority
    assert PartyPlayerRepository(database).get_queue_entry(entry.queue_id).priority == expected_priority  # type: ignore[union-attr]


@pytest.mark.parametrize("priority", [-1, 1000, True])
def test_queue_priority_rejects_values_outside_zero_to_999(
    tmp_path: Path,
    priority: int,
) -> None:
    database = database_with_tracks(tmp_path / f"invalid-{priority}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Invalid priority")
    service = QueueService(repository, TrackRepository(database), session.session_id)

    with pytest.raises(ValueError, match="zwischen 0 und 999"):
        service.add(1, priority=priority)


@pytest.mark.parametrize("priority", [0, 999])
def test_queue_priority_accepts_inclusive_boundaries(tmp_path: Path, priority: int) -> None:
    database = database_with_tracks(tmp_path / f"boundary-{priority}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Boundary priority")
    service = QueueService(repository, TrackRepository(database), session.session_id)

    entry = service.add(1, priority=priority)
    updated = service.update_metadata(
        entry.queue_id,
        priority=priority,
        locked=False,
        request_count=0,
    )

    assert updated.priority == priority


def test_queue_order_is_priority_position_timestamp_and_id_across_restart(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "stable-order.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Stable order")
    low = repository.add_queue_entry(
        session.session_id,
        1,
        source=QueueSource.AUTOMATIC,
    )
    first_high = repository.add_queue_entry(
        session.session_id,
        2,
        source=QueueSource.EMERGENCY,
    )
    second_high = repository.add_queue_entry(
        session.session_id,
        1,
        source=QueueSource.EMERGENCY,
    )
    with database.connect() as connection:
        connection.execute(
            """UPDATE party_queue
               SET position = 1, added_at = '2026-01-01 00:00:00'
               WHERE id IN (?, ?)""",
            (first_high.queue_id, second_high.queue_id),
        )

    first_read = PartyPlayerRepository(database).list_queue(session.session_id)
    second_read = PartyPlayerRepository(database).list_queue(session.session_id)

    assert [entry.queue_id for entry in first_read] == [
        first_high.queue_id,
        second_high.queue_id,
        low.queue_id,
    ]
    assert [entry.queue_id for entry in second_read] == [entry.queue_id for entry in first_read]


def test_pending_queue_copy_uses_same_deterministic_priority_order(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "copy-priority-order.db")
    repository = PartyPlayerRepository(database)
    source = repository.create_session("Source")
    automatic = repository.add_queue_entry(
        source.session_id,
        1,
        source=QueueSource.AUTOMATIC,
    )
    emergency = repository.add_queue_entry(
        source.session_id,
        2,
        source=QueueSource.EMERGENCY,
    )
    target = repository.create_session("Target")

    assert repository.copy_pending_queue(source.session_id, target.session_id) == 2

    copied = repository.list_queue(target.session_id)
    assert [entry.track_id for entry in copied] == [
        emergency.track_id,
        automatic.track_id,
    ]
    assert [entry.position for entry in copied] == [1, 2]


def test_queue_ordering_and_status_survive_repository_recreation(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    first = repository.add_queue_entry(session.session_id, 1)
    second = repository.add_queue_entry(session.session_id, 2)
    repository.swap_positions(first.queue_id, second.queue_id)
    QueueService(repository, TrackRepository(database), session.session_id).mark_loaded(
        second.queue_id, "A"
    )
    restored = PartyPlayerRepository(database).list_queue(session.session_id)
    assert [entry.track_id for entry in restored] == [2, 1]
    assert restored[0].status == QueueStatus.LOADED
    assert restored[0].loaded_deck == "A"


def test_queue_metadata_survives_repository_recreation(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Metadaten")
    entry = repository.add_queue_entry(session.session_id, 1)

    repository.update_queue_metadata(
        entry.queue_id,
        priority=4,
        locked=True,
        request_count=3,
    )

    restored = PartyPlayerRepository(database).get_queue_entry(entry.queue_id)
    assert restored is not None
    assert restored.priority == 4
    assert restored.locked
    assert restored.request_count == 3


def test_queue_cue_overrides_are_owned_by_entry_and_survive_restart(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    first = repository.add_queue_entry(session.session_id, 1)
    second = repository.add_queue_entry(session.session_id, 1)

    repository.set_queue_cue_overrides(first.queue_id, 1.8, 110.0, 7.0, "snapshot")
    restored = PartyPlayerRepository(database).list_queue(session.session_id)

    assert restored[0].cue_in_override == 1.8
    assert restored[0].cue_out_override == 110.0
    assert restored[0].fade_duration_override == 7.0
    assert restored[0].cue_override_source == "snapshot"
    assert restored[0].has_cue_overrides
    assert restored[1].queue_id == second.queue_id
    assert restored[1].cue_in_override is None
    assert restored[1].cue_override_source == "inherited"
    assert not restored[1].has_cue_overrides


def test_pending_queue_copy_preserves_entry_cue_overrides(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    source = repository.create_session("Quelle")
    entry = repository.add_queue_entry(source.session_id, 1)
    repository.set_queue_cue_overrides(entry.queue_id, 2.0, 100.0, 5.0, "queue")
    target = repository.create_session("Ziel")

    repository.copy_pending_queue(source.session_id, target.session_id)

    copied = repository.list_queue(target.session_id)[0]
    assert (copied.cue_in_override, copied.cue_out_override) == (2.0, 100.0)
    assert copied.fade_duration_override == 5.0
    assert copied.cue_override_source == "queue"


def test_unfinished_session_is_recovered_without_playback(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    original = repository.create_session("Test")
    restored = PartySessionService(repository).restore_or_start()
    assert restored.session_id == original.session_id
    assert restored.status == SessionStatus.RECOVERED


def test_restart_resets_volatile_queue_states_and_aborts_playing_entry(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "restart.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    waiting = repository.add_queue_entry(session.session_id, 1)
    preparing = repository.add_queue_entry(session.session_id, 2)
    ready = repository.add_queue_entry(session.session_id, 1)
    playing = repository.add_queue_entry(session.session_id, 2)
    with database.connect() as connection:
        connection.execute(
            """UPDATE party_queue
               SET status = 'preparing', loaded_deck = NULL,
                   locked = 1, lock_source = 'SYSTEM'
               WHERE id = ?""",
            (preparing.queue_id,),
        )
        connection.execute(
            """UPDATE party_queue
               SET status = 'ready', loaded_deck = 'B',
                   locked = 1, lock_source = 'MANUAL_SYSTEM'
               WHERE id = ?""",
            (ready.queue_id,),
        )
        connection.execute(
            """UPDATE party_queue
               SET status = 'playing', loaded_deck = 'A', played_at = CURRENT_TIMESTAMP,
                   locked = 1, lock_source = 'SYSTEM'
               WHERE id = ?""",
            (playing.queue_id,),
        )

    PartySessionService(repository).restore_or_start()

    entries = {entry.queue_id: entry for entry in repository.list_queue(session.session_id)}
    assert entries[waiting.queue_id].status is QueueStatus.WAITING
    for queue_id in (preparing.queue_id, ready.queue_id, playing.queue_id):
        assert entries[queue_id].status is QueueStatus.WAITING
        assert entries[queue_id].loaded_deck is None
    assert not entries[preparing.queue_id].locked
    assert entries[preparing.queue_id].lock_source == "NONE"
    assert entries[ready.queue_id].locked
    assert entries[ready.queue_id].lock_source == "MANUAL"
    recovered_queue = QueueService(repository, TrackRepository(database), session.session_id)
    next_candidate = recovered_queue.get_next_candidate()
    assert next_candidate is not None and next_candidate.queue_id == waiting.queue_id
    assert recovered_queue.last_source_resolution is not None
    assert recovered_queue.last_source_resolution.selected_source is SelectionSourceClass.MANUAL
    with database.connect() as connection:
        history = connection.execute(
            """SELECT completion_status, result_code, skip_code, queue_id
               FROM play_history"""
        ).fetchall()
        recovered = connection.execute(
            """SELECT details FROM session_audit_events
               WHERE event_code = 'SESSION_RECOVERED'"""
        ).fetchone()
    assert [tuple(row) for row in history] == [
        ("ABORTED", "ABORTED", "APPLICATION_SHUTDOWN", playing.queue_id)
    ]
    assert recovered is not None
    assert '"audio_started": false' in str(recovered["details"])


def test_finished_session_is_not_restored(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    service = PartySessionService(repository)
    session = service.start("Abgeschlossene Party")

    service.finish(session.session_id)

    replacement = service.restore_or_start()
    assert replacement.session_id != session.session_id
    assert replacement.status == SessionStatus.ACTIVE


def test_pending_queue_from_finished_session_is_copied_to_new_session(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    service = PartySessionService(repository)
    session = service.start("Abgeschlossene Party")
    played = repository.add_queue_entry(session.session_id, 1)
    QueueService(repository, TrackRepository(database), session.session_id).mark_played(
        played.queue_id
    )
    repository.add_queue_entry(session.session_id, 2)
    service.finish(session.session_id)

    replacement = service.restore_or_start()
    restored = repository.list_queue(replacement.session_id)

    assert replacement.session_id != session.session_id
    assert replacement.status == SessionStatus.RECOVERED
    assert [entry.track_id for entry in restored] == [2]
    assert restored[0].status == QueueStatus.WAITING


def test_session_recovery_can_be_disabled(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    previous = PartySessionService(repository).start("Vorherige Party")

    fresh = PartySessionService(repository).restore_or_start(restore=False)

    assert fresh.session_id != previous.session_id
    assert fresh.status == SessionStatus.ACTIVE


@pytest.mark.parametrize(
    ("replace_waiting", "expected_track_ids"),
    [
        (True, [2, 1]),
        (False, [1, 2, 1]),
    ],
)
def test_recovered_old_queue_can_be_replaced_or_prefixed_before_cd_and_survives_restart(
    tmp_path: Path,
    replace_waiting: bool,
    expected_track_ids: list[int],
) -> None:
    database = database_with_tracks(tmp_path / f"recovery-{replace_waiting}.db")
    repository = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    session_service = PartySessionService(repository)
    original = session_service.start("Offene Veranstaltung")
    old_queue = QueueService(repository, tracks, original.session_id)
    old_entry = old_queue.add(1)
    old_queue.mark_preparing(old_entry.queue_id, "B")
    old_queue.mark_loaded(old_entry.queue_id, "B")
    saved = SavedQueueRepository(database).save(
        "Neue CD",
        [SavedQueueEntry(2, 1), SavedQueueEntry(1, 2)],
    )

    recovered = session_service.restore_or_start()
    assert recovered.session_id == original.session_id
    queue = QueueService(repository, tracks, recovered.session_id)
    recovered_old = queue.entry(old_entry.queue_id)
    assert recovered_old is not None
    assert recovered_old.status == QueueStatus.WAITING
    assert recovered_old.loaded_deck is None

    SavedQueueService(SavedQueueRepository(database), queue).load(
        saved.saved_queue_id,
        replace_waiting=replace_waiting,
    )
    assert [entry.track_id for entry in queue.entries()] == expected_track_ids

    played_track_ids: list[int] = []
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    while result := queue.load_next_into_free_deck(deck_a, deck_b):
        entry, deck = result
        queue.mark_playing(entry.queue_id)
        played_track_ids.append(entry.track_id)
        repository.add_history(
            recovered.session_id,
            entry.track_id,
            deck.model.deck_id,
            datetime.now(),
            CompletionStatus.COMPLETED,
            120.0,
            queue_id=entry.queue_id,
        )
        queue.mark_finished(entry.queue_id, QueueStatus.PLAYED)
        deck.eject()

    assert played_track_ids == expected_track_ids
    restarted = session_service.restore_or_start()
    assert restarted.session_id == recovered.session_id
    after_restart = repository.list_queue(restarted.session_id)
    assert [entry.track_id for entry in after_restart] == expected_track_ids
    assert all(entry.status == QueueStatus.PLAYED for entry in after_restart)
    with database.connect() as connection:
        history = connection.execute(
            """SELECT track_id, completion_status FROM play_history
               WHERE session_id = ? ORDER BY id""",
            (restarted.session_id,),
        ).fetchall()
    assert [int(row["track_id"]) for row in history] == expected_track_ids
    assert all(row["completion_status"] == "PLAYED" for row in history)


def test_session_lifecycle_routes_queue_mutations_through_queue_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = database_with_tracks(tmp_path / "session-mutation-routing.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Recovery")
    calls: list[tuple[str, int, int | None]] = []

    def recover(_repository: PartyPlayerRepository, session_id: int) -> None:
        calls.append(("recover", session_id, None))

    monkeypatch.setattr(
        QueueService,
        "recover_persisted_session",
        staticmethod(recover),
    )

    PartySessionService(repository).restore_or_start()

    assert calls == [("recover", session.session_id, None)]


def test_playback_history_is_created(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    repository.add_history(
        session.session_id, 1, "A", datetime.now(), CompletionStatus.COMPLETED, 150.0
    )
    with database.connect() as connection:
        row = connection.execute(
            "SELECT completion_status, play_duration FROM play_history WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
    assert row is not None
    assert row["completion_status"] == "PLAYED"
    assert row["play_duration"] == 150.0


def test_track_search_is_bounded(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    results = TrackRepository(database).search("o", limit=1)
    assert len(results) == 1


def test_deck_assignment_restores_without_playback(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    entry = repository.add_queue_entry(session.session_id, 1)
    queue = QueueService(repository, TrackRepository(database), session.session_id)
    queue.mark_loaded(entry.queue_id, "B")
    queue.mark_playing(entry.queue_id)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())

    restored = QueueService(
        repository, TrackRepository(database), session.session_id
    ).restore_deck_assignments(deck_a, deck_b)

    assert restored == ["B"]
    assert deck_b.model.loaded_track is not None
    assert not deck_b.backend.is_playing()
    restored_entry = repository.get_queue_entry(entry.queue_id)
    assert restored_entry is not None
    assert restored_entry.status == QueueStatus.LOADED


def test_queue_move_to_top_remove_and_clear_keep_contiguous_positions(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    first = service.add(1)
    second = service.add(2)
    third = service.add(1)

    service.move_to_top(third.queue_id)
    service.remove(first.queue_id)
    entries = service.entries()
    assert [entry.queue_id for entry in entries] == [third.queue_id, second.queue_id]
    assert [entry.position for entry in entries] == [1, 2]

    assert service.clear_waiting() == 2
    assert service.entries() == []


def test_queue_manual_order_priority_and_lock_actions_survive_restart(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "manual-actions.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Manual actions")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    first = service.add(1)
    second = service.add(2)
    third = service.add(1)

    service.move_to_top(third.queue_id)
    service.move_to_end(first.queue_id)
    service.set_priority(second.queue_id, 850)
    locked = service.toggle_lock(second.queue_id)

    assert locked.locked
    assert locked.lock_source == "MANUAL"
    restored = QueueService(
        PartyPlayerRepository(database),
        TrackRepository(database),
        session.session_id,
    ).entries()
    assert restored[0].queue_id == second.queue_id
    assert restored[0].priority == 850
    assert restored[0].locked
    assert restored[0].lock_source == "MANUAL"
    same_priority = [entry.queue_id for entry in restored if entry.priority == 700]
    assert same_priority == [third.queue_id, first.queue_id]

    unlocked = service.toggle_lock(second.queue_id)
    assert not unlocked.locked
    assert unlocked.lock_source == "NONE"


def test_only_unlocked_waiting_entries_can_be_moved_or_removed(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "editable-waiting.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Editable")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    locked = service.add(1)
    ready = service.add(2)
    service.toggle_lock(locked.queue_id)
    service.mark_loaded(ready.queue_id, "A")

    for operation in (
        lambda: service.move_up(locked.queue_id),
        lambda: service.move_to_end(locked.queue_id),
        lambda: service.remove(locked.queue_id),
    ):
        with pytest.raises(ValueError, match="gesperrt"):
            operation()
    for operation in (
        lambda: service.move_down(ready.queue_id),
        lambda: service.move_to_top(ready.queue_id),
        lambda: service.remove(ready.queue_id),
    ):
        with pytest.raises(ValueError, match="Nur wartende"):
            operation()

    assert service.clear_waiting() == 0
    assert service.entry(locked.queue_id) is not None


def test_system_and_manual_locks_survive_lifecycle_changes_independently(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "separate-locks.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Separate locks")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)

    service.toggle_lock(entry.queue_id)
    service.mark_preparing(entry.queue_id, "A")
    preparing = service.entry(entry.queue_id)
    assert preparing is not None
    assert preparing.locked
    assert preparing.lock_source == "MANUAL_SYSTEM"

    service.toggle_lock(entry.queue_id)
    system_only = service.entry(entry.queue_id)
    assert system_only is not None
    assert system_only.locked
    assert system_only.lock_source == "SYSTEM"

    service.mark_loaded(entry.queue_id, "A")
    service.mark_playing(entry.queue_id)
    playing = service.entry(entry.queue_id)
    assert playing is not None
    assert not playing.locked
    assert playing.lock_source == "NONE"


def test_preparation_system_lock_is_released_when_reset_to_waiting(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "reset-system-lock.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Reset system lock")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)

    service.mark_preparing(entry.queue_id, "B")
    preparing = service.entry(entry.queue_id)
    assert preparing is not None
    assert preparing.lock_source == "SYSTEM"

    service.reset_prepared(entry.queue_id)
    waiting = service.entry(entry.queue_id)
    assert waiting is not None
    assert not waiting.locked
    assert waiting.lock_source == "NONE"


def test_shuffle_preserves_locked_waiting_entry_position(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "locked-shuffle.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Locked shuffle")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    first = service.add(1)
    locked = service.add(2)
    third = service.add(1)
    service.toggle_lock(locked.queue_id)

    assert service.shuffle_waiting(random.Random(4)) == 2

    entries = service.entries()
    assert entries[1].queue_id == locked.queue_id
    assert {entries[0].queue_id, entries[2].queue_id} == {
        first.queue_id,
        third.queue_id,
    }


@pytest.mark.parametrize("status", [QueueStatus.PREPARING, QueueStatus.READY])
def test_prepared_entries_require_explicit_removal_path(
    tmp_path: Path,
    status: QueueStatus,
) -> None:
    database = database_with_tracks(tmp_path / f"remove-{status.value}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Prepared removal")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_preparing(entry.queue_id, "A")
    if status == QueueStatus.READY:
        service.mark_loaded(entry.queue_id, "A")

    with pytest.raises(ValueError, match="Nur wartende"):
        service.remove(entry.queue_id)
    service.remove_prepared(entry.queue_id)

    removed = service.entry(entry.queue_id)
    assert removed is not None
    assert removed.status == QueueStatus.REMOVED
    assert removed.loaded_deck is None


@pytest.mark.parametrize("terminal_status", [QueueStatus.FAILED, QueueStatus.SKIPPED])
def test_terminal_transition_atomically_clears_loaded_deck(
    tmp_path: Path,
    terminal_status: QueueStatus,
) -> None:
    database = database_with_tracks(tmp_path / f"clear-deck-{terminal_status.value}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Clear deck")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_preparing(entry.queue_id, "B")
    if terminal_status == QueueStatus.FAILED:
        service.mark_error(entry.queue_id)
    else:
        service.mark_loaded(entry.queue_id, "B")
        service.mark_skipped(entry.queue_id)

    terminal = service.entry(entry.queue_id)
    assert terminal is not None
    assert terminal.status == terminal_status
    assert terminal.loaded_deck is None


def test_playing_deck_release_is_idempotent_after_concurrent_completion(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "idempotent-deck-release.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Deck release")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_preparing(entry.queue_id, "A")
    service.mark_loaded(entry.queue_id, "A")
    service.mark_playing(entry.queue_id)
    service.mark_finished(entry.queue_id, QueueStatus.PLAYED)

    assert service.release_playing_deck_assignment(entry.queue_id, "A")
    assert not service.release_playing_deck_assignment(entry.queue_id, "A")

    completed = service.entry(entry.queue_id)
    assert completed is not None
    assert completed.status == QueueStatus.PLAYED
    assert completed.loaded_deck is None


def test_stale_queue_completion_does_not_finish_reprepared_entry(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "stale-queue-completion.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Stale completion")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_preparing(entry.queue_id, "A")
    service.mark_loaded(entry.queue_id, "A")

    service.mark_finished(entry.queue_id, QueueStatus.PLAYED)

    current = service.entry(entry.queue_id)
    assert current is not None
    assert current.status == QueueStatus.READY


def test_stale_deck_release_does_not_clear_a_new_assignment(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "stale-deck-release.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Stale deck release")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_preparing(entry.queue_id, "B")
    service.mark_loaded(entry.queue_id, "B")
    service.mark_playing(entry.queue_id)

    assert not service.release_playing_deck_assignment(entry.queue_id, "A")

    playing = service.entry(entry.queue_id)
    assert playing is not None
    assert playing.status == QueueStatus.PLAYING
    assert playing.loaded_deck == "B"


@pytest.mark.parametrize("status", [QueueStatus.PREPARING, QueueStatus.READY])
def test_prepared_entries_can_be_reset_before_explicit_move(
    tmp_path: Path,
    status: QueueStatus,
) -> None:
    database = database_with_tracks(tmp_path / f"move-{status.value}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Prepared move")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    first = service.add(1)
    prepared = service.add(2)
    service.mark_preparing(prepared.queue_id, "A")
    if status == QueueStatus.READY:
        service.mark_loaded(prepared.queue_id, "A")

    service.reset_prepared(prepared.queue_id)
    service.move_up(prepared.queue_id)

    entries = service.entries()
    assert [entry.queue_id for entry in entries] == [prepared.queue_id, first.queue_id]
    assert entries[0].status == QueueStatus.WAITING
    assert entries[0].loaded_deck is None


def test_next_candidate_respects_order_status_and_examined_exclusions(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "next-candidate.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Candidate")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    lower_priority = service.add(1, priority=100)
    first = service.add(2, priority=700)
    second = service.add(1, priority=700)

    assert service.get_next_candidate() == first
    assert service.get_next_candidate({first.queue_id}) == second

    service.mark_preparing(second.queue_id, "A")

    assert service.get_next_candidate({first.queue_id}) == lower_priority
    assert service.get_next_candidate({first.queue_id, lower_priority.queue_id}) is None


def test_load_candidate_skips_missing_track_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = database_with_tracks(tmp_path / "missing-candidate.db")
    repository = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    session = repository.create_session("Missing candidate")
    service = QueueService(repository, tracks, session.session_id)
    missing = service.add(1, priority=700)
    playable = service.add(2, priority=600)
    original_get = tracks.get_active
    monkeypatch.setattr(
        tracks,
        "get_active",
        lambda track_id: None if track_id == missing.track_id else original_get(track_id),
    )
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())

    candidate = service.next_load_candidate(deck_a, deck_b)

    assert candidate is not None
    assert candidate[0].queue_id == playable.queue_id
    failed = service.entry(missing.queue_id)
    assert failed is not None
    assert failed.status == QueueStatus.FAILED
    assert failed.failure_code == "TRACK_MISSING"


def test_autoload_continues_after_candidate_preparation_failure(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "failed-preparation.db")
    repository = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    session = repository.create_session("Preparation failure")
    service = QueueService(repository, tracks, session.session_id)
    broken = service.add(1, priority=700)
    playable = service.add(2, priority=600)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    original_load = deck_a.load

    def load_with_first_failure(track: object) -> None:
        if getattr(track, "id", None) == broken.track_id:
            raise RuntimeError("Decoderfehler")
        original_load(track)  # type: ignore[arg-type]

    deck_a.load = load_with_first_failure  # type: ignore[method-assign]

    result = service.load_next_into_free_deck(deck_a, deck_b)

    assert result is not None
    assert result[0].queue_id == playable.queue_id
    failed = service.entry(broken.queue_id)
    assert failed is not None
    assert failed.status == QueueStatus.FAILED
    assert failed.failure_code == "PREPARATION_FAILED"


def test_autoload_applies_selection_rules_and_continues(tmp_path: Path) -> None:
    class RejectFirstTrack:
        def evaluate(self, entry: object, track: object) -> SelectionDecision | None:
            if getattr(track, "id", None) == 1:
                return SelectionDecision.reject(
                    "BLOCKED_TRACK",
                    reason="Automatisch gesperrt",
                )
            return None

    database = database_with_tracks(tmp_path / "selection-rule.db")
    repository = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    session = repository.create_session("Selection rule")
    service = QueueService(
        repository,
        tracks,
        session.session_id,
        selection_service=TrackSelectionService((RejectFirstTrack(),)),
    )
    rejected = service.add(1, priority=700)
    playable = service.add(2, priority=600)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())

    result = service.load_next_into_free_deck(deck_a, deck_b)

    assert result is not None
    assert result[0].queue_id == playable.queue_id
    skipped = service.entry(rejected.queue_id)
    assert skipped is not None
    assert skipped.status == QueueStatus.SKIPPED
    assert skipped.skip_code == "BLOCKED_TRACK"
    assert skipped.skip_reason == "Automatisch gesperrt"


def test_autoload_skips_unavailable_file_and_uses_next_candidate(tmp_path: Path) -> None:
    class RejectFirstFile:
        def evaluate(self, track: object) -> SelectionDecision:
            if getattr(track, "id", None) == 1:
                return SelectionDecision.reject(
                    "FILE_MISSING",
                    terminal_status=QueueStatus.FAILED,
                    reason="Datei fehlt",
                )
            return SelectionDecision.allow()

    database = database_with_tracks(tmp_path / "availability-candidate.db")
    repository = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    session = repository.create_session("Availability")
    service = QueueService(
        repository,
        tracks,
        session.session_id,
        file_availability=RejectFirstFile(),
    )
    unavailable = service.add(1, priority=700)
    playable = service.add(2, priority=600)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())

    result = service.load_next_into_free_deck(deck_a, deck_b)

    assert result is not None
    assert result[0].queue_id == playable.queue_id
    failed = service.entry(unavailable.queue_id)
    assert failed is not None
    assert failed.status == QueueStatus.FAILED
    assert failed.failure_code == "FILE_MISSING"


def test_autoload_rejects_hidden_catalog_entry_before_preparation(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "hidden-candidate.db")
    repository = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    session = repository.create_session("Hidden candidate")
    service = QueueService(repository, tracks, session.session_id)
    hidden = service.add(1, priority=700)
    playable = service.add(2, priority=600)
    tracks.hide_from_catalog(1)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())

    result = service.load_next_into_free_deck(deck_a, deck_b)

    assert result is not None
    assert result[0].queue_id == playable.queue_id
    rejected = service.entry(hidden.queue_id)
    assert rejected is not None
    assert rejected.status is QueueStatus.FAILED
    assert rejected.failure_code == "TRACK_MISSING"


def test_recovered_candidate_is_revalidated_before_preparation(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "revalidate-recovery.db")
    repository = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    session = repository.create_session("Recovery")
    entry = repository.add_queue_entry(session.session_id, 1)
    with database.connect() as connection:
        connection.execute(
            """UPDATE party_queue
               SET status = 'ready', loaded_deck = 'A',
                   locked = 1, lock_source = 'SYSTEM'
               WHERE id = ?""",
            (entry.queue_id,),
        )
    PartySessionService(repository).restore_or_start()
    track = tracks.get(1)
    assert track is not None
    Path(track.file_path).unlink()
    service = QueueService(repository, tracks, session.session_id)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())

    result = service.load_next_into_free_deck(deck_a, deck_b)

    assert result is None
    recovered = service.entry(entry.queue_id)
    assert recovered is not None
    assert recovered.status is QueueStatus.FAILED
    assert recovered.failure_code == "FILE_MISSING"
    assert deck_a.model.loaded_track is None
    with database.connect() as connection:
        audit = connection.execute(
            """SELECT details FROM session_audit_events
               WHERE event_code = 'CANDIDATE_REVALIDATED'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    assert audit is not None
    assert '"accepted": false' in str(audit["details"])
    assert '"code": "FILE_MISSING"' in str(audit["details"])


def test_playing_entry_cannot_be_removed_directly_or_by_complete_clear(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "playing-delete.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Playing")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_loaded(entry.queue_id, "A")
    service.mark_playing(entry.queue_id)

    with pytest.raises(ValueError, match="spielender"):
        service.remove(entry.queue_id)
    assert service.clear_complete() == 0
    assert service.entry(entry.queue_id).status == QueueStatus.PLAYING  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "terminal_status",
    [QueueStatus.PLAYED, QueueStatus.SKIPPED, QueueStatus.FAILED],
)
def test_terminal_queue_entry_can_be_removed_directly(
    tmp_path: Path,
    terminal_status: QueueStatus,
) -> None:
    database = database_with_tracks(tmp_path / f"remove-{terminal_status.value}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Terminal")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    if terminal_status == QueueStatus.PLAYED:
        service.mark_played(entry.queue_id)
    elif terminal_status == QueueStatus.SKIPPED:
        service.mark_skipped(entry.queue_id, "Test")
    else:
        service.mark_error(entry.queue_id)

    service.remove(entry.queue_id)

    removed = service.entry(entry.queue_id)
    assert removed is not None
    assert removed.status == QueueStatus.REMOVED
    assert service.entries() == []


def test_queue_manual_status_and_retry_failed_entry(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    played = service.add(1)
    skipped = service.add(2)
    failed = service.add(1)

    service.mark_played(played.queue_id)
    service.mark_skipped(skipped.queue_id, "Wunsch zurückgezogen")
    service.mark_error(failed.queue_id)
    failed_state = service.entry(failed.queue_id)
    assert failed_state is not None
    assert failed_state.failure_code == "PREPARATION_FAILED"
    service.retry(failed.queue_id)

    entries = {entry.queue_id: entry for entry in service.entries()}
    assert entries[played.queue_id].status == QueueStatus.PLAYED
    assert entries[skipped.queue_id].status == QueueStatus.SKIPPED
    assert entries[skipped.queue_id].skip_reason == "Wunsch zurückgezogen"
    assert entries[skipped.queue_id].skip_code == "OPERATOR_SKIPPED"
    assert entries[failed.queue_id].status == QueueStatus.WAITING
    assert entries[failed.queue_id].failure_code is None


def test_skipped_entry_keeps_visible_reason_and_structured_audit_event(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "skip-audit.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Skip audit")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)

    service.mark_skipped(
        entry.queue_id,
        "Titel liegt innerhalb des Wiederholungsschutzes",
        code="TRACK_REPETITION",
    )

    stored = service.entry(entry.queue_id)
    assert stored is not None
    assert stored.status == QueueStatus.SKIPPED
    assert stored.skip_reason == "Titel liegt innerhalb des Wiederholungsschutzes"
    assert stored.skip_code == "TRACK_REPETITION"
    with database.connect() as connection:
        audit = connection.execute(
            """SELECT event_code, entity_id, details
               FROM session_audit_events
               WHERE event_code = 'QUEUE_SKIPPED' AND entity_id = ?""",
            (entry.queue_id,),
        ).fetchone()
    assert audit is not None
    assert audit["event_code"] == "QUEUE_SKIPPED"
    assert audit["entity_id"] == entry.queue_id
    assert '"skip_code": "TRACK_REPETITION"' in audit["details"]
    assert '"reason": "Titel liegt innerhalb des Wiederholungsschutzes"' in audit["details"]


def test_queue_service_validates_complete_status_lifecycle(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "status-lifecycle.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Lifecycle")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)

    service.mark_preparing(entry.queue_id, "A")
    preparing = service.entry(entry.queue_id)
    assert preparing is not None
    assert preparing.preparation_attempts == 1
    assert preparing.updated_at is not None
    service.mark_loaded(entry.queue_id, "A")
    service.mark_playing(entry.queue_id)
    service.mark_played(entry.queue_id)

    assert service.entry(entry.queue_id).status == QueueStatus.PLAYED  # type: ignore[union-attr]
    service.reset_played(entry.queue_id)
    assert service.entry(entry.queue_id).status == QueueStatus.WAITING  # type: ignore[union-attr]


def test_queue_service_rejects_invalid_status_transitions(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "invalid-transitions.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Invalid")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    waiting = service.add(1)
    skipped = service.add(2)
    removed = service.add(1)
    service.mark_skipped(skipped.queue_id)
    service.remove(removed.queue_id)

    with pytest.raises(ValueError, match="kann nicht gestartet"):
        service.mark_playing(waiting.queue_id)
    with pytest.raises(ValueError, match="Ungültiger Queue-Statusübergang"):
        service.mark_played(skipped.queue_id)
    with pytest.raises(ValueError, match="Ungültiger Queue-Statusübergang"):
        service.mark_error(removed.queue_id)


def test_prepared_entry_cannot_be_recorded_as_played_without_playback(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "prepared-not-played.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Prepared")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_preparing(entry.queue_id, "B")
    service.mark_loaded(entry.queue_id, "B")

    with pytest.raises(ValueError, match="Ungültiger Queue-Statusübergang"):
        service.mark_played(entry.queue_id)

    stored = service.entry(entry.queue_id)
    assert stored is not None
    assert stored.status == QueueStatus.READY
    assert stored.loaded_deck == "B"


def test_queue_status_machine_has_the_complete_explicit_transition_contract() -> None:
    assert QueueService._ALLOWED_TRANSITIONS == {
        QueueStatus.WAITING: {
            QueueStatus.PREPARING,
            QueueStatus.READY,
            QueueStatus.PLAYED,
            QueueStatus.SKIPPED,
            QueueStatus.FAILED,
            QueueStatus.REMOVED,
        },
        QueueStatus.PREPARING: {
            QueueStatus.READY,
            QueueStatus.WAITING,
            QueueStatus.SKIPPED,
            QueueStatus.FAILED,
            QueueStatus.REMOVED,
        },
        QueueStatus.READY: {
            QueueStatus.PLAYING,
            QueueStatus.WAITING,
            QueueStatus.SKIPPED,
            QueueStatus.FAILED,
            QueueStatus.REMOVED,
        },
        QueueStatus.PLAYING: {
            QueueStatus.READY,
            QueueStatus.PLAYED,
            QueueStatus.SKIPPED,
            QueueStatus.FAILED,
            QueueStatus.REMOVED,
        },
        QueueStatus.PLAYED: {
            QueueStatus.PLAYING,
            QueueStatus.WAITING,
            QueueStatus.REMOVED,
        },
        QueueStatus.SKIPPED: {QueueStatus.WAITING, QueueStatus.REMOVED},
        QueueStatus.FAILED: {QueueStatus.WAITING, QueueStatus.REMOVED},
        QueueStatus.REMOVED: set(),
    }


def test_finishing_deck_does_not_mark_preloaded_track_as_played(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "finish-preloaded.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_loaded(entry.queue_id, "A")

    service.mark_finished_for_deck("A", QueueStatus.PLAYED)

    finished = service.entry(entry.queue_id)
    assert finished is not None
    assert finished.status == QueueStatus.SKIPPED
    assert finished.skip_reason == "Deck beendet, bevor der Titel gestartet wurde"


def test_finishing_deck_marks_actually_playing_track_as_played(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "finish-playing.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_loaded(entry.queue_id, "A")
    service.mark_playing(entry.queue_id)

    service.mark_finished_for_deck("A", QueueStatus.PLAYED)

    finished = service.entry(entry.queue_id)
    assert finished is not None
    assert finished.status == QueueStatus.PLAYED
    assert finished.skip_reason is None


def test_played_queue_entry_can_be_reset_to_waiting(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    entry = service.add(1)
    service.mark_played(entry.queue_id)

    service.reset_played(entry.queue_id)

    reset = repository.get_queue_entry(entry.queue_id)
    assert reset is not None
    assert reset.status == QueueStatus.WAITING
    assert reset.loaded_deck is None
    assert reset.played_at is None


def test_queue_duplicate_policy_blocks_only_active_duplicates(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(
        repository, TrackRepository(database), session.session_id, allow_duplicates=False
    )
    first = service.add(1)
    with pytest.raises(ValueError, match="bereits"):
        service.add(1)

    service.mark_played(first.queue_id)
    assert service.add(1).track_id == 1


def test_guest_duplicate_can_merge_requests_and_unique_requesters(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "guest-merge.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest merge")
    service = QueueService(
        repository,
        TrackRepository(database),
        session.session_id,
        guest_duplicate_policy="merge",
    )

    first = service.add_guest_request(1, " Alice ")
    merged = service.add_guest_request(1, "alice")
    merged = service.add_guest_request(1, "Bob")

    assert merged.queue_id == first.queue_id
    assert merged.request_count == 3
    assert merged.unique_requester_count == 2
    assert len(service.entries()) == 1


@pytest.mark.parametrize("prepared", [False, True])
def test_guest_duplicate_reject_policy_covers_active_states(
    tmp_path: Path,
    prepared: bool,
) -> None:
    database = database_with_tracks(tmp_path / f"guest-reject-{prepared}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest reject")
    service = QueueService(
        repository,
        TrackRepository(database),
        session.session_id,
        guest_duplicate_policy="reject",
    )
    entry = service.add_guest_request(1, "Alice")
    if prepared:
        service.mark_preparing(entry.queue_id, "A")

    with pytest.raises(ValueError, match="bereits aktiv"):
        service.add_guest_request(1, "Bob")


def test_guest_request_rejects_recently_completed_track(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "guest-recent.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest recent")
    repository.add_history(
        session.session_id,
        1,
        "A",
        datetime.now(),
        CompletionStatus.COMPLETED,
        120,
    )
    service = QueueService(
        repository,
        TrackRepository(database),
        session.session_id,
        guest_recent_minutes=120,
    )

    with pytest.raises(ValueError, match="vor Kurzem"):
        service.add_guest_request(1, "Alice")


@pytest.mark.parametrize(
    ("guest_priority", "expected"),
    [
        (GuestPriority.NORMAL, 600),
        ("high", 650),
        (GuestPriority.VIP, 690),
    ],
)
def test_guest_priority_is_bounded_below_manual_priority(
    tmp_path: Path,
    guest_priority: GuestPriority | str,
    expected: int,
) -> None:
    database = database_with_tracks(tmp_path / f"guest-{expected}.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest priority")
    service = QueueService(repository, TrackRepository(database), session.session_id)

    entry = service.add_guest_request(
        1,
        "",
        guest_priority=guest_priority,
    )

    assert entry.requested_by == ""
    assert entry.priority == expected
    assert entry.priority < QueueSource.MANUAL.default_priority


def test_guest_priority_rejects_unbounded_custom_value(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "guest-invalid-priority.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest priority")
    service = QueueService(repository, TrackRepository(database), session.session_id)

    with pytest.raises(ValueError, match="NORMAL, HIGH oder VIP"):
        service.add_guest_request(1, "Pseudonym", guest_priority="999")


def test_guest_limits_active_requests_and_minimum_interval(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "guest-limits.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest limits")
    now = datetime(2026, 7, 27, 12, 0)
    service = QueueService(
        repository,
        TrackRepository(database),
        session.session_id,
        maximum_active_guest_requests=1,
        minimum_guest_request_interval_seconds=60,
        wall_clock=lambda: now,
    )
    service.add_guest_request(1, "Alice")

    with pytest.raises(ValueError, match="mehr Abstand"):
        service.add_guest_request(2, " alice ")

    later_service = QueueService(
        repository,
        TrackRepository(database),
        session.session_id,
        maximum_active_guest_requests=1,
        minimum_guest_request_interval_seconds=60,
        wall_clock=lambda: now + timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="Maximale Anzahl"):
        later_service.add_guest_request(2, "Alice")


def test_guest_limit_blocks_another_consecutive_played_track(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "guest-consecutive.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest consecutive")
    now = datetime(2026, 7, 27, 12, 0)
    service = QueueService(
        repository,
        TrackRepository(database),
        session.session_id,
        maximum_consecutive_guest_tracks=1,
        wall_clock=lambda: now,
    )
    first = service.add_guest_request(1, "Alice")
    service.mark_played(first.queue_id)
    repository.add_history(
        session.session_id,
        1,
        "A",
        now - timedelta(minutes=3),
        CompletionStatus.COMPLETED,
        180,
        queue_id=first.queue_id,
        completed_at=now - timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="aufeinanderfolgende"):
        service.add_guest_request(2, "alice")


def test_equal_priority_guest_candidates_are_selected_in_fair_rounds(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "guest-fairness.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest fairness")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    alice_first = service.add(1, QueueSource.GUEST_REQUEST, "Alice")
    alice_second = service.add(2, QueueSource.GUEST_REQUEST, "alice")
    bob_first = service.add(1, QueueSource.GUEST_REQUEST, "Bob")
    bob_second = service.add(2, QueueSource.GUEST_REQUEST, " bob ")

    selected: list[int] = []
    excluded: set[int] = set()
    while candidate := service.get_next_candidate(excluded):
        selected.append(candidate.queue_id)
        excluded.add(candidate.queue_id)

    assert selected == [
        alice_first.queue_id,
        bob_first.queue_id,
        alice_second.queue_id,
        bob_second.queue_id,
    ]
    assert [entry.queue_id for entry in service.entries()] == [
        alice_first.queue_id,
        alice_second.queue_id,
        bob_first.queue_id,
        bob_second.queue_id,
    ]


def test_optional_guest_popularity_never_reaches_manual_priority(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "guest-popularity.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Guest popularity")
    service = QueueService(
        repository,
        TrackRepository(database),
        session.session_id,
        guest_popularity_enabled=True,
        guest_popularity_points_per_request=50,
    )
    entry = service.add_guest_request(1, "Alice", guest_priority=GuestPriority.VIP)

    for requester in ("Bob", "Chris", "Dana"):
        entry = service.add_guest_request(
            1,
            requester,
            guest_priority=GuestPriority.VIP,
        )

    assert entry.priority == QueueSource.MANUAL.default_priority - 1

    manual = service.add(2, QueueSource.MANUAL)
    service.add_guest_request(2, "Eve", guest_priority=GuestPriority.VIP)
    preserved = service.entry(manual.queue_id)
    assert preserved is not None
    assert preserved.priority == QueueSource.MANUAL.default_priority


def test_shuffle_changes_only_waiting_queue_slots(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    first = service.add(1)
    loaded = service.add(2)
    third = service.add(1)
    fourth = service.add(2)
    service.mark_loaded(loaded.queue_id, "A")

    assert service.shuffle_waiting(random.Random(1)) == 3
    entries = service.entries()

    assert entries[1].queue_id == loaded.queue_id
    assert entries[1].status == QueueStatus.LOADED
    waiting_order = [entry.queue_id for entry in entries if entry.status == QueueStatus.WAITING]
    assert waiting_order != [first.queue_id, third.queue_id, fourth.queue_id]
    assert sorted(waiting_order) == sorted([first.queue_id, third.queue_id, fourth.queue_id])
    assert [entry.position for entry in entries] == [1, 2, 3, 4]


def test_cue_overrides_stay_with_entry_through_move_shuffle_reset_and_restart(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    first = service.add(1)
    second = service.add(2)
    third = service.add(1)
    expected = {
        first.queue_id: (1.0, 90.0, 5.0),
        second.queue_id: (2.0, 100.0, 6.0),
        third.queue_id: (3.0, 110.0, 7.0),
    }
    for queue_id, values in expected.items():
        service.set_cue_overrides(queue_id, *values, source="snapshot")

    service.move_to_top(third.queue_id)
    service.shuffle_waiting(random.Random(2))
    service.mark_played(second.queue_id)
    service.reset_played(second.queue_id)

    restored = PartyPlayerRepository(database).list_queue(session.session_id)
    assert {entry.queue_id for entry in restored} == set(expected)
    for entry in restored:
        assert (
            entry.cue_in_override,
            entry.cue_out_override,
            entry.fade_duration_override,
        ) == expected[entry.queue_id]
        assert entry.cue_override_source == "snapshot"


def test_active_deck_assignment_rejects_a_second_queue_entry(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    assigned_a = service.add(1)
    orphaned_a = service.add(2)
    assigned_b = service.add(2)
    service.mark_loaded(assigned_a.queue_id, "A")
    with pytest.raises(ValueError, match="bereits einem Queue-Eintrag"):
        service.mark_loaded(orphaned_a.queue_id, "A")
    service.mark_loaded(assigned_b.queue_id, "B")
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())
    track_repository = TrackRepository(database)
    track_a = track_repository.get(1)
    track_b = track_repository.get(2)
    assert track_a is not None and track_b is not None
    deck_a.load(track_a)
    deck_b.load(track_b)

    service.reconcile_deck_assignments(deck_a, deck_b)
    assert service.clear_waiting() == 1

    entries = service.entries()
    assert [entry.queue_id for entry in entries] == [assigned_a.queue_id, assigned_b.queue_id]
    assert all(entry.status == QueueStatus.LOADED for entry in entries)


def test_reconcile_does_not_reset_playing_entry_from_detached_outgoing_deck(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "transition-race.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    playing = service.add(1)
    service.mark_loaded(playing.queue_id, "A")
    service.mark_playing(playing.queue_id)
    deck_a = DeckController("A", FakeAudioBackend())
    deck_b = DeckController("B", FakeAudioBackend())

    assert not service.reconcile_deck_assignments(deck_a, deck_b)

    entry = service.entry(playing.queue_id)
    assert entry is not None
    assert entry.status == QueueStatus.PLAYING
    assert entry.loaded_deck == "A"


def test_complete_clear_removes_every_visible_queue_status(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "test.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Test")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    played = service.add(1)
    loaded = service.add(2)
    service.mark_played(played.queue_id)
    service.mark_loaded(loaded.queue_id, "A")

    assert service.clear_complete() == 2
    assert service.entries() == []


def test_complete_clear_uses_one_bulk_repository_operation(tmp_path: Path) -> None:
    database = database_with_tracks(tmp_path / "bulk-complete-clear.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Bulk clear")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    for _index in range(75):
        service.add(1)
    failed = service.entries()[0]
    service.mark_error(failed.queue_id)
    calls = 0
    original = repository.clear_complete_queue

    def observed_clear(session_id: int) -> int:
        nonlocal calls
        calls += 1
        return original(session_id)

    repository.clear_complete_queue = observed_clear  # type: ignore[method-assign]

    assert service.clear_complete() == 75
    assert calls == 1
    assert service.entries() == []


def test_waiting_clear_uses_one_bulk_operation_and_preserves_locks(
    tmp_path: Path,
) -> None:
    database = database_with_tracks(tmp_path / "bulk-waiting-clear.db")
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Bulk waiting clear")
    service = QueueService(repository, TrackRepository(database), session.session_id)
    for _index in range(75):
        service.add(1)
    locked = service.entries()[0]
    service.toggle_lock(locked.queue_id)
    calls = 0
    original = repository.clear_waiting_queue

    def observed_clear(session_id: int) -> int:
        nonlocal calls
        calls += 1
        return original(session_id)

    repository.clear_waiting_queue = observed_clear  # type: ignore[method-assign]

    assert service.clear_waiting() == 74
    assert calls == 1
    remaining = service.entries()
    assert [entry.queue_id for entry in remaining] == [locked.queue_id]
    assert remaining[0].locked
