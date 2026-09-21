from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlan,
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStep,
    AutomaticSelectionPlanStepStatus,
)
from party_player.automatic_selection_plan_execution import (
    AutomaticSelectionPlanExecutionResult,
    AutomaticSelectionPlanExecutionService,
    AutomaticSelectionPlanExecutionStatus,
)
from party_player.automatic_selection_planning import (
    AutomaticSelectionPlanCompletion,
    AutomaticSelectionPlanCreationResult,
)
from party_player.enums import QueueSource, QueueStatus
from party_player.enums import EmptyQueuePolicy
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.models import QueueEntry, Track
from party_player.queue_service import QueueService
from party_player.repository import PartyPlayerRepository
from party_player.repositories.track_repository import TrackRepository
from party_player.track_selection import SelectionDecision, TrackSelectionService

PLAN_ID = "10000000-0000-4000-8000-000000000001"
STEP_ID = "20000000-0000-4000-8000-000000000001"
CURRENT_DIGEST = "a" * 64


def _track(track_id: int = 1) -> Track:
    return Track(track_id, f"track-{track_id}.mp3", f"Track {track_id}", "Artist", "", 180.0)


def _bundle(
    *,
    status: AutomaticSelectionPlanStatus = AutomaticSelectionPlanStatus.DRAFT,
    revision: int = 0,
    step_status: AutomaticSelectionPlanStepStatus = AutomaticSelectionPlanStepStatus.PLANNED,
    digest: str = CURRENT_DIGEST,
    queue_status: QueueStatus | None = None,
    relaxation_stage: str = "STRICT",
) -> AutomaticSelectionPlanBundle:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    plan = AutomaticSelectionPlan(
        PLAN_ID,
        7,
        status,
        now,
        now,
        41,
        1,
        digest,
        1,
        "AUTOMATIC",
        1,
        revision,
    )
    step = AutomaticSelectionPlanStep(
        STEP_ID,
        plan.plan_id,
        1,
        1,
        step_status,
        now,
        None,
        relaxation_stage,
        0,
        0.0,
        1,
        "SEEDED_RANDOM",
        "BEST_SCORE",
        91 if step_status is AutomaticSelectionPlanStepStatus.QUEUED else None,
        queue_status,
        None,
    )
    return AutomaticSelectionPlanBundle(plan, (step,))


class _Plans:
    def __init__(self, bundle: AutomaticSelectionPlanBundle | None = None) -> None:
        self.bundle = bundle
        self.materialize_calls = 0

    def get_resumable_for_session(self, session_id: int):
        assert session_id == 7
        return (
            self.bundle
            if self.bundle
            and self.bundle.plan.status
            in {
                AutomaticSelectionPlanStatus.ACTIVE,
                AutomaticSelectionPlanStatus.PAUSED,
            }
            else None
        )

    def get(self, plan_id: str):
        return self.bundle if self.bundle and self.bundle.plan.plan_id == plan_id else None

    def activate_draft_plan(self, plan_id: str, expected_revision: int, digest: str):
        assert self.bundle is not None
        assert self.bundle.plan.plan_id == plan_id
        assert self.bundle.plan.revision == expected_revision
        if self.bundle.plan.rule_configuration_digest != digest:
            raise ValueError("digest")
        self.bundle = AutomaticSelectionPlanBundle(
            replace(
                self.bundle.plan,
                status=AutomaticSelectionPlanStatus.ACTIVE,
                revision=expected_revision + 1,
            ),
            self.bundle.steps,
        )
        return self.bundle

    def pause_invalid_step(self, plan_id: str, position: int, revision: int, reason: str):
        assert self.bundle is not None
        self.bundle = AutomaticSelectionPlanBundle(
            replace(
                self.bundle.plan,
                status=AutomaticSelectionPlanStatus.PAUSED,
                revision=revision + 1,
            ),
            (
                replace(
                    self.bundle.steps[0],
                    status=AutomaticSelectionPlanStepStatus.INVALIDATED,
                    invalid_reason_code=reason,
                ),
            ),
        )
        return self.bundle

    def materialize_step(self, plan_id: str, position: int, revision: int):
        assert self.bundle is not None
        self.materialize_calls += 1
        entry = QueueEntry(
            91,
            1,
            1,
            QueueStatus.WAITING,
            source=QueueSource.AUTOMATIC,
            priority=100,
        )
        if self.bundle.steps[0].status is AutomaticSelectionPlanStepStatus.QUEUED:
            return self.bundle, entry
        self.bundle = AutomaticSelectionPlanBundle(
            replace(self.bundle.plan, revision=revision + 1),
            (
                replace(
                    self.bundle.steps[0],
                    status=AutomaticSelectionPlanStepStatus.QUEUED,
                    executed_queue_entry_id=91,
                    executed_queue_status=QueueStatus.WAITING,
                ),
            ),
        )
        return self.bundle, entry

    def change_status(self, plan_id: str, revision: int, target: AutomaticSelectionPlanStatus):
        assert self.bundle is not None
        self.bundle = AutomaticSelectionPlanBundle(
            replace(self.bundle.plan, status=target, revision=revision + 1),
            self.bundle.steps,
        )
        return self.bundle


