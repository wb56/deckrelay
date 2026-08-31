from party_player.presentation import (
    GlobalStatusState,
    LayoutPolicy,
    LogicalClientSize,
    PresentationPreference,
    ResolvedPresentation,
    Workspace,
    presentation_preference,
    force_live_for_operational_update,
    workspace,
    global_status_text,
    logical_client_size,
)


def resolve(
    width: int,
    height: int,
    preference: PresentationPreference = PresentationPreference.AUTO,
    current: ResolvedPresentation = ResolvedPresentation.COMPACT,
):
    return LayoutPolicy().resolve(LogicalClientSize(width, height), preference, current)


def test_auto_selects_large_above_all_large_boundaries() -> None:
    decision = resolve(1600, 950)
    assert decision.resolved is ResolvedPresentation.LARGE
    assert decision.reason == "auto-large-fits"


def test_auto_selects_compact_below_width_or_height_boundary() -> None:
    assert resolve(1300, 950).reason == "auto-large-width-insufficient"
    assert resolve(1600, 800).reason == "auto-large-height-insufficient"


def test_auto_hysteresis_is_stable_inside_boundary_band() -> None:
    policy = LayoutPolicy()
    size = LogicalClientSize(1420, 850)
    from_large = policy.resolve(size, PresentationPreference.AUTO, ResolvedPresentation.LARGE)
    from_compact = policy.resolve(size, PresentationPreference.AUTO, ResolvedPresentation.COMPACT)
    assert from_large.resolved is ResolvedPresentation.LARGE
    assert from_compact.resolved is ResolvedPresentation.COMPACT
    assert not from_large.changed
    assert not from_compact.changed


def test_manual_compact_is_always_resolved_compact() -> None:
    assert (
        resolve(2000, 1200, PresentationPreference.COMPACT).resolved is ResolvedPresentation.COMPACT
    )


def test_manual_large_falls_back_without_changing_preference() -> None:
    too_small = resolve(1200, 700, PresentationPreference.LARGE)
    large_again = resolve(1600, 950, PresentationPreference.LARGE)
    assert too_small.resolved is ResolvedPresentation.COMPACT
    assert too_small.reason == "manual-large-does-not-fit"
    assert large_again.resolved is ResolvedPresentation.LARGE
    assert large_again.reason == "manual-large-fits"


def test_identical_input_reports_no_change() -> None:
    decision = resolve(1600, 950, current=ResolvedPresentation.LARGE)
    assert not decision.changed


def test_invalid_persisted_values_use_safe_defaults() -> None:
    assert presentation_preference(None) is PresentationPreference.AUTO
    assert presentation_preference("broken") is PresentationPreference.AUTO
    assert workspace(None) is Workspace.LIVE
    assert workspace("broken") is Workspace.LIVE


def test_global_status_is_rendered_from_explicit_state() -> None:
    state = GlobalStatusState(
        source="Playlist",
        deck_a="ON AIR",
        deck_b="BEREIT",
        automatic="Automatik aktiv",
        warning="Audioausgabe prüfen",
    )
    assert global_status_text(state) == (
        "A ON AIR · B BEREIT · Quelle Playlist · Automatik aktiv · Übergang 50%"
        " · ⚠ Audioausgabe prüfen"
    )


def test_operational_updates_force_live_only_during_startup() -> None:
    assert force_live_for_operational_update(startup_guard=True, active=True)
    assert not force_live_for_operational_update(startup_guard=False, active=True)
    assert not force_live_for_operational_update(startup_guard=True, active=False)


def test_native_client_pixels_are_converted_to_logical_units_once() -> None:
    assert logical_client_size(1920, 1050, 1.0) == LogicalClientSize(1920, 1050)
    assert logical_client_size(1920, 1050, 1.25) == LogicalClientSize(1536, 840)
