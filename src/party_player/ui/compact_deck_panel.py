"""Compact deck presentation component and shared lightweight helpers."""

from collections.abc import Callable
import tkinter as tk
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.models import Deck
from party_player.performance_monitor import PerformanceMonitor
from party_player.ui import theme
from party_player.ui.compact_deck_presentation import compact_deck_presentation
from party_player.ui.tooltip import Tooltip


def _time_text(seconds: float) -> str:
    value = max(0, round(seconds))
    return f"{value // 60:02d}:{value % 60:02d}"


class _SeekProgressBar(tk.Canvas):
    """Small native canvas bar that avoids CustomTkinter redraw overhead."""

    def __init__(self, parent: Any, *, progress_color: str) -> None:
        super().__init__(
            parent,
            height=14,
            bg=theme.BORDER,
            borderwidth=0,
            highlightthickness=0,
        )
        self._ratio = 0.0
        self._progress_color = progress_color
        self._fill = self.create_rectangle(
            0,
            0,
            0,
            14,
            fill=progress_color,
            outline="",
        )
        self.bind("<Configure>", self._redraw)

    def set(self, ratio: float) -> None:
        self._ratio = min(1.0, max(0.0, ratio))
        self._redraw()

    def set_color(self, color: str) -> None:
        if color == self._progress_color:
            return
        self._progress_color = color
        self.itemconfigure(self._fill, fill=color)

    def _redraw(self, _event: Any = None) -> None:
        self.coords(self._fill, 0, 0, int(self.winfo_width() * self._ratio), self.winfo_height())


