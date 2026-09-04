"""History-aware deterministic automatic selection tests."""

from datetime import datetime, timedelta
from pathlib import Path
import random
import logging
from threading import Event, Thread
from types import SimpleNamespace

import party_player.automatic_selection as automatic_selection_module
import pytest
from party_player.automatic_selection import (
    AutomaticRecentTrackRule,
    AutomaticSelectionHistory,
    AutomaticSelectionService,
)
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import CompletionStatus, EmptyQueuePolicy, QueueSource, QueueStatus
from party_player.queue_service import QueueService
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.models import QueueEntry, SavedQueueEntry, Track
from party_player.repositories.saved_queue_repository import SavedQueueRepository
from party_player.track_selection import TrackSelectionService
from party_player.track_selection import SelectionDecision
from party_player.track_suitability import (
    TrackSuitabilityRepository,
    TrackSuitabilityService,
)
from party_player.selection_decision import RuleOutcome, SelectionOutcome
from party_player.selection_decision import SelectionContext, SelectionRuleInput
from party_player.emergency_playlist import EmergencyMediaType, LocalEmergencyPlaylistService
from party_player.emergency_storage import EmergencyDriveKind, EmergencyStoragePolicy
from party_player.file_availability import FileAvailabilityService


def _database(path: Path) -> tuple[Database, int]:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO tracks (id, file_path, title, artist)
               VALUES (?, ?, ?, ?)""",
            [
                (1, str(path.parent / "one.mp3"), "One", "A"),
                (2, str(path.parent / "two.mp3"), "Two", "B"),
                (3, str(path.parent / "three.mp3"), "Three", "C"),
            ],
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Automatic")
    return database, session.session_id


def _played(
    repository: PartyPlayerRepository,
    session_id: int,
    track_id: int,
    completed_at: datetime,
) -> None:
    repository.add_history(
        session_id,
        track_id,
        "A",
        completed_at - timedelta(minutes=3),
        CompletionStatus.COMPLETED,
        180,
        completed_at=completed_at,
    )


def test_selection_avoids_recent_and_prefers_lower_play_count(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "automatic.db")
    repository = PartyPlayerRepository(database)
    now = datetime(2026, 7, 27, 12, 0)
    _played(repository, session_id, 1, now - timedelta(minutes=3))
    _played(repository, session_id, 1, now - timedelta(minutes=2))
    _played(repository, session_id, 2, now - timedelta(minutes=1))
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        recent_track_limit=1,
        randomizer=random.Random(4),
    )

    selected = selector.select(TrackSelectionService())

    assert selected is not None
    assert selected.id == 3
    assert selector.last_rationale is not None
    assert selector.last_rationale.outcome is SelectionOutcome.ACCEPTED
    assert selector.last_rationale.selected_candidate is not None
    assert selector.last_rationale.selected_candidate.track_id == selected.id
    assert selector.last_rationale.tie_break_method == "LOWEST_PLAY_COUNT_THEN_INJECTED_RNG"


def test_selection_rationale_explains_relaxation_without_changing_rng_result(
    tmp_path: Path,
) -> None:
    class ArtistDistanceRule:
        rule_id = "test.artist_distance"
        rule_version = 1

        def evaluate(self, _entry, _track):
            return SelectionDecision.reject("ARTIST_REPETITION", reason="too recent")

    database, _session_id = _database(tmp_path / "rationale-relaxation.db")
    expected = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        randomizer=random.Random(11),
    ).select(TrackSelectionService((ArtistDistanceRule(),)))
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        randomizer=random.Random(11),
    )

    selected = selector.select(TrackSelectionService((ArtistDistanceRule(),)))

    assert selected is not None and expected is not None
    assert selected.id == expected.id
    assert selector.last_rationale is not None
    assert selector.last_rationale.relaxation_stage == "ARTIST_DISTANCE"
    assert any(
        evaluation.result_code is RuleOutcome.RELAXED
        and evaluation.reason_code == "ARTIST_REPETITION"
        for evaluation in selector.last_rationale.rule_evaluations
    )


def test_recent_track_rule_is_relaxed_only_at_track_distance() -> None:
    entry = QueueEntry(
        -1,
        1,
        0,
        QueueStatus.WAITING,
        source=QueueSource.AUTOMATIC,
    )
    track = Track(1, "one.mp3", "One", "Artist", "", 120.0)
    rule_input = SelectionRuleInput.from_values(entry, track)
    rule = AutomaticRecentTrackRule({1})

    strict = rule.evaluate_rule(rule_input, SelectionContext("strict", "STRICT"))
    artist = rule.evaluate_rule(
        rule_input,
        SelectionContext(
            "artist",
            "ARTIST_DISTANCE",
            frozenset({"ARTIST_REPETITION"}),
        ),
    )
    track_distance = rule.evaluate_rule(
        rule_input,
        SelectionContext(
            "track",
            "TRACK_DISTANCE",
            frozenset({"ARTIST_REPETITION", "TRACK_REPETITION", "RECENT_TRACK"}),
        ),
    )

    assert strict.result_code is RuleOutcome.EXCLUDE
    assert artist.result_code is RuleOutcome.EXCLUDE
    assert track_distance.result_code is RuleOutcome.RELAXED


def test_selection_rationale_is_bounded_and_keeps_selected_candidate(tmp_path: Path) -> None:
    database, _session_id = _database(tmp_path / "bounded-rationale.db")
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id, file_path, title, artist) VALUES (?, ?, ?, ?)",
            [
                (track_id, f"{track_id}.mp3", f"Track {track_id}", f"Artist {track_id}")
                for track_id in range(4, 65)
            ],
        )
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        randomizer=random.Random(5),
    )

    selected = selector.select(TrackSelectionService())

    assert selected is not None
    assert selector.last_rationale is not None
    assert len(selector.last_rationale.evaluated_candidates) == 50
    assert selector.last_rationale.evaluated_candidate_count == 64
    assert selector.last_rationale.omitted_candidate_count == 14
    assert selector.last_rationale.selected_candidate is not None
    assert selector.last_rationale.selected_candidate.track_id == selected.id
    assert selector.last_rationale.warnings
    selected_summaries = [
        evaluation
        for evaluation in selector.last_rationale.evaluated_candidates
        if evaluation.accepted and evaluation.candidate.track_id == selected.id
    ]
    assert len(selected_summaries) == 1
    assert all(
        evaluation.relaxation_stage == selector.last_rationale.relaxation_stage
        for evaluation in selected_summaries[0].rules
    )


def test_overlapping_selections_publish_the_last_completed_rationale(
    tmp_path: Path, monkeypatch
) -> None:
    class BlockingFirstRule:
        rule_id = "test.blocking"
        rule_version = 1

        def __init__(self) -> None:
            self.first_started = Event()
            self.release_first = Event()
            self.calls = 0

        def evaluate(self, _entry, _track):
            self.calls += 1
            if self.calls == 1:
                self.first_started.set()
                assert self.release_first.wait(timeout=5)
            return None

    database, _session_id = _database(tmp_path / "parallel-rationale.db")
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        randomizer=random.Random(2),
    )
    rule = BlockingFirstRule()
    contexts = iter([SimpleNamespace(hex="first-context"), SimpleNamespace(hex="second-context")])
    monkeypatch.setattr(automatic_selection_module.uuid, "uuid4", lambda: next(contexts))
    results = []

    first = Thread(target=lambda: results.append(selector.select(TrackSelectionService((rule,)))))
    second = Thread(target=lambda: results.append(selector.select(TrackSelectionService((rule,)))))
    first.start()
    assert rule.first_started.wait(timeout=5)
    second.start()
    rule.release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2
    assert selector.last_rationale is not None
    assert selector.last_rationale.context_id == "second-context"


def test_failed_selection_clears_previous_rationale(tmp_path: Path, monkeypatch) -> None:
    database, _session_id = _database(tmp_path / "failed-rationale.db")
    history = AutomaticSelectionHistory(database)
    selector = AutomaticSelectionService(
        TrackRepository(database),
        history,
        randomizer=random.Random(2),
    )
    assert selector.select(TrackSelectionService()) is not None
    assert selector.last_rationale is not None

    def fail(_limit: int) -> set[int]:
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(history, "recent_track_ids", fail)

    with pytest.raises(RuntimeError, match="history unavailable"):
        selector.select(TrackSelectionService())

    assert selector.last_rationale is None
    assert selector.last_relaxation_stage == "NONE"


def test_empty_queue_can_create_automatic_entry_with_injected_rng(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "queue-automatic.db")
    tracks = TrackRepository(database)
    selector = AutomaticSelectionService(
        tracks,
        AutomaticSelectionHistory(database),
        randomizer=random.Random(7),
    )
    service = QueueService(
        PartyPlayerRepository(database),
        tracks,
        session_id,
        empty_queue_policy=EmptyQueuePolicy.AUTOMATIC_SELECTION,
        automatic_selection=selector,
    )

    candidate = service.get_next_candidate()

    assert candidate is not None
    assert candidate.source is QueueSource.AUTOMATIC
    assert candidate.priority == QueueSource.AUTOMATIC.default_priority
    assert len(service.entries()) == 1
    with database.connect() as connection:
        events = [
            str(row["event_code"])
            for row in connection.execute("SELECT event_code FROM session_audit_events ORDER BY id")
        ]
    assert events == ["RULE_RELAXATION", "AUTOMATIC_SELECTION", "QUEUE_ADDED"]


def test_empty_queue_does_not_add_track_requiring_suitability_approval(
    tmp_path: Path,
) -> None:
    database, session_id = _database(tmp_path / "queue-approval-required.db")
    tracks = TrackRepository(database)
    selector = AutomaticSelectionService(
        tracks,
        AutomaticSelectionHistory(database),
        randomizer=random.Random(7),
    )
    service = QueueService(
        PartyPlayerRepository(database),
        tracks,
        session_id,
        empty_queue_policy=EmptyQueuePolicy.AUTOMATIC_SELECTION,
        automatic_selection=selector,
        selection_service=TrackSelectionService(
            (TrackSuitabilityService(TrackSuitabilityRepository(database)),)
        ),
    )

    assert service.get_next_candidate() is None
    assert selector.last_relaxation_stage == "NO_SAFE_CANDIDATE"
    assert service.entries() == []


def test_selection_logs_and_exposes_used_relaxation_stage(
    tmp_path: Path,
    caplog,
) -> None:
    class ArtistDistanceRule:
        def evaluate(self, _entry, _track):
            return SelectionDecision.reject("ARTIST_REPETITION")

    database, _session_id = _database(tmp_path / "relaxation.db")
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        randomizer=random.Random(2),
    )

    with caplog.at_level(logging.WARNING):
        selected = selector.select(TrackSelectionService((ArtistDistanceRule(),)))

    assert selected is not None
    assert selector.last_relaxation_stage == "ARTIST_DISTANCE"
    assert "Regelentspannung ARTIST_DISTANCE" in caplog.text


def test_selection_logs_bounded_structured_decision_fields(tmp_path: Path, caplog) -> None:
    database, _session_id = _database(tmp_path / "structured-selection-log.db")
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
        randomizer=random.Random(2),
    )

    with caplog.at_level(logging.INFO):
        selected = selector.select(TrackSelectionService())

    assert selected is not None
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Automatische Auswahlentscheidung"
    )
    assert record.selection_context_id  # type: ignore[attr-defined]
    assert record.selection_outcome == "ACCEPTED"  # type: ignore[attr-defined]
    assert record.reason_code == "SELECTED"  # type: ignore[attr-defined]
    assert record.relaxation_stage == "STRICT"  # type: ignore[attr-defined]


def test_local_emergency_playlist_is_last_stage_and_keeps_hard_blocks(
    tmp_path: Path,
) -> None:
    class BlockEmergency:
        def evaluate(self, _entry, track):
            if track.id == 3:
                return SelectionDecision.reject("BLOCKED_TRACK")
            return None

    database, _session_id = _database(tmp_path / "emergency.db")
    emergency_file = tmp_path / "three.mp3"
    emergency_file.write_bytes(b"local emergency audio")
    tracks = TrackRepository(database)
    emergency = LocalEmergencyPlaylistService(
        tracks,
        FileAvailabilityService(network_retry_delay_seconds=0),
        [3],
    )
    selector = AutomaticSelectionService(
        tracks,
        AutomaticSelectionHistory(database),
        emergency_playlist=emergency,
    )

    selected = selector.select_emergency(TrackSelectionService())
    assert selected is not None
    assert selected.id == 3
    assert selector.last_relaxation_stage == "EMERGENCY_PLAYLIST"

    assert selector.select_emergency(TrackSelectionService((BlockEmergency(),))) is None
    assert selector.last_relaxation_stage == "NO_SAFE_CANDIDATE"


def test_emergency_playlist_exposes_timestamped_readiness_and_rejection_reasons(
    tmp_path: Path,
) -> None:
    database, _session_id = _database(tmp_path / "emergency-validation.db")
    local_file = tmp_path / "three.mp3"
    local_file.write_bytes(b"local emergency audio")
    events: list[tuple[str, dict[str, object]]] = []

    emergency = LocalEmergencyPlaylistService(
        TrackRepository(database),
        FileAvailabilityService(network_retry_delay_seconds=0),
        [999, 3, 3],
        audit=lambda code, details: events.append((code, details)),
    )

    validation = emergency.validation()
    assert validation.ready
    assert validation.primary_track_id == 3
    assert validation.accepted_track_ids == (3,)
    assert [(issue.track_id, issue.code) for issue in validation.issues] == [(999, "TRACK_MISSING")]
    assert validation.validated_at
    assert events[0][0] == "EMERGENCY_PLAYLIST_VALIDATED"
    assert events[0][1]["ready"] is True


def test_emergency_playlist_rejects_track_outside_approved_ssd_root(tmp_path: Path) -> None:
    database, _session_id = _database(tmp_path / "emergency-storage.db")
    policy = EmergencyStoragePolicy(
        [tmp_path / "approved"],
        drive_classifier=lambda _path: EmergencyDriveKind.FIXED,
    )

    emergency = LocalEmergencyPlaylistService(
        TrackRepository(database),
        FileAvailabilityService(network_retry_delay_seconds=0),
        [3],
        storage_policy=policy,
    )

    assert not emergency.validation().ready
    assert emergency.validation().issues[0].code == "OUTSIDE_APPROVED_LOCAL_SSD_ROOT"


def test_emergency_media_roles_are_separate_and_only_break_music_can_loop(
    tmp_path: Path,
) -> None:
    database, _session_id = _database(tmp_path / "emergency-roles.db")
    for name in ("one.mp3", "two.mp3", "three.mp3"):
        (tmp_path / name).write_bytes(b"local emergency audio")
    emergency = LocalEmergencyPlaylistService(
        TrackRepository(database),
        FileAvailabilityService(network_retry_delay_seconds=0),
        [1],
        media_track_ids={
            EmergencyMediaType.BREAK_MUSIC: [2],
            EmergencyMediaType.JINGLE: [3],
            EmergencyMediaType.ANNOUNCEMENT: [],
        },
    )

    assert [track.id for track in emergency.candidates()] == [1]
    assert [track.id for track in emergency.candidates(EmergencyMediaType.BREAK_MUSIC)] == [2]
    assert [track.id for track in emergency.candidates(EmergencyMediaType.JINGLE)] == [3]
    entries = emergency.media_entries()
    assert [(entry.media_type, entry.track.id, entry.loop_allowed) for entry in entries] == [
        (EmergencyMediaType.PRIMARY, 1, False),
        (EmergencyMediaType.BREAK_MUSIC, 2, True),
        (EmergencyMediaType.JINGLE, 3, False),
    ]
    assert emergency.loop_allowed(EmergencyMediaType.BREAK_MUSIC)
    assert not emergency.loop_allowed(EmergencyMediaType.PRIMARY)
    assert not emergency.loop_allowed(EmergencyMediaType.JINGLE)
    assert not emergency.loop_allowed(EmergencyMediaType.ANNOUNCEMENT)
    accepted = dict(emergency.validation().accepted_media)
    assert accepted[EmergencyMediaType.PRIMARY] == (1,)
    assert accepted[EmergencyMediaType.BREAK_MUSIC] == (2,)


def test_no_safe_candidate_is_exposed_when_every_stage_is_empty(tmp_path: Path) -> None:
    database, _session_id = _database(tmp_path / "no-safe.db")
    selector = AutomaticSelectionService(
        TrackRepository(database),
        AutomaticSelectionHistory(database),
    )

    class HardBlock:
        def evaluate(self, _entry, _track):
            return SelectionDecision.reject("BLOCKED_TRACK")

    assert selector.select(TrackSelectionService((HardBlock(),))) is None
    assert selector.last_relaxation_stage == "NO_SAFE_CANDIDATE"


def test_empty_queue_can_repeat_current_saved_playlist(tmp_path: Path) -> None:
    database, session_id = _database(tmp_path / "repeat-playlist.db")
    saved_repository = SavedQueueRepository(database)
    saved = saved_repository.save(
        "Current",
        [
            SavedQueueEntry(2, 1, 4.0, 120.0, 5.0, "snapshot"),
            SavedQueueEntry(3, 2),
        ],
    )
    repository = PartyPlayerRepository(database)
    repository.set_selected_playlist(session_id, saved.saved_queue_id)

    def entries() -> list[SavedQueueEntry]:
        selected_id = repository.selected_playlist_id(session_id)
        restored = saved_repository.get(selected_id) if selected_id is not None else None
        return list(restored.entries) if restored is not None else []

    service = QueueService(
        repository,
        TrackRepository(database),
        session_id,
        empty_queue_policy=EmptyQueuePolicy.REPEAT_CURRENT_PLAYLIST,
        repeat_playlist_entries=entries,
    )

    candidate = service.get_next_candidate()

    assert candidate is not None
    assert candidate.track_id == 2
    assert candidate.source is QueueSource.PLAYLIST
    assert candidate.cue_in_override == 4.0
    assert [entry.track_id for entry in service.entries()] == [2, 3]
