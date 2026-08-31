"""Shared work-area-aware behavior for DeckRelay dialogs."""

from __future__ import annotations

from typing import Any

from party_player.window_geometry import (
    WindowsDisplayProvider,
    parse_tk_geometry,
    resolve_child_window_geometry,
)


def apply_responsive_dialog_geometry(
    dialog: Any,
    parent: Any,
    *,
    preferred_size: tuple[int, int],
    minimum_size: tuple[int, int],
    resizable: bool = True,
) -> None:
    """Place a dialog on the parent's monitor within its actual work area."""
    parent.update_idletasks()
    parent_geometry = parse_tk_geometry(parent.geometry(), 1.0)
    if parent_geometry is None:
        dialog.geometry(f"{preferred_size[0]}x{preferred_size[1]}")
        dialog.minsize(*minimum_size)
        dialog.resizable(resizable, resizable)
        return
    try:
        snapshot = WindowsDisplayProvider().snapshot(parent.winfo_id())
        resolved = resolve_child_window_geometry(
            parent_geometry,
            snapshot,
            preferred_size=preferred_size,
            standard_minimum=minimum_size,
        )
    except OSError:
        dialog.geometry(f"{preferred_size[0]}x{preferred_size[1]}")
        dialog.minsize(*minimum_size)
    else:
        dialog.geometry(resolved.tk_geometry)
        dialog.minsize(resolved.minimum_width, resolved.minimum_height)
    dialog.resizable(resizable, resizable)


def bind_dialog_escape(dialog: Any, close: Any) -> None:
    dialog.bind("<Escape>", lambda _event: close())


def release_dialog(dialog: Any) -> None:
    """Release modal ownership defensively before destroying a dialog."""
    try:
        current = dialog.grab_current() if hasattr(dialog, "grab_current") else dialog
        if current is dialog:
            dialog.grab_release()
    except Exception:  # Tk may already be tearing the toplevel down.
        pass