class CompactDeckPanel(ctk.CTkFrame):  # type: ignore[misc]
    """Dense second view of an existing deck without owning playback state."""

    def __init__(
        self,
        master: object,
        deck_id: str,
        action: Callable[[str, str], None],
        performance_monitor: PerformanceMonitor | None = None,
    ) -> None:
        accent = theme.DECK_ACCENTS[deck_id]
        super().__init__(
            master,
            corner_radius=theme.PANEL_CORNER_RADIUS,
            fg_color=theme.SURFACE_RAISED,
            border_width=1,
            border_color=accent,
        )
        self.deck_id = deck_id
        self._action = action
        self._accent = accent
        self._performance = performance_monitor or PerformanceMonitor()
        self._initialize_callbacks()
        self._build_header_row()
        self._build_progress_rows()
        self._build_volume_row()

    def _build_header_row(self) -> None:
        self._identity = ctk.CTkLabel(
            self,
            text=f"DECK {self.deck_id}",
            font=(theme.FONT_FAMILY, 18, "bold"),
            text_color=self._accent,
        )
        self._identity.grid(row=0, column=0, padx=(10, 6), pady=(6, 0), sticky="w")
        self._title = ctk.CTkLabel(
            self,
            text="Kein Titel geladen",
            font=(theme.FONT_FAMILY, 14, "bold"),
            anchor="w",
        )
        self._title.grid(row=0, column=1, padx=4, pady=(6, 0), sticky="ew")
        self._state = ctk.CTkLabel(self, text="LEER", text_color=theme.TEXT_MUTED, anchor="e")
        self._state.grid(row=0, column=2, padx=(6, 10), pady=(6, 0), sticky="e")

    def _build_progress_rows(self) -> None:
        self._source = ctk.CTkLabel(self, text="Quelle: —", text_color=theme.TEXT_MUTED, anchor="w")
        self._source.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 1), sticky="ew")
        self._time = ctk.CTkLabel(self, text="00:00 / 00:00 · Rest 00:00", anchor="e")
        self._time.grid(row=1, column=2, padx=10, pady=(0, 1), sticky="e")
        self._progress = _SeekProgressBar(self, progress_color=self._accent)
        self._progress.set(0)
        self._progress.bind("<Button-1>", self._seek_from_pointer)
        self._progress.bind("<B1-Motion>", self._seek_from_pointer)
        self._progress.grid(row=2, column=0, columnspan=3, padx=10, pady=(1, 4), sticky="ew")

    def _build_volume_row(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self._volume_label = ctk.CTkLabel(self, text="Lautstärke 100 %", width=108, anchor="w")
        self._volume_label.grid(row=4, column=0, padx=(10, 2), pady=(39, 6), sticky="w")
        self._volume = ctk.CTkSlider(self, from_=0, to=1, command=self._volume_changed)
        self._volume.set(1)
        self._volume.grid(row=4, column=1, padx=2, pady=(3, 6), sticky="ew")
        self._message = ctk.CTkLabel(self, text="Übergang bereit", anchor="e", width=135)
        self._message.grid(row=4, column=2, padx=(4, 10), pady=(3, 6), sticky="e")

    def _initialize_callbacks(self) -> None:
        self._seek_callback: Callable[[str, float], None] | None = None
        self._volume_callback: Callable[[str, float], None] | None = None
        self._fade_callback: Callable[[str, bool], None] | None = None
        self._cancel_fade_callback: Callable[[str], None] | None = None
        self._updating_controls = False
        self._progress_max = 1.0
        self._render_cache: dict[str, object] = {}
        self._tooltips: list[Tooltip] = []

    def bind_controls(
        self,
        seek: Callable[[str, float], None],
        volume: Callable[[str, float], None],
        fade: Callable[[str, bool], None],
        cancel_fade: Callable[[str], None],
    ) -> None:
        self._seek_callback = seek
        self._volume_callback = volume
        self._fade_callback = fade
        self._cancel_fade_callback = cancel_fade

    def dispose(self) -> None:
        for tooltip in self._tooltips:
            tooltip.close()
        self._tooltips.clear()

    def render(self, deck: Deck) -> None:
        """Render the same Deck instance delivered to the large deck view."""
        self._updating_controls = True
        model = compact_deck_presentation(deck)
        with self._performance.measure(
            f"status_render.compact_deck_{self.deck_id.lower()}",
            warning_threshold_ms=10.0,
        ):
            state_color = theme.TEXT_MUTED
            if model.on_air:
                state_color = theme.ON_AIR
            elif model.error:
                state_color = theme.ERROR
            self._configure_if_changed("title", self._title, text=model.title)
            source = f"Quelle: {model.source}"
            if model.bpm is not None:
                source += f" · {model.bpm:g} BPM"
            self._configure_if_changed("source", self._source, text=source)
            self._configure_if_changed(
                "state", self._state, text=model.state, text_color=state_color
            )
            self._configure_if_changed(
                "time",
                self._time,
                text=(
                    f"{_time_text(model.position)} / {_time_text(model.duration)} · "
                    f"Rest {_time_text(model.remaining)}"
                ),
            )
            self._configure_if_changed(
                "border",
                self,
                border_color=theme.ON_AIR if model.on_air else self._accent,
                border_width=2 if model.on_air else 1,
            )
            progress_max = max(1.0, model.duration)
            self._progress_max = progress_max
            progress_bucket = round(model.progress * 300)
            if self._render_cache.get("progress") != progress_bucket:
                self._progress.set(model.progress)
                self._render_cache["progress"] = progress_bucket
            if self._render_cache.get("volume") != model.volume:
                self._volume.set(model.volume)
                self._render_cache["volume"] = model.volume
            self._configure_if_changed(
                "volume_text", self._volume_label, text=f"Lautstärke {model.volume:.0%}"
            )
            message = model.error or model.warning or "Übergang bereit"
            message_color = theme.TEXT_MUTED
            if model.error:
                message_color = theme.ERROR
            elif model.warning:
                message_color = theme.WARNING
            self._configure_if_changed(
                "message", self._message, text=message, text_color=message_color
            )
        self._updating_controls = False

    def _configure_if_changed(self, key: str, widget: Any, **values: object) -> None:
        signature = tuple(sorted(values.items()))
        if self._render_cache.get(key) != signature:
            widget.configure(**values)
            self._render_cache[key] = signature

    def _seek_from_pointer(self, event: Any) -> None:
        width = max(1, int(self._progress.winfo_width()))
        if self._seek_callback is not None:
            self._seek_callback(
                self.deck_id, min(1.0, max(0.0, event.x / width)) * self._progress_max
            )

    def _volume_changed(self, value: float) -> None:
        self._volume_label.configure(text=f"Lautstärke {float(value):.0%}")
        if not self._updating_controls and self._volume_callback is not None:
            self._volume_callback(self.deck_id, float(value))

    def _fade(self, fade_in: bool) -> None:
        if self._fade_callback is not None:
            self._fade_callback(self.deck_id, fade_in)

    def _cancel_fade(self) -> None:
        if self._cancel_fade_callback is not None:
            self._cancel_fade_callback(self.deck_id)
