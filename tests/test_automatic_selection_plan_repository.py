from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlan,
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStep,
    AutomaticSelectionPlanStepStatus,
    selection_configuration_digest,
)
from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.database import migrations
from party_player.enums import CompletionStatus, QueueSource, QueueStatus
from party_player.repository import PartyPlayerRepository
from party_player.repositories.automatic_selection_plan_repository import (
    AutomaticSelectionPlanConflictError,
    AutomaticSelectionPlanRepository,
)

PLAN_ID = "10000000-0000-4000-8000-000000000001"


def _setup(path: Path) -> tuple[Database, AutomaticSelectionPlanRepository, int]:
    database = Database(path)
    migrate(database)
    party = PartyPlayerRepository(database)
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id,file_path,title) VALUES (?,?,?)",
            [(1, "one.mp3", "One"), (2, "two.mp3", "Two")],
        )
    session_id = party.create_session("Plan").session_id
    return database, AutomaticSelectionPlanRepository(database, party), session_id


def _bundle(session_id: int) -> AutomaticSelectionPlanBundle:
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    plan = AutomaticSelectionPlan(
        PLAN_ID,
        session_id,
        AutomaticSelectionPlanStatus.DRAFT,
        now,
        now,
        7,
        1,
        selection_configuration_digest({"selection.rating": {"enabled": True}}),
        1,
        "AUTOMATIC",
        2,
    )
    return AutomaticSelectionPlanBundle(
        plan,
        tuple(
            AutomaticSelectionPlanStep(
                f"20000000-0000-4000-8000-{position:012d}",
                PLAN_ID,
                position,
                position,
                AutomaticSelectionPlanStepStatus.PLANNED,
                now,
                position - 1 or None,
                "STRICT",
                0,
                float(position),
                1,
                "SEEDED_RANDOM",
                "BEST_SCORE",
            )
            for position in (1, 2)
        ),
    )


def test_v44_adds_exact_plan_tables_without_catalog_foreign_keys(tmp_path: Path) -> None:
    database, _, _ = _setup(tmp_path / "schema.db")
    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'automatic_selection_plan%'"
            )
        }
        step_fks = connection.execute(
            "PRAGMA foreign_key_list(automatic_selection_plan_steps)"
        ).fetchall()
    assert version == LATEST_SCHEMA_VERSION == 44
    assert tables == {"automatic_selection_plans", "automatic_selection_plan_steps"}
    assert {row[2] for row in step_fks} == {"automatic_selection_plans", "party_queue"}


def test_v44_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _, _ = _setup(tmp_path / "migration-rollback.db")
    with database.connect() as connection:
        connection.execute("DROP TABLE automatic_selection_plan_steps")
        connection.execute("DROP TABLE automatic_selection_plans")
        connection.execute("UPDATE schema_version SET version=43")

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE automatic_selection_plans (plan_id TEXT)")
        raise sqlite3.OperationalError("simulated migration failure")

    monkeypatch.setattr(migrations, "_migrate_to_v44", fail)
    with pytest.raises(sqlite3.OperationalError):
        migrate(database)
    with database.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='automatic_selection_plans'"
        ).fetchone()
    assert version == 43
    assert table is None


def test_create_load_transition_and_atomic_idempotent_queue_link(tmp_path: Path) -> None:
    database, repository, session_id = _setup(tmp_path / "plan.db")
    created = repository.create(_bundle(session_id))
    assert repository.get(PLAN_ID) == created
    active = repository.change_status(PLAN_ID, 0, AutomaticSelectionPlanStatus.ACTIVE)
    assert active.plan.revision == 1
    materialized, queue_entry = repository.materialize_step(PLAN_ID, 1, 1)
    assert queue_entry.source is QueueSource.AUTOMATIC
    assert queue_entry.priority == QueueSource.AUTOMATIC.default_priority
    assert materialized.steps[0].executed_queue_status is QueueStatus.WAITING
    repeated, same_entry = repository.materialize_step(PLAN_ID, 1, 0)
    assert same_entry.queue_id == queue_entry.queue_id
    assert repeated.plan.revision == 2
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM party_queue").fetchone()[0] == 1
    with pytest.raises(AutomaticSelectionPlanConflictError):
        repository.invalidate_remaining(PLAN_ID, 1, 2, "CATALOG_CHANGED")


def test_recovered_session_can_create_and_activate_replacement_plan(tmp_path: Path) -> None:
    database, repository, session_id = _setup(tmp_path / "recovered-plan.db")
    with database.connect() as connection:
        connection.execute("UPDATE party_sessions SET status='recovered' WHERE id=?", (session_id,))

    draft = repository.create(_bundle(session_id))
    active = repository.activate_draft_plan(
        draft.plan.plan_id,
        draft.plan.revision,
        draft.plan.rule_configuration_digest,
    )

    assert active.plan.status is AutomaticSelectionPlanStatus.ACTIVE


