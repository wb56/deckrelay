import json

import pytest

from party_player.window_geometry import (
    DisplaySnapshot,
    MonitorGeometry,
    Rect,
    StoredWindowGeometry,
    WindowInsets,
    parse_stored_geometry,
    resolve_child_window_geometry,
    resolve_window_geometry,
)


def snapshot(
    work: Rect = Rect(0, 0, 1920, 1040),
    *,
    dpi: float = 1.0,
    bounds: Rect | None = None,
) -> DisplaySnapshot:
    return DisplaySnapshot(
        (MonitorGeometry(bounds or Rect(0, 0, 1920, 1080), work, dpi, True),),
        WindowInsets(8, 31, 8, 8),
    )


def stored(width: int, height: int, x: int, y: int, dpi: float = 1.0) -> str:
    return StoredWindowGeometry(width, height, x, y, dpi).serialize()


def test_valid_saved_geometry_is_unchanged() -> None:
    result = resolve_window_geometry(stored(1400, 900, 100, 50), snapshot())
    assert (result.width, result.height, result.x, result.y) == (1400, 900, 100, 50)
    assert result.reasons == ()


def test_geometry_larger_than_work_area_is_limited() -> None:
    result = resolve_window_geometry(stored(2500, 1600, 0, 0), snapshot())
    assert (result.width, result.height) == (1904, 1001)
    assert result.minimum_height == 800
    assert "width_limited_to_work_area" in result.reasons
    assert "height_limited_to_work_area" in result.reasons


@pytest.mark.parametrize(
    "value",
    [
        '{"width":-1,"height":500,"x":0,"y":0,"dpi_scale":1}',
        '{"width":500,"height":0,"x":0,"y":0,"dpi_scale":1}',
        '{"width":500,"height":400,"x":0,"y":0,"dpi_scale":"hoch"}',
        "keine Geometrie",
    ],
)
def test_invalid_geometry_is_discarded(value: str) -> None:
    assert parse_stored_geometry(value) is None
    result = resolve_window_geometry(value, snapshot())
    assert "stored_geometry_invalid" in result.reasons
    assert "stored_geometry_discarded" in result.reasons


def test_position_outside_all_monitors_returns_to_primary() -> None:
    result = resolve_window_geometry(stored(900, 600, 5000, 4000), snapshot())
    assert result.x >= 0 and result.y >= 0
    assert "position_outside_available_monitors" in result.reasons


def test_removed_monitor_returns_window_to_remaining_primary() -> None:
    result = resolve_window_geometry(stored(1000, 700, -1800, 40), snapshot())
    assert result.monitor_index == 0
    assert result.x >= 0
    assert "position_outside_available_monitors" in result.reasons


def test_very_small_work_area_produces_reachable_size_and_adaptive_minimum() -> None:
    result = resolve_window_geometry(None, snapshot(Rect(0, 0, 640, 440)))
    assert (result.width, result.height) == (624, 401)
    assert (result.minimum_width, result.minimum_height) == (624, 401)
    assert result.x == 0 and result.y == 0


def test_large_work_area_keeps_preferred_size() -> None:
    result = resolve_window_geometry(None, snapshot(Rect(0, 0, 2560, 1400)))
    assert (result.width, result.height) == (1500, 950)
    assert (result.minimum_width, result.minimum_height) == (1180, 800)


def test_missing_geometry_uses_centered_safe_default() -> None:
    result = resolve_window_geometry(None, snapshot())
    assert result.reasons == ("stored_geometry_missing",)
    assert result.x > 0 and result.y > 0


def test_malformed_json_is_never_partially_applied() -> None:
    result = resolve_window_geometry(json.dumps({"width": 800}), snapshot())
    assert (result.width, result.height) == (1500, 950)
    assert "stored_geometry_discarded" in result.reasons


def test_valid_geometry_on_negative_coordinate_monitor_is_preserved() -> None:
    displays = DisplaySnapshot(
        (
            MonitorGeometry(Rect(-1920, 0, 0, 1080), Rect(-1920, 0, 0, 1040), 1.0),
            MonitorGeometry(Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 1.0, True),
        ),
        WindowInsets(8, 31, 8, 8),
    )
    result = resolve_window_geometry(stored(1200, 800, -1800, 50), displays)
    assert (result.x, result.y, result.monitor_index) == (-1800, 50, 0)
    assert result.reasons == ()


def test_dpi_converts_physical_work_area_to_logical_window_size_once() -> None:
    result = resolve_window_geometry(
        None,
        snapshot(Rect(0, 0, 1366, 728), dpi=1.25, bounds=Rect(0, 0, 1366, 768)),
    )
    assert (result.width, result.height) == (1080, 551)
    assert result.dpi_scale == 1.25


def test_empty_monitor_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="Arbeitsfläche"):
        resolve_window_geometry(None, DisplaySnapshot(()))


def test_child_dialog_uses_parent_monitor_and_is_clamped_to_its_work_area() -> None:
    displays = DisplaySnapshot(
        (
            MonitorGeometry(Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 1.0, True),
            MonitorGeometry(Rect(1920, 0, 3286, 768), Rect(1920, 0, 3286, 728), 1.25),
        ),
        WindowInsets(8, 31, 8, 8),
    )
    parent = StoredWindowGeometry(900, 600, 2050, 40, 1.25)

    result = resolve_child_window_geometry(
        parent,
        displays,
        preferred_size=(820, 900),
        standard_minimum=(600, 420),
    )

    assert result.monitor_index == 1
    assert result.dpi_scale == 1.25
    assert result.height == 551
    assert 1920 <= result.x < 3286
    assert result.y == 0
