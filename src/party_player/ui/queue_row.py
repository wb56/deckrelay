"""Reusable row view for the paged party queue."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
import tkinter as tk
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.models import QueueEntry, Track
from party_player.enums import QueueSource
from party_player.gui_callback import measured_gui_callback
from party_player.gui_heartbeat_watchdog import GuiCallbackState
from party_player.performance_monitor import PerformanceMonitor
from party_player.ui import theme
from party_player.ui.catalog_row import track_version_text
from party_player.ui.tooltip import SharedTooltipManager, SharedTooltipTarget, Tooltip


@dataclass(frozen=True, slots=True)
class QueueEntryViewModel:
    """Immutable queue entry, catalog metadata and inherited Cue marker."""

    entry: QueueEntry
    track: Track | None
    inherits_manual_cues: bool = False
    selected: bool = False
    request_count: int = 0
    restored: bool = False
    cue_warning: str = ""


STATUS_BADGES = {
    "waiting": "○ WARTET",
    "preparing": "◌ WIRD VORBEREITET",
    "ready": "◆ BEREIT",
    "playing": "● ON AIR",
    "played": "✓ GESPIELT",
    "skipped": "↷ ÜBERSPRUNGEN",
    "failed": "! FEHLER",
    "removed": "× ENTFERNT",
}


def _status_badge(status: str) -> str:
    return STATUS_BADGES.get(status, status.upper())


def _priority_suffix(entry: QueueEntry) -> str:
    """Describe the playlist origin instead of exposing its internal default value."""
    source = QueueSource.normalize(entry.source)
    if source == QueueSource.PLAYLIST and entry.priority == source.default_priority:
        return " · CD/Playlist"
    return f" · Priorität {entry.priority}" if entry.priority else ""


def _row_colors(*, selected: bool, status: str, locked: bool = False) -> tuple[str, str]:
    if selected:
        return theme.SURFACE_HOVER, theme.DECK_ACCENTS["A"]
    if status == "playing":
        return "#4c2229", theme.ON_AIR
    if status == "failed":
        return "#3a2025", theme.ERROR
    if status == "preparing":
        return theme.SURFACE_RAISED, theme.WARNING
    if status == "ready":
        return theme.SURFACE_RAISED, theme.READY
    if status == "played":
        return theme.SURFACE, theme.SUCCESS
    if locked:
        return "#40391f", theme.WARNING
    return theme.SURFACE, theme.BORDER


class QueueRowView:
    """Own one stable widget tree and bind changing queue entries to it."""

    ACTIONS = ("cue", "deck_a", "deck_b", "more")

    def __init__(
        self,
        parent: Any,
        callbacks: dict[str, Callable[[int], None]],
        performance: PerformanceMonitor,
        callback_state: GuiCallbackState | None = None,
        tooltip_manager: SharedTooltipManager | None = None,
    ) -> None:
        self._callbacks = callbacks
        self._performance = performance
        self._callback_state = callback_state
        self._entry_id: int | None = None
        self._last_values: tuple[object, ...] | None = None
        self._fields: dict[str, object] = {}
        self._visible = False
        self._frame = ctk.CTkFrame(
            parent,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            border_width=1,
            border_color=theme.BORDER,
        )
        self._title = ctk.CTkLabel(self._frame, text="", anchor="w")
        self._title.pack(side="left", fill="x", expand=True, padx=6)
        self._display_base = ""
        self._display_without_requests = ""
        self._display_metadata_suffix = ""
        self._configured_widget_count = 0
        self._disposed = False
        self._active_menu: tk.Menu | None = None
        specs = {
            "cue": ("✎", "Titel-Cues bearbeiten"),
            "deck_a": ("A", "Titel in Deck A laden"),
            "deck_b": ("B", "Titel in Deck B laden"),
            "more": ("⋯", "Weitere Queue-Aktionen"),
        }
        self._buttons: dict[str, Any] = {}
        self._tooltips: dict[str, Tooltip | SharedTooltipTarget] = {}
        for action in self.ACTIONS:
            text, tooltip = specs[action]
            button = ctk.CTkButton(
                self._frame,
                text=text,
                width=theme.ICON_BUTTON_SIZE,
                height=30,
                corner_radius=theme.CONTROL_CORNER_RADIUS,
                fg_color=theme.SURFACE_RAISED,
                hover_color=theme.SURFACE_HOVER,
                border_width=1,
                border_color=theme.BORDER,
                command=(
                    self._show_more_actions
                    if action == "more"
                    else measured_gui_callback(
                        self._performance,
                        f"command.queue.{action}",
                        partial(self._invoke_current, action),
                        callback_state=self._callback_state,
                    )
                ),
            )
            button.pack(side="left", padx=1, pady=3)
            self._buttons[action] = button
            self._tooltips[action] = (
                tooltip_manager.register(button, tooltip)
                if tooltip_manager is not None
                else Tooltip(button, tooltip)
            )
        if self._callbacks.get("select") is not None:
            for widget in (self._frame, self._title):
                bind = getattr(widget, "bind", None)
                if bind is not None:
                    bind("<Button-1>", self._select_current)

    @property
    def entry_id(self) -> int | None:
        """Return the currently bound queue-entry identifier."""
        return self._entry_id

    @property
    def focus_root(self) -> Any:
        """Return only this row's widget subtree for deferred focus setup."""
        return self._frame

    @property
    def configured_widget_count(self) -> int:
        """Return the exact number of widget ``configure`` calls issued by this row."""
        return self._configured_widget_count

    def bind_entry(self, view_model: QueueEntryViewModel | None) -> bool:
        """Update changed values while retaining the row's widgets and tooltips.

        Passing ``None`` hides the row. Rebinding never creates a new tooltip and
        cancels any delayed tooltip callback belonging to the previous entry.
        """
        if view_model is None:
            changed = self._entry_id is not None
            self._entry_id = None
            self._last_values = None
            self._fields.clear()
            if self._visible:
                self._frame.pack_forget()
                self._visible = False
            for tooltip in self._tooltips.values():
                tooltip.cancel()
            return changed
        entry = view_model.entry
        track = view_model.track
        name = f"{track.artist} — {track.title}" if track else f"Titel #{entry.track_id}"
        if track is not None:
            name = f"{name} · {track_version_text(track)}"
        duration_text = _format_duration(track.duration_seconds if track else None)
        request_suffix = (
            f" · {view_model.request_count} Wünsche" if view_model.request_count > 0 else ""
        )
        priority_suffix = _priority_suffix(entry)
        locked_suffix = " · Gesperrt" if entry.locked else ""
        restored_suffix = " · ↻ Wiederhergestellt" if view_model.restored else ""
        cue_warning_suffix = f" · ⚠ Cue: {view_model.cue_warning}" if view_model.cue_warning else ""
        skip_suffix = f" · Grund: {entry.skip_reason}" if entry.skip_reason else ""
        display_without_requests = (
            f"{entry.position}. {name}{f' · {duration_text}' if duration_text else ''}"
        )
        display_base = (
            f"{display_without_requests}{request_suffix}{priority_suffix}"
            f"{locked_suffix}{restored_suffix}{cue_warning_suffix}{skip_suffix}"
        )
        values = (
            entry.queue_id,
            entry.position,
            name,
            entry.status.value,
            entry.has_cue_overrides,
            view_model.inherits_manual_cues,
            entry.skip_reason,
            entry.skip_code,
            view_model.selected,
            view_model.request_count,
            entry.source,
            entry.priority,
            entry.locked,
            view_model.restored,
            view_model.cue_warning,
        )
        if values == self._last_values:
            self._performance.record("queue_row_noop_bind_total", 1.0, 100.0)
            return False
        self._entry_id = entry.queue_id
        self._last_values = values
        fields = {
            "position": entry.position,
            "title": track.title if track else f"Titel #{entry.track_id}",
            "artist": track.artist if track else "",
            "duration": track.duration_seconds if track else None,
            "state": entry.status.value,
            "style": entry.status.value == "playing",
            "selected": view_model.selected,
            "request_count": view_model.request_count,
            "source": entry.source,
            "priority": entry.priority,
            "locked": entry.locked,
            "restored": view_model.restored,
            "cue_warning": view_model.cue_warning,
            "skip_reason": entry.skip_reason,
            "skip_code": entry.skip_code,
            "tooltip": (entry.has_cue_overrides, view_model.inherits_manual_cues),
        }
        changed_fields = {
            field for field, value in fields.items() if self._fields.get(field) != value
        }
        self._performance.record("queue_field_update_requested_total", float(len(fields)), 100.0)
        self._performance.record(
            "queue_field_update_executed_total", float(len(changed_fields)), 100.0
        )
        for field in changed_fields:
            self._performance.record(f"gui.queue_row.{field}_update", 0.0, 10.0)

        if changed_fields & {
            "position",
            "title",
            "artist",
            "duration",
            "state",
            "request_count",
            "source",
            "priority",
            "locked",
            "restored",
            "cue_warning",
            "skip_reason",
            "skip_code",
        }:
            self._display_without_requests = display_without_requests
            self._display_metadata_suffix = (
                f"{priority_suffix}{locked_suffix}{restored_suffix}"
                f"{cue_warning_suffix}{skip_suffix}"
            )
            self._display_base = display_base
            self._title.configure(text=f"{display_base}  ·  {_status_badge(entry.status.value)}")
            self._configured_widget_count += 1
        if changed_fields & {"style", "selected", "locked"}:
            row_color, border_color = _row_colors(
                selected=view_model.selected,
                status=entry.status.value,
                locked=entry.locked,
            )
            self._frame.configure(
                fg_color=row_color,
                border_color=border_color,
            )
            self._configured_widget_count += 1
        if "state" in changed_fields:
            self._configure_cue_visibility(entry.status.value)
            self._configure_deck_actions(entry.status.value)
        if "tooltip" in changed_fields:
            self._configure_cue_marker(view_model)
        self._fields = fields
        self._performance.record("gui.queue_render.configured_widget_count", 1.0, 50.0)
        if not self._visible:
            self._frame.pack(fill="x", pady=2)
            self._visible = True
        return True

    def update_status(self, status: object) -> bool:
        """Update status text, cue action and styling on this row only."""
        value = getattr(status, "value", status)
        if not isinstance(value, str) or self._fields.get("state") == value:
            return False
        self._fields["state"] = value
        self._fields["style"] = value == "playing"
        self._title.configure(text=f"{self._display_base}  ·  {_status_badge(value)}")
        self._configured_widget_count += 1
        selected = bool(self._fields.get("selected"))
        row_color, border_color = _row_colors(
            selected=selected,
            status=value,
            locked=bool(self._fields.get("locked")),
        )
        self._frame.configure(
            fg_color=row_color,
            border_color=border_color,
        )
        self._configured_widget_count += 1
        self._configure_cue_visibility(value)
        self._configure_deck_actions(value)
        self._last_values = None
        return True

    def update_request_count(self, request_count: int) -> bool:
        """Update only the request badge text for this row."""
        normalized = max(0, request_count)
        if self._fields.get("request_count") == normalized:
            return False
        self._fields["request_count"] = normalized
        suffix = f" · {normalized} Wünsche" if normalized else ""
        self._display_base = (
            f"{self._display_without_requests}{suffix}{self._display_metadata_suffix}"
        )
        state = str(self._fields.get("state", ""))
        self._title.configure(text=f"{self._display_base}  ·  {_status_badge(state)}")
        self._configured_widget_count += 1
        self._last_values = None
        return True

    def update_selection(self, selected: bool) -> bool:
        """Update the selection styling without rebuilding the row."""
        if self._fields.get("selected") == selected:
            return False
        self._fields["selected"] = selected
        status = str(self._fields.get("state", ""))
        row_color, border_color = _row_colors(
            selected=selected,
            status=status,
            locked=bool(self._fields.get("locked")),
        )
        self._frame.configure(
            fg_color=row_color,
            border_color=border_color,
        )
        self._configured_widget_count += 1
        self._last_values = None
        return True

    def _invoke_current(self, action: str) -> None:
        queue_id = self._entry_id
        if queue_id is not None and (
            action not in {"deck_a", "deck_b"} or self._fields.get("state") == "waiting"
        ):
            self._callbacks[action](queue_id)

    def _select_current(self, _event: object) -> None:
        callback = self._callbacks.get("select")
        queue_id = self._entry_id
        if callback is not None and queue_id is not None:
            callback(queue_id)

    def _configure_cue_visibility(self, status: str) -> None:
        if status in {"waiting", "preparing", "ready", "playing"}:
            self._buttons["cue"].pack(side="left", padx=1, pady=3)
        else:
            self._buttons["cue"].pack_forget()

    def _configure_deck_actions(self, status: str) -> None:
        state = "normal" if status == "waiting" else "disabled"
        self._buttons["deck_a"].configure(state=state)
        self._buttons["deck_b"].configure(state=state)
        self._configured_widget_count += 2

    def _configure_cue_marker(self, model: QueueEntryViewModel) -> None:
        entry = model.entry
        cue = self._buttons["cue"]
        tooltip = self._tooltips["cue"]
        with self._performance.measure(
            "gui.queue_render.tooltip_update", warning_threshold_ms=10.0
        ):
            if entry.has_cue_overrides:
                cue.configure(text="C●", fg_color="#2f7d4f", hover_color="#3d9962")
                self._configured_widget_count += 1
                tooltip.set_text("Eigene Veranstaltungswerte – zum Bearbeiten öffnen")
            elif model.inherits_manual_cues:
                cue.configure(text="C↳", fg_color="#315f86", hover_color="#3d769f")
                self._configured_widget_count += 1
                tooltip.set_text(
                    "Geerbte manuelle Katalog-Cues – keine eigenen Veranstaltungswerte"
                )
            else:
                cue.configure(
                    text="C",
                    fg_color=("#3B8ED0", "#1F6AA5"),
                    hover_color=("#36719F", "#144870"),
                )
                self._configured_widget_count += 1
                tooltip.set_text("Aktuelle Titelwerte werden verwendet – zum Bearbeiten öffnen")

    def _show_more_actions(self) -> None:
        """Create status-dependent uncommon actions without permanent extra buttons."""
        if self._entry_id is None or not self._fields:
            return
        queue_id = self._entry_id
        status = str(self._fields.get("state", ""))
        locked = bool(self._fields.get("locked"))
        skip_code = self._fields.get("skip_code")
        if self._active_menu is not None:
            self._active_menu.destroy()
        menu = tk.Menu(self._frame, tearoff=False)
        self._active_menu = menu
        freely_waiting = status == "waiting" and not locked
        if freely_waiting:
            menu.add_command(
                label="Eine Position nach oben", command=lambda: self._callbacks["up"](queue_id)
            )
            menu.add_command(
                label="Eine Position nach unten",
                command=lambda: self._callbacks["down"](queue_id),
            )
            menu.add_command(
                label="Als Nächstes in dieser Prioritätsstufe",
                command=lambda: self._callbacks["top"](queue_id),
            )
            menu.add_command(
                label="Ans Ende verschieben",
                command=lambda: self._callbacks["end"](queue_id),
            )
        else:
            menu.add_command(
                label=(
                    "Bearbeitung gesperrt" if locked else "Nur wartende Titel sind verschiebbar"
                ),
                state="disabled",
            )
        menu.add_command(
            label="Priorität setzen …",
            command=lambda: self._callbacks["priority"](queue_id),
        )
        menu.add_command(
            label="Entsperren" if locked else "Sperren",
            command=lambda: self._callbacks["lock"](queue_id),
        )
        menu.add_separator()
        menu.add_command(
            label="Equalizer zuweisen…",
            command=lambda: self._callbacks["equalizer"](queue_id),
        )
        menu.add_command(
            label="Equalizer-Zuweisung entfernen",
            command=lambda: self._callbacks["equalizer_remove"](queue_id),
        )
        menu.add_separator()
        menu.add_command(
            label="Als gespielt markieren",
            command=lambda: self._callbacks["played"](queue_id),
        )
        menu.add_command(
            label="Als übersprungen markieren",
            command=lambda: self._callbacks["skip"](queue_id),
        )
        if status == "played":
            menu.add_command(
                label="Wieder auf wartend setzen",
                command=lambda: self._callbacks["reset"](queue_id),
            )
        if status == "skipped":
            menu.add_command(
                label="Wieder auf wartend setzen",
                command=lambda: self._callbacks["retry"](queue_id),
            )
        if status in {"failed", "error"}:
            menu.add_command(
                label="Erneut versuchen", command=lambda: self._callbacks["retry"](queue_id)
            )
        if status == "skipped" and skip_code in {
            "TRACK_REPETITION",
            "ARTIST_REPETITION",
        }:
            menu.add_command(
                label="Trotzdem abspielen",
                command=lambda: self._callbacks["override_skip"](queue_id),
            )
        if freely_waiting:
            menu.add_separator()
            menu.add_command(
                label="Aus Queue entfernen",
                command=lambda: self._callbacks["remove"](queue_id),
            )
        elif status in {"preparing", "ready"}:
            menu.add_separator()
            menu.add_command(
                label="Vorbereitung abbrechen und nach oben …",
                command=lambda: self._callbacks["move_prepared_up"](queue_id),
            )
            menu.add_command(
                label="Vorbereitung abbrechen und nach unten …",
                command=lambda: self._callbacks["move_prepared_down"](queue_id),
            )
            menu.add_command(
                label="Vorbereitung abbrechen und entfernen …",
                command=lambda: self._callbacks["remove_prepared"](queue_id),
            )
        x, y = self._buttons["more"].winfo_pointerxy()
        menu.tk_popup(x, y)

    def dispose(self) -> None:
        """Permanently close tooltips and destroy this row's widget tree."""
        if self._disposed:
            return
        self._disposed = True
        for tooltip in self._tooltips.values():
            tooltip.close()
        self._tooltips.clear()
        if self._active_menu is not None:
            self._active_menu.destroy()
            self._active_menu = None
        self._frame.destroy()
        self._visible = False
        self._fields.clear()


def _format_duration(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return ""
    total_seconds = max(0, round(duration_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"
