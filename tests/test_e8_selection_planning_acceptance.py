"""Reproducible E8 acceptance checks for migration and selection-plan load."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
from time import perf_counter

import pytest

from party_player.automatic_selection import AutomaticSelectionHistory, AutomaticSelectionService
from party_player.automatic_selection_planning import AutomaticSelectionPlanningService
from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.repositories.automatic_selection_plan_repository import (
    AutomaticSelectionPlanRepository,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.selection_rule_settings import SelectionRuleSettingsRepository
from party_player.track_selection import TrackSelectionService


MIGRATION_CATALOG_SIZE = 10_000
MIGRATION_QUEUE_SIZE = 500
LOAD_CATALOG_SIZE = 1_000
LOAD_QUEUE_SIZE = 250


def _canonical_rows(database: Database, statement: str) -> str:
    with database.connect() as connection:
        rows = [tuple(row) for row in connection.execute(statement).fetchall()]
    payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _populate_schema_41_database(path: Path, *, catalog_size: int, queue_size: int) -> Database:
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.execute("DROP TABLE automatic_selection_plan_steps")
        connection.execute("DROP TABLE automatic_selection_plans")
        connection.execute("DROP TABLE selection_rule_settings")
        connection.execute("UPDATE schema_version SET version=41")
        connection.executemany(
            """INSERT INTO tracks
               (id,file_path,title,artist,genre,bpm,energy,rating)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                (
                    track_id,
                    f"media/{track_id}.mp3",
                    f"Track {track_id}",
                    f"Artist {track_id}",
                    f"Genre {track_id % 12}",
                    80.0 + (track_id % 80),
                    (track_id % 100) / 100.0,
                    1 + (track_id % 5),
                )
                for track_id in range(1, catalog_size + 1)
            ),
        )
        connection.execute(
            "INSERT INTO party_sessions (id,name,status) VALUES (1,'E8 migration','active')"
        )
        connection.executemany(
            """INSERT INTO party_queue
               (id,session_id,track_id,position,status,source,priority)
               VALUES (?,1,?,?,'waiting','playlist',300)""",
            ((track_id, track_id, track_id) for track_id in range(1, queue_size + 1)),
        )
        connection.execute(
            "INSERT OR REPLACE INTO party_settings (key,value) VALUES ('empty_queue_policy','automatic')"
        )
    return database


def test_schema_41_large_database_migrates_idempotently_to_45(tmp_path: Path) -> None:
    database = _populate_schema_41_database(
        tmp_path / "e8-migration.db",
        catalog_size=MIGRATION_CATALOG_SIZE,
        queue_size=MIGRATION_QUEUE_SIZE,
    )
    tracks_before = _canonical_rows(
        database,
        "SELECT id,file_path,title,artist,genre,bpm,energy,rating FROM tracks ORDER BY id",
    )
    queue_before = _canonical_rows(
        database,
        """SELECT id,session_id,track_id,position,status,source,priority
           FROM party_queue ORDER BY id""",
    )
    settings_before = _canonical_rows(database, "SELECT key,value FROM party_settings ORDER BY key")

    started = perf_counter()
    migrate(database)
    first_duration = perf_counter() - started
    after_first = (tracks_before, queue_before, settings_before)

    started = perf_counter()
    migrate(database)
    second_duration = perf_counter() - started

    assert first_duration <= 5.0
    assert second_duration <= 5.0
    assert after_first == (
        _canonical_rows(
            database,
            "SELECT id,file_path,title,artist,genre,bpm,energy,rating FROM tracks ORDER BY id",
        ),
        _canonical_rows(
            database,
            """SELECT id,session_id,track_id,position,status,source,priority
               FROM party_queue ORDER BY id""",
        ),
        _canonical_rows(database, "SELECT key,value FROM party_settings ORDER BY key"),
    )
    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        rule_count = connection.execute("SELECT COUNT(*) FROM selection_rule_settings").fetchone()[
            0
        ]
        plan_tables = connection.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type='table' AND name IN
                   ('automatic_selection_plans','automatic_selection_plan_steps')"""
        ).fetchone()[0]
    assert version == LATEST_SCHEMA_VERSION == 45
    assert integrity == "ok"
    assert foreign_keys == []
    assert rule_count == 14
    assert plan_tables == 2


def test_newer_database_schema_has_understandable_version_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "newer.db")
    migrate(database)
    with database.connect() as connection:
        connection.execute("UPDATE schema_version SET version=46")

    with pytest.raises(RuntimeError, match=r"Datenbankschema 46 .* Anwendung \(45\)"):
        migrate(database)


def test_large_filled_queue_plan_is_bounded_and_excludes_active_tracks(
    tmp_path: Path,
) -> None:
    database = _populate_schema_41_database(
        tmp_path / "e8-planning.db",
        catalog_size=LOAD_CATALOG_SIZE,
        queue_size=LOAD_QUEUE_SIZE,
    )
    migrate(database)
    party = PartyPlayerRepository(database)
    tracks = TrackRepository(database)
    plans = AutomaticSelectionPlanRepository(database, party)
    rules = TrackSelectionService()
    selection = AutomaticSelectionService(
        tracks,
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(8008),
        rule_settings=SelectionRuleSettingsRepository(database),
    )
    planning = AutomaticSelectionPlanningService(selection, rules, plans)
    started = perf_counter()
    result = planning.create_draft_plan(
        1,
        10,
        planning_seed=8008,
        excluded_track_ids=frozenset(range(1, LOAD_QUEUE_SIZE + 1)),
    )
    duration = perf_counter() - started

    assert duration <= 10.0
    assert result.generated_depth == 10
    assert result.plan is not None
    selected = [step.track_id for step in result.plan.steps]
    assert len(selected) == len(set(selected)) == 10
    assert not set(selected).intersection(range(1, LOAD_QUEUE_SIZE + 1))
    with database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM automatic_selection_plans").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM party_queue").fetchone()[0] == LOAD_QUEUE_SIZE
        )