class _Planning:
    def __init__(self, plans: _Plans, result: AutomaticSelectionPlanCreationResult | None = None):
        self.plans = plans
        self.result = result
        self.depths: list[int] = []

    def current_configuration_digest(self) -> str:
        return CURRENT_DIGEST

    def create_draft_plan(self, session_id: int, depth: int = 5):
        self.depths.append(depth)
        if self.result is not None:
            return self.result
        draft = _bundle()
        self.plans.bundle = draft
        return AutomaticSelectionPlanCreationResult(
            5,
            1,
            AutomaticSelectionPlanCompletion.COMPLETE,
            draft,
        )


class _Tracks:
    def __init__(self, track: Track | None = None) -> None:
        self.track = track

    def get_active(self, track_id: int):
        return self.track if self.track and self.track.id == track_id else None


class _Availability:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted

    def evaluate(self, track: Track) -> SelectionDecision:
        return (
            SelectionDecision.allow()
            if self.accepted
            else SelectionDecision.reject("FILE_UNAVAILABLE")
        )


def _service(
    plans: _Plans,
    *,
    track: Track | None = None,
    available: bool = True,
    planning: _Planning | None = None,
    rules: object | None = None,
) -> AutomaticSelectionPlanExecutionService:
    return AutomaticSelectionPlanExecutionService(
        plans,  # type: ignore[arg-type]
        planning or _Planning(plans),  # type: ignore[arg-type]
        _Tracks(track),  # type: ignore[arg-type]
        rules or TrackSelectionService(),  # type: ignore[arg-type]
        _Availability(available),
    )


def test_materialization_reuses_the_relaxation_approved_by_the_preview() -> None:
    plans = _Plans(
        _bundle(
            status=AutomaticSelectionPlanStatus.ACTIVE,
            revision=1,
            relaxation_stage="TRACK_DISTANCE",
        )
    )

    class Rules:
        relaxed_codes: frozenset[str] = frozenset()

        def evaluate(
            self,
            _entry: QueueEntry,
            _track: Track | None,
            *,
            relaxed_codes: frozenset[str],
        ) -> SelectionDecision:
            self.relaxed_codes = relaxed_codes
            return SelectionDecision.allow()

    rules = Rules()

    result = _service(plans, track=_track(), rules=rules).materialize_next_step(PLAN_ID, 1)

    assert result.status is AutomaticSelectionPlanExecutionStatus.MATERIALIZED
    assert rules.relaxed_codes == frozenset(
        {"ARTIST_REPETITION", "TRACK_REPETITION", "RECENT_TRACK"}
    )


def test_materialized_queue_entry_exposes_its_reviewed_relaxation() -> None:
    plans = _Plans(
        _bundle(
            status=AutomaticSelectionPlanStatus.ACTIVE,
            revision=2,
            step_status=AutomaticSelectionPlanStepStatus.QUEUED,
            queue_status=QueueStatus.WAITING,
            relaxation_stage="TRACK_DISTANCE",
        )
    )
    service = _service(plans)

    stage, codes = service.relaxation_for_queue_entry(7, 91)

    assert stage == "TRACK_DISTANCE"
    assert codes == frozenset({"ARTIST_REPETITION", "TRACK_REPETITION", "RECENT_TRACK"})


