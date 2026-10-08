"""Mixer presentation widgets for the DeckRelay main window."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.ui import theme
from party_player.ui.tooltip import Tooltip


class MixerPanel:
    """Own and render the mixer widget tree without owning domain state."""

    def __init__(
        self,
        master: Any,
        center: Any,
        *,
        on_toggle: Callable[[], None],
        on_overlay_stop: Callable[[], None],
        on_crossfade: Callable[[float], None],
        on_crossfade_step: Callable[[float], str],
        on_crossfade_set: Callable[[float], str],
        on_master: Callable[[float], None],
        on_mute: Callable[[], None],
        on_player_mode: Callable[[str], None],
        on_fade_duration: Callable[[float], None],
        on_fade_stop: Callable[[bool], None],
        on_fullscreen_start: Callable[[bool], None],
        on_automatic_plan: Callable[[], None],
    ) -> None:
        self._render_cache: dict[str, object] = {}
        self._updating = False
        self._on_crossfade = on_crossfade
        self._on_master = on_master
        self._on_fade_duration = on_fade_duration
        self._on_fade_stop = on_fade_stop
        self._on_fullscreen_start = on_fullscreen_start

        self.crossfader_bar = ctk.CTkFrame(center, corner_radius=10, border_width=1)
        self.crossfader_bar.grid(row=3, column=0, padx=12, pady=(4, 8), sticky="ew")
        self.crossfader_bar.grid_columnconfigure(1, weight=1)
        self.deck_status_labels: dict[str, ctk.CTkLabel] = {}
        self.deck_status_labels["A"] = self._deck_status_label(
            self.crossfader_bar, "DECK A\nKeine Titel geladen"
        )
        self.deck_status_labels["A"].grid(row=0, column=0, padx=(12, 8), pady=8)
        fader_frame = ctk.CTkFrame(self.crossfader_bar, fg_color="transparent")
        fader_frame.grid(row=0, column=1, padx=4, pady=6, sticky="ew")
        fader_frame.grid_columnconfigure(0, weight=1)
        self.crossfader_label = ctk.CTkLabel(
            fader_frame, text="Crossfader · 50%", font=("Segoe UI", 14, "bold")
        )
        self.crossfader_label.grid(row=0, column=0, pady=(0, 2))
        self.crossfader = ctk.CTkSlider(fader_frame, from_=0, to=1, command=self._crossfade)
        self.crossfader.grid(row=1, column=0, sticky="ew")
        self.crossfader._canvas.configure(takefocus=True)
        self.crossfader.bind("<Button-1>", lambda _event: self.crossfader.focus_set())
        self.crossfader.bind("<Left>", lambda _event: on_crossfade_step(-0.05))
        self.crossfader.bind("<Right>", lambda _event: on_crossfade_step(0.05))
        self.crossfader.bind("<Home>", lambda _event: on_crossfade_set(0.0))
        self.crossfader.bind("<End>", lambda _event: on_crossfade_set(1.0))
        self.crossfader.bind(
            "<FocusIn>",
            lambda _event: self.crossfader.configure(border_color="#55aaff", border_width=2),
        )
        self.crossfader.bind("<FocusOut>", lambda _event: self.crossfader.configure(border_width=0))
        self.deck_status_labels["B"] = self._deck_status_label(
            self.crossfader_bar, "DECK B\nKeine Titel geladen"
        )
        self.deck_status_labels["B"].grid(row=0, column=2, padx=(8, 12), pady=8)

        self.container = ctk.CTkFrame(master, corner_radius=12)
        self.container.grid(row=2, column=0, columnspan=3, padx=16, pady=(8, 16), sticky="ew")
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_rowconfigure(1, weight=1)
        self.toggle = ctk.CTkButton(
            self.container,
            text="Mixer einblenden ▼",
            height=30,
            fg_color="transparent",
            command=on_toggle,
        )
        self.toggle.grid(row=0, column=0, padx=8, pady=4, sticky="ew")
        self.overlay_stop = ctk.CTkButton(
            self.container,
            text="■ Stop",
            width=84,
            height=30,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=on_overlay_stop,
        )
        self.overlay_stop.grid(row=0, column=1, padx=(0, 8), pady=4)
        self.overlay_stop.grid_remove()
        self.tooltips = (
            Tooltip(
                self.toggle, "Mixer öffnen oder schließen; aktive Jingles bleiben hier sichtbar"
            ),
            Tooltip(
                self.overlay_stop,
                "Aktiven Jingle mit kurzem Sicherheitsfade sofort stoppen",
            ),
        )
        self.body = ctk.CTkScrollableFrame(self.container, fg_color="transparent", corner_radius=0)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1, uniform="mixer_groups")
        self.body.grid_columnconfigure(1, weight=1, uniform="mixer_groups")

        self.status_group = ctk.CTkFrame(self.body, corner_radius=8)
        self.status_group.grid(row=0, column=0, columnspan=2, padx=12, pady=(4, 6), sticky="ew")
        for column in range(4):
            self.status_group.grid_columnconfigure(column, weight=1)
        ctk.CTkLabel(
            self.status_group,
            text="BETRIEBSZUSTAND",
            font=(theme.FONT_FAMILY, 13, "bold"),
        ).grid(row=0, column=0, columnspan=4, padx=12, pady=(10, 4), sticky="w")
        self.mode_status = ctk.CTkLabel(
            self.status_group, text="Betriebsart: HALBAUTOMATISCH", anchor="w"
        )
        self.source_status = ctk.CTkLabel(self.status_group, text="Quelle: —", anchor="w")
        self.queue_status = ctk.CTkLabel(self.status_group, text="Queue: 0 Titel", anchor="w")
        self.automatic_status = ctk.CTkLabel(
            self.status_group, text="Automatik: bereit", anchor="w"
        )
        for column, widget in enumerate(self.status_widgets):
            widget.grid(row=1, column=column, padx=12, pady=(2, 10), sticky="ew")
        self.automatic_plan_status = ctk.CTkLabel(
            self.status_group, text="Fortlaufende Automatik: Wird geladen …", anchor="w"
        )
        self.automatic_plan_status.grid(
            row=2, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="ew"
        )
        self.automatic_plan_button = ctk.CTkButton(
            self.status_group,
            text="Automatik anpassen…",
            width=150,
            command=on_automatic_plan,
        )
        self.automatic_plan_button.grid(row=2, column=3, padx=12, pady=(0, 10), sticky="e")

        self.playback_group = ctk.CTkFrame(self.body, corner_radius=8)
        self.playback_group.grid(row=1, column=0, padx=(12, 6), pady=(4, 6), sticky="nsew")
        self.playback_group.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            self.playback_group,
            text="WIEDERGABE UND AUTOMATIK",
            font=(theme.FONT_FAMILY, 13, "bold"),
        ).grid(row=0, column=0, columnspan=4, padx=12, pady=(10, 6), sticky="w")
        self.master = ctk.CTkSlider(
            self.playback_group, from_=0, to=1, command=self._master_changed
        )
        self.master.grid(row=1, column=0, columnspan=2, padx=(12, 6), pady=5, sticky="ew")
        self.master_label = ctk.CTkLabel(self.playback_group, text="Master 80%", width=82)
        self.master_label.grid(row=1, column=2, padx=4, pady=5)
        self.mute_button = ctk.CTkButton(
            self.playback_group, text="Stumm", width=72, command=on_mute
        )
        self.mute_button.grid(row=1, column=3, padx=(4, 12), pady=5)
        self.player_mode = ctk.CTkSegmentedButton(
            self.playback_group,
            values=["MANUELL", "HALBAUTOMATISCH", "AUTOMATISCH"],
            command=on_player_mode,
        )
        self.player_mode.set("HALBAUTOMATISCH")
        self.player_mode.grid(row=2, column=0, columnspan=4, padx=12, pady=5, sticky="ew")
        ctk.CTkLabel(self.playback_group, text="Fade-Dauer").grid(
            row=3, column=0, padx=(12, 4), pady=5
        )
        self.fade_duration = ctk.CTkSlider(
            self.playback_group,
            from_=1,
            to=30,
            number_of_steps=29,
            command=self._fade_duration_changed,
        )
        self.fade_duration.set(5)
        self.fade_duration.grid(row=3, column=1, padx=4, pady=5, sticky="ew")
        self.fade_duration_label = ctk.CTkLabel(self.playback_group, text="5 s", width=44)
        self.fade_duration_label.grid(row=3, column=2, padx=4, pady=5)
        self.fade_stop_switch = ctk.CTkSwitch(
            self.playback_group,
            text="Nach Fade-out stoppen",
            command=self._fade_stop_changed,
        )
        self.fade_stop_switch.grid(row=4, column=0, columnspan=2, padx=12, pady=(5, 10), sticky="w")
        self.fullscreen_start_switch = ctk.CTkSwitch(
            self.playback_group,
            text="Vollbild beim Start",
            command=self._fullscreen_start_changed,
        )
        self.fullscreen_start_switch.grid(
            row=4, column=2, columnspan=2, padx=(4, 12), pady=(5, 10), sticky="w"
        )

    @staticmethod
    def _deck_status_label(master: Any, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            master,
            text=text,
            width=145,
            font=("Segoe UI", 13, "bold"),
            text_color="#999999",
        )

    @property
    def status_widgets(self) -> tuple[Any, Any, Any, Any]:
        return (self.mode_status, self.source_status, self.queue_status, self.automatic_status)

    def render_visibility(self, *, workspace_is_preparation: bool, expanded: bool) -> None:
        if workspace_is_preparation or expanded:
            self.body.grid()
        else:
            self.body.grid_remove()

    def render_mixer(self, crossfader: float, master: float) -> None:
        self._updating = True
        self.render_crossfader(crossfader)
        master_percent = round(master * 100)
        if self._render_cache.get("master_percent") != master_percent:
            self.master.set(master)
            self.master_label.configure(text=f"Master {master_percent}%")
            self._render_cache["master_percent"] = master_percent
        mute_text = "Ton an" if master == 0 else "Stumm"
        if self._render_cache.get("mute_text") != mute_text:
            self.mute_button.configure(text=mute_text)
            self._render_cache["mute_text"] = mute_text
        self._updating = False

    def render_crossfader(self, crossfader: float) -> int | None:
        percent = round(crossfader * 100)
        if self._render_cache.get("crossfade_percent") == percent:
            return None
        self._updating = True
        self.crossfader.set(crossfader)
        self.crossfader_label.configure(text=f"Crossfader · {percent}%")
        self._render_cache["crossfade_percent"] = percent
        self._updating = False
        return percent

    def render_fade_settings(self, duration: float, stop_after: bool) -> None:
        self.fade_duration.set(duration)
        self.fade_duration_label.configure(text=f"{duration:.0f} s")
        if stop_after:
            self.fade_stop_switch.select()
        else:
            self.fade_stop_switch.deselect()

    def render_player_mode(self, label: str) -> None:
        self.player_mode.set(label)
        self.mode_status.configure(text=f"Betriebsart: {label}")

    def move_crossfader(self, change: float) -> float:
        return self.set_crossfader(self.crossfader.get() + change)

    def set_crossfader(self, value: float) -> float:
        position = max(0.0, min(float(value), 1.0))
        self.crossfader.set(position)
        self.crossfader_label.configure(text=f"Crossfader · {position:.0%}")
        return position

    def emit_crossfade(self, value: float) -> None:
        if not self._updating:
            self._on_crossfade(float(value))

    def emit_master(self, value: float) -> None:
        if not self._updating:
            self._on_master(float(value))

    def _crossfade(self, value: float) -> None:
        self.emit_crossfade(value)

    def _master_changed(self, value: float) -> None:
        self.emit_master(value)

    def _fade_duration_changed(self, value: float) -> None:
        duration = round(float(value))
        self.fade_duration_label.configure(text=f"{duration} s")
        self._on_fade_duration(duration)

    def _fade_stop_changed(self) -> None:
        self._on_fade_stop(bool(self.fade_stop_switch.get()))

    def _fullscreen_start_changed(self) -> None:
        self._on_fullscreen_start(bool(self.fullscreen_start_switch.get()))
