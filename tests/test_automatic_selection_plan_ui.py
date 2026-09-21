from dataclasses import replace
from pathlib import Path
import random
import sqlite3

from party_player.automatic_selection import AutomaticSelectionHistory, AutomaticSelectionService
from party_player.automatic_selection_plan import AutomaticSelectionPlanStatus
from party_player.automatic_selection_plan_recovery import AutomaticSelectionPlanRecoveryService
from party_player.automatic_selection_plan_ui import (
    AutomaticSelectionPlanUiService,
    PreviewAdoptionCode,
)
from party_player.automatic_selection_planning import AutomaticSelectionPlanningService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.file_availability import FileAvailabilityService
from party_player.repositories.automatic_selection_plan_repository import (
    AutomaticSelectionPlanRepository,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.selection_preview import SelectionPreview
from party_player.track_selection import TrackSelectionService


def _services(tmp_path: Path):
    database = Database(tmp_path / "plan-ui.db")
    migrate(database)
    party = PartyPlayerRepository(database)
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id,file_path,title,artist) VALUES (?,?,?,?)",
            [
                (value, f"C:/private/{value}.mp3", f"Track {value}", f"Artist {value}")
                for value in range(1, 7)
            ],
        )
    session_id = party.create_session("Plan UI").session_id
    tracks = TrackRepository(database)
    rules = TrackSelectionService()
    selection = AutomaticSelectionService(
        tracks,
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(19),
    )
    repository = AutomaticSelectionPlanRepository(database, party)
    planning = AutomaticSelectionPlanningService(selection, rules, repository)
    recovery = AutomaticSelectionPlanRecoveryService(
        repository, planning, tracks, rules, FileAvailabilityService()
    )
    ui = AutomaticSelectionPlanUiService(repository, tracks, planning, recovery, selection)
    return database, session_id, rules, selection, planning, repository, ui


def _preview(session_id, rules, selection, planning, depth=5) -> SelectionPreview:
    return selection.preview(
        rules,
        depth,
        session_id=session_id,
        configuration_digest=planning.current_configuration_digest(),
    )


def test_preview_adoption_preserves_exact_sequence_and_decision_facts(tmp_path: Path) -> None:
    database, session_id, rules, selection, planning, _, ui = _services(tmp_path)
    preview = _preview(session_id, rules, selection, planning)

    result = ui.adopt_preview(preview)

    assert result.code is PreviewAdoptionCode.CREATED
    assert result.plan is not None
    assert result.plan.plan.status is AutomaticSelectionPlanStatus.DRAFT
    assert result.plan.plan.source_context_code == "PREVIEW_ADOPTION"
    assert result.plan.plan.planning_seed is None
    assert [step.track_id for step in result.plan.steps] == [
        step.track_id for step in preview.steps
    ]
    assert [step.secondary_score for step in result.plan.steps] == [
        step.rationale.secondary_score or 0.0 for step in preview.steps
    ]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM party_queue").fetchone()[0] == 0


def test_stale_or_incomplete_preview_is_rejected_without_partial_persistence(
    tmp_path: Path,
) -> None:
    database, session_id, rules, selection, planning, _, ui = _services(tmp_path)
    preview = _preview(session_id, rules, selection, planning, depth=3)
    assert preview.planning_basis is not None
    stale = replace(
        preview,
        planning_basis=replace(preview.planning_basis, history_fingerprint="0" * 64),
    )
    incomplete = replace(preview, achieved_depth=2)

    assert ui.adopt_preview(stale).code is PreviewAdoptionCode.STALE
    assert ui.adopt_preview(incomplete).code is PreviewAdoptionCode.INVALID
    with database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM automatic_selection_plans").fetchone()[0] == 0
        )


