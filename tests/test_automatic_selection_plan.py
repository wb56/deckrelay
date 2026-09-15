from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlan,
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStep,
    AutomaticSelectionPlanStepStatus,
    selection_configuration_digest,
)

PLAN_ID = "00000000-0000-4000-8000-000000000001"
STEP_ID = "00000000-0000-4000-8000-000000000002"


def _bundle() -> AutomaticSelectionPlanBundle:
    now = datetime(2026, 9, 15, tzinfo=UTC)
    plan = AutomaticSelectionPlan(
        PLAN_ID,
        1,
        AutomaticSelectionPlanStatus.DRAFT,
        now,
        now,
        42,
        1,
        selection_configuration_digest({"rules": {"b": 2, "a": 1}}),
        1,
        "AUTOMATIC",
        1,
    )
    step = AutomaticSelectionPlanStep(
        STEP_ID,
        PLAN_ID,
        1,
        7,
        AutomaticSelectionPlanStepStatus.PLANNED,
        now,
        None,
        "STRICT",
        0,
        1.5,
        2,
        "SEEDED_RANDOM",
        "BEST_SCORE",
    )
    return AutomaticSelectionPlanBundle(plan, (step,))


def test_models_are_immutable_and_validate_bundle_invariants() -> None:
    bundle = _bundle()
    with pytest.raises(FrozenInstanceError):
        bundle.plan.revision = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="step count"):
        AutomaticSelectionPlanBundle(bundle.plan, ())
    with pytest.raises(ValueError, match="finite"):
        AutomaticSelectionPlanStep(
            STEP_ID,
            PLAN_ID,
            1,
            7,
            AutomaticSelectionPlanStepStatus.PLANNED,
            bundle.plan.created_at,
            None,
            "STRICT",
            0,
            float("nan"),
            1,
            "SEEDED_RANDOM",
            "BEST_SCORE",
        )


def test_configuration_digest_is_canonical_and_rejects_unsafe_values() -> None:
    assert selection_configuration_digest({"a": 1, "b": [True, 2.0]}) == (
        selection_configuration_digest({"b": [True, 2.0], "a": 1})
    )
    with pytest.raises(ValueError, match="finite"):
        selection_configuration_digest({"weight": float("inf")})
    with pytest.raises(ValueError, match="unsupported"):
        selection_configuration_digest({"secret": object()})
