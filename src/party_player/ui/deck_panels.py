"""Deck presentation components for large and compact layouts."""

from collections.abc import Callable
import logging
import math
import tkinter as tk
from tkinter import filedialog
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.controllers.loudness_controller import LoudnessController
from party_player.enums import DeckState
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


def _deck_loudness_text(deck: Deck) -> str:
    if deck.loaded_track is None:
        return "Gain: —"
    source = LoudnessController.source_text(deck.loudness_source)
    protection = " · Clip-Schutz aktiv" if deck.loudness_peak_limited else ""
    return (
        f"Gain: {deck.loudness_requested_gain_db:+.2f} → "
        f"{deck.loudness_effective_gain_db:+.2f} dB · {source}{protection}"
    )


def _equalizer_source_text(source: str) -> str:
    return {
        "TITLE": "Titel",
        "QUEUE": "aktuelle Queue",
        "PLAYLIST": "Playlist",
        "GENRE": "Genre",
        "GLOBAL": "Standard",
        "PREVIEW": "Vorschau",
        "EDITOR": "Vorschau",
        "DISABLED": "Aus",
        "UNSUPPORTED": "nicht unterstützt",
        "ERROR": "Fehler",
    }.get(source, source.title())


DECK_STATE_TEXT = {
    DeckState.EMPTY: "LEER",
    DeckState.LOADED: "GELADEN",
    DeckState.PLAYING: "WIEDERGABE",
    DeckState.PAUSED: "PAUSIERT",
    DeckState.STOPPED: "GESTOPPT",
    DeckState.FINISHED: "BEENDET",
    DeckState.ERROR: "FEHLER",
}


