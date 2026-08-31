from party_player.presentation import (
    LayoutPolicy,
    LogicalClientSize,
    PresentationPreference,
    PresentationState,
    ResolvedPresentation,
    Workspace,
)
from party_player.presentation_coordinator import MainWindowPresentationCoordinator


class QueueScheduler:
    def __init__(self) -> None:
        self.callbacks = []

    def __call__(self, _delay_ms, callback):
        self.callbacks.append(callback)
        return len(self.callbacks)

    def run(self) -> None:
        callback = self.callbacks.pop(0)
        callback()


def coordinator(*, blocked=lambda: False):
    scheduler = QueueScheduler()
    applied = []
    value = MainWindowPresentationCoordinator(
        PresentationState(resolved=ResolvedPresentation.LARGE),
        LayoutPolicy(),
        scheduler,
        lambda state, decision: applied.append((state, decision)),
        blocked,
    )
    return value, scheduler, applied


def test_resize_events_are_coalesced_and_identical_target_is_not_applied() -> None:
    value, scheduler, applied = coordinator()
    value.resize(LogicalClientSize(1600, 950))
    value.resize(LogicalClientSize(1700, 1000))
    assert len(scheduler.callbacks) == 1
    scheduler.run()
    assert applied == []
    assert value.diagnostics().resize_events == 2


def test_work_area_reevaluation_applies_changed_target_once() -> None:
    value, _scheduler, applied = coordinator()
    value.reevaluate(LogicalClientSize(1200, 700), reason="display-change")
    assert len(applied) == 1
    assert applied[0][0].resolved is ResolvedPresentation.COMPACT
    value.reevaluate(LogicalClientSize(1200, 700), reason="dpi-change")
    assert len(applied) == 1


def test_blocked_switch_is_applied_once_after_interaction() -> None:
    active = True
    value, _scheduler, applied = coordinator(blocked=lambda: active)
    value.reevaluate(LogicalClientSize(1200, 700), reason="resize")
    assert value.state.pending_mode is ResolvedPresentation.COMPACT
    assert applied == []
    active = False
    value.interaction_ended()
    value.interaction_ended()
    assert len(applied) == 1
    assert value.state.pending_mode is None


def test_workspace_change_only_updates_presentation_state() -> None:
    value, _scheduler, applied = coordinator()
    value.reevaluate(LogicalClientSize(1600, 950), reason="startup")
    assert value.set_workspace(Workspace.PREPARATION)
    assert value.state.workspace is Workspace.PREPARATION
    assert len(applied) == 1
    assert not value.set_workspace(Workspace.PREPARATION)


def test_manual_large_preference_survives_temporary_compact_resolution() -> None:
    value, _scheduler, _applied = coordinator()
    value.reevaluate(LogicalClientSize(1200, 700), reason="startup")
    value.set_preference(PresentationPreference.LARGE)
    assert value.state.preference is PresentationPreference.LARGE
    assert value.state.resolved is ResolvedPresentation.COMPACT
