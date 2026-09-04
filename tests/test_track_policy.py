"""Persistent track-policy selection tests."""

from pathlib import Path

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import QueueStatus
from party_player.models import QueueEntry
from party_player.selection_decision import RuleOutcome, SelectionContext, SelectionRuleInput
from party_player.repositories.track_repository import TrackRepository
from party_player.track_policy import (
    PersistentTrackBlockService,
    TrackPolicyRepository,
    TrackPolicyStatus,
)
from party_player.track_selection import (
    TrackSelectionService,
    selection_decision_from_evaluation,
)


def _database(path: Path) -> Database:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO tracks (file_path, title, artist)
               VALUES ('song.mp3', 'Song', 'Artist')"""
        )
    return database


def test_track_policy_survives_repository_restart(tmp_path: Path) -> None:
    database = _database(tmp_path / "policy.db")
    repository = TrackPolicyRepository(database)

    repository.set(1, TrackPolicyStatus.BLOCKED, "Nicht für diese Veranstaltung")
    restored = TrackPolicyRepository(database).get(1)

    assert restored.status is TrackPolicyStatus.BLOCKED
    assert restored.reason == "Nicht für diese Veranstaltung"


def test_block_and_restriction_require_explicit_queue_override(tmp_path: Path) -> None:
    database = _database(tmp_path / "override.db")
    tracks = TrackRepository(database)
    track = tracks.get(1)
    assert track is not None
    entry = QueueEntry(7, track.id, 1, QueueStatus.WAITING)
    service = PersistentTrackBlockService(TrackPolicyRepository(database))
    service.set_policy(track.id, TrackPolicyStatus.RESTRICTED, "Nur auf Nachfrage")

    rejected = service.evaluate(entry, track)
    assert rejected is not None
    assert rejected.code == "RESTRICTED_TRACK"

    service.allow_queue_entry(entry.queue_id)
    assert service.evaluate(entry, track) is None


def test_executable_track_policy_queries_once_and_explains_override(
    tmp_path: Path, monkeypatch
) -> None:
    database = _database(tmp_path / "structured-override.db")
    repository = TrackPolicyRepository(database)
    repository.set(1, TrackPolicyStatus.RESTRICTED, "Nur auf Nachfrage")
    tracks = TrackRepository(database)
    track = tracks.get(1)
    assert track is not None
    entry = QueueEntry(7, track.id, 1, QueueStatus.WAITING)
    service = PersistentTrackBlockService(repository)
    service.allow_queue_entry(entry.queue_id)
    original_get = repository.get
    calls = 0

    def counted_get(track_id: int):
        nonlocal calls
        calls += 1
        return original_get(track_id)

    monkeypatch.setattr(repository, "get", counted_get)

    decision, rationale = TrackSelectionService((service,)).evaluate_with_rationale(entry, track)

    assert decision.accepted
    assert calls == 1
    evaluation = rationale.rule_evaluations[-1]
    assert evaluation.rule_id == "selection.track_policy"
    assert evaluation.reason_code == "RESTRICTED_TRACK"
    assert evaluation.result_code is RuleOutcome.OVERRIDDEN
    assert evaluation.operator_override


def test_legacy_and_executable_track_policy_results_are_equivalent(tmp_path: Path) -> None:
    database = _database(tmp_path / "equivalent-policy-paths.db")
    repository = TrackPolicyRepository(database)
    repository.set(1, TrackPolicyStatus.BLOCKED, "Nicht spielen")
    track = TrackRepository(database).get(1)
    assert track is not None
    entry = QueueEntry(7, track.id, 1, QueueStatus.WAITING)
    service = PersistentTrackBlockService(repository)

    legacy = service.evaluate(entry, track)
    structured = service.evaluate_rule(
        SelectionRuleInput.from_values(entry, track),
        SelectionContext("structured"),
    )

    assert legacy == selection_decision_from_evaluation(structured)
