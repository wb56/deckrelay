from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import random

from party_player.automatic_selection import AutomaticSelectionHistory, AutomaticSelectionService
from party_player.automatic_selection_plan import (
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStepStatus,
)
from party_player.automatic_selection_plan_execution import (
    AutomaticSelectionPlanExecutionService,
    AutomaticSelectionPlanExecutionStatus,
)
from party_player.automatic_selection_plan_recovery import (
    AutomaticSelectionPlanRecoveryResultCode,
    AutomaticSelectionPlanRecoveryService,
)
from party_player.automatic_selection_plan_ui import (
    AutomaticSelectionPlanUiService,
    PreviewAdoptionCode,
)
from party_player.automatic_selection_planning import AutomaticSelectionPlanningService
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import EmptyQueuePolicy, QueueSource, QueueStatus
from party_player.file_availability import FileAvailabilityService
from party_player.queue_service import QueueService
from party_player.repositories.automatic_selection_plan_repository import (
    AutomaticSelectionPlanRepository,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.track_policy import (
    PersistentTrackBlockService,
    TrackPolicyRepository,
    TrackPolicyStatus,
)
from party_player.track_selection import TrackSelectionService


@dataclass(frozen=True)
class _System:
    database: Database
    party: PartyPlayerRepository
    tracks: TrackRepository
    plans: AutomaticSelectionPlanRepository
    planning: AutomaticSelectionPlanningService
    execution: AutomaticSelectionPlanExecutionService
    recovery: AutomaticSelectionPlanRecoveryService
    ui: AutomaticSelectionPlanUiService
    queue: QueueService
    policy: PersistentTrackBlockService
    rules: TrackSelectionService
    selection: AutomaticSelectionService
    session_id: int


def _system(tmp_path: Path, *, track_count: int = 12) -> _System:
    database = Database(tmp_path / "plan-e2e.db")
    migrate(database)
    media = tmp_path / "media"
    media.mkdir()
    rows: list[tuple[int, str, str, str]] = []
    for track_id in range(1, track_count + 1):
        audio = media / f"track-{track_id}.mp3"
        audio.write_bytes(b"test")
        rows.append((track_id, str(audio), f"Track {track_id}", f"Artist {track_id}"))
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO tracks (id,file_path,title,artist) VALUES (?,?,?,?)", rows
        )
    party = PartyPlayerRepository(database)
    session_id = party.create_session("E6").session_id
    tracks = TrackRepository(database)
    plans = AutomaticSelectionPlanRepository(database, party)
    policy = PersistentTrackBlockService(TrackPolicyRepository(database))
    rules = TrackSelectionService((policy,))
    selection = AutomaticSelectionService(
        tracks,
        AutomaticSelectionHistory(database),
        recent_track_limit=0,
        randomizer=random.Random(671),
    )
    planning = AutomaticSelectionPlanningService(selection, rules, plans)
    availability = FileAvailabilityService(network_retry_attempts=0)
    execution = AutomaticSelectionPlanExecutionService(plans, planning, tracks, rules, availability)
    recovery = AutomaticSelectionPlanRecoveryService(plans, planning, tracks, rules, availability)
    ui = AutomaticSelectionPlanUiService(plans, tracks, planning, recovery, selection)
    queue = QueueService(
        party,
        tracks,
        session_id,
        empty_queue_policy=EmptyQueuePolicy.AUTOMATIC_SELECTION,
        selection_service=rules,
        file_availability=availability,
        automatic_selection=selection,
        automatic_plan_execution=execution,
        automatic_planning=planning,
    )
    return _System(
        database,
        party,
        tracks,
        plans,
        planning,
        execution,
        recovery,
        ui,
        queue,
        policy,
        rules,
        selection,
        session_id,
    )


def _adopt_and_activate(system: _System, depth: int = 5):
    preview = system.queue.preview_automatic_selection(depth)
    adopted = system.ui.adopt_preview(preview)
    assert adopted.code is PreviewAdoptionCode.CREATED
    assert adopted.plan is not None
    activated = system.execution.activate_draft_plan(
        adopted.plan.plan.plan_id, adopted.plan.plan.revision
    )
    assert activated.status is AutomaticSelectionPlanExecutionStatus.ACTIVATED
    assert activated.plan is not None
    return preview, activated.plan