def test_empty_session_creates_depth_five_plan_and_materializes_one_step() -> None:
    plans = _Plans()
    planning = _Planning(plans)
    service = _service(plans, track=_track(), planning=planning)

    result = service.ensure_next_step(7)

    assert result.status is AutomaticSelectionPlanExecutionStatus.MATERIALIZED
    assert planning.depths == [5]
    assert plans.materialize_calls == 1
    assert result.queue_entry is not None
    assert result.queue_entry.source is QueueSource.AUTOMATIC
    assert result.queue_entry.priority == 100


def test_missing_or_unavailable_track_invalidates_step_and_pauses_plan() -> None:
    for track, available, reason in (
        (None, True, "TRACK_MISSING"),
        (_track(), False, "FILE_UNAVAILABLE"),
    ):
        plans = _Plans(_bundle(status=AutomaticSelectionPlanStatus.ACTIVE, revision=1))
        result = _service(plans, track=track, available=available).materialize_next_step(PLAN_ID, 1)
        assert result.status is AutomaticSelectionPlanExecutionStatus.RECALCULATION_REQUIRED
        assert result.reason_code == reason
        assert plans.bundle is not None
        assert plans.bundle.plan.status is AutomaticSelectionPlanStatus.PAUSED
        assert plans.materialize_calls == 0


def test_no_safe_candidate_does_not_create_or_activate_plan() -> None:
    plans = _Plans()
    planning = _Planning(
        plans,
        AutomaticSelectionPlanCreationResult(
            5,
            0,
            AutomaticSelectionPlanCompletion.NO_SAFE_CANDIDATE,
            None,
            "NO_SAFE_CANDIDATE",
        ),
    )

    result = _service(plans, planning=planning).ensure_next_step(7)

    assert result.status is AutomaticSelectionPlanExecutionStatus.NO_SAFE_CANDIDATE
    assert plans.bundle is None


def test_terminal_materialized_plan_completes_idempotently() -> None:
    plans = _Plans(
        _bundle(
            status=AutomaticSelectionPlanStatus.ACTIVE,
            revision=2,
            step_status=AutomaticSelectionPlanStepStatus.QUEUED,
            queue_status=QueueStatus.PLAYED,
        )
    )
    service = _service(plans)

    result = service.materialize_next_step(PLAN_ID, 2)
    repeated = service.materialize_next_step(PLAN_ID, 3)

    assert result.status is AutomaticSelectionPlanExecutionStatus.PLAN_COMPLETED
    assert repeated.status is AutomaticSelectionPlanExecutionStatus.NO_ACTIVE_PLAN
    assert plans.bundle is not None and plans.bundle.plan.revision == 3


def test_exhausted_plan_completes_while_materialized_queue_row_remains_active() -> None:
    waiting = _Plans(
        _bundle(
            status=AutomaticSelectionPlanStatus.ACTIVE,
            revision=2,
            step_status=AutomaticSelectionPlanStepStatus.QUEUED,
            queue_status=QueueStatus.WAITING,
        )
    )
    missing = _Plans(
        _bundle(
            status=AutomaticSelectionPlanStatus.ACTIVE,
            revision=2,
            step_status=AutomaticSelectionPlanStepStatus.QUEUED,
            queue_status=None,
        )
    )

    active = _service(waiting).materialize_next_step(PLAN_ID, 2)
    inconsistent = _service(missing).materialize_next_step(PLAN_ID, 2)

    assert active.status is AutomaticSelectionPlanExecutionStatus.PLAN_COMPLETED
    assert inconsistent.status is AutomaticSelectionPlanExecutionStatus.INCONSISTENT_QUEUE_LINK
    assert missing.bundle is not None
    assert missing.bundle.plan.status is AutomaticSelectionPlanStatus.PAUSED


