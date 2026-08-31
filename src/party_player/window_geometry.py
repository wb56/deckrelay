"""Pure window placement rules plus a small Windows work-area adapter."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import math
import re
import sys
from typing import Protocol


_TK_GEOMETRY = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")


@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersection_area(self, other: Rect) -> int:
        width = max(0, min(self.right, other.right) - max(self.left, other.left))
        height = max(0, min(self.bottom, other.bottom) - max(self.top, other.top))
        return width * height


@dataclass(frozen=True, slots=True)
class WindowInsets:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    @property
    def horizontal(self) -> int:
        return max(0, self.left) + max(0, self.right)

    @property
    def vertical(self) -> int:
        return max(0, self.top) + max(0, self.bottom)


@dataclass(frozen=True, slots=True)
class MonitorGeometry:
    bounds: Rect
    work_area: Rect
    dpi_scale: float = 1.0
    primary: bool = False


@dataclass(frozen=True, slots=True)
class DisplaySnapshot:
    monitors: tuple[MonitorGeometry, ...]
    insets: WindowInsets = WindowInsets()

    @property
    def primary(self) -> MonitorGeometry:
        return next((monitor for monitor in self.monitors if monitor.primary), self.monitors[0])


@dataclass(frozen=True, slots=True)
class StoredWindowGeometry:
    width: int
    height: int
    x: int
    y: int
    dpi_scale: float

    def serialize(self) -> str:
        return json.dumps(
            {
                "width": self.width,
                "height": self.height,
                "x": self.x,
                "y": self.y,
                "dpi_scale": round(self.dpi_scale, 4),
            },
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class ResolvedWindowGeometry:
    width: int
    height: int
    x: int
    y: int
    minimum_width: int
    minimum_height: int
    dpi_scale: float
    monitor_index: int
    reasons: tuple[str, ...]

    @property
    def tk_geometry(self) -> str:
        return f"{self.width}x{self.height}{self.x:+d}{self.y:+d}"


class DisplayProvider(Protocol):
    def snapshot(self, window_handle: int) -> DisplaySnapshot: ...


def parse_stored_geometry(value: str | None) -> StoredWindowGeometry | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict) or set(decoded) != {
        "width",
        "height",
        "x",
        "y",
        "dpi_scale",
    }:
        return None
    width, height, x, y, dpi_scale = (
        decoded["width"],
        decoded["height"],
        decoded["x"],
        decoded["y"],
        decoded["dpi_scale"],
    )
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or not isinstance(x, int)
        or isinstance(x, bool)
        or not isinstance(y, int)
        or isinstance(y, bool)
        or not isinstance(dpi_scale, (int, float))
        or isinstance(dpi_scale, bool)
        or width <= 0
        or height <= 0
        or not math.isfinite(float(dpi_scale))
        or float(dpi_scale) <= 0
    ):
        return None
    return StoredWindowGeometry(width, height, x, y, float(dpi_scale))


def parse_tk_geometry(value: str, dpi_scale: float) -> StoredWindowGeometry | None:
    match = _TK_GEOMETRY.fullmatch(value.strip())
    if match is None or not math.isfinite(dpi_scale) or dpi_scale <= 0:
        return None
    width, height, x, y = (int(part) for part in match.groups())
    if width <= 0 or height <= 0:
        return None
    return StoredWindowGeometry(width, height, x, y, dpi_scale)


def _physical_window_rect(geometry: StoredWindowGeometry, insets: WindowInsets) -> Rect:
    width = round(geometry.width * geometry.dpi_scale) + insets.horizontal
    height = round(geometry.height * geometry.dpi_scale) + insets.vertical
    return Rect(geometry.x, geometry.y, geometry.x + width, geometry.y + height)


def _monitor_index_for_geometry(
    geometry: StoredWindowGeometry, snapshot: DisplaySnapshot
) -> int | None:
    physical = _physical_window_rect(geometry, snapshot.insets)
    center_x = (physical.left + physical.right) / 2
    center_y = (physical.top + physical.bottom) / 2
    for index, monitor in enumerate(snapshot.monitors):
        if monitor.bounds.contains(center_x, center_y):
            return index
    intersections = [monitor.bounds.intersection_area(physical) for monitor in snapshot.monitors]
    best = max(intersections, default=0)
    return intersections.index(best) if best > 0 else None


def resolve_window_geometry(
    stored_value: str | None,
    snapshot: DisplaySnapshot,
    *,
    preferred_size: tuple[int, int] = (1500, 950),
    standard_minimum: tuple[int, int] = (1180, 800),
) -> ResolvedWindowGeometry:
    if not snapshot.monitors:
        raise ValueError("Mindestens eine Arbeitsfläche ist erforderlich")
    reasons: list[str] = []
    stored = parse_stored_geometry(stored_value)
    if stored_value and stored is None:
        reasons.append("stored_geometry_invalid")
    if stored is None:
        reasons.append(
            "stored_geometry_missing" if not stored_value else "stored_geometry_discarded"
        )

    monitor_index = _monitor_index_for_geometry(stored, snapshot) if stored is not None else None
    if monitor_index is None:
        monitor_index = snapshot.monitors.index(snapshot.primary)
        if stored is not None:
            reasons.append("position_outside_available_monitors")
    monitor = snapshot.monitors[monitor_index]
    scale = monitor.dpi_scale if math.isfinite(monitor.dpi_scale) and monitor.dpi_scale > 0 else 1.0
    work = monitor.work_area
    maximum_width = max(1, math.floor((work.width - snapshot.insets.horizontal) / scale))
    maximum_height = max(1, math.floor((work.height - snapshot.insets.vertical) / scale))
    requested_width, requested_height = (
        (stored.width, stored.height) if stored is not None else preferred_size
    )
    width = min(requested_width, maximum_width)
    height = min(requested_height, maximum_height)
    if width != requested_width:
        reasons.append("width_limited_to_work_area")
    if height != requested_height:
        reasons.append("height_limited_to_work_area")

    outer_width = round(width * scale) + snapshot.insets.horizontal
    outer_height = round(height * scale) + snapshot.insets.vertical
    if stored is None:
        x = work.left + max(0, (work.width - outer_width) // 2)
        y = work.top + max(0, (work.height - outer_height) // 2)
    else:
        x, y = stored.x, stored.y
    clamped_x = min(max(x, work.left), max(work.left, work.right - outer_width))
    clamped_y = min(max(y, work.top), max(work.top, work.bottom - outer_height))
    if clamped_x != x or clamped_y != y:
        reasons.append("position_limited_to_work_area")

    return ResolvedWindowGeometry(
        width,
        height,
        clamped_x,
        clamped_y,
        min(standard_minimum[0], width),
        min(standard_minimum[1], height),
        scale,
        monitor_index,
        tuple(dict.fromkeys(reasons)),
    )


def resolve_child_window_geometry(
    parent: StoredWindowGeometry,
    snapshot: DisplaySnapshot,
    *,
    preferred_size: tuple[int, int],
    standard_minimum: tuple[int, int],
) -> ResolvedWindowGeometry:
    """Center a child on its parent's monitor and clamp it to that work area."""
    parent_index = _monitor_index_for_geometry(parent, snapshot)
    if parent_index is None:
        parent_index = snapshot.monitors.index(snapshot.primary)
    monitor = snapshot.monitors[parent_index]
    scale = monitor.dpi_scale if math.isfinite(monitor.dpi_scale) and monitor.dpi_scale > 0 else 1.0
    width, height = preferred_size
    parent_rect = _physical_window_rect(parent, snapshot.insets)
    outer_width = round(width * scale) + snapshot.insets.horizontal
    outer_height = round(height * scale) + snapshot.insets.vertical
    centered = StoredWindowGeometry(
        width,
        height,
        round((parent_rect.left + parent_rect.right - outer_width) / 2),
        round((parent_rect.top + parent_rect.bottom - outer_height) / 2),
        scale,
    )
    return resolve_window_geometry(
        centered.serialize(),
        snapshot,
        preferred_size=preferred_size,
        standard_minimum=standard_minimum,
    )


