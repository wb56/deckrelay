"""Activation and safe one-step execution of persisted automatic-selection plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStepStatus,
)
from party_player.automatic_selection_planning import (
    AutomaticSelectionPlanCompletion,
    AutomaticSelectionPlanningService,
)
from party_player.enums import QueueSource, QueueStatus
from party_player.file_availability import FileAvailabilityChecker
from party_player.models import QueueEntry
from party_player.repositories.automatic_selection_plan_repository import (
    AutomaticSelectionPlanConflictError,
    AutomaticSelectionPlanNotFoundError,
    AutomaticSelectionPlanRepository,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.track_selection import TrackSelectionService


class AutomaticSelectionPlanExecutionStatus(StrEnum):
    ACTIVATED = "ACTIVATED"
    MATERIALIZED = "MATERIALIZED"
    ALREADY_MATERIALIZED = "ALREADY_MATERIALIZED"
    NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"
    NO_REMAINING_STEP = "NO_REMAINING_STEP"
    PLAN_COMPLETED = "PLAN_COMPLETED"
    NO_SAFE_CANDIDATE = "NO_SAFE_CANDIDATE"
    RECALCULATION_REQUIRED = "RECALCULATION_REQUIRED"
    CONCURRENT_UPDATE = "CONCURRENT_UPDATE"
    INCONSISTENT_QUEUE_LINK = "INCONSISTENT_QUEUE_LINK"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class AutomaticSelectionPlanExecutionResult:
    status: AutomaticSelectionPlanExecutionStatus
    plan: AutomaticSelectionPlanBundle | None = None
    queue_entry: QueueEntry | None = None
    reason_code: str | None = None


_TERMINAL_QUEUE_STATUSES = {
    QueueStatus.PLAYED,
    QueueStatus.SKIPPED,
    QueueStatus.FAILED,
    QueueStatus.REMOVED,
}


class AutomaticSelectionPlanExecutionService:
    """Coordinate plan lifecycle without duplicating queue execution state."""

    def __init__(
        self,
        plans: AutomaticSelectionPlanRepository,
        planning: AutomaticSelectionPlanningService,
        tracks: TrackRepository,
        rules: TrackSelectionService,
        file_availability: FileAvailabilityChecker,
    ) -> None:
        self._plans = plans
        self._planning = planning
        self._tracks = tracks
        self._rules = rules
        self._file_availability = file_availability
        self._lock = Lock()

    def activate_draft_plan(
        self,
        plan_id: str,
        expected_revision: int,
    ) -> AutomaticSelectionPlanExecutionResult:
        try:
            digest = self._planning.current_configuration_digest()
            plan = self._plans.activate_draft_plan(plan_id, expected_revision, digest)
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.ACTIVATED,
                plan,
            )
        except AutomaticSelectionPlanConflictError:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.CONCURRENT_UPDATE,
                reason_code="PLAN_CONCURRENT_UPDATE",
            )
        except (AutomaticSelectionPlanNotFoundError, ValueError):
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.NO_ACTIVE_PLAN,
                reason_code="PLAN_ACTIVATION_REJECTED",
            )
        except Exception:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.PERSISTENCE_FAILED,
                reason_code="PLAN_ACTIVATION_FAILED",
            )

    def observe_playback_started(self, session_id: int, queue_id: int) -> None:
        """Pause an active plan when actual playback is not its linked next step."""
        try:
            self._plans.pause_for_foreign_playback(session_id, queue_id)
        except Exception:
            # Playback is authoritative and must never be stopped by plan bookkeeping.
            return

    def ensure_next_step(self, session_id: int) -> AutomaticSelectionPlanExecutionResult:
        """Use or create one active plan and materialize at most one step."""
        with self._lock:
            try:
                plan = self._plans.get_resumable_for_session(session_id)
                if plan is not None and plan.plan.status is AutomaticSelectionPlanStatus.PAUSED:
                    return AutomaticSelectionPlanExecutionResult(
                        AutomaticSelectionPlanExecutionStatus.NO_ACTIVE_PLAN,
                        plan,
                        reason_code="PLAN_PAUSED",
                    )
                if plan is None:
                    return self._create_activate_materialize(session_id)
                assert plan is not None
                existing = self._existing_active_queue_entry(plan)
                if existing is not None:
                    return existing
                result = self.materialize_next_step(plan.plan.plan_id, plan.plan.revision)
                if result.status is AutomaticSelectionPlanExecutionStatus.PLAN_COMPLETED:
                    return self._create_activate_materialize(session_id)
                return result
            except AutomaticSelectionPlanConflictError:
                return AutomaticSelectionPlanExecutionResult(
                    AutomaticSelectionPlanExecutionStatus.CONCURRENT_UPDATE,
                    reason_code="PLAN_CONCURRENT_UPDATE",
                )
            except Exception:
                return AutomaticSelectionPlanExecutionResult(
                    AutomaticSelectionPlanExecutionStatus.PERSISTENCE_FAILED,
                    reason_code="PLAN_EXECUTION_FAILED",
                )

    def materialize_next_step(
        self,
        plan_id: str,
        expected_revision: int,
    ) -> AutomaticSelectionPlanExecutionResult:
        try:
            bundle = self._plans.get(plan_id)
            if bundle is None or bundle.plan.status is not AutomaticSelectionPlanStatus.ACTIVE:
                return AutomaticSelectionPlanExecutionResult(
                    AutomaticSelectionPlanExecutionStatus.NO_ACTIVE_PLAN,
                    bundle,
                    reason_code="PLAN_NOT_ACTIVE",
                )
            if bundle.plan.revision != expected_revision:
                return AutomaticSelectionPlanExecutionResult(
                    AutomaticSelectionPlanExecutionStatus.CONCURRENT_UPDATE,
                    bundle,
                    reason_code="PLAN_CONCURRENT_UPDATE",
                )
            step = next(
                (
                    item
                    for item in bundle.steps
                    if item.status is AutomaticSelectionPlanStepStatus.PLANNED
                ),
                None,
            )
            if step is None:
                return self._reconcile_completion(bundle)
            track = self._tracks.get_active(step.track_id)
            synthetic = QueueEntry(
                -step.position,
                step.track_id,
                0,
                QueueStatus.WAITING,
                source=QueueSource.AUTOMATIC,
            )
            decision = self._rules.evaluate(synthetic, track)
            if decision.accepted and track is not None:
                decision = self._file_availability.evaluate(track)
            if not decision.accepted:
                return self._invalidate_and_pause(
                    bundle,
                    reason_code=self._safe_reason_code(decision.code),
                )
            updated, entry = self._plans.materialize_step(
                plan_id,
                step.position,
                expected_revision,
            )
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.MATERIALIZED,
                updated,
                entry,
            )
        except AutomaticSelectionPlanConflictError:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.CONCURRENT_UPDATE,
                reason_code="PLAN_CONCURRENT_UPDATE",
            )
        except AutomaticSelectionPlanNotFoundError:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.NO_ACTIVE_PLAN,
                reason_code="PLAN_NOT_FOUND",
            )
        except Exception:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.PERSISTENCE_FAILED,
                reason_code="PLAN_MATERIALIZATION_FAILED",
            )

    def _existing_active_queue_entry(
        self,
        bundle: AutomaticSelectionPlanBundle,
    ) -> AutomaticSelectionPlanExecutionResult | None:
        step = next(
            (
                item
                for item in bundle.steps
                if item.status is AutomaticSelectionPlanStepStatus.QUEUED
                and item.executed_queue_status not in _TERMINAL_QUEUE_STATUSES
            ),
            None,
        )
        if step is None:
            return None
        if step.executed_queue_status is None:
            paused = self._plans.change_status(
                bundle.plan.plan_id,
                bundle.plan.revision,
                AutomaticSelectionPlanStatus.PAUSED,
            )
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.INCONSISTENT_QUEUE_LINK,
                paused,
                reason_code="PLAN_QUEUE_LINK_MISSING",
            )
        updated, entry = self._plans.materialize_step(
            bundle.plan.plan_id,
            step.position,
            bundle.plan.revision,
        )
        return AutomaticSelectionPlanExecutionResult(
            AutomaticSelectionPlanExecutionStatus.ALREADY_MATERIALIZED,
            updated,
            entry,
        )

    def _create_activate_materialize(
        self,
        session_id: int,
    ) -> AutomaticSelectionPlanExecutionResult:
        created = self._planning.create_draft_plan(session_id, depth=5)
        if created.completion_status is AutomaticSelectionPlanCompletion.NO_SAFE_CANDIDATE:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.NO_SAFE_CANDIDATE,
                reason_code=created.terminal_reason_code or "NO_SAFE_CANDIDATE",
            )
        if created.completion_status is AutomaticSelectionPlanCompletion.FAILED:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.PERSISTENCE_FAILED,
                reason_code=created.terminal_reason_code or "PLAN_CREATION_FAILED",
            )
        if created.plan is None:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.NO_SAFE_CANDIDATE,
                reason_code="NO_SAFE_CANDIDATE",
            )
        activated = self.activate_draft_plan(
            created.plan.plan.plan_id,
            created.plan.plan.revision,
        )
        if activated.status is not AutomaticSelectionPlanExecutionStatus.ACTIVATED:
            return activated
        assert activated.plan is not None
        return self.materialize_next_step(
            activated.plan.plan.plan_id,
            activated.plan.plan.revision,
        )

    def _invalidate_and_pause(
        self,
        bundle: AutomaticSelectionPlanBundle,
        *,
        reason_code: str,
    ) -> AutomaticSelectionPlanExecutionResult:
        step = next(
            (
                item
                for item in bundle.steps
                if item.status is AutomaticSelectionPlanStepStatus.PLANNED
            ),
            None,
        )
        if step is None:
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.NO_REMAINING_STEP,
                bundle,
                reason_code="PLAN_HAS_NO_PLANNED_STEP",
            )
        paused = self._plans.pause_invalid_step(
            bundle.plan.plan_id,
            step.position,
            bundle.plan.revision,
            reason_code,
        )
        return AutomaticSelectionPlanExecutionResult(
            AutomaticSelectionPlanExecutionStatus.RECALCULATION_REQUIRED,
            paused,
            reason_code=reason_code,
        )

    def _reconcile_completion(
        self,
        bundle: AutomaticSelectionPlanBundle,
    ) -> AutomaticSelectionPlanExecutionResult:
        linked = tuple(
            step for step in bundle.steps if step.status is AutomaticSelectionPlanStepStatus.QUEUED
        )
        if any(step.executed_queue_status is None for step in linked):
            paused = self._plans.change_status(
                bundle.plan.plan_id,
                bundle.plan.revision,
                AutomaticSelectionPlanStatus.PAUSED,
            )
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.INCONSISTENT_QUEUE_LINK,
                paused,
                reason_code="PLAN_QUEUE_LINK_MISSING",
            )
        if any(step.executed_queue_status not in _TERMINAL_QUEUE_STATUSES for step in linked):
            return AutomaticSelectionPlanExecutionResult(
                AutomaticSelectionPlanExecutionStatus.NO_REMAINING_STEP,
                bundle,
                reason_code="PLAN_STEPS_STILL_ACTIVE",
            )
        completed = self._plans.change_status(
            bundle.plan.plan_id,
            bundle.plan.revision,
            AutomaticSelectionPlanStatus.COMPLETED,
        )
        return AutomaticSelectionPlanExecutionResult(
            AutomaticSelectionPlanExecutionStatus.PLAN_COMPLETED,
            completed,
        )

    @staticmethod
    def _safe_reason_code(code: str) -> str:
        normalized = code.strip().upper()
        if (
            normalized
            and len(normalized) <= 64
            and all(character.isalnum() or character == "_" for character in normalized)
        ):
            return normalized
        return "PLANNED_TRACK_REJECTED"