def test_active_materialized_step_is_reused_before_another_step_is_queued() -> None:
    plans = _Plans(
        _bundle(
            status=AutomaticSelectionPlanStatus.ACTIVE,
            revision=2,
            step_status=AutomaticSelectionPlanStepStatus.QUEUED,
            queue_status=QueueStatus.WAITING,
        )
    )

    result = _service(plans).ensure_next_step(7)

    assert result.status is AutomaticSelectionPlanExecutionStatus.ALREADY_MATERIALIZED
    assert result.queue_entry is not None and result.queue_entry.queue_id == 91
    assert plans.materialize_calls == 1


def test_playing_plan_step_does_not_block_materializing_its_successor() -> None:
    bundle = _bundle(
        status=AutomaticSelectionPlanStatus.ACTIVE,
        revision=2,
        step_status=AutomaticSelectionPlanStepStatus.QUEUED,
        queue_status=QueueStatus.PLAYING,
    )
    service = _service(_Plans(bundle))

    assert service._existing_active_queue_entry(bundle) is None


class _QueuePlanExecution:
    def __init__(self, repository: PartyPlayerRepository, session_id: int) -> None:
        self.repository = repository
        self.session_id = session_id
        self.calls = 0

    def ensure_next_step(self, session_id: int) -> AutomaticSelectionPlanExecutionResult:
        assert session_id == self.session_id
        self.calls += 1
        entry = self.repository.add_queue_entry(session_id, 1, QueueSource.AUTOMATIC)
        return AutomaticSelectionPlanExecutionResult(
            AutomaticSelectionPlanExecutionStatus.MATERIALIZED,
            queue_entry=entry,
        )


def _queue_setup(path: Path):
    database = Database(path)
    migrate(database)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO tracks (id,file_path,title,artist) VALUES (1,'one.mp3','One','Artist')"
        )
    repository = PartyPlayerRepository(database)
    session = repository.create_session("Execution")
    execution = _QueuePlanExecution(repository, session.session_id)
    service = QueueService(
        repository,
        TrackRepository(database),
        session.session_id,
        empty_queue_policy=EmptyQueuePolicy.AUTOMATIC_SELECTION,
        automatic_plan_execution=execution,  # type: ignore[arg-type]
    )
    return service, execution


def test_queue_materializes_plan_only_after_existing_candidate_is_consumed(
    tmp_path: Path,
) -> None:
    service, execution = _queue_setup(tmp_path / "queue-priority.db")
    manual = service.add(1, QueueSource.MANUAL)

    first = service.get_next_candidate()
    service.mark_played(manual.queue_id)
    planned = service.get_next_candidate()

    assert first == manual
    assert execution.calls == 1
    assert planned is not None
    assert planned.source is QueueSource.AUTOMATIC
    assert planned.priority == 100


def test_queue_does_not_fall_back_to_unpersisted_selection_after_plan_failure(
    tmp_path: Path,
) -> None:
    service, execution = _queue_setup(tmp_path / "queue-plan-failure.db")

    def fail(_session_id: int) -> AutomaticSelectionPlanExecutionResult:
        execution.calls += 1
        return AutomaticSelectionPlanExecutionResult(
            AutomaticSelectionPlanExecutionStatus.PERSISTENCE_FAILED,
            reason_code="PLAN_CREATION_FAILED",
        )

    execution.ensure_next_step = fail  # type: ignore[method-assign]

    assert service.get_next_candidate() is None
    assert service.automatic_plan_pause_reason() is not None
    assert service.entries() == []


def test_already_materialized_preparing_step_is_not_claimed_twice(tmp_path: Path) -> None:
    service, execution = _queue_setup(tmp_path / "queue-preparing-idempotence.db")
    entry = service.add(1, QueueSource.AUTOMATIC)
    service.mark_preparing(entry.queue_id, "A")

    def already_materialized(_session_id: int) -> AutomaticSelectionPlanExecutionResult:
        current = service.entry(entry.queue_id)
        assert current is not None
        return AutomaticSelectionPlanExecutionResult(
            AutomaticSelectionPlanExecutionStatus.ALREADY_MATERIALIZED,
            queue_entry=current,
        )

    execution.ensure_next_step = already_materialized  # type: ignore[method-assign]

    assert service.get_next_candidate() is None
    current = service.entry(entry.queue_id)
    assert current is not None
    assert current.status is QueueStatus.PREPARING
