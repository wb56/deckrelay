import logging
from threading import get_ident
from tkinter import TclError

from party_player.presentation import LogicalClientSize
from party_player.ui.window_geometry_ui_coordinator import WindowGeometryUiCoordinator
from party_player.window_geometry import (
    DisplaySnapshot,
    MonitorGeometry,
    Rect,
    StoredWindowGeometry,
    WindowInsets,
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


class FakeDisplayProvider:
    def __init__(self, *snapshots: DisplaySnapshot | OSError) -> None:
        self.snapshots = list(snapshots)
        self.handles: list[int] = []

    def snapshot(self, window_handle: int) -> DisplaySnapshot:
        self.handles.append(window_handle)
        value = self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if isinstance(value, OSError):
            raise value
        return value


class FakeWindow:
    def __init__(self, geometry: str = "1200x800+100+50") -> None:
        self.geometry_value = geometry
        self.minimum = (0, 0)
        self.fullscreen = False
        self.thread_id = get_ident()
        self.scheduled: dict[str, tuple[int, object]] = {}
        self.cancelled: list[str] = []
        self.next_after_id = 0

    def _assert_gui_thread(self) -> None:
        assert get_ident() == self.thread_id

    def winfo_id(self) -> int:
        self._assert_gui_thread()
        return 42

    def winfo_width(self) -> int:
        self._assert_gui_thread()
        return int(self.geometry_value.split("x", 1)[0])

    def winfo_height(self) -> int:
        self._assert_gui_thread()
        return int(self.geometry_value.split("x", 1)[1].split("+", 1)[0])

    def winfo_screenwidth(self) -> int:
        self._assert_gui_thread()
        return 1920

    def winfo_screenheight(self) -> int:
        self._assert_gui_thread()
        return 1080

    def _get_window_scaling(self) -> float:
        self._assert_gui_thread()
        return 1.25

    def geometry(self, value: str | None = None) -> str:
        self._assert_gui_thread()
        if value is not None:
            self.geometry_value = value
        return self.geometry_value

    def minsize(self, width: int, height: int) -> None:
        self._assert_gui_thread()
        self.minimum = (width, height)

    def attributes(self, name: str) -> bool:
        self._assert_gui_thread()
        assert name == "-fullscreen"
        return self.fullscreen

    def after_cancel(self, after_id: str) -> None:
        self._assert_gui_thread()
        self.cancelled.append(after_id)
        self.scheduled.pop(after_id, None)

    def schedule(self, delay_ms: int, callback: object) -> str:
        self._assert_gui_thread()
        self.next_after_id += 1
        after_id = f"after-{self.next_after_id}"
        self.scheduled[after_id] = (delay_ms, callback)
        return after_id

    def run(self, after_id: str) -> None:
        _delay, callback = self.scheduled.pop(after_id)
        assert callable(callback)
        callback()


def coordinator(
    window: FakeWindow,
    provider: FakeDisplayProvider,
    *,
    saved: list[str] | None = None,
    reevaluated: list[LogicalClientSize] | None = None,
) -> WindowGeometryUiCoordinator:
    saved_values = saved if saved is not None else []
    reevaluated_values = reevaluated if reevaluated is not None else []
    return WindowGeometryUiCoordinator(
        window,
        display_provider=provider,
        save_geometry=saved_values.append,
        schedule=window.schedule,
        discard_scheduled=lambda after_id: None,
        reevaluate_presentation=reevaluated_values.append,
        logger=logging.getLogger("test.window_geometry"),
    )


def test_start_applies_saved_geometry_and_minimum_size() -> None:
    window = FakeWindow()
    value = coordinator(window, FakeDisplayProvider(snapshot()))
    stored = StoredWindowGeometry(1400, 900, 100, 50, 1.0).serialize()

    value.apply_initial_geometry(stored)

    assert window.geometry_value == "1400x900+100+50"
    assert window.minimum == (1180, 800)


def test_monitor_and_dpi_change_repositions_and_reevaluates_presentation() -> None:
    initial = snapshot()
    changed = snapshot(
        Rect(0, 0, 1366, 728),
        dpi=1.25,
        bounds=Rect(0, 0, 1366, 768),
    )
    window = FakeWindow("1400x900+100+50")
    reevaluated: list[LogicalClientSize] = []
    value = coordinator(
        window,
        FakeDisplayProvider(initial, changed),
        reevaluated=reevaluated,
    )
    value.apply_initial_geometry(None)

    value.poll_display_environment()

    assert window.geometry_value == "1080x551+0+0"
    assert reevaluated == [LogicalClientSize(864, 441)]
    assert any(delay == 2000 for delay, _callback in window.scheduled.values())


def test_unchanged_display_fingerprint_only_schedules_next_poll() -> None:
    current = snapshot()
    window = FakeWindow()
    reevaluated: list[LogicalClientSize] = []
    value = coordinator(
        window,
        FakeDisplayProvider(current),
        reevaluated=reevaluated,
    )
    value.apply_initial_geometry(None)
    geometry = window.geometry_value

    value.poll_display_environment()

    assert window.geometry_value == geometry
    assert reevaluated == []
    assert list(delay for delay, _callback in window.scheduled.values()) == [2000]


def test_fullscreen_blocks_correction_and_persistence() -> None:
    window = FakeWindow("900x600+5000+4000")
    window.fullscreen = True
    saved: list[str] = []
    value = coordinator(window, FakeDisplayProvider(snapshot()), saved=saved)

    value.ensure_window_in_work_area("configure")
    value.persist()

    assert window.geometry_value == "900x600+5000+4000"
    assert saved == []


def test_off_screen_window_is_corrected_to_available_work_area() -> None:
    window = FakeWindow("900x600+5000+4000")
    value = coordinator(window, FakeDisplayProvider(snapshot()))

    value.ensure_window_in_work_area("configure")

    assert window.geometry_value == "900x600+1004+401"
    assert window.minimum == (900, 600)


def test_save_is_debounced_for_400_ms_and_previous_callback_is_cancelled() -> None:
    window = FakeWindow()
    value = coordinator(window, FakeDisplayProvider(snapshot()))

    value.schedule_save()
    first = next(iter(window.scheduled))
    value.schedule_save()

    assert window.cancelled == [first]
    assert len(window.scheduled) == 1
    assert next(iter(window.scheduled.values()))[0] == 400


def test_persist_saves_current_geometry_for_window_close() -> None:
    window = FakeWindow("1200x800+100+50")
    saved: list[str] = []
    value = coordinator(window, FakeDisplayProvider(snapshot()), saved=saved)

    value.persist()

    assert saved == [StoredWindowGeometry(1200, 800, 100, 50, 1.0).serialize()]


def test_display_provider_error_is_tolerated_and_poll_continues(caplog) -> None:
    window = FakeWindow()
    value = coordinator(window, FakeDisplayProvider(OSError("display unavailable")))

    with caplog.at_level(logging.ERROR):
        value.poll_display_environment()

    assert "Fensterarbeitsfläche konnte nicht abgefragt werden" in caplog.text
    assert list(delay for delay, _callback in window.scheduled.values()) == [2000]


def test_cancel_tcl_error_does_not_prevent_replacing_debounce_callback() -> None:
    window = FakeWindow()
    value = coordinator(window, FakeDisplayProvider(snapshot()))
    value.schedule_save()

    def reject_cancel(_after_id: str) -> None:
        raise TclError("already completed")

    window.after_cancel = reject_cancel  # type: ignore[method-assign]
    value.schedule_save()

    assert len(window.scheduled) == 2
    assert sorted(delay for delay, _callback in window.scheduled.values()) == [400, 400]


def test_all_window_access_stays_on_calling_gui_thread() -> None:
    window = FakeWindow()
    value = coordinator(window, FakeDisplayProvider(snapshot()))

    value.apply_initial_geometry(None)
    value.ensure_window_in_work_area("configure")
    value.schedule_save()
    value.logical_client_size(1200, 800)

    after_id = next(key for key, (delay, _callback) in window.scheduled.items() if delay == 400)
    window.run(after_id)
