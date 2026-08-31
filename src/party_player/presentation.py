"""Pure presentation state and responsive layout decisions for DeckRelay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class PresentationPreference(str, Enum):
    AUTO = "auto"
    LARGE = "large"
    COMPACT = "compact"


class ResolvedPresentation(str, Enum):
    LARGE = "large"
    COMPACT = "compact"


class Workspace(str, Enum):
    LIVE = "live"
    PREPARATION = "preparation"


@dataclass(frozen=True, slots=True)
class GlobalStatusState:
    source: str = "—"
    deck_a: str = "LEER"
    deck_b: str = "LEER"
    automatic: str = "Automatik bereit"
    transition: str = "Übergang 50%"
    warning: str = ""


def global_status_text(state: GlobalStatusState, resolved_note: str = "") -> str:
    warning = f" · ⚠ {state.warning}" if state.warning else ""
    return (
        f"A {state.deck_a} · B {state.deck_b} · Quelle {state.source} · "
        f"{state.automatic} · {state.transition}{warning}{resolved_note}"
    )


@dataclass(frozen=True, slots=True)
class LogicalClientSize:
    width: int
    height: int

    def normalized(self) -> LogicalClientSize:
        return LogicalClientSize(max(0, int(self.width)), max(0, int(self.height)))


def logical_client_size(width: int, height: int, window_scaling: float) -> LogicalClientSize:
    """Convert native Tk pixels to the logical units used by CustomTkinter layouts."""
    scale = max(0.1, float(window_scaling))
    return LogicalClientSize(round(width / scale), round(height / scale)).normalized()


@dataclass(frozen=True, slots=True)
class LayoutThresholds:
    """Logical client requirements; these are not physical display resolutions."""

    large_min_width: int = 1420
    large_min_height: int = 850
    compact_min_width: int = 1000
    compact_min_height: int = 560
    width_hysteresis: int = 48
    height_hysteresis: int = 32


DEFAULT_LAYOUT_THRESHOLDS = LayoutThresholds()


@dataclass(frozen=True, slots=True)
class LayoutCapabilities:
    large_fits: bool
    compact_fits: bool
    large_width_fits: bool
    large_height_fits: bool
    thresholds: LayoutThresholds = DEFAULT_LAYOUT_THRESHOLDS


@dataclass(frozen=True, slots=True)
class LayoutDecision:
    resolved: ResolvedPresentation
    reason: str
    capabilities: LayoutCapabilities
    changed: bool


@dataclass(frozen=True, slots=True)
class PresentationState:
    preference: PresentationPreference = PresentationPreference.AUTO
    resolved: ResolvedPresentation = ResolvedPresentation.LARGE
    workspace: Workspace = Workspace.LIVE
    mixer_expanded: bool = False
    preparation_tools_expanded: bool = False
    pending_mode: ResolvedPresentation | None = None
    pending_reason: str | None = None
    last_reason: str = "startup"
    compact_content_available: bool = False

    def with_decision(self, decision: LayoutDecision) -> PresentationState:
        return replace(
            self,
            resolved=decision.resolved,
            pending_mode=None,
            pending_reason=None,
            last_reason=decision.reason,
        )


class LayoutPolicy:
    """Resolve a stable presentation mode from logical client dimensions."""

    def __init__(self, thresholds: LayoutThresholds = DEFAULT_LAYOUT_THRESHOLDS) -> None:
        self.thresholds = thresholds

    def capabilities(
        self,
        size: LogicalClientSize,
        current: ResolvedPresentation,
    ) -> LayoutCapabilities:
        normalized = size.normalized()
        thresholds = self.thresholds
        if current is ResolvedPresentation.COMPACT:
            large_width = normalized.width >= (
                thresholds.large_min_width + thresholds.width_hysteresis
            )
            large_height = normalized.height >= (
                thresholds.large_min_height + thresholds.height_hysteresis
            )
        else:
            large_width = normalized.width >= (
                thresholds.large_min_width - thresholds.width_hysteresis
            )
            large_height = normalized.height >= (
                thresholds.large_min_height - thresholds.height_hysteresis
            )
        return LayoutCapabilities(
            large_fits=large_width and large_height,
            compact_fits=(
                normalized.width >= thresholds.compact_min_width
                and normalized.height >= thresholds.compact_min_height
            ),
            large_width_fits=large_width,
            large_height_fits=large_height,
            thresholds=thresholds,
        )

    def resolve(
        self,
        size: LogicalClientSize,
        preference: PresentationPreference,
        current: ResolvedPresentation,
    ) -> LayoutDecision:
        capabilities = self.capabilities(size, current)
        if preference is PresentationPreference.COMPACT:
            target = ResolvedPresentation.COMPACT
            reason = "manual-compact"
        elif preference is PresentationPreference.LARGE:
            if capabilities.large_fits:
                target = ResolvedPresentation.LARGE
                reason = "manual-large-fits"
            else:
                target = ResolvedPresentation.COMPACT
                reason = "manual-large-does-not-fit"
        elif capabilities.large_fits:
            target = ResolvedPresentation.LARGE
            reason = "auto-large-fits"
        else:
            target = ResolvedPresentation.COMPACT
            if not capabilities.large_width_fits:
                reason = "auto-large-width-insufficient"
            else:
                reason = "auto-large-height-insufficient"
        return LayoutDecision(target, reason, capabilities, target is not current)


def presentation_preference(value: object) -> PresentationPreference:
    try:
        return PresentationPreference(str(value).strip().casefold())
    except ValueError:
        return PresentationPreference.AUTO


def workspace(value: object) -> Workspace:
    try:
        return Workspace(str(value).strip().casefold())
    except ValueError:
        return Workspace.LIVE


def force_live_for_operational_update(*, startup_guard: bool, active: bool) -> bool:
    """Force Live only while initial operational state is being established."""
    return startup_guard and active