def _automatic_buffer(system: _System):
    return [
        entry
        for entry in system.queue.entries()
        if entry.source is QueueSource.AUTOMATIC
        and entry.status in {QueueStatus.WAITING, QueueStatus.PREPARING, QueueStatus.READY}
    ]


def test_buffer_refill_adds_exact_deficit_and_excludes_playing_title(tmp_path: Path) -> None:
    system = _system(tmp_path)
    _adopt_and_activate(system, depth=5)
    system.queue.ensure_automatic_buffer(3)
    initial = _automatic_buffer(system)
    assert len(initial) == 3

    system.queue.mark_preparing(initial[0].queue_id, "A")
    system.queue.mark_loaded(initial[0].queue_id, "A")
    system.queue.mark_playing(initial[0].queue_id)
    result = system.queue.ensure_automatic_buffer(3)

    assert result is not None
    assert len(_automatic_buffer(system)) == 3
    active_tracks = [
        entry.track_id
        for entry in system.queue.entries()
        if entry.status
        in {QueueStatus.WAITING, QueueStatus.PREPARING, QueueStatus.READY, QueueStatus.PLAYING}
    ]
    assert len(active_tracks) == len(set(active_tracks))


def test_preview_excludes_every_track_already_active_in_queue(tmp_path: Path) -> None:
    system = _system(tmp_path, track_count=1)
    existing = system.queue.add(1, QueueSource.AUTOMATIC)
    system.queue.mark_preparing(existing.queue_id, "A")
    system.queue.mark_loaded(existing.queue_id, "A")

    preview = system.queue.preview_automatic_selection(1)

    assert preview.steps == ()


def test_preview_excludes_removed_automatic_track_for_whole_session(tmp_path: Path) -> None:
    system = _system(tmp_path, track_count=1)
    existing = system.queue.add(1, QueueSource.AUTOMATIC)
    system.queue.remove(existing.queue_id)

    preview = system.queue.preview_automatic_selection(1)

    assert preview.steps == ()


def test_playback_from_completed_predecessor_plan_does_not_pause_successor(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path, track_count=8)
    _, active = _adopt_and_activate(system, depth=2)
    first = system.execution.materialize_next_step(active.plan.plan_id, active.plan.revision)
    assert first.plan is not None and first.queue_entry is not None
    second = system.execution.materialize_next_step(active.plan.plan_id, first.plan.plan.revision)
    assert second.plan is not None
    completed = system.execution.materialize_next_step(
        active.plan.plan_id, second.plan.plan.revision
    )
    assert completed.status is AutomaticSelectionPlanExecutionStatus.PLAN_COMPLETED
    successor = system.execution.ensure_buffer(system.session_id, 3)
    assert successor.plan is not None

    system.queue.mark_preparing(first.queue_entry.queue_id, "A")
    system.queue.mark_loaded(first.queue_entry.queue_id, "A")
    system.queue.mark_playing(first.queue_entry.queue_id)

    stored = system.plans.get(successor.plan.plan.plan_id)
    assert stored is not None
    assert stored.plan.status is AutomaticSelectionPlanStatus.ACTIVE


def test_buffer_refill_from_two_to_five_adds_exactly_three(tmp_path: Path) -> None:
    system = _system(tmp_path)
    _, active = _adopt_and_activate(system, depth=5)
    first = system.execution.materialize_next_step(active.plan.plan_id, active.plan.revision)
    assert first.plan is not None
    system.execution.materialize_next_step(active.plan.plan_id, first.plan.plan.revision)

    system.queue.ensure_automatic_buffer(5)

    assert len(_automatic_buffer(system)) == 5


def test_concurrent_buffer_triggers_do_not_overshoot_target(tmp_path: Path) -> None:
    system = _system(tmp_path)
    _adopt_and_activate(system, depth=5)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(lambda _index: system.queue.ensure_automatic_buffer(3), range(6))
        )

    assert all(result is not None for result in results)
    assert len(_automatic_buffer(system)) == 3


