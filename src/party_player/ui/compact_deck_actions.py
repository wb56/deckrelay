"""Construction helpers for the fixed compact deck action row."""

from collections.abc import Callable
from functools import partial
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]

from party_player.ui import theme
from party_player.ui.tooltip import Tooltip


_ACTIONS = (
    ("▶", "play", "Titel abspielen"),
    ("⏸", "pause", "Wiedergabe pausieren"),
    ("Weiter", "resume", "Wiedergabe fortsetzen"),
    ("■ Stop", "stop", "Wiedergabe stoppen"),
    ("Einbl.", "fade_in", "Deck langsam einblenden"),
    ("Ausbl.", "fade_out", "Deck langsam ausblenden"),
    ("Fade ■", "cancel_fade", "Laufenden Fade stoppen"),
    ("Auswerf.", "eject", "Titel auswerfen"),
)


def build_compact_deck_actions(
    panel: Any,
) -> None:
    """Create one bounded row that delegates only to existing deck callbacks."""
    if getattr(panel, "_actions_built", False):
        return
    host = ctk.CTkFrame(panel, fg_color="transparent")
    host.grid(row=3, column=0, columnspan=3, padx=7, pady=0, sticky="ew")
    panel._actions_host = host
    panel._volume_label.grid_configure(pady=(3, 6))
    deck_id: str = panel.deck_id
    action: Callable[[str, str], None] = panel._action
    fade: Callable[[bool], None] = panel._fade
    cancel_fade: Callable[[], None] = panel._cancel_fade
    tooltips: list[Tooltip] = panel._tooltips
    for column in range(len(_ACTIONS)):
        host.grid_columnconfigure(column, weight=1)
    for column, (label, action_name, description) in enumerate(_ACTIONS):
        danger = label in {"■ Stop", "Auswerf."}
        command: Callable[[], None]
        if action_name == "fade_in":
            command = partial(fade, True)
        elif action_name == "fade_out":
            command = partial(fade, False)
        elif action_name == "cancel_fade":
            command = cancel_fade
        else:
            command = partial(action, deck_id, action_name)
        button = ctk.CTkButton(
            host,
            text=label,
            height=32,
            width=48,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.DANGER if danger else theme.SURFACE,
            hover_color=theme.DANGER_HOVER if danger else theme.SURFACE_HOVER,
            border_width=0 if danger else 1,
            border_color=theme.BORDER,
            command=command,
        )
        button.grid(row=0, column=column, padx=2, sticky="ew")
        tooltips.append(Tooltip(button, description))
    panel._actions_built = True


def bind_compact_decks(controller: Any, *panels: Any) -> None:
    """Bind both compact views to the existing controller command paths."""
    for panel in panels:
        panel.bind_controls(
            controller.seek,
            controller.set_deck_volume,
            controller.fade,
            controller.cancel_fade,
        )
        build_compact_deck_actions(panel)