class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _WinRect),
        ("rcWork", _WinRect),
        ("dwFlags", wintypes.DWORD),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class WindowsDisplayProvider:
    """Read physical monitor/work-area pixels without adding a runtime dependency."""

    def snapshot(self, window_handle: int) -> DisplaySnapshot:
        if not sys.platform.startswith("win"):
            raise OSError("Windows-Arbeitsflächen sind nur unter Windows verfügbar")
        user32 = ctypes.windll.user32
        shcore = ctypes.windll.shcore
        monitors: list[MonitorGeometry] = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(_WinRect), wintypes.LPARAM
        )

        def collect(handle: int, _hdc: int, _rect: object, _data: int) -> bool:
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                return True
            x_dpi, y_dpi = wintypes.UINT(96), wintypes.UINT(96)
            if shcore.GetDpiForMonitor(handle, 0, ctypes.byref(x_dpi), ctypes.byref(y_dpi)) != 0:
                x_dpi, y_dpi = wintypes.UINT(96), wintypes.UINT(96)
            monitors.append(
                MonitorGeometry(
                    Rect(
                        info.rcMonitor.left,
                        info.rcMonitor.top,
                        info.rcMonitor.right,
                        info.rcMonitor.bottom,
                    ),
                    Rect(info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom),
                    (x_dpi.value + y_dpi.value) / 192,
                    bool(info.dwFlags & 1),
                )
            )
            return True

        callback = callback_type(collect)
        if not user32.EnumDisplayMonitors(0, None, callback, 0) or not monitors:
            raise OSError("Windows-Arbeitsflächen konnten nicht ermittelt werden")
        return DisplaySnapshot(tuple(monitors), self._window_insets(window_handle))

    @staticmethod
    def _window_insets(window_handle: int) -> WindowInsets:
        user32 = ctypes.windll.user32
        window = _WinRect()
        client = _WinRect()
        top_left = _Point(0, 0)
        bottom_right = _Point()
        if user32.GetWindowRect(window_handle, ctypes.byref(window)) and user32.GetClientRect(
            window_handle, ctypes.byref(client)
        ):
            bottom_right.x, bottom_right.y = client.right, client.bottom
            if user32.ClientToScreen(
                window_handle, ctypes.byref(top_left)
            ) and user32.ClientToScreen(window_handle, ctypes.byref(bottom_right)):
                measured = WindowInsets(
                    top_left.x - window.left,
                    top_left.y - window.top,
                    window.right - bottom_right.x,
                    window.bottom - bottom_right.y,
                )
                if measured.horizontal or measured.vertical:
                    return measured

        # Before the first map Windows can report equal client/window rectangles.
        # Derive the non-client frame from the native style so startup clamping is safe.
        style = user32.GetWindowLongW(window_handle, -16) or 0x00CF0000  # WS_OVERLAPPEDWINDOW
        extended_style = user32.GetWindowLongW(window_handle, -20)  # GWL_EXSTYLE
        adjusted = _WinRect(0, 0, 100, 100)
        get_dpi = getattr(user32, "GetDpiForWindow", None)
        dpi = int(get_dpi(window_handle)) if get_dpi is not None else 96
        adjust_for_dpi = getattr(user32, "AdjustWindowRectExForDpi", None)
        adjusted_ok = (
            adjust_for_dpi(ctypes.byref(adjusted), style, False, extended_style, max(96, dpi))
            if adjust_for_dpi is not None
            else user32.AdjustWindowRectEx(ctypes.byref(adjusted), style, False, extended_style)
        )
        adjusted_insets = WindowInsets(
            -adjusted.left,
            -adjusted.top,
            adjusted.right - 100,
            adjusted.bottom - 100,
        )
        if adjusted_ok and (adjusted_insets.horizontal or adjusted_insets.vertical):
            return adjusted_insets
        get_metric_for_dpi = getattr(user32, "GetSystemMetricsForDpi", None)

        def metric(index: int) -> int:
            if get_metric_for_dpi is not None:
                return int(get_metric_for_dpi(index, max(96, dpi)))
            return int(user32.GetSystemMetrics(index))

        frame_x = metric(32) + metric(92)  # SM_CXSIZEFRAME + SM_CXPADDEDBORDER
        frame_y = metric(33) + metric(92)  # SM_CYSIZEFRAME + SM_CXPADDEDBORDER
        caption = metric(4)  # SM_CYCAPTION
        return WindowInsets(frame_x, frame_y + caption, frame_x, frame_y)