def test_reached_buffer_is_a_planning_and_write_noop(tmp_path: Path) -> None:
    system = _system(tmp_path)
    _adopt_and_activate(system, depth=3)
    system.queue.ensure_automatic_buffer(3)
    before = system.plans.get_resumable_for_session(system.session_id)
    assert before is not None

    result = system.queue.ensure_automatic_buffer(3)
    after = system.plans.get_resumable_for_session(system.session_id)

    assert result is not None
    assert result.status is AutomaticSelectionPlanExecutionStatus.ALREADY_MATERIALIZED
    assert after == before


def test_insufficient_candidates_leave_visible_lower_buffer_without_loop(tmp_path: Path) -> None:
    system = _system(tmp_path, track_count=2)
    _adopt_and_activate(system, depth=2)

    first = system.queue.ensure_automatic_buffer(3)
    second = system.queue.ensure_automatic_buffer(3)

    assert first is not None and second is not None
    assert first.status is AutomaticSelectionPlanExecutionStatus.NO_SAFE_CANDIDATE
    assert second.status is AutomaticSelectionPlanExecutionStatus.NO_SAFE_CANDIDATE
    assert len(_automatic_buffer(system)) == 2


def test_paused_plan_does_not_refill(tmp_path: Path) -> None:
    system = _system(tmp_path)
    _, active = _adopt_and_activate(system, depth=3)
    system.plans.change_status(
        active.plan.plan_id,
        active.plan.revision,
        AutomaticSelectionPlanStatus.PAUSED,
    )

    result = system.queue.ensure_automatic_buffer(3)

    assert result is not None and result.reason_code == "PLAN_PAUSED"
    assert _automatic_buffer(system) == []


def test_preview_adoption_executes_exact_sequence_and_completes(tmp_path: Path) -> None:
    system = _system(tmp_path)
    preview, active = _adopt_and_activate(system, depth=5)
    expected = [step.track_id for step in preview.steps]
    actual: list[int] = []
    revision = active.plan.revision

    for _ in expected:
        result = system.execution.materialize_next_step(active.plan.plan_id, revision)
        assert result.status is AutomaticSelectionPlanExecutionStatus.MATERIALIZED
        assert result.plan is not None and result.queue_entry is not None
        actual.append(result.queue_entry.track_id)
        repeated = system.execution.ensure_next_step(system.session_id)
        assert repeated.status is AutomaticSelectionPlanExecutionStatus.ALREADY_MATERIALIZED
        assert repeated.queue_entry is not None
        assert repeated.queue_entry.queue_id == result.queue_entry.queue_id
        system.queue.mark_played(result.queue_entry.queue_id)
        revision = result.plan.plan.revision

    completed = system.execution.materialize_next_step(active.plan.plan_id, revision)

    assert actual == expected
    assert completed.status is AutomaticSelectionPlanExecutionStatus.PLAN_COMPLETED
    assert completed.plan is not None
    assert completed.plan.plan.status is AutomaticSelectionPlanStatus.COMPLETED
    with system.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM party_queue").fetchone()[0] == 5


def test_foreign_queue_sources_keep_priority_over_the_plan(tmp_path: Path) -> None:
    system = _system(tmp_path)
    _, active = _adopt_and_activate(system, depth=2)
    playlist = system.queue.add(11, QueueSource.PLAYLIST)
    guest = system.queue.add_guest_request(12, "Gast")

    assert system.queue.get_next_candidate() == guest
    system.queue.mark_played(guest.queue_id)
    assert system.queue.get_next_candidate() == playlist
    system.queue.mark_played(playlist.queue_id)
    planned = system.queue.get_next_candidate()

    assert planned is not None
    assert planned.source is QueueSource.AUTOMATIC
    stored = system.plans.get(active.plan.plan_id)
    assert stored is not None
    assert sum(step.status is AutomaticSelectionPlanStepStatus.QUEUED for step in stored.steps) == 1


