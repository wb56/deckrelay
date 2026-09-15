"""Deterministic, state-neutral creation of persisted automatic-selection plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import random
import secrets
from typing import Protocol
from uuid import uuid4

from party_player.automatic_selection import AutomaticSelectionService, SelectionSimulation
from party_player.automatic_selection_plan import (
    AutomaticSelectionPlan,
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStep,
    AutomaticSelectionPlanStepStatus,
    selection_configuration_digest,
)
from party_player.selection_decision import SelectionRationale
from party_player.selection_rule_settings import SelectionScoringSettings
from party_player.track_selection import TrackSelectionService

PLANNING_CONFIGURATION_VERSION = 1
PLANNING_ALGORITHM_ID = "AUTOMATIC_SELECTION_SEQUENCE"
PLANNING_ALGORITHM_VERSION = 1
RATIONALE_SCHEMA_VERSION = 3
SOURCE_CONTEXT_CODE = "AUTOMATIC_PLANNING"
_MAX_SEED = 2**63 - 1


class AutomaticSelectionPlanCompletion(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NO_SAFE_CANDIDATE = "NO_SAFE_CANDIDATE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AutomaticSelectionPlanCreationResult:
    requested_depth: int
    generated_depth: int
    completion_status: AutomaticSelectionPlanCompletion
    plan: AutomaticSelectionPlanBundle | None
    terminal_reason_code: str | None = None


class _PlanWriter(Protocol):
    def create(self, bundle: AutomaticSelectionPlanBundle) -> AutomaticSelectionPlanBundle: ...


class AutomaticSelectionPlanningService:
    """Compute a complete draft in memory and persist it in one final transaction."""

    def __init__(
        self,
        automatic_selection: AutomaticSelectionService,
        rules: TrackSelectionService,
        plans: _PlanWriter,
    ) -> None:
        self._automatic_selection = automatic_selection
        self._rules = rules
        self._plans = plans

    def create_draft_plan(
        self,
        session_id: int,
        depth: int = 5,
        planning_seed: int | None = None,
    ) -> AutomaticSelectionPlanCreationResult:
        if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 0:
            raise ValueError("session_id muss positiv sein")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 10:
            raise ValueError("Planungstiefe muss zwischen 1 und 10 liegen")
        seed = self._validated_seed(planning_seed)
        try:
            simulation = self._automatic_selection.simulate_sequence(
                self._rules,
                depth,
                randomizer=random.Random(seed),
                context_code=SOURCE_CONTEXT_CODE,
            )
            if not simulation.steps:
                return AutomaticSelectionPlanCreationResult(
                    depth,
                    0,
                    AutomaticSelectionPlanCompletion.NO_SAFE_CANDIDATE,
                    None,
                    self._terminal_reason(simulation),
                )
            bundle = self._bundle(session_id, seed, simulation)
            persisted = self._plans.create(bundle)
            completion = (
                AutomaticSelectionPlanCompletion.COMPLETE
                if len(simulation.steps) == depth
                else AutomaticSelectionPlanCompletion.PARTIAL
            )
            return AutomaticSelectionPlanCreationResult(
                depth,
                len(simulation.steps),
                completion,
                persisted,
                self._terminal_reason(simulation),
            )
        except Exception:
            return AutomaticSelectionPlanCreationResult(
                depth,
                0,
                AutomaticSelectionPlanCompletion.FAILED,
                None,
                "PLANNING_TECHNICAL_ERROR",
            )

    @staticmethod
    def _validated_seed(seed: int | None) -> int:
        if seed is None:
            return secrets.randbelow(_MAX_SEED + 1)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= _MAX_SEED:
            raise ValueError("planning_seed muss zwischen 0 und 2^63-1 liegen")
        return seed

    def _bundle(
        self,
        session_id: int,
        seed: int,
        simulation: SelectionSimulation,
    ) -> AutomaticSelectionPlanBundle:
        now = datetime.now(UTC)
        plan_id = str(uuid4())
        configuration = self._configuration(simulation.settings)
        plan = AutomaticSelectionPlan(
            plan_id=plan_id,
            session_id=session_id,
            status=AutomaticSelectionPlanStatus.DRAFT,
            created_at=now,
            updated_at=now,
            planning_seed=seed,
            rule_configuration_version=PLANNING_CONFIGURATION_VERSION,
            rule_configuration_digest=selection_configuration_digest(configuration),
            rationale_schema_version=RATIONALE_SCHEMA_VERSION,
            source_context_code=simulation.context_code,
            planned_depth=len(simulation.steps),
        )
        steps = tuple(
            AutomaticSelectionPlanStep(
                step_id=str(uuid4()),
                plan_id=plan_id,
                position=step.position,
                track_id=step.track.id,
                status=AutomaticSelectionPlanStepStatus.PLANNED,
                planned_at=now,
                previous_track_id=step.previous_track_id,
                relaxation_stage_code=step.rationale.relaxation_stage,
                primary_play_count=step.rationale.primary_play_count,
                secondary_score=step.rationale.secondary_score or 0.0,
                tie_candidate_count=max(1, step.rationale.final_tie_candidate_count),
                tie_break_method_code=self._tie_break_code(step.rationale),
                selection_reason_code=step.rationale.decision_reason_code,
            )
            for step in simulation.steps
        )
        return AutomaticSelectionPlanBundle(plan, steps)

    def _configuration(self, settings: SelectionScoringSettings) -> dict[str, object]:
        soft_rules = tuple(
            {
                "rule_id": setting.rule_id,
                "rule_version": 1,
                "config_version": setting.config_version,
                "enabled": setting.enabled,
                "weight": setting.weight,
            }
            for setting in (
                settings.play_count,
                settings.rating,
                settings.genre_diversity,
                settings.bpm_continuity,
                settings.energy_continuity,
                settings.mood_continuity,
            )
        )
        return {
            "configuration_version": PLANNING_CONFIGURATION_VERSION,
            "algorithm": {
                "id": PLANNING_ALGORITHM_ID,
                "version": PLANNING_ALGORITHM_VERSION,
                "ranking": "PRIMARY_PLAY_COUNT_THEN_SECONDARY_SCORE_THEN_STABLE_ID_THEN_RNG",
            },
            "hard_rules": self._rules.configuration_projection(),
            "automatic_recent_track": {
                "rule_id": "selection.automatic_recent_track",
                "rule_version": 1,
                "recent_track_limit": self._automatic_selection.recent_track_limit,
            },
            "soft_rules": soft_rules,
            "relaxation_stages": (
                {"code": "STRICT", "relaxed_reason_codes": ()},
                {
                    "code": "ARTIST_DISTANCE",
                    "relaxed_reason_codes": ("ARTIST_REPETITION",),
                },
                {
                    "code": "TRACK_DISTANCE",
                    "relaxed_reason_codes": (
                        "ARTIST_REPETITION",
                        "RECENT_TRACK",
                        "TRACK_REPETITION",
                    ),
                },
            ),
            "rationale_schema_version": RATIONALE_SCHEMA_VERSION,
        }

    @staticmethod
    def _tie_break_code(rationale: SelectionRationale) -> str:
        if rationale.final_tie_candidate_count > 1:
            return "SEEDED_RANDOM"
        return "SOLE_FINALIST"

    @staticmethod
    def _terminal_reason(simulation: SelectionSimulation) -> str | None:
        rationale = simulation.completion_rationale
        return rationale.decision_reason_code if rationale is not None else None