def test_activate_draft_checks_digest_revision_session_and_creates_no_queue(
    tmp_path: Path,
) -> None:
    database, repository, session_id = _setup(tmp_path / "activate.db")
    draft = repository.create(_bundle(session_id))

    active = repository.activate_draft_plan(
        PLAN_ID,
        0,
        draft.plan.rule_configuration_digest,
    )

    assert active.plan.status is AutomaticSelectionPlanStatus.ACTIVE
    assert active.plan.revision == 1
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM party_queue").fetchone()[0] == 0
    with pytest.raises(AutomaticSelectionPlanConflictError):
        repository.activate_draft_plan(PLAN_ID, 0, draft.plan.rule_configuration_digest)


def test_activate_draft_rejects_changed_configuration_and_closed_session(
    tmp_path: Path,
) -> None:
    database, repository, session_id = _setup(tmp_path / "activate-rejected.db")
    draft = repository.create(_bundle(session_id))

    with pytest.raises(ValueError, match="configuration changed"):
        repository.activate_draft_plan(PLAN_ID, 0, "different")
    with database.connect() as connection:
        connection.execute("UPDATE party_sessions SET status='completed' WHERE id=?", (session_id,))
    with pytest.raises(ValueError, match="not eligible"):
        repository.activate_draft_plan(
            PLAN_ID,
            0,
            draft.plan.rule_configuration_digest,
        )


def test_startup_recovery_keeps_plan_playback_terminal_and_pauses_plan(
    tmp_path: Path,
) -> None:
    database, repository, session_id = _setup(tmp_path / "recovery-playing.db")
    party = PartyPlayerRepository(database)
    draft = repository.create(_bundle(session_id))
    active = repository.activate_draft_plan(PLAN_ID, 0, draft.plan.rule_configuration_digest)
    linked, entry = repository.materialize_step(PLAN_ID, 1, active.plan.revision)
    with database.connect() as connection:
        connection.execute(
            "UPDATE party_queue SET status='playing', played_at=CURRENT_TIMESTAMP WHERE id=?",
            (entry.queue_id,),
        )

    party.recover_queue_after_restart(session_id)
    state = repository.recovery_state(session_id, draft.plan.rule_configuration_digest)

    assert state is not None
    assert state.plan_status is AutomaticSelectionPlanStatus.PAUSED
    assert state.interrupted_track_id == 1
    assert state.actual_predecessor_track_id == 1
    assert party.get_queue_entry(entry.queue_id).status is QueueStatus.SKIPPED  # type: ignore[union-attr]
    with database.connect() as connection:
        history = connection.execute(
            "SELECT completion_status FROM play_history WHERE queue_id=?", (entry.queue_id,)
        ).fetchone()
    assert history["completion_status"] == "ABORTED"
    assert linked.plan.status is AutomaticSelectionPlanStatus.ACTIVE


def test_recovery_resume_discard_and_foreign_playback_are_cas_safe(tmp_path: Path) -> None:
    database, repository, session_id = _setup(tmp_path / "recovery-actions.db")
    draft = repository.create(_bundle(session_id))
    repository.activate_draft_plan(PLAN_ID, 0, draft.plan.rule_configuration_digest)
    state = repository.recovery_state(session_id, draft.plan.rule_configuration_digest)
    assert state is not None
    resumed = repository.resume_recovered_plan(PLAN_ID, state.plan_revision)
    assert resumed.plan.status is AutomaticSelectionPlanStatus.ACTIVE

    party = PartyPlayerRepository(database)
    foreign = party.add_queue_entry(session_id, 2, QueueSource.MANUAL)
    assert repository.pause_for_foreign_playback(session_id, foreign.queue_id)
    paused = repository.get(PLAN_ID)
    assert paused is not None
    assert paused.plan.status is AutomaticSelectionPlanStatus.PAUSED
    assert all(step.status is AutomaticSelectionPlanStepStatus.INVALIDATED for step in paused.steps)
    discarded = repository.discard_recovered_plan(PLAN_ID, paused.plan.revision)
    assert discarded.plan.status is AutomaticSelectionPlanStatus.DISCARDED
    repeated = repository.discard_recovered_plan(PLAN_ID, paused.plan.revision)
    assert repeated.plan == discarded.plan


def test_finished_session_discards_only_its_resumable_plan(tmp_path: Path) -> None:
    _, repository, session_id = _setup(tmp_path / "session-finish.db")
    draft = repository.create(_bundle(session_id))
    repository.activate_draft_plan(PLAN_ID, 0, draft.plan.rule_configuration_digest)
    repository.discard_for_finished_session(session_id)
    stored = repository.get(PLAN_ID)
    assert stored is not None
    assert stored.plan.status is AutomaticSelectionPlanStatus.DISCARDED
    assert all(step.invalid_reason_code == "SESSION_FINISHED" for step in stored.steps)


def test_recovery_predecessor_uses_same_cross_session_played_history_as_planning(
    tmp_path: Path,
) -> None:
    database, repository, session_id = _setup(tmp_path / "cross-session-predecessor.db")
    party = PartyPlayerRepository(database)
    older_session = party.create_session("Older").session_id
    now = datetime.now(UTC)
    party.add_history(
        older_session,
        1,
        "A",
        now,
        CompletionStatus.PLAYED,
        120.0,
        completed_at=now,
    )
    bundle = _bundle(session_id)
    bundle = AutomaticSelectionPlanBundle(
        bundle.plan,
        (replace(bundle.steps[0], previous_track_id=1), bundle.steps[1]),
    )
    draft = repository.create(bundle)
    repository.activate_draft_plan(PLAN_ID, 0, draft.plan.rule_configuration_digest)

    state = repository.recovery_state(session_id, draft.plan.rule_configuration_digest)

    assert state is not None
    assert state.actual_predecessor_track_id == 1
    assert state.reason_code.value == "RECOVERY_REQUIRED"


