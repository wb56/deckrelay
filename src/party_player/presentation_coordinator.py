"""Main-thread coordinator for coalesced presentation changes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from party_player.presentation import (
    LayoutDecision,
    LayoutPolicy,
    LogicalClientSize,
    PresentationPreference,
    PresentationState,
    Workspace,
)


class Scheduler(Protocol):
    def __call__(self, delay_ms: int, callback: Callable[[], None]) -> object: ...


@dataclass(frozen=True, slots=True)
class PresentationDiagnostics:
    state: PresentationState
    client_size: LogicalClientSize
    resize_events: int
    evaluations: int
    applied_changes: int


class MainWindowPresentationCoordinator:
    """Coalesce GUI size events without owning widgets or domain state."""

    def __init__(
        self,
        state: PresentationState,
        policy: LayoutPolicy,
        schedule: Scheduler,
        apply: Callable[[PresentationState, LayoutDecision], None],
        interaction_active: Callable[[], bool] = lambda: False,
        *,
        debounce_ms: int = 250,
    ) -> None:
        self.state = state
        self._policy = policy
        self._schedule = schedule
        self._apply = apply
        self._interaction_active = interaction_active
        self._debounce_ms = debounce_ms
        self._pending_callback = False
        self._client_size = LogicalClientSize(0, 0)
        self._resize_events = 0
        self._evaluations = 0
        self._applied_changes = 0

    def resize(self, size: LogicalClientSize, *, reason: str = "resize") -> None:
        self._client_size = size.normalized()
        self._resize_events += 1
        if self._pending_callback:
            return
        self._pending_callback = True
        self._schedule(self._debounce_ms, lambda: self._evaluate(reason))

    def reevaluate(self, size: LogicalClientSize, *, reason: str) -> None:
        self._client_size = size.normalized()
        self._evaluate(reason)

    def set_preference(self, preference: PresentationPreference) -> None:
        self.state = replace(self.state, preference=preference)
        self._evaluate("preference-change")

    def set_workspace(self, selected: Workspace, *, reason: str = "workspace-change") -> bool:
        if selected is self.state.workspace:
            return False
        self.state = replace(self.state, workspace=selected, last_reason=reason)
        decision = self._policy.resolve(
            self._client_size, self.state.preference, self.state.resolved
        )
        self._applied_changes += 1
        self._apply(self.state, replace(decision, changed=False))
        return True

    def interaction_ended(self) -> None:
        if self.state.pending_mode is not None:
            self._evaluate("deferred-interaction-ended")

    def diagnostics(self) -> PresentationDiagnostics:
        return PresentationDiagnostics(
            self.state,
            self._client_size,
            self._resize_events,
            self._evaluations,
            self._applied_changes,
        )

    def _evaluate(self, trigger: str) -> None:
        self._pending_callback = False
        self._evaluations += 1
        decision = self._policy.resolve(
            self._client_size, self.state.preference, self.state.resolved
        )
        if not decision.changed and self.state.pending_mode is None:
            self.state = replace(self.state, last_reason=f"{trigger}:{decision.reason}")
            return
        if decision.changed and self._interaction_active():
            self.state = replace(
                self.state,
                pending_mode=decision.resolved,
                pending_reason=f"{trigger}:{decision.reason}",
                last_reason="switch-deferred",
            )
            return
        new_state = self.state.with_decision(
            replace(decision, reason=f"{trigger}:{decision.reason}")
        )
        self.state = new_state
        self._applied_changes += 1
        self._apply(self.state, replace(decision, reason=self.state.last_reason))
