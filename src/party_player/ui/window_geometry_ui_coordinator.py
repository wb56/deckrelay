"""Coordinate main-window placement, persistence and display changes."""

from collections.abc import Callable
import logging
import sys
from tkinter import TclError
from typing import Any

from party_player.presentation import LogicalClientSize, logical_client_size
from party_player.window_geometry import (
    DisplayProvider,
    DisplaySnapshot,
    MonitorGeometry,
    Rect,
    ResolvedWindowGeometry,
    StoredWindowGeometry,
    WindowsDisplayProvider,
    parse_tk_geometry,
    resolve_window_geometry,
)


class WindowGeometryUiCoordinator:
    """Own technical main-window geometry state and GUI-thread callbacks."""

    def __init__(
        self,
        window: Any,
        *,
        display_provider: DisplayProvider | None,
        save_geometry: Callable[[str], None] | None,
        schedule: Callable[[int, object], object],
        discard_scheduled: Callable[[object], None],
        reevaluate_presentation: Callable[[LogicalClientSize], None],
        logger: logging.Logger,
    ) -> None:
        self._window = window
        self._display_provider = display_provider
        self._save_geometry = save_geometry
        self._schedule = schedule
        self._discard_scheduled = discard_scheduled
        self._reevaluate_presentation = reevaluate_presentation
        self._logger = logger
        self._display_fingerprint: tuple[object, ...] | None = None
        self._save_after_id: object | None = None

    def display_snapshot(self) -> DisplaySnapshot:
        if self._display_provider is not None:
            return self._display_provider.snapshot(self._window.winfo_id())
        if sys.platform.startswith("win"):
            return WindowsDisplayProvider().snapshot(self._window.winfo_id())
        width = self._window.winfo_screenwidth()
        height = self._window.winfo_screenheight()
        return DisplaySnapshot(
            (MonitorGeometry(Rect(0, 0, width, height), Rect(0, 0, width, height), 1.0, True),)
        )

    def logical_client_size(self, width: int, height: int) -> LogicalClientSize:
        return logical_client_size(width, height, self._window._get_window_scaling())

    @staticmethod
    def snapshot_fingerprint(snapshot: DisplaySnapshot) -> tuple[object, ...]:
        return tuple(
            (
                monitor.bounds,
                monitor.work_area,
                round(monitor.dpi_scale, 4),
                monitor.primary,
            )
            for monitor in snapshot.monitors
        ) + (snapshot.insets,)

    def apply_initial_geometry(self, saved_geometry: str | None) -> None:
        snapshot = self.display_snapshot()
        resolved = resolve_window_geometry(saved_geometry, snapshot)
        self._window.minsize(resolved.minimum_width, resolved.minimum_height)
        self._window.geometry(resolved.tk_geometry)
        self._display_fingerprint = self.snapshot_fingerprint(snapshot)
        self._log_geometry("startup", saved_geometry, snapshot, resolved)

    def current_stored_geometry(self, snapshot: DisplaySnapshot) -> StoredWindowGeometry | None:
        current = parse_tk_geometry(self._window.geometry(), 1.0)
        if current is None:
            return None
        monitor = max(
            snapshot.monitors,
            key=lambda item: item.bounds.intersection_area(
                Rect(
                    current.x,
                    current.y,
                    current.x + round(current.width * item.dpi_scale) + snapshot.insets.horizontal,
                    current.y + round(current.height * item.dpi_scale) + snapshot.insets.vertical,
                )
            ),
        )
        return StoredWindowGeometry(
            current.width,
            current.height,
            current.x,
            current.y,
            monitor.dpi_scale,
        )

    def ensure_window_in_work_area(self, trigger: str) -> None:
        if bool(self._window.attributes("-fullscreen")):
            return
        try:
            snapshot = self.display_snapshot()
        except OSError:
            self._logger.exception("Fensterarbeitsfläche konnte nicht aktualisiert werden")
            return
        current = self.current_stored_geometry(snapshot)
        if current is None:
            return
        resolved = resolve_window_geometry(current.serialize(), snapshot)
        self._display_fingerprint = self.snapshot_fingerprint(snapshot)
        if resolved.reasons:
            self._window.minsize(resolved.minimum_width, resolved.minimum_height)
            self._window.geometry(resolved.tk_geometry)
            self._log_geometry(trigger, current.serialize(), snapshot, resolved)

    def poll_display_environment(self) -> None:
        try:
            snapshot = self.display_snapshot()
        except OSError:
            self._logger.exception("Fensterarbeitsfläche konnte nicht abgefragt werden")
        else:
            fingerprint = self.snapshot_fingerprint(snapshot)
            if fingerprint != self._display_fingerprint:
                self.ensure_window_in_work_area("display_change")
                self._reevaluate_presentation(
                    self.logical_client_size(
                        self._window.winfo_width(), self._window.winfo_height()
                    )
                )
        self._schedule(2000, self.poll_display_environment)

    def schedule_save(self) -> None:
        if self._save_geometry is None:
            return
        pending = self._save_after_id
        if pending is not None:
            try:
                self._window.after_cancel(pending)
            except TclError:
                pass
            self._discard_scheduled(pending)

        def save() -> None:
            self._save_after_id = None
            self.ensure_window_in_work_area("configure")
            self.persist()

        self._save_after_id = self._schedule(400, save)

    def persist(self) -> None:
        if self._save_geometry is None or bool(self._window.attributes("-fullscreen")):
            return
        try:
            snapshot = self.display_snapshot()
            geometry = self.current_stored_geometry(snapshot)
        except OSError:
            return
        if geometry is not None:
            self._save_geometry(geometry.serialize())

    def _log_geometry(
        self,
        trigger: str,
        stored: str | None,
        snapshot: DisplaySnapshot,
        resolved: ResolvedWindowGeometry,
    ) -> None:
        self._logger.info(
            "Fenstergeometrie trigger=%s monitors=%s stored=%s applied=%s reasons=%s",
            trigger,
            [
                {
                    "bounds": (
                        monitor.bounds.left,
                        monitor.bounds.top,
                        monitor.bounds.right,
                        monitor.bounds.bottom,
                    ),
                    "work": (
                        monitor.work_area.left,
                        monitor.work_area.top,
                        monitor.work_area.right,
                        monitor.work_area.bottom,
                    ),
                    "dpi_scale": round(monitor.dpi_scale, 3),
                    "primary": monitor.primary,
                }
                for monitor in snapshot.monitors
            ],
            stored,
            resolved.tk_geometry,
            resolved.reasons or ("unchanged",),
        )