def test_invalid_step_and_plan_pause_are_one_revision_change(tmp_path: Path) -> None:
    _, repository, session_id = _setup(tmp_path / "pause-invalid.db")
    draft = repository.create(_bundle(session_id))
    active = repository.activate_draft_plan(PLAN_ID, 0, draft.plan.rule_configuration_digest)

    paused = repository.pause_invalid_step(PLAN_ID, 1, active.plan.revision, "TRACK_BLOCKED")

    assert paused.plan.status is AutomaticSelectionPlanStatus.PAUSED
    assert paused.plan.revision == 2
    assert paused.steps[0].status is AutomaticSelectionPlanStepStatus.INVALIDATED
    assert paused.steps[0].invalid_reason_code == "TRACK_BLOCKED"


def test_discard_invalidates_only_unmaterialized_steps_atomically(tmp_path: Path) -> None:
    _, repository, session_id = _setup(tmp_path / "discard.db")
    repository.create(_bundle(session_id))
    repository.change_status(PLAN_ID, 0, AutomaticSelectionPlanStatus.ACTIVE)
    repository.materialize_step(PLAN_ID, 1, 1)
    discarded = repository.change_status(
        PLAN_ID,
        2,
        AutomaticSelectionPlanStatus.DISCARDED,
        reason_code="USER_DISCARDED",
    )
    assert discarded.plan.revision == 3
    assert discarded.steps[0].status is AutomaticSelectionPlanStepStatus.QUEUED
    assert discarded.steps[1].status is AutomaticSelectionPlanStepStatus.INVALIDATED
    assert discarded.steps[1].invalid_reason_code == "USER_DISCARDED"


def test_only_one_resumable_plan_per_session_and_malformed_rows_are_rejected(
    tmp_path: Path,
) -> None:
    database, repository, session_id = _setup(tmp_path / "invariants.db")
    repository.create(_bundle(session_id))
    repository.change_status(PLAN_ID, 0, AutomaticSelectionPlanStatus.ACTIVE)
    second = _bundle(session_id)
    second_id = "10000000-0000-4000-8000-000000000003"
    second = AutomaticSelectionPlanBundle(
        replace(second.plan, plan_id=second_id),
        tuple(
            replace(step, plan_id=second_id, step_id=step.step_id.replace("2000", "3000", 1))
            for step in second.steps
        ),
    )
    repository.create(second)
    with pytest.raises(sqlite3.IntegrityError):
        repository.change_status(second_id, 0, AutomaticSelectionPlanStatus.ACTIVE)

    with database.connect() as connection:
        connection.execute(
            "UPDATE automatic_selection_plans SET source_context_code='not a code' WHERE plan_id=?",
            (second_id,),
        )
    with pytest.raises(ValueError, match="machine-readable"):
        repository.get(second_id)


def test_create_and_materialization_roll_back_as_one_unit(tmp_path: Path) -> None:
    database, repository, session_id = _setup(tmp_path / "rollback.db")
    bundle = _bundle(session_id)
    repository.create(bundle)
    second_plan_id = "10000000-0000-4000-8000-000000000002"
    conflicting = AutomaticSelectionPlanBundle(
        replace(bundle.plan, plan_id=second_plan_id),
        tuple(
            replace(
                step,
                plan_id=second_plan_id,
                step_id=(
                    bundle.steps[0].step_id
                    if step.position == 1
                    else "20000000-0000-4000-8000-000000000099"
                ),
            )
            for step in bundle.steps
        ),
    )
    with pytest.raises(sqlite3.IntegrityError):
        repository.create(conflicting)
    assert repository.get(second_plan_id) is None

    repository.change_status(PLAN_ID, 0, AutomaticSelectionPlanStatus.ACTIVE)
    with database.connect() as connection:
        connection.execute("DELETE FROM tracks WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        repository.materialize_step(PLAN_ID, 1, 1)
    loaded = repository.get(PLAN_ID)
    assert loaded is not None
    assert loaded.plan.revision == 1
    assert loaded.steps[0].status is AutomaticSelectionPlanStepStatus.PLANNED


def test_load_query_count_is_constant_for_plan_depth(tmp_path: Path) -> None:
    database, repository, session_id = _setup(tmp_path / "queries.db")
    repository.create(_bundle(session_id))
    statements: list[str] = []
    original_open = database._open_connection

    def traced() -> sqlite3.Connection:
        connection = original_open()
        connection.set_trace_callback(statements.append)
        return connection

    database._open_connection = traced  # type: ignore[method-assign]
    assert repository.get(PLAN_ID) is not None
    selects = [
        statement for statement in statements if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 2
