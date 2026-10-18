import ctypes
from ctypes import wintypes
import json

import pytest

import party_player.window_geometry as window_geometry
from party_player.window_geometry import (
    DisplaySnapshot,
    MonitorGeometry,
    Rect,
    StoredWindowGeometry,
    WindowInsets,
    WindowsDisplayProvider,
    parse_stored_geometry,
    resolve_child_window_geometry,
    resolve_window_geometry,
)


class FakeWindowsFunction:
    def __init__(self, implementation: object) -> None:
        self.implementation = implementation
        self.argtypes: list[object] | None = None
        self.restype: object | None = None

    def __call__(self, *args: object) -> object:
        return self.implementation(*args)  # type: ignore[operator]


class FakeWindowsLibrary:
    pass


def fake_monitor_libraries(
    monitor_handle: int, *, monitor_info_succeeds: bool = True, enumeration_succeeds: bool = True
) -> tuple[FakeWindowsLibrary, FakeWindowsLibrary, list[int]]:
    user32 = FakeWindowsLibrary()
    shcore = FakeWindowsLibrary()
    observed_handles: list[int] = []

    def get_monitor_info(handle: int, info_pointer: object) -> bool:
        argtypes = user32.GetMonitorInfoW.argtypes  # type: ignore[attr-defined]
        if not argtypes:
            raise ctypes.ArgumentError("argument 1: OverflowError: int too long to convert")
        converted_handle = argtypes[0](handle)
        observed_handles.append(converted_handle.value)
        if not monitor_info_succeeds:
            return False
        info = ctypes.cast(info_pointer, ctypes.POINTER(window_geometry._MonitorInfo)).contents
        info.rcMonitor = window_geometry._WinRect(0, 0, 1920, 1080)
        info.rcWork = window_geometry._WinRect(0, 0, 1920, 1040)
        info.dwFlags = 1
        return True

    def enum_display_monitors(_hdc: object, _clip: object, callback: object, _data: object) -> bool:
        if not enumeration_succeeds:
            return False
        rect = window_geometry._WinRect(0, 0, 1920, 1080)
        callback(monitor_handle, None, ctypes.pointer(rect), 0)  # type: ignore[operator]
        return True

    def get_dpi_for_monitor(
        _handle: object, _kind: object, x_dpi_pointer: object, y_dpi_pointer: object
    ) -> int:
        ctypes.cast(x_dpi_pointer, ctypes.POINTER(wintypes.UINT)).contents.value = 96
        ctypes.cast(y_dpi_pointer, ctypes.POINTER(wintypes.UINT)).contents.value = 96
        return 0

    user32.GetMonitorInfoW = FakeWindowsFunction(get_monitor_info)  # type: ignore[attr-defined]
    user32.EnumDisplayMonitors = FakeWindowsFunction(enum_display_monitors)  # type: ignore[attr-defined]
    shcore.GetDpiForMonitor = FakeWindowsFunction(get_dpi_for_monitor)  # type: ignore[attr-defined]
    return user32, shcore, observed_handles


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


def test_windows_snapshot_passes_pointer_width_monitor_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor_handle = 0x1_0000_1234
    user32, shcore, observed_handles = fake_monitor_libraries(monitor_handle)
    monkeypatch.setattr(window_geometry.ctypes.windll, "user32", user32)
    monkeypatch.setattr(window_geometry.ctypes.windll, "shcore", shcore)
    provider = WindowsDisplayProvider()
    monkeypatch.setattr(provider, "_window_insets", lambda _handle: WindowInsets())

    result = provider.snapshot(0x1_0000_5678)

    assert observed_handles == [monitor_handle]
    assert result.monitors == (
        MonitorGeometry(Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 1.0, True),
    )
    assert user32.GetMonitorInfoW.argtypes[0] is wintypes.HMONITOR  # type: ignore[attr-defined]
    assert user32.GetMonitorInfoW.restype is wintypes.BOOL  # type: ignore[attr-defined]


def test_windows_snapshot_rejects_monitor_when_get_monitor_info_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32, shcore, _observed_handles = fake_monitor_libraries(
        0x1_0000_1234, monitor_info_succeeds=False
    )
    monkeypatch.setattr(window_geometry.ctypes.windll, "user32", user32)
    monkeypatch.setattr(window_geometry.ctypes.windll, "shcore", shcore)

    with pytest.raises(OSError, match="Arbeitsflächen"):
        WindowsDisplayProvider().snapshot(1)


def test_windows_snapshot_rejects_failed_monitor_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32, shcore, _observed_handles = fake_monitor_libraries(
        0x1_0000_1234, enumeration_succeeds=False
    )
    monkeypatch.setattr(window_geometry.ctypes.windll, "user32", user32)
    monkeypatch.setattr(window_geometry.ctypes.windll, "shcore", shcore)

    with pytest.raises(OSError, match="Arbeitsflächen"):
        WindowsDisplayProvider().snapshot(1)


def test_window_insets_passes_pointer_width_window_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window_handle = 0x1_0000_5678
    user32 = FakeWindowsLibrary()
    observed_handles: list[int] = []

    def failed_rect_lookup(handle: int, _rect_pointer: object) -> bool:
        argtypes = user32.GetWindowRect.argtypes  # type: ignore[attr-defined]
        assert argtypes
        observed_handles.append(argtypes[0](handle).value)
        return False

    user32.GetWindowRect = FakeWindowsFunction(failed_rect_lookup)  # type: ignore[attr-defined]
    user32.GetClientRect = FakeWindowsFunction(lambda *_args: False)  # type: ignore[attr-defined]
    user32.ClientToScreen = FakeWindowsFunction(lambda *_args: False)  # type: ignore[attr-defined]
    user32.GetWindowLongW = FakeWindowsFunction(lambda *_args: 0)  # type: ignore[attr-defined]
    user32.AdjustWindowRectEx = FakeWindowsFunction(lambda *_args: True)  # type: ignore[attr-defined]
    user32.GetSystemMetrics = FakeWindowsFunction(lambda _index: 0)  # type: ignore[attr-defined]
    monkeypatch.setattr(window_geometry.ctypes.windll, "user32", user32)

    WindowsDisplayProvider._window_insets(window_handle)

    assert observed_handles == [window_handle]
    assert user32.GetWindowRect.argtypes[0] is wintypes.HWND  # type: ignore[attr-defined]