def test_overview_uses_batched_track_projection_and_contains_no_paths(
    tmp_path: Path, monkeypatch
) -> None:
    database, session_id, rules, selection, planning, _, ui = _services(tmp_path)
    adopted = ui.adopt_preview(_preview(session_id, rules, selection, planning, depth=3))
    assert adopted.plan is not None
    statements: list[str] = []
    original_open = database._open_connection

    def traced_connection() -> sqlite3.Connection:
        connection = original_open()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database, "_open_connection", traced_connection)
    overview = ui.overview(session_id)

    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert overview is not None
    assert len(reads) == 3
    assert [step.position for step in overview.steps] == [1, 2, 3]
    assert "C:/private" not in repr(overview)


def test_missing_catalog_track_has_safe_overview_text(tmp_path: Path) -> None:
    database, session_id, rules, selection, planning, _, ui = _services(tmp_path)
    adopted = ui.adopt_preview(_preview(session_id, rules, selection, planning, depth=2))
    assert adopted.plan is not None
    missing_id = adopted.plan.steps[0].track_id
    with database.connect() as connection:
        connection.execute("DELETE FROM tracks WHERE id=?", (missing_id,))

    overview = ui.overview(session_id)

    assert overview is not None
    assert overview.steps[0].title == "Titel nicht mehr im Katalog"


def test_confirmed_replacement_keeps_a_playing_plan_entry_untouched(tmp_path: Path) -> None:
    database, session_id, rules, selection, planning, repository, ui = _services(tmp_path)
    preview = _preview(session_id, rules, selection, planning, depth=2)
    adopted = ui.adopt_preview(preview)
    assert adopted.plan is not None
    active = repository.activate_draft_plan(
        adopted.plan.plan.plan_id,
        adopted.plan.plan.revision,
        adopted.plan.plan.rule_configuration_digest,
    )
    linked, entry = repository.materialize_step(active.plan.plan_id, 1, active.plan.revision)
    with database.connect() as connection:
        connection.execute("UPDATE party_queue SET status='playing' WHERE id=?", (entry.queue_id,))

    replaced = ui.adopt_preview(
        preview,
        replace_plan_id=linked.plan.plan_id,
        replace_revision=linked.plan.revision,
    )

    assert replaced.code is PreviewAdoptionCode.REPLACED
    assert replaced.plan is not None
    assert replaced.plan.plan.status is AutomaticSelectionPlanStatus.ACTIVE
    with database.connect() as connection:
        queue_status = connection.execute(
            "SELECT status FROM party_queue WHERE id=?", (entry.queue_id,)
        ).fetchone()[0]
        resumable = connection.execute(
            """SELECT COUNT(*) FROM automatic_selection_plans
               WHERE session_id=? AND status IN ('ACTIVE','PAUSED')""",
            (session_id,),
        ).fetchone()[0]
    assert queue_status == "playing"
    assert resumable == 1


def test_ending_resumable_plan_preserves_materialized_queue_entries(tmp_path: Path) -> None:
    database, session_id, rules, selection, planning, repository, ui = _services(tmp_path)
    adopted = ui.adopt_preview(_preview(session_id, rules, selection, planning, depth=2))
    assert adopted.plan is not None
    active = repository.activate_draft_plan(
        adopted.plan.plan.plan_id,
        adopted.plan.plan.revision,
        adopted.plan.plan.rule_configuration_digest,
    )
    linked, first = repository.materialize_step(active.plan.plan_id, 1, active.plan.revision)
    linked, second = repository.materialize_step(
        linked.plan.plan_id,
        2,
        linked.plan.revision,
    )
    with database.connect() as connection:
        connection.execute("UPDATE party_queue SET status='playing' WHERE id=?", (first.queue_id,))

    assert ui.end_resumable_for_session(session_id)

    ended = repository.get(adopted.plan.plan.plan_id)
    assert ended is not None
    assert ended.plan.status is AutomaticSelectionPlanStatus.DISCARDED
    with database.connect() as connection:
        statuses = {
            row["id"]: row["status"]
            for row in connection.execute(
                "SELECT id,status FROM party_queue WHERE id IN (?,?)",
                (first.queue_id, second.queue_id),
            )
        }
    assert statuses == {first.queue_id: "playing", second.queue_id: "waiting"}