class DeckPanel(ctk.CTkFrame):  # type: ignore[misc]
    """Large, high-contrast controls for one deck."""

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
        self._seek_callback: Callable[[str, float], None] | None = None
        self._volume_callback: Callable[[str, float], None] | None = None
        self._fade_callback: Callable[[str, bool], None] | None = None
        self._cancel_fade_callback: Callable[[str], None] | None = None
        self._import_callback: Callable[[str, str], None] | None = None
        self._equalizer_callback: Callable[[str], None] | None = None
        self._tooltips: list[Tooltip] = []
        self._updating_controls = False
        self._render_cache: dict[str, object] = {}
        self._performance = performance_monitor or PerformanceMonitor()
        self._logger = logging.getLogger(__name__)
        self._render_operation = f"status_render.deck_{deck_id.lower()}"
        self._accent = accent
        self._last_on_air = False
        self._air_transition_after_id: str | None = None
        self._progress_max = 1.0

        self.grid_columnconfigure(0, weight=1)
        self._header = ctk.CTkLabel(
            self,
            text=f"DECK {deck_id}",
            font=(theme.FONT_FAMILY, 25, "bold"),
            text_color=accent,
        )
        self._header.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        self._air_badge = ctk.CTkLabel(
            self,
            text="● BEREIT",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=theme.TEXT_MUTED,
            fg_color=theme.SURFACE,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            width=72,
        )
        self._air_badge.grid(row=0, column=0, padx=14, pady=(16, 8), sticky="e")
        self._cover = ctk.CTkLabel(
            self,
            text="Kein Cover",
            width=190,
            height=160,
            fg_color="#20242b",
            corner_radius=8,
        )
        self._cover.grid(row=1, column=0, padx=16, pady=8)
        self._file_button = ctk.CTkButton(
            self,
            text="Audiodatei laden",
            height=36,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=accent,
            hover_color=theme.DECK_ACCENT_HOVER[deck_id],
            command=self._choose_file,
        )
        self._file_button.grid(row=2, column=0, padx=16, pady=(2, 8), sticky="ew")
        self._title = ctk.CTkLabel(self, text="Kein Titel geladen", font=("Segoe UI", 18, "bold"))
        self._title.grid(row=3, column=0, padx=16, pady=(8, 2), sticky="ew")
        self._metadata = ctk.CTkLabel(self, text="—", wraplength=270)
        self._metadata.grid(row=4, column=0, padx=16, pady=(0, 8), sticky="ew")
        self._cue_points = ctk.CTkLabel(
            self, text="Cue: —", text_color=theme.TEXT_MUTED, wraplength=300
        )
        self._cue_points.grid(row=5, column=0, padx=16, pady=(0, 5), sticky="ew")
        self._loudness = ctk.CTkLabel(
            self, text="Gain: —", text_color=theme.TEXT_MUTED, wraplength=300
        )
        self._loudness.grid(row=6, column=0, padx=16, pady=(0, 5), sticky="ew")
        equalizer_line = ctk.CTkFrame(self, fg_color="transparent")
        equalizer_line.grid(row=7, column=0, padx=16, pady=(0, 5), sticky="ew")
        equalizer_line.grid_columnconfigure(0, weight=1)
        self._equalizer = ctk.CTkLabel(
            equalizer_line,
            text="EQ: Aus",
            text_color=theme.TEXT_MUTED,
            anchor="w",
        )
        self._equalizer.grid(row=0, column=0, sticky="ew")
        self._equalizer_button = ctk.CTkButton(
            equalizer_line,
            text="Ändern",
            width=62,
            height=26,
            command=self._change_equalizer,
        )
        self._equalizer_button.grid(row=0, column=1, padx=(6, 0))
        self._tooltips.append(
            Tooltip(
                self._equalizer_button,
                "Equalizer-Preset und Wirkungsziel für dieses Deck öffnen",
            )
        )
        self._ducking_status = ctk.CTkLabel(
            equalizer_line,
            text="",
            text_color=theme.WARNING,
            font=(theme.FONT_FAMILY, 11, "bold"),
        )
        self._ducking_status.grid(row=0, column=2, padx=(8, 0))
        self._state = ctk.CTkLabel(self, text="LEER", font=("Segoe UI", 14, "bold"))
        self._state.grid(row=8, column=0, padx=16, pady=4)
        self._time = tk.Label(
            self,
            text="00:00 / 00:00   Rest 00:00",
            bg=theme.SURFACE_RAISED,
            fg=theme.TEXT,
            font=(theme.FONT_FAMILY, 13),
            borderwidth=0,
            highlightthickness=0,
        )
        self._time.grid(row=9, column=0, padx=16, pady=2)
        self._progress = _SeekProgressBar(
            self,
            progress_color=accent,
        )
        self._progress.set(0)
        self._progress.bind("<Button-1>", self._seek_from_pointer)
        self._progress.bind("<B1-Motion>", self._seek_from_pointer)
        self._progress.grid(row=10, column=0, padx=16, pady=8, sticky="ew")

        transport = ctk.CTkFrame(self, fg_color="transparent")
        transport.grid(row=11, column=0, padx=12, pady=6)
        for column, (label, command, description) in enumerate(
            (
                ("▶", "play", "Titel von Anfang an abspielen"),
                ("⏸", "pause", "Wiedergabe pausieren"),
                ("Weiter", "resume", "Pausierte Wiedergabe fortsetzen"),
                ("■", "stop", "Wiedergabe stoppen und zum Anfang springen"),
            )
        ):
            button = ctk.CTkButton(
                transport,
                text=label,
                width=58,
                height=38,
                corner_radius=theme.CONTROL_CORNER_RADIUS,
                fg_color=theme.SURFACE_RAISED,
                hover_color=theme.SURFACE_HOVER,
                border_width=1,
                border_color=theme.BORDER,
                command=lambda name=command: self._action(self.deck_id, name),
            )
            button.grid(row=0, column=column, padx=3)
            self._tooltips.append(Tooltip(button, description))

        fades = ctk.CTkFrame(self, fg_color="transparent")
        fades.grid(row=12, column=0, padx=12, pady=4)
        ctk.CTkButton(
            fades,
            text="Fade in",
            width=72,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=lambda: self._fade(True),
        ).grid(row=0, column=0, padx=4)
        ctk.CTkButton(
            fades,
            text="Fade out",
            width=72,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=lambda: self._fade(False),
        ).grid(row=0, column=1, padx=4)
        ctk.CTkButton(
            fades,
            text="Fade stoppen",
            width=90,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=self._cancel_fade,
        ).grid(row=0, column=2, padx=4)
        ctk.CTkButton(
            fades,
            text="Auswerfen",
            width=72,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=lambda: self._action(self.deck_id, "eject"),
        ).grid(row=0, column=3, padx=4)

        overlay_quick_start = ctk.CTkFrame(
            self,
            fg_color=theme.SURFACE,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            border_width=1,
            border_color=theme.WARNING,
        )
        overlay_quick_start.grid(row=13, column=0, padx=16, pady=(8, 4), sticky="ew")
        overlay_quick_start.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            overlay_quick_start,
            text="JINGLE-SCHNELLSTART · UNABHÄNGIGER KANAL",
            font=(theme.FONT_FAMILY, 10, "bold"),
            text_color=theme.WARNING,
        ).grid(row=0, column=0, padx=8, pady=(5, 2), sticky="w")
        self.overlay_pad_host = ctk.CTkFrame(
            overlay_quick_start,
            fg_color="transparent",
        )
        self.overlay_pad_host.grid(row=1, column=0, padx=5, pady=(2, 6), sticky="ew")
        for column in range(3):
            self.overlay_pad_host.grid_columnconfigure(column, weight=1)

        self._volume_label = ctk.CTkLabel(self, text="Lautstärke: 100 %")
        self._volume_label.grid(row=14, column=0, padx=16, pady=(8, 0))
        self._volume = ctk.CTkSlider(self, from_=0, to=1, command=self._volume_changed)
        self._volume.set(1)
        self._volume.grid(row=15, column=0, padx=16, pady=(3, 16), sticky="ew")
        self._error = ctk.CTkLabel(self, text="", text_color=theme.ERROR, wraplength=270)
        self._error.grid(row=16, column=0, padx=16, pady=(0, 12), sticky="ew")

    def bind_controls(
        self,
        seek: Callable[[str, float], None],
        volume: Callable[[str, float], None],
        fade: Callable[[str, bool], None],
        cancel_fade: Callable[[str], None],
        import_file: Callable[[str, str], None],
        equalizer: Callable[[str], None],
    ) -> None:
        self._seek_callback = seek
        self._volume_callback = volume
        self._fade_callback = fade
        self._cancel_fade_callback = cancel_fade
        self._import_callback = import_file
        self._equalizer_callback = equalizer

    def dispose(self) -> None:
        """Release delayed tooltip callbacks owned by this deck panel."""
        if self._air_transition_after_id is not None:
            try:
                self.after_cancel(self._air_transition_after_id)
            except (ValueError, RuntimeError):
                pass
            self._air_transition_after_id = None
        for tooltip in self._tooltips:
            tooltip.close()
        self._tooltips.clear()

    def show_file_browser(self, enabled: bool) -> None:
        if enabled:
            self._file_button.grid()
        else:
            self._file_button.grid_remove()

    def show_ducking(self, factor: float, phase: str) -> None:
        """Show transient attenuation without changing any deck control."""

        if factor >= 0.999:
            text = ""
        else:
            db = 20.0 * math.log10(max(0.001, factor))
            suffix = {
                "attack": " · senkt ab",
                "release": " · stellt wieder her",
            }.get(phase, "")
            text = f"DUCK {db:.0f} dB{suffix}"
        self._configure_if_changed("ducking_status", self._ducking_status, text=text)

    def render(self, deck: Deck) -> None:
        self._updating_controls = True
        track = deck.loaded_track
        with self._performance.measure(f"{self._render_operation}.text", warning_threshold_ms=10.0):
            if track is None:
                self._configure_if_changed("title", self._title, text="Kein Titel geladen")
                self._configure_if_changed(
                    "metadata",
                    self._metadata,
                    text="Titel aus der Queue wählen oder Audiodatei laden",
                )
            else:
                year = track.original_release_year or track.year
                details = f"{track.artist or 'Unbekannt'}\n{track.album or 'Unbekanntes Album'}"
                if year:
                    details += f" · {year}"
                if track.bpm is not None:
                    details += f" · {track.bpm:g} BPM"
                self._configure_if_changed("title", self._title, text=track.title)
                self._configure_if_changed("metadata", self._metadata, text=details)
        with self._performance.measure(f"{self._render_operation}.cues", warning_threshold_ms=10.0):
            if track is None:
                self._configure_if_changed(
                    "cues", self._cue_points, text="Cue: —", text_color=theme.TEXT_MUTED
                )
            else:
                manual = deck.cue_in_source == "MANUAL" or deck.cue_out_source == "MANUAL"
                fade_start = max(deck.cue_in, deck.cue_out - deck.cue_fade_duration)
                self._configure_if_changed(
                    "cues",
                    self._cue_points,
                    text=(
                        f"Cue: {_time_text(deck.cue_in)} → {_time_text(deck.cue_out)} · "
                        f"Überblendung ab {_time_text(fade_start)}"
                        f"{' · MANUELL' if manual else ''}"
                    ),
                    text_color=theme.SUCCESS if manual else theme.TEXT_MUTED,
                )
        with self._performance.measure(
            f"{self._render_operation}.status", warning_threshold_ms=10.0
        ):
            on_air = "  • ON AIR" if deck.is_on_air else ""
            self._configure_if_changed(
                "panel_air",
                self,
                border_color=theme.ON_AIR if deck.is_on_air else self._accent,
                border_width=2 if deck.is_on_air else 1,
            )
            self._configure_if_changed(
                "air_badge",
                self._air_badge,
                text="● ON AIR" if deck.is_on_air else "● BEREIT",
                text_color=theme.ON_AIR if deck.is_on_air else theme.TEXT_MUTED,
                fg_color="#3a1f25" if deck.is_on_air else theme.SURFACE,
            )
            self._animate_air_transition(deck.is_on_air)
            self._configure_if_changed(
                "loudness",
                self._loudness,
                text=_deck_loudness_text(deck),
                text_color=(theme.WARNING if deck.loudness_peak_limited else theme.TEXT_MUTED),
            )
            self._configure_if_changed(
                "equalizer",
                self._equalizer,
                text=(
                    f"EQ: {deck.equalizer_preset_name} · "
                    f"{_equalizer_source_text(deck.equalizer_source)}"
                ),
                text_color=(theme.WARNING if deck.equalizer_error else theme.TEXT_MUTED),
            )
            self._configure_if_changed(
                "equalizer_button",
                self._equalizer_button,
                state="normal" if deck.loaded_track is not None else "disabled",
            )
            self._configure_if_changed(
                "state",
                self._state,
                text=f"{DECK_STATE_TEXT[deck.state]}{on_air}",
                text_color=theme.ON_AIR if deck.is_on_air else theme.TEXT,
            )
        with self._performance.measure(f"{self._render_operation}.time", warning_threshold_ms=10.0):
            remaining = max(0.0, deck.duration - deck.position)
            self._configure_if_changed(
                "time",
                self._time,
                text=(
                    f"{_time_text(deck.position)} / {_time_text(deck.duration)}   "
                    f"Rest {_time_text(remaining)}"
                ),
            )
        with self._performance.measure(
            f"{self._render_operation}.progress", warning_threshold_ms=15.0
        ):
            progress_max = max(1.0, deck.duration)
            progress_color = (
                theme.ON_AIR
                if deck.is_on_air
                else self._accent if deck.loaded_track is not None else theme.BORDER
            )
            if self._render_cache.get("progress_style") != progress_color:
                self._progress.set_color(progress_color)
                self._render_cache["progress_style"] = progress_color
            self._progress_max = progress_max
            progress = min(deck.position, progress_max)
            progress_bucket = round((progress / progress_max) * 300)
            if self._render_cache.get("progress_bucket") != progress_bucket:
                self._progress.set(progress / progress_max)
                self._render_cache["progress_bucket"] = progress_bucket
        with self._performance.measure(
            f"{self._render_operation}.volume", warning_threshold_ms=10.0
        ):
            if self._render_cache.get("volume") != deck.volume:
                self._volume.set(deck.volume)
                self._render_cache["volume"] = deck.volume
            self._configure_if_changed(
                "volume_text", self._volume_label, text=f"Lautstärke: {deck.volume:.0%}"
            )
        with self._performance.measure(
            f"{self._render_operation}.message", warning_threshold_ms=10.0
        ):
            message = deck.error_message or deck.cue_warning
            self._configure_if_changed(
                "error",
                self._error,
                text=message,
                text_color=theme.ERROR if deck.error_message else theme.WARNING,
            )
        self._updating_controls = False

    def _animate_air_transition(self, on_air: bool) -> None:
        """Pulse the border once when a deck becomes audible."""
        if on_air == self._last_on_air:
            return
        self._last_on_air = on_air
        if self._air_transition_after_id is not None:
            self.after_cancel(self._air_transition_after_id)
            self._air_transition_after_id = None
        if not on_air:
            return
        self.configure(border_color=theme.ON_AIR, border_width=3)

        def settle() -> None:
            self._air_transition_after_id = None
            if self._last_on_air:
                self.configure(border_color=theme.ON_AIR, border_width=2)

        self._air_transition_after_id = self.after(140, settle)

    def _configure_if_changed(self, key: str, widget: Any, **values: object) -> None:
        signature = tuple(sorted(values.items()))
        if self._render_cache.get(key) == signature:
            return
        widget.configure(**values)
        self._render_cache[key] = signature

    def _seek(self, value: float) -> None:
        if not self._updating_controls and self._seek_callback is not None:
            self._seek_callback(self.deck_id, float(value))

    def _seek_from_pointer(self, event: Any) -> None:
        """Seek using the lightweight progress bar without an expensive slider redraw."""
        width = max(1, int(self._progress.winfo_width()))
        ratio = min(1.0, max(0.0, float(event.x) / width))
        self._seek(ratio * self._progress_max)

    def _volume_changed(self, value: float) -> None:
        self._volume_label.configure(text=f"Lautstärke: {float(value):.0%}")
        if not self._updating_controls and self._volume_callback is not None:
            self._volume_callback(self.deck_id, float(value))

    def _change_equalizer(self) -> None:
        if self._equalizer_callback is not None:
            self._equalizer_callback(self.deck_id)

    def _fade(self, fade_in: bool) -> None:
        if self._fade_callback is not None:
            self._fade_callback(self.deck_id, fade_in)

    def _cancel_fade(self) -> None:
        if self._cancel_fade_callback is not None:
            self._cancel_fade_callback(self.deck_id)

    def _choose_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title=f"Audiodatei für Deck {self.deck_id} wählen",
            filetypes=(
                ("MP3 und FLAC", "*.mp3 *.flac"),
                ("MP3", "*.mp3"),
                ("FLAC", "*.flac"),
            ),
        )
        if file_path and self._import_callback is not None:
            self._import_callback(file_path, self.deck_id)


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