def test_restart_preserves_materialized_step_and_resume_keeps_identity(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    _, active = _adopt_and_activate(system, depth=3)
    first = system.execution.materialize_next_step(active.plan.plan_id, active.plan.revision)
    assert first.plan is not None and first.queue_entry is not None

    restarted_plans = AutomaticSelectionPlanRepository(system.database, system.party)
    restarted = AutomaticSelectionPlanRecoveryService(
        restarted_plans,
        system.planning,
        TrackRepository(system.database),
        system.rules,
        FileAvailabilityService(network_retry_attempts=0),
    )
    state = restarted.inspect(system.session_id)
    assert state.code is AutomaticSelectionPlanRecoveryResultCode.RECOVERY_REQUIRED
    assert state.state is not None
    resumed = restarted.resume_plan(active.plan.plan_id, state.state.plan_revision)

    assert resumed.code is AutomaticSelectionPlanRecoveryResultCode.RESUMED
    assert resumed.plan is not None
    assert resumed.plan.plan.plan_id == active.plan.plan_id
    assert [step.track_id for step in resumed.plan.steps] == [
        step.track_id for step in active.steps
    ]
    repeated = system.execution.ensure_next_step(system.session_id)
    assert repeated.status is AutomaticSelectionPlanExecutionStatus.ALREADY_MATERIALIZED
    assert repeated.queue_entry is not None
    assert repeated.queue_entry.queue_id == first.queue_entry.queue_id


def test_recalculate_replaces_once_and_discard_stops_materialization(tmp_path: Path) -> None:
    system = _system(tmp_path)
    _, active = _adopt_and_activate(system, depth=3)
    state = system.recovery.inspect(system.session_id)
    assert state.state is not None

    recalculated = system.recovery.recalculate_plan(
        active.plan.plan_id,
        state.state.plan_revision,
        state.state.actual_predecessor_track_id,
    )

    assert recalculated.code is AutomaticSelectionPlanRecoveryResultCode.RECALCULATED
    assert recalculated.plan is not None
    assert recalculated.plan.plan.plan_id != active.plan.plan_id
    old = system.plans.get(active.plan.plan_id)
    assert old is not None
    assert old.plan.status is AutomaticSelectionPlanStatus.INVALIDATED
    with system.database.connect() as connection:
        resumable = connection.execute(
            """SELECT COUNT(*) FROM automatic_selection_plans
               WHERE session_id=? AND status IN ('ACTIVE','PAUSED')""",
            (system.session_id,),
        ).fetchone()[0]
    assert resumable == 1

    discarded = system.recovery.discard_plan(
        recalculated.plan.plan.plan_id, recalculated.plan.plan.revision
    )
    assert discarded.code is AutomaticSelectionPlanRecoveryResultCode.DISCARDED
    assert discarded.plan is not None
    rejected = system.execution.materialize_next_step(
        recalculated.plan.plan.plan_id, discarded.plan.plan.revision
    )
    assert rejected.status is AutomaticSelectionPlanExecutionStatus.NO_ACTIVE_PLAN
    stored = system.plans.get(recalculated.plan.plan.plan_id)
    assert stored is not None
    assert stored.plan.status is AutomaticSelectionPlanStatus.DISCARDED


def test_track_blocked_after_planning_is_invalidated_before_queue_insert(
    tmp_path: Path,
) -> None:
    system = _system(tmp_path)
    _, active = _adopt_and_activate(system, depth=2)
    first_track_id = active.steps[0].track_id
    system.policy.set_policy(first_track_id, TrackPolicyStatus.BLOCKED, "E6")

    result = system.execution.materialize_next_step(active.plan.plan_id, active.plan.revision)

    assert result.status is AutomaticSelectionPlanExecutionStatus.RECALCULATION_REQUIRED
    assert result.plan is not None
    assert result.plan.plan.status is AutomaticSelectionPlanStatus.PAUSED
    assert result.plan.steps[0].status is AutomaticSelectionPlanStepStatus.INVALIDATED
    with system.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM party_queue").fetchone()[0] == 0


def test_stale_digest_and_stale_preview_leave_no_partial_state(tmp_path: Path) -> None:
    system = _system(tmp_path)
    preview = system.queue.preview_automatic_selection(3)
    system.selection.recent_track_limit = 1

    stale = system.ui.adopt_preview(preview)

    assert stale.code is PreviewAdoptionCode.STALE
    with system.database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM automatic_selection_plans").fetchone()[0] == 0
        )
