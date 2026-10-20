"""Stateless responsive layout policies for the main window."""

from party_player.presentation import ResolvedPresentation, Workspace


def _center_panel_grid_options(compact: bool) -> dict[str, object]:
    """Return a complete placement so a previous column span cannot leak."""
    return {
        "row": 1,
        "column": 0 if compact else 1,
        "columnspan": 3 if compact else 1,
        "padx": 16 if compact else 8,
        "pady": (4, 10) if compact else 8,
        "sticky": "nsew",
    }


def _presentation_header_grid_options(compact: bool) -> dict[str, object]:
    """Return complete header placement for reversible presentation changes."""
    return {
        "row": 0,
        "column": 0 if compact else 1,
        "columnspan": 2 if compact else 1,
        "padx": (16, 8) if compact else 8,
        "pady": (4, 2) if compact else (6, 3),
        "sticky": "ew",
    }


def _mixer_container_grid_options(compact: bool) -> dict[str, object]:
    """Keep the existing mixer disclosure reachable in both presentations."""
    return {
        "row": 2,
        "column": 0,
        "columnspan": 3,
        "padx": 16,
        "pady": (0, 8) if compact else (8, 16),
        "sticky": "ew",
    }


def _compact_preparation_container_grid_options() -> dict[str, object]:
    """Use the existing center cell for the locally scrollable preparation view."""
    return {
        "row": 1,
        "column": 1,
        "padx": 8,
        "pady": (4, 8),
        "sticky": "nsew",
    }


def _workspace_content(resolved: ResolvedPresentation, workspace: Workspace) -> str:
    """Select presentation content without consulting or mutating domain state."""
    if workspace is Workspace.PREPARATION:
        return "preparation"
    if resolved is ResolvedPresentation.COMPACT:
        return "compact_live"
    return "large_live"


def _compact_mixer_visible(overlays_expanded: bool) -> bool:
    """Reserve the short compact footer for one disclosure at a time."""
    return not overlays_expanded


def _compact_live_rows() -> dict[str, int]:
    """Keep direct Jingle access above the flexible queue viewport."""
    return {
        "decks": 0,
        "crossfader": 1,
        "overlays": 2,
        "queue_header": 3,
        "queue_toolbar": 4,
        "directory_progress": 5,
        "queue": 6,
    }


def _compact_preparation_rows() -> dict[str, int]:
    """Reserve the remaining height for the shared scrollable catalog."""
    return {
        "live_status": 0,
        "search": 1,
        "summary": 2,
        "catalog": 3,
        "tools": 4,
        "progress": 5,
        "playlist": 6,
    }
