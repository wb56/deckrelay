"""Persistent history-backed repetition rules."""

from datetime import datetime, timedelta
from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import CompletionStatus, QueueSource, QueueStatus
from party_player.models import QueueEntry, Track
from party_player.selection_decision import (
    RuleOutcome,
    SelectionContext,
    SelectionRuleInput,
)
from party_player.repetition_policy import (
    PersistentRepetitionService,
    RepetitionHistoryRepository,
)
from party_player.repository import PartyPlayerRepository


def _database(path: Path) -> tuple[Database, int]:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO tracks (id, file_path, title, artist)
               VALUES (?, ?, ?, ?)""",
            [
                (1, "one.mp3", "One", "Artist A"),
                (2, "two.mp3", "Two", "Artist B"),
                (3, "three.mp3", "Three", " artist A "),
            ],
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Repetition")
    return database, session.session_id


def _record(
    database: Database,
    session_id: int,
    track_id: int,
    finished_at: datetime,
) -> None:
    PartyPlayerRepository(database).add_history(
        session_id,
        track_id,
        "A",
        finished_at - timedelta(minutes=3),
        CompletionStatus.COMPLETED,
        180,
        completed_at=finished_at,
    )


def test_track_repetition_uses_count_and_time_windows(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "track-repeat.db")
    now = datetime(2026, 7, 27, 12, 0)
    _record(database, session_id, 1, now - timedelta(minutes=30))
    _record(database, session_id, 2, now - timedelta(minutes=5))
    service = PersistentRepetitionService(
        RepetitionHistoryRepository(database),
        track_window_size=1,
        track_window_minutes=120,
        artist_window_size=0,
        artist_window_minutes=0,
        clock=lambda: now,
    )

    decision = service.evaluate(
        QueueEntry(7, 1, 1, QueueStatus.WAITING),
        Track(1, "one.mp3", "One", "Artist A", "", 180),
    )

    assert decision is not None
    assert decision.code == "TRACK_REPETITION"


def test_track_repetition_relaxes_only_at_track_distance(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "track-relaxation.db")
    now = datetime(2026, 7, 27, 12, 0)
    _record(database, session_id, 1, now - timedelta(minutes=5))
    service = PersistentRepetitionService(
        RepetitionHistoryRepository(database),
        track_window_size=1,
        track_window_minutes=0,
        artist_window_size=0,
        artist_window_minutes=0,
        clock=lambda: now,
    )
    entry = QueueEntry(7, 1, 1, QueueStatus.WAITING)
    track = Track(1, "one.mp3", "One", "Artist A", "", 180)
    rule_input = SelectionRuleInput.from_values(entry, track)

    strict = service.evaluate_rule(rule_input, SelectionContext("strict", "STRICT"))
    artist = service.evaluate_rule(
        rule_input,
        SelectionContext(
            "artist",
            "ARTIST_DISTANCE",
            frozenset({"ARTIST_REPETITION"}),
        ),
    )
    track_distance = service.evaluate_rule(
        rule_input,
        SelectionContext(
            "track",
            "TRACK_DISTANCE",
            frozenset({"ARTIST_REPETITION", "TRACK_REPETITION"}),
        ),
    )

    assert strict.result_code is RuleOutcome.EXCLUDE
    assert artist.result_code is RuleOutcome.EXCLUDE
    assert track_distance.result_code is RuleOutcome.RELAXED


def test_artist_repetition_uses_normalized_identity_and_override(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "artist-repeat.db")
    now = datetime(2026, 7, 27, 12, 0)
    _record(database, session_id, 1, now - timedelta(minutes=10))
    entry = QueueEntry(8, 3, 1, QueueStatus.WAITING)
    track = Track(3, "three.mp3", "Three", "ARTIST   A", "", 180)
    service = PersistentRepetitionService(
        RepetitionHistoryRepository(database),
        track_window_size=0,
        track_window_minutes=0,
        artist_window_size=5,
        artist_window_minutes=20,
        clock=lambda: now,
    )

    decision = service.evaluate(entry, track)
    assert decision is not None
    assert decision.code == "ARTIST_REPETITION"

    service.allow_queue_entry(entry.queue_id)
    assert service.evaluate(entry, track) is None


def test_artist_repetition_relaxes_at_artist_distance(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "artist-relaxation.db")
    now = datetime(2026, 7, 27, 12, 0)
    _record(database, session_id, 1, now - timedelta(minutes=5))
    service = PersistentRepetitionService(
        RepetitionHistoryRepository(database),
        track_window_size=0,
        track_window_minutes=0,
        artist_window_size=5,
        artist_window_minutes=20,
        clock=lambda: now,
    )
    entry = QueueEntry(8, 3, 1, QueueStatus.WAITING)
    track = Track(3, "three.mp3", "Three", "Artist A", "", 180)
    rule_input = SelectionRuleInput.from_values(entry, track)

    strict = service.evaluate_rule(rule_input, SelectionContext("strict", "STRICT"))
    relaxed = service.evaluate_rule(
        rule_input,
        SelectionContext(
            "artist",
            "ARTIST_DISTANCE",
            frozenset({"ARTIST_REPETITION"}),
        ),
    )

    assert strict.reason_code == "ARTIST_REPETITION"
    assert strict.result_code is RuleOutcome.EXCLUDE
    assert relaxed.reason_code == "ARTIST_REPETITION"
    assert relaxed.result_code is RuleOutcome.RELAXED


def test_artist_repetition_can_be_disabled_only_for_explicit_queue_sources(
    tmp_path: Path,
) -> None:
    database, session_id = _database(tmp_path / "queue-artist-switch.db")
    now = datetime(2026, 7, 27, 12, 0)
    _record(database, session_id, 1, now - timedelta(minutes=5))
    service = PersistentRepetitionService(
        RepetitionHistoryRepository(database),
        track_window_size=0,
        track_window_minutes=0,
        artist_window_size=5,
        artist_window_minutes=20,
        clock=lambda: now,
    )
    service.queue_artist_repetition_enabled = False
    track = Track(3, "three.mp3", "Three", "Artist A", "", 180)

    manual = QueueEntry(10, 3, 1, QueueStatus.WAITING, source=QueueSource.MANUAL)
    playlist = QueueEntry(11, 3, 2, QueueStatus.WAITING, source=QueueSource.PLAYLIST)
    automatic = QueueEntry(12, 3, 3, QueueStatus.WAITING, source=QueueSource.AUTOMATIC)

    assert service.evaluate(manual, track) is None
    assert service.evaluate(playlist, track) is None
    decision = service.evaluate(automatic, track)
    assert decision is not None and decision.code == "ARTIST_REPETITION"


def test_guest_rule_uses_stricter_source_window_and_cannot_weaken_global(
    tmp_path: Path,
) -> None:
    database, session_id = _database(tmp_path / "guest-repeat.db")
    now = datetime(2026, 7, 27, 12, 0)
    _record(database, session_id, 1, now - timedelta(hours=3))
    _record(database, session_id, 2, now - timedelta(minutes=5))
    track = Track(1, "one.mp3", "One", "Artist A", "", 180)
    service = PersistentRepetitionService(
        RepetitionHistoryRepository(database),
        track_window_size=1,
        track_window_minutes=0,
        artist_window_size=0,
        artist_window_minutes=0,
        guest_track_window_size=2,
        automatic_track_window_size=0,
        clock=lambda: now,
    )

    guest = QueueEntry(
        9,
        1,
        1,
        QueueStatus.WAITING,
        source=QueueSource.GUEST_REQUEST,
    )
    automatic = QueueEntry(
        10,
        1,
        1,
        QueueStatus.WAITING,
        source=QueueSource.AUTOMATIC,
    )

    assert service.evaluate(guest, track) is not None
    assert service.evaluate(automatic, track) is None


def test_partial_play_enters_repetition_history_at_configured_ratio(
    tmp_path: Path,
) -> None:
    database, session_id = _database(tmp_path / "partial-repeat.db")
    repository = PartyPlayerRepository(database)
    now = datetime(2026, 7, 27, 12, 0)
    repository.add_history(
        session_id,
        1,
        "A",
        now - timedelta(minutes=10),
        CompletionStatus.PARTIALLY_PLAYED,
        40.0,
        completed_at=now - timedelta(minutes=9),
        effective_duration=100.0,
        playback_ratio=0.4,
    )
    repository.add_history(
        session_id,
        2,
        "A",
        now - timedelta(minutes=5),
        CompletionStatus.PARTIALLY_PLAYED,
        60.0,
        completed_at=now - timedelta(minutes=4),
        effective_duration=100.0,
        playback_ratio=0.6,
    )

    history = RepetitionHistoryRepository(
        database,
        partial_playback_ratio_threshold=0.5,
    )

    assert [play.track_id for play in history.recent_completed(10)] == [2]
    assert [play.track_id for play in history.completed_since(now - timedelta(hours=1))] == [2]
