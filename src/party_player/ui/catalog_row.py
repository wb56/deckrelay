"""Reusable catalog row with compact actions and cached rendering."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.models import Track
from party_player.gui_callback import measured_gui_callback
from party_player.gui_heartbeat_watchdog import GuiCallbackState
from party_player.performance_monitor import PerformanceMonitor
from party_player.ui.tooltip import Tooltip


def track_version_text(track: Track) -> str:
    """Return a compact, file-specific label for otherwise identical entries."""
    path = Path(track.file_path)
    format_name = path.suffix.removeprefix(".").upper() or "Datei"
    filename = path.name
    if len(filename) > 52:
        filename = f"{filename[:33]}…{filename[-18:]}"
    duration = ""
    if track.duration_seconds is not None:
        minutes, seconds = divmod(max(0, round(track.duration_seconds)), 60)
        duration = f" · {minutes}:{seconds:02d}"
    return f"{format_name} · {filename}{duration}"


@dataclass(frozen=True, slots=True)
class CatalogEntryViewModel:
    """Immutable values required to render one catalog row."""

    track: Track
    has_manual_cues: bool = False


class CatalogRowView:
    """Own a stable catalog widget tree and rebind it to different tracks."""

    def __init__(
        self,
        parent: Any,
        callbacks: dict[str, Callable[[Track], None]],
        performance: PerformanceMonitor,
        callback_state: GuiCallbackState | None = None,
    ) -> None:
        self._callbacks = callbacks
        self._performance = performance
        self._callback_state = callback_state
        self._track: Track | None = None
        self._last_values: tuple[object, ...] | None = None
        self._frame = ctk.CTkFrame(parent)
        self._label = ctk.CTkLabel(self._frame, text="", anchor="w", justify="left")
        self._label.pack(side="left", fill="x", expand=True, padx=6)
        self._buttons: dict[str, Any] = {}
        self._tooltips: dict[str, Tooltip] = {}
        for action, text, tooltip_text in (
            ("deck_a", "A", "Titel in Deck A laden"),
            ("deck_b", "B", "Titel in Deck B laden"),
            ("queue", "+", "Titel zur Party-Queue hinzufügen"),
            ("more", "⋯", "Weitere Titelaktionen"),
        ):
            button = ctk.CTkButton(self._frame, text=text, width=34)
            button.pack(side="left", padx=2, pady=3)
            self._buttons[action] = button
            self._tooltips[action] = Tooltip(button, tooltip_text)
        self._tooltips["identity"] = Tooltip(self._label, "")

    @property
    def track_id(self) -> int | None:
        """Return the currently bound track identifier, if the row is visible."""
        return self._track.id if self._track is not None else None

    @property
    def focus_root(self) -> Any:
        """Return only this row's widget subtree for deferred focus setup."""
        return self._frame

    def bind_entry(self, model: CatalogEntryViewModel | None) -> bool:
        """Rebind this stable widget tree and report whether its display changed.

        ``None`` hides the row without destroying widgets or tooltips. An identical
        model performs no ``configure()`` calls.
        """
        if model is None:
            changed = self._track is not None
            self._track = None
            self._last_values = None
            self._frame.pack_forget()
            for tooltip in self._tooltips.values():
                tooltip.cancel()
            return changed
        track = model.track
        version = track_version_text(track)
        values = (track.id, track.artist, track.title, version, model.has_manual_cues)
        if values == self._last_values:
            return False
        self._track = track
        self._last_values = values
        with self._performance.measure(
            "gui.catalog_render.configure_widgets", warning_threshold_ms=10.0
        ):
            self._label.configure(text=f"{track.artist or 'Unbekannt'} — {track.title}\n{version}")
            self._buttons["deck_a"].configure(
                command=measured_gui_callback(
                    self._performance,
                    "command.catalog.deck_a",
                    lambda item=track: self._callbacks["deck_a"](item),
                    callback_state=self._callback_state,
                )
            )
            self._buttons["deck_b"].configure(
                command=measured_gui_callback(
                    self._performance,
                    "command.catalog.deck_b",
                    lambda item=track: self._callbacks["deck_b"](item),
                    callback_state=self._callback_state,
                )
            )
            self._buttons["queue"].configure(
                command=measured_gui_callback(
                    self._performance,
                    "command.catalog.queue",
                    lambda item=track: self._callbacks["queue"](item),
                    callback_state=self._callback_state,
                )
            )
            self._buttons["more"].configure(command=self._show_more_actions)
        with self._performance.measure(
            "gui.catalog_render.tooltip_update", warning_threshold_ms=10.0
        ):
            self._tooltips["identity"].set_text(
                f"Ausgewählte Datei:\n{track.file_path}\n\n"
                "A, B, + und das Aktionsmenü gelten genau für diese Version."
            )
            self._tooltips["more"].set_text(
                "Weitere Titelaktionen"
                + (" – manuelle Cue-Werte vorhanden" if model.has_manual_cues else "")
            )
        self._performance.record("gui.catalog_render.configured_widget_count", 1.0, 50.0)
        self._frame.pack(fill="x", pady=2)
        return True

    def _show_more_actions(self) -> None:
        """Build the short-lived menu for uncommon, potentially destructive actions."""
        if self._track is None:
            return
        with self._performance.measure("command.catalog.more.build", warning_threshold_ms=25.0):
            track = self._track
            menu = tk.Menu(self._frame, tearoff=False)
            cue_label = "Titel bearbeiten"
            if self._last_values is not None and bool(self._last_values[-1]):
                cue_label += " ●"
            menu.add_command(label=cue_label, command=lambda: self._callbacks["cue"](track))
            menu.add_command(
                label="Lautstärke / ReplayGain",
                command=lambda: self._callbacks["loudness"](track),
            )
            menu.add_command(
                label="Titeldetails", command=lambda: self._callbacks["details"](track)
            )
            menu.add_separator()
            menu.add_command(
                label="Equalizer zuweisen…",
                command=lambda: self._callbacks["equalizer"](track),
            )
            menu.add_command(
                label="Equalizer-Zuweisung entfernen",
                command=lambda: self._callbacks["equalizer_remove"](track),
            )
            menu.add_separator()
            menu.add_command(
                label="Aus Katalog entfernen", command=lambda: self._callbacks["remove"](track)
            )
        x, y = self._buttons["more"].winfo_pointerxy()
        menu.tk_popup(x, y)

    def dispose(self) -> None:
        """Permanently release tooltips and widgets when the view itself is rebuilt."""
        for tooltip in self._tooltips.values():
            tooltip.close()
        self._tooltips.clear()
        self._frame.destroy()
