"""Safe recovery decisions for interrupted automatic-selection plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanRecoveryState,
)
from party_player.automatic_selection_planning import AutomaticSelectionPlanningService
from party_player.file_availability import FileAvailabilityChecker
from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry
from party_player.repositories.automatic_selection_plan_repository import (
    AutomaticSelectionPlanConflictError,
    AutomaticSelectionPlanRepository,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.track_selection import TrackSelectionService


class AutomaticSelectionPlanRecoveryResultCode(StrEnum):
    RECOVERY_NOT_REQUIRED = "RECOVERY_NOT_REQUIRED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RESUMED = "RESUMED"
    RECALCULATED = "RECALCULATED"
    DISCARDED = "DISCARDED"
    RECALCULATION_REQUIRED = "RECALCULATION_REQUIRED"
    INTERRUPTED_PLAYBACK = "INTERRUPTED_PLAYBACK"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    PREDECESSOR_CHANGED = "PREDECESSOR_CHANGED"
    INCONSISTENT_QUEUE_LINK = "INCONSISTENT_QUEUE_LINK"
    CONCURRENT_UPDATE = "CONCURRENT_UPDATE"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class AutomaticSelectionPlanRecoveryResult:
    code: AutomaticSelectionPlanRecoveryResultCode
    state: AutomaticSelectionPlanRecoveryState | None = None
    plan: AutomaticSelectionPlanBundle | None = None


class AutomaticSelectionPlanRecoveryService:
    """Keep startup paused until one explicit, single-shot recovery decision."""

    def __init__(
        self,
        plans: AutomaticSelectionPlanRepository,
        planning: AutomaticSelectionPlanningService,
        tracks: TrackRepository,
        rules: TrackSelectionService,
        availability: FileAvailabilityChecker,
    ) -> None:
        self._plans = plans
        self._planning = planning
        self._tracks = tracks
        self._rules = rules
        self._availability = availability
        self._decision_lock = Lock()

    def inspect(self, session_id: int) -> AutomaticSelectionPlanRecoveryResult:
        try:
            state = self._plans.recovery_state(
                session_id,
                self._planning.current_configuration_digest(),
            )
        except Exception:
            return AutomaticSelectionPlanRecoveryResult(
                AutomaticSelectionPlanRecoveryResultCode.PERSISTENCE_FAILED
            )
        if state is None:
            return AutomaticSelectionPlanRecoveryResult(
                AutomaticSelectionPlanRecoveryResultCode.RECOVERY_NOT_REQUIRED
            )
        code = {
            "INTERRUPTED_PLAYBACK": AutomaticSelectionPlanRecoveryResultCode.INTERRUPTED_PLAYBACK,
            "CONFIGURATION_CHANGED": AutomaticSelectionPlanRecoveryResultCode.CONFIGURATION_CHANGED,
            "PREDECESSOR_CHANGED": AutomaticSelectionPlanRecoveryResultCode.PREDECESSOR_CHANGED,
            "INCONSISTENT_QUEUE_LINK": AutomaticSelectionPlanRecoveryResultCode.INCONSISTENT_QUEUE_LINK,
        }.get(
            state.reason_code.value,
            AutomaticSelectionPlanRecoveryResultCode.RECOVERY_REQUIRED,
        )
        return AutomaticSelectionPlanRecoveryResult(code, state)

    def resume_plan(
        self, plan_id: str, expected_revision: int
    ) -> AutomaticSelectionPlanRecoveryResult:
        with self._decision_lock:
            try:
                bundle = self._plans.get(plan_id)
                if bundle is None or bundle.plan.revision != expected_revision:
                    return self._concurrent()
                state = self._plans.recovery_state(
                    bundle.plan.session_id,
                    self._planning.current_configuration_digest(),
                )
                if state is None:
                    return AutomaticSelectionPlanRecoveryResult(
                        AutomaticSelectionPlanRecoveryResultCode.RECOVERY_NOT_REQUIRED
                    )
                if not state.configuration_matches:
                    return AutomaticSelectionPlanRecoveryResult(
                        AutomaticSelectionPlanRecoveryResultCode.RECALCULATION_REQUIRED, state
                    )
                if state.interrupted_step_id is not None:
                    return AutomaticSelectionPlanRecoveryResult(
                        AutomaticSelectionPlanRecoveryResultCode.RECALCULATION_REQUIRED, state
                    )
                if state.reason_code.value in {
                    "PREDECESSOR_CHANGED",
                    "INCONSISTENT_QUEUE_LINK",
                }:
                    return AutomaticSelectionPlanRecoveryResult(
                        AutomaticSelectionPlanRecoveryResultCode.RECALCULATION_REQUIRED, state
                    )
                relaxed_codes = self._rules.all_relaxable_reason_codes()
                for step in bundle.steps:
                    if step.status.value != "PLANNED" and step.executed_queue_status in {
                        QueueStatus.PLAYED,
                        QueueStatus.SKIPPED,
                        QueueStatus.FAILED,
                        QueueStatus.REMOVED,
                    }:
                        continue
                    track = self._tracks.get_active(step.track_id)
                    entry = QueueEntry(
                        -step.position,
                        step.track_id,
                        step.position,
                        QueueStatus.WAITING,
                        source=QueueSource.AUTOMATIC,
                    )
                    if (
                        track is None
                        or not self._rules.evaluate(
                            entry,
                            track,
                            relaxed_codes=relaxed_codes,
                        ).accepted
                    ):
                        return AutomaticSelectionPlanRecoveryResult(
                            AutomaticSelectionPlanRecoveryResultCode.RECALCULATION_REQUIRED, state
                        )
                    if not self._availability.evaluate(track).accepted:
                        return AutomaticSelectionPlanRecoveryResult(
                            AutomaticSelectionPlanRecoveryResultCode.RECALCULATION_REQUIRED, state
                        )
                resumed = self._plans.resume_recovered_plan(plan_id, expected_revision)
                return AutomaticSelectionPlanRecoveryResult(
                    AutomaticSelectionPlanRecoveryResultCode.RESUMED, state, resumed
                )
            except AutomaticSelectionPlanConflictError:
                return self._concurrent()
            except Exception:
                return AutomaticSelectionPlanRecoveryResult(
                    AutomaticSelectionPlanRecoveryResultCode.PERSISTENCE_FAILED
                )

    def recalculate_plan(
        self,
        plan_id: str,
        expected_revision: int,
        actual_predecessor: int | None,
        depth: int | None = None,
    ) -> AutomaticSelectionPlanRecoveryResult:
        with self._decision_lock:
            old = self._plans.get(plan_id)
            if old is None or old.plan.revision != expected_revision:
                return self._concurrent()
            remaining = sum(step.status.value == "PLANNED" for step in old.steps)
            requested_depth = max(1, min(10, depth if depth is not None else remaining or 1))
            created = self._planning.create_draft_plan(
                old.plan.session_id,
                requested_depth,
                previous_track_id=actual_predecessor,
            )
            if created.plan is None:
                return AutomaticSelectionPlanRecoveryResult(
                    AutomaticSelectionPlanRecoveryResultCode.PERSISTENCE_FAILED
                )
            try:
                active = self._plans.replace_recovered_plan(
                    plan_id,
                    expected_revision,
                    created.plan.plan.plan_id,
                    self._planning.current_configuration_digest(),
                )
                return AutomaticSelectionPlanRecoveryResult(
                    AutomaticSelectionPlanRecoveryResultCode.RECALCULATED, plan=active
                )
            except AutomaticSelectionPlanConflictError:
                return self._concurrent()
            except Exception:
                return AutomaticSelectionPlanRecoveryResult(
                    AutomaticSelectionPlanRecoveryResultCode.PERSISTENCE_FAILED
                )

    def discard_plan(
        self, plan_id: str, expected_revision: int
    ) -> AutomaticSelectionPlanRecoveryResult:
        with self._decision_lock:
            try:
                discarded = self._plans.discard_recovered_plan(plan_id, expected_revision)
                return AutomaticSelectionPlanRecoveryResult(
                    AutomaticSelectionPlanRecoveryResultCode.DISCARDED, plan=discarded
                )
            except AutomaticSelectionPlanConflictError:
                return self._concurrent()
            except Exception:
                return AutomaticSelectionPlanRecoveryResult(
                    AutomaticSelectionPlanRecoveryResultCode.PERSISTENCE_FAILED
                )

    @staticmethod
    def _concurrent() -> AutomaticSelectionPlanRecoveryResult:
        return AutomaticSelectionPlanRecoveryResult(
            AutomaticSelectionPlanRecoveryResultCode.CONCURRENT_UPDATE
        )
