"""Safe presentation and exact adoption of automatic-selection plans."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from uuid import uuid4

from party_player.automatic_selection import AutomaticSelectionService
from party_player.automatic_selection_plan import (
    AutomaticSelectionPlan,
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStep,
    AutomaticSelectionPlanStepStatus,
)
from party_player.automatic_selection_plan_recovery import (
    AutomaticSelectionPlanRecoveryResultCode,
    AutomaticSelectionPlanRecoveryResult,
    AutomaticSelectionPlanRecoveryService,
)
from party_player.automatic_selection_planning import (
    PLANNING_CONFIGURATION_VERSION,
    AutomaticSelectionPlanningService,
)
from party_player.enums import QueueStatus
from party_player.models import Track
from party_player.repositories.automatic_selection_plan_repository import (
    AutomaticSelectionPlanConflictError,
    AutomaticSelectionPlanRepository,
)
from party_player.repositories.track_repository import TrackRepository
from party_player.selection_decision import CandidateDecisionCategory
from party_player.selection_preview import SelectionPreview


class PreviewAdoptionCode(StrEnum):
    CREATED = "CREATED"
    REPLACED = "REPLACED"
    STALE = "STALE"
    INVALID = "INVALID"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PreviewAdoptionResult:
    code: PreviewAdoptionCode
    plan: AutomaticSelectionPlanBundle | None = None


@dataclass(frozen=True, slots=True)
class PlanStepView:
    position: int
    title: str
    artist: str
    plan_status: str
    execution_status: str
    primary_play_count: int | None
    secondary_score: float
    reason: str
    previous_title: str
    relaxation: str
    tie_text: str


@dataclass(frozen=True, slots=True)
class PlanOverview:
    plan_id: str
    revision: int
    status: AutomaticSelectionPlanStatus
    status_text: str
    session_id: int
    created_at: datetime
    total: int
    open_count: int
    queued_count: int
    played_count: int
    terminal_count: int
    reason: str
    steps: tuple[PlanStepView, ...]


_PLAN_STATUS = {
    AutomaticSelectionPlanStatus.DRAFT: "Entwurf",
    AutomaticSelectionPlanStatus.ACTIVE: "Aktiv",
    AutomaticSelectionPlanStatus.PAUSED: "Pausiert",
    AutomaticSelectionPlanStatus.COMPLETED: "Abgeschlossen",
    AutomaticSelectionPlanStatus.INVALIDATED: "Ungültig",
    AutomaticSelectionPlanStatus.DISCARDED: "Verworfen",
}
_REASONS = {
    "TRACK_UNAVAILABLE": "Titel ist nicht mehr verfügbar",
    "HARD_RULE_CHANGED": "Harte Auswahlregel hat sich geändert",
    "PREDECESSOR_CHANGED": "Vorgängertitel hat sich geändert",
    "CONFIGURATION_CHANGED": "Regelkonfiguration wurde geändert",
    "RECOVERY_INTERRUPTED_PLAYBACK": "Wiedergabe wurde beim Neustart unterbrochen",
    "INCONSISTENT_QUEUE_LINK": "Queue-Verknüpfung ist nicht mehr konsistent",
    "PLAN_DISCARDED": "Plan wurde verworfen",
    "PLAN_RECALCULATED": "Plan wurde neu berechnet",
}
_SELECTION_REASONS = {
    "SELECTED_HIGHEST_SCORE": "Höchste Bewertung",
    "SELECTED_HIGHEST_SECONDARY_SCORE": "Höchste sekundäre Bewertung",
    "SELECTED_STABLE_TIE_BREAK": "Eindeutige stabile Auswahl",
    "SELECTED_RNG_TIE_BREAK": "Zufallsentscheidung bei Gleichstand",
}


class AutomaticSelectionPlanUiService:
    def __init__(
        self,
        plans: AutomaticSelectionPlanRepository,
        tracks: TrackRepository,
        planning: AutomaticSelectionPlanningService,
        recovery: AutomaticSelectionPlanRecoveryService,
        automatic_selection: AutomaticSelectionService,
    ) -> None:
        self._plans = plans
        self._tracks = tracks
        self._planning = planning
        self._recovery = recovery
        self._automatic_selection = automatic_selection
        self._lock = Lock()

    def overview(self, session_id: int) -> PlanOverview | None:
        bundle = self._plans.get_latest_for_session(session_id)
        if bundle is None:
            return None
        track_ids = list(
            {step.track_id for step in bundle.steps}
            | {step.previous_track_id for step in bundle.steps if step.previous_track_id}
        )
        tracks = self._tracks.get_many(track_ids) if track_ids else {}
        views = tuple(self._step_view(step, tracks) for step in bundle.steps)
        played = sum(step.executed_queue_status is QueueStatus.PLAYED for step in bundle.steps)
        terminal = sum(
            step.status is AutomaticSelectionPlanStepStatus.INVALIDATED
            or step.executed_queue_status
            in {QueueStatus.PLAYED, QueueStatus.SKIPPED, QueueStatus.FAILED, QueueStatus.REMOVED}
            for step in bundle.steps
        )
        return PlanOverview(
            bundle.plan.plan_id,
            bundle.plan.revision,
            bundle.plan.status,
            _PLAN_STATUS[bundle.plan.status],
            bundle.plan.session_id,
            bundle.plan.created_at,
            len(bundle.steps),
            sum(step.status is AutomaticSelectionPlanStepStatus.PLANNED for step in bundle.steps),
            sum(
                step.status is AutomaticSelectionPlanStepStatus.QUEUED
                and step.executed_queue_status
                not in {
                    QueueStatus.PLAYED,
                    QueueStatus.SKIPPED,
                    QueueStatus.FAILED,
                    QueueStatus.REMOVED,
                }
                for step in bundle.steps
            ),
            played,
            terminal,
            self._reason(bundle.plan.invalid_reason_code),
            views,
        )

    def activate(self, plan_id: str, revision: int) -> AutomaticSelectionPlanBundle:
        return self._plans.activate_draft_plan(
            plan_id, revision, self._planning.current_configuration_digest()
        )

    def resume(self, plan_id: str, revision: int) -> AutomaticSelectionPlanRecoveryResult:
        return self._recovery.resume_plan(plan_id, revision)

    def recalculate(
        self, plan_id: str, revision: int, previous_track_id: int | None = None
    ) -> AutomaticSelectionPlanRecoveryResult:
        bundle = self._plans.get(plan_id)
        if bundle is None:
            return AutomaticSelectionPlanRecoveryResult(
                AutomaticSelectionPlanRecoveryResultCode.PERSISTENCE_FAILED
            )
        state = self._recovery.inspect(bundle.plan.session_id).state
        if state is not None:
            revision = state.plan_revision
            previous_track_id = state.actual_predecessor_track_id
        return self._recovery.recalculate_plan(plan_id, revision, previous_track_id)

    def discard(self, plan_id: str, revision: int) -> AutomaticSelectionPlanRecoveryResult:
        return self._recovery.discard_plan(plan_id, revision)

    def adopt_preview(
        self,
        preview: SelectionPreview,
        *,
        replace_plan_id: str | None = None,
        replace_revision: int | None = None,
    ) -> PreviewAdoptionResult:
        with self._lock:
            basis = preview.planning_basis
            if basis is None or not preview.steps or preview.achieved_depth != len(preview.steps):
                return PreviewAdoptionResult(PreviewAdoptionCode.INVALID)
            if tuple(step.position for step in preview.steps) != tuple(
                range(1, len(preview.steps) + 1)
            ):
                return PreviewAdoptionResult(PreviewAdoptionCode.INVALID)
            previous, fingerprint = self._automatic_selection.current_history_basis()
            if (
                basis.configuration_digest != self._planning.current_configuration_digest()
                or basis.previous_track_id != previous
                or basis.history_fingerprint != fingerprint
                or basis.rationale_schema_version != 3
            ):
                return PreviewAdoptionResult(PreviewAdoptionCode.STALE)
            if len(self._tracks.get_many([step.track_id for step in preview.steps])) != len(
                {step.track_id for step in preview.steps}
            ):
                return PreviewAdoptionResult(PreviewAdoptionCode.STALE)
            conflict = self._plans.get_resumable_for_session(basis.session_id)
            if conflict is not None and conflict.plan.plan_id != replace_plan_id:
                return PreviewAdoptionResult(PreviewAdoptionCode.CONFLICT, conflict)
            try:
                bundle = self._preview_bundle(preview)
                if replace_plan_id is not None:
                    if replace_revision is None:
                        return PreviewAdoptionResult(PreviewAdoptionCode.INVALID)
                    persisted = self._plans.create_replacing_resumable(
                        bundle,
                        replace_plan_id,
                        replace_revision,
                        basis.configuration_digest,
                    )
                    return PreviewAdoptionResult(PreviewAdoptionCode.REPLACED, persisted)
                persisted = self._plans.create(bundle)
                return PreviewAdoptionResult(PreviewAdoptionCode.CREATED, persisted)
            except AutomaticSelectionPlanConflictError:
                return PreviewAdoptionResult(PreviewAdoptionCode.CONFLICT)
            except Exception:
                return PreviewAdoptionResult(PreviewAdoptionCode.FAILED)

    @staticmethod
    def _preview_bundle(preview: SelectionPreview) -> AutomaticSelectionPlanBundle:
        basis = preview.planning_basis
        assert basis is not None
        now = datetime.now(UTC)
        plan_id = str(uuid4())
        plan = AutomaticSelectionPlan(
            plan_id,
            basis.session_id,
            AutomaticSelectionPlanStatus.DRAFT,
            now,
            now,
            None,
            PLANNING_CONFIGURATION_VERSION,
            basis.configuration_digest,
            basis.rationale_schema_version,
            "PREVIEW_ADOPTION",
            len(preview.steps),
        )
        steps: list[AutomaticSelectionPlanStep] = []
        previous = basis.previous_track_id
        for item in preview.steps:
            selected = next(
                (
                    candidate
                    for candidate in item.rationale.evaluated_candidates
                    if candidate.decision_category is CandidateDecisionCategory.SELECTED
                ),
                None,
            )
            steps.append(
                AutomaticSelectionPlanStep(
                    str(uuid4()),
                    plan_id,
                    item.position,
                    item.track_id,
                    AutomaticSelectionPlanStepStatus.PLANNED,
                    now,
                    previous,
                    item.relaxation_stage,
                    item.rationale.primary_play_count,
                    item.rationale.secondary_score or 0.0,
                    max(1, item.rationale.final_tie_candidate_count),
                    (
                        "SEEDED_RANDOM"
                        if item.rationale.final_tie_candidate_count > 1
                        else "SOLE_FINALIST"
                    ),
                    (
                        selected.decision_reason_code
                        if selected is not None
                        else item.rationale.decision_reason_code
                    ),
                )
            )
            previous = item.track_id
        return AutomaticSelectionPlanBundle(plan, tuple(steps))

    @classmethod
    def _step_view(
        cls, step: AutomaticSelectionPlanStep, tracks: Mapping[int, Track]
    ) -> PlanStepView:
        track = tracks.get(step.track_id)
        previous = tracks.get(step.previous_track_id) if step.previous_track_id else None
        title = getattr(track, "title", "Titel nicht mehr im Katalog")
        artist = getattr(track, "artist", "")
        previous_title = (
            f"{getattr(previous, 'artist', '')} – {getattr(previous, 'title', '')}".strip(" –")
            if previous is not None
            else "Kein Vorgängertitel"
        )
        return PlanStepView(
            step.position,
            title,
            artist,
            (
                "Ungültig"
                if step.status is AutomaticSelectionPlanStepStatus.INVALIDATED
                else (
                    "In Queue"
                    if step.status is AutomaticSelectionPlanStepStatus.QUEUED
                    else "Geplant"
                )
            ),
            cls._execution_text(step),
            step.primary_play_count,
            step.secondary_score,
            _SELECTION_REASONS.get(
                step.selection_reason_code, "Weitere technische Prüfung erforderlich"
            ),
            previous_title,
            step.relaxation_stage_code,
            "Zufallsentscheidung" if step.tie_candidate_count > 1 else "Eindeutig",
        )

    @staticmethod
    def _execution_text(step: AutomaticSelectionPlanStep) -> str:
        if step.executed_queue_status is None:
            return "Noch nicht eingereiht"
        return {
            QueueStatus.PLAYED: "Gespielt",
            QueueStatus.SKIPPED: "Übersprungen",
            QueueStatus.FAILED: "Fehlgeschlagen",
            QueueStatus.REMOVED: "Entfernt",
            QueueStatus.WAITING: "Wartet in Queue",
            QueueStatus.PREPARING: "Wird vorbereitet",
            QueueStatus.READY: "Bereit",
            QueueStatus.PLAYING: "Wird gespielt",
        }.get(step.executed_queue_status, "Weitere technische Prüfung erforderlich")

    @staticmethod
    def _reason(code: str | None) -> str:
        return "" if code is None else _REASONS.get(code, "Weitere technische Prüfung erforderlich")
