"""Batch and immutability coverage for hard selection-rule facts."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import random

import pytest

from party_player.artist_policy import ArtistPolicyRepository, PersistentArtistBlockService
from party_player.automatic_selection import AutomaticSelectionHistory, AutomaticSelectionService
from party_player.cue_points import CuePointRepository, CuePointService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import QueueSource, QueueStatus, ShortTrackPolicy
from party_player.models import QueueEntry
from party_player.repetition_policy import PersistentRepetitionService, RepetitionHistoryRepository
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.short_track_policy import ShortTrackSelectionRule
from party_player.track_policy import PersistentTrackBlockService, TrackPolicyRepository
from party_player.track_selection import TrackSelectionService
from party_player.track_suitability import (
    TrackSuitabilityRepository,
    TrackSuitabilityService,
    TrackSuitabilityStatus,
)


def _database(path: Path, count: int = 3) -> Database:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (file_path, title, artist, duration_seconds) VALUES (?, ?, ?, ?)",
            (
                (f"{index}.mp3", f"Track {index}", f"Artist {index % 2}", 180.0)
                for index in range(1, count + 1)
            ),
        )
    return database


def _rules(database: Database, session_id: int) -> TrackSelectionService:
    cues = CuePointService(
        CuePointRepository(database),
        7.0,
        0.5,
        5.0,
        short_track_threshold=30.0,
        short_track_policy=ShortTrackPolicy.USE_REDUCED_FADE,
    )
    return TrackSelectionService(
        (
            PersistentTrackBlockService(TrackPolicyRepository(database)),
            PersistentArtistBlockService(ArtistPolicyRepository(database), session_id),
            TrackSuitabilityService(TrackSuitabilityRepository(database)),
            PersistentRepetitionService(RepetitionHistoryRepository(database)),
            ShortTrackSelectionRule(cues, policy=ShortTrackPolicy.USE_REDUCED_FADE),
        )
    )


def test_snapshot_is_immutable_complete_and_path_free(tmp_path: Path) -> None:
    database = _database(tmp_path / "snapshot.db")
    party = PartyPlayerRepository(database)
    session = party.create_session("Test")
    tracks = tuple(TrackRepository(database).automatic_candidates())
    TrackSuitabilityRepository(database).set_many(
        tuple(track.id for track in tracks), TrackSuitabilityStatus.SUITABLE
    )

    snapshot = _rules(database, session.session_id).prepare_catalog(tracks)

    assert set(snapshot.track_policies) == {1, 2, 3}
    assert set(snapshot.suitability) == {1, 2, 3}
    assert set(snapshot.cues) == {1, 2, 3}
    assert set(snapshot.artist_policies) == set()
    with pytest.raises(TypeError):
        snapshot.suitability[1] = snapshot.suitability[1]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        snapshot.recent_plays = ()  # type: ignore[misc]
    assert ".mp3" not in repr(snapshot)


def test_prepared_rules_execute_without_sql_and_keep_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "decisions.db")
    party = PartyPlayerRepository(database)
    session = party.create_session("Test")
    tracks = tuple(TrackRepository(database).automatic_candidates())
    TrackSuitabilityRepository(database).set_many(
        tuple(track.id for track in tracks), TrackSuitabilityStatus.SUITABLE
    )
    legacy = _rules(database, session.session_id)
    prepared = _rules(database, session.session_id)
    prepared.prepare_catalog(tracks)
    entry = QueueEntry(-1, 1, 0, QueueStatus.WAITING, source=QueueSource.AUTOMATIC)
    expected, expected_rationale = legacy.evaluate_with_rationale(entry, tracks[0])
    statements: list[str] = []
    database.close_cached_connection()
    original_open = database._open_connection

    def traced_open():
        connection = original_open()
        connection.set_trace_callback(
            lambda sql: (
                statements.append(sql)
                if sql.lstrip().upper().startswith(("SELECT", "WITH"))
                else None
            )
        )
        return connection

    monkeypatch.setattr(database, "_open_connection", traced_open)
    actual, actual_rationale = prepared.evaluate_with_rationale(entry, tracks[0])

    assert actual == expected
    assert [rule.reason_code for rule in actual_rationale.rule_evaluations] == [
        rule.reason_code for rule in expected_rationale.rule_evaluations
    ]
    assert statements == []


def test_batch_loading_chunks_large_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "chunks.db", count=901)
    repository = TrackPolicyRepository(database)
    statements: list[str] = []
    database.close_cached_connection()
    original_open = database._open_connection

    def traced_open():
        connection = original_open()
        connection.set_trace_callback(
            lambda sql: statements.append(sql) if "track_playback_policies" in sql else None
        )
        return connection

    monkeypatch.setattr(database, "_open_connection", traced_open)

    policies = repository.get_many(tuple(range(1, 902)))

    assert len(policies) == 901
    assert len(statements) == 2


@pytest.mark.parametrize("depth", (1, 5, 10))
def test_simulation_depth_does_not_add_hard_rule_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    database = _database(tmp_path / "depth.db", count=12)
    party = PartyPlayerRepository(database)
    session = party.create_session("Test")
    TrackSuitabilityRepository(database).set_many(
        tuple(range(1, 13)), TrackSuitabilityStatus.SUITABLE
    )
    selector = AutomaticSelectionService(
        TrackRepository(database), AutomaticSelectionHistory(database), recent_track_limit=0
    )
    prepared = selector._prepare_simulation(_rules(database, session.session_id))
    statements: list[str] = []
    database.close_cached_connection()
    original_open = database._open_connection

    def traced_open():
        connection = original_open()
        connection.set_trace_callback(
            lambda sql: (
                statements.append(sql)
                if sql.lstrip().upper().startswith(("SELECT", "WITH"))
                else None
            )
        )
        return connection

    monkeypatch.setattr(database, "_open_connection", traced_open)

    simulation = selector._simulate_prepared(
        prepared,
        depth,
        random.Random(123),
        context_code="TEST",
    )

    assert len(simulation.steps) == depth
    assert statements == []
