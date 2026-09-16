from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from party_player.automatic_selection_plan import (
    AutomaticSelectionPlan,
    AutomaticSelectionPlanBundle,
    AutomaticSelectionPlanRecoveryAction,
    AutomaticSelectionPlanRecoveryReason,
    AutomaticSelectionPlanRecoveryState,
    AutomaticSelectionPlanStatus,
    AutomaticSelectionPlanStep,
    AutomaticSelectionPlanStepStatus,
)
from party_player.automatic_selection_plan_recovery import (
    AutomaticSelectionPlanRecoveryResultCode,
    AutomaticSelectionPlanRecoveryService,
)
from party_player.models import Track
from party_player.track_selection import SelectionDecision
from party_player.ui.automatic_selection_plan_recovery_dialog import (
    AutomaticSelectionPlanRecoveryDialog,
)

PLAN_ID = "10000000-0000-4000-8000-000000000001"
STEP_ID = "20000000-0000-4000-8000-000000000001"


def _bundle(*, revision: int = 2) -> AutomaticSelectionPlanBundle:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    return AutomaticSelectionPlanBundle(
        AutomaticSelectionPlan(
            PLAN_ID,
            7,
            AutomaticSelectionPlanStatus.PAUSED,
            now,
            now,
            4,
            1,
            "a" * 64,
            1,
            "AUTOMATIC",
            1,
            revision,
        ),
        (
            AutomaticSelectionPlanStep(
                STEP_ID,
                PLAN_ID,
                1,
                1,
                AutomaticSelectionPlanStepStatus.PLANNED,
                now,
                None,
                "STRICT",
                0,
                0.0,
                1,
                "SOLE_FINALIST",
                "BEST_SCORE",
            ),
        ),
    )


def _state(*, configuration_matches: bool = True, interrupted: bool = False):
    return AutomaticSelectionPlanRecoveryState(
        PLAN_ID,
        7,
        AutomaticSelectionPlanStatus.PAUSED,
        2,
        1,
        0,
        STEP_ID if interrupted else None,
        1 if interrupted else None,
        1 if interrupted else None,
        configuration_matches,
        (
            AutomaticSelectionPlanRecoveryAction.RESUME,
            AutomaticSelectionPlanRecoveryAction.RECALCULATE,
            AutomaticSelectionPlanRecoveryAction.DISCARD,
        ),
        (
            AutomaticSelectionPlanRecoveryReason.INTERRUPTED_PLAYBACK
            if interrupted
            else AutomaticSelectionPlanRecoveryReason.RECOVERY_REQUIRED
        ),
    )


class _Plans:
    def __init__(self, state=None) -> None:
        self.bundle = _bundle()
        self.state = state
        self.calls: list[str] = []

    def recovery_state(self, session_id, digest):
        assert session_id == 7 and digest == "a" * 64
        return self.state

    def get(self, plan_id):
        return self.bundle if plan_id == PLAN_ID else None

    def resume_recovered_plan(self, plan_id, revision):
        self.calls.append("resume")
        self.bundle = AutomaticSelectionPlanBundle(
            replace(self.bundle.plan, status=AutomaticSelectionPlanStatus.ACTIVE, revision=3),
            self.bundle.steps,
        )
        return self.bundle

    def discard_recovered_plan(self, plan_id, revision):
        self.calls.append("discard")
        return self.bundle


class _Planning:
    def current_configuration_digest(self):
        return "a" * 64


class _Tracks:
    def get_active(self, track_id):
        return Track(track_id, "one.mp3", "One", "Artist", "", 180.0)


class _Rules:
    def all_relaxable_reason_codes(self):
        return frozenset({"RECENT_TRACK"})

    def evaluate(self, entry, track, *, relaxed_codes=frozenset()):
        assert relaxed_codes == frozenset({"RECENT_TRACK"})
        return SelectionDecision.allow()


class _Availability(_Rules):
    def evaluate(self, track):
        return SelectionDecision.allow()


def _service(plans):
    return AutomaticSelectionPlanRecoveryService(
        plans, _Planning(), _Tracks(), _Rules(), _Availability()
    )


def test_snapshot_is_immutable_and_inspection_requires_explicit_decision() -> None:
    state = _state()
    with pytest.raises(FrozenInstanceError):
        state.plan_revision = 9  # type: ignore[misc]
    result = _service(_Plans(state)).inspect(7)
    assert result.code is AutomaticSelectionPlanRecoveryResultCode.RECOVERY_REQUIRED
    assert result.state is state
    assert "unverändert fortsetzen" in AutomaticSelectionPlanRecoveryDialog._reason_text(state)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            _state(configuration_matches=False),
            AutomaticSelectionPlanRecoveryResultCode.RECALCULATION_REQUIRED,
        ),
        (_state(interrupted=True), AutomaticSelectionPlanRecoveryResultCode.RECALCULATION_REQUIRED),
    ],
)
def test_resume_rejects_changed_or_interrupted_context(state, expected) -> None:
    plans = _Plans(state)
    result = _service(plans).resume_plan(PLAN_ID, 2)
    assert result.code is expected
    assert plans.calls == []


def test_resume_and_discard_are_explicit_single_operations() -> None:
    plans = _Plans(_state())
    resumed = _service(plans).resume_plan(PLAN_ID, 2)
    assert resumed.code is AutomaticSelectionPlanRecoveryResultCode.RESUMED
    plans.bundle = _bundle(revision=2)
    discarded = _service(plans).discard_plan(PLAN_ID, 2)
    assert discarded.code is AutomaticSelectionPlanRecoveryResultCode.DISCARDED
    assert plans.calls == ["resume", "discard"]


def test_dialog_waits_for_worker_result_before_closing() -> None:
    button = SimpleNamespace(configure=lambda **values: setattr(button, "state", values["state"]))
    label = SimpleNamespace(configure=lambda **values: setattr(label, "text", values["text"]))
    dialog = SimpleNamespace(
        _handled=False,
        _action_buttons=[button],
        _decision_status=label,
        destroyed=False,
        grab_released=False,
        destroy=lambda: setattr(dialog, "destroyed", True),
        grab_release=lambda: setattr(dialog, "grab_released", True),
    )
    called: list[bool] = []

    AutomaticSelectionPlanRecoveryDialog._choose(dialog, lambda: called.append(True))

    assert called == [True]
    assert button.state == "disabled"
    assert not dialog.destroyed
    AutomaticSelectionPlanRecoveryDialog.finish_decision(dialog, success=True)
    assert dialog.destroyed and dialog.grab_released
