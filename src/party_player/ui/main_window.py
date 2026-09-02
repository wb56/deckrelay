"""CustomTkinter DeckRelay main window."""

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from io import BytesIO
import logging
import math
from pathlib import Path
import sys
from time import monotonic
import tkinter as tk
from tkinter import filedialog, simpledialog, TclError
from typing import Any

import customtkinter as ctk  # type: ignore[import-untyped]
from PIL import Image, ImageOps

from party_player import __version__
from party_player.product import PRODUCT_NAME
from party_player.gui_callback import measured_gui_callback
from party_player.gui_heartbeat_watchdog import GuiCallbackState
from party_player.controllers.main_controller import (
    EqualizerDialogState,
    EmergencyDashboardViewModel,
    MainController,
    RecoveryReturnRequirement,
)
from party_player.controllers.cue_point_controller import CuePointController
from party_player.controllers.loudness_controller import (
    LoudnessController,
    LoudnessEditorState,
)
from party_player.controllers.track_editor_controller import (
    TrackEditorController,
    TrackEditorViewModel,
)
from party_player.metadata_analysis_service import MetadataAnalysisService
from party_player.enums import DeckState
from party_player.emergency_actions import EmergencyActionProfile
from party_player.emergency_playlist import EmergencyMediaType
from party_player.models import (
    Deck,
    PartySession,
    QueueEntry,
    QueueStats,
    SavedQueue,
    Track,
)
from party_player.ui.tooltip import SharedTooltipManager, Tooltip
from party_player.ui.queue_row import QueueEntryViewModel, QueueRowView
from party_player.ui import theme
from party_player.queue_view_events import (
    QueueViewEvent,
    QueueViewEventType,
    QueueViewRevision,
)
from party_player.ui.catalog_row import (
    CatalogEntryViewModel,
    CatalogRowView,
    track_version_text,
)
from party_player.ui.compact_deck_presentation import compact_deck_presentation
from party_player.ui.overlay_panel import OverlayPanel
from party_player.ui.overlay_management_dialog import OverlayManagementDialog
from party_player.ui.system_diagnostic_dialog import SystemDiagnosticDialog
from party_player.ui.external_programs_dialog import ExternalProgramsDialog
from party_player.ui.database_backup_dialog import (
    DatabaseBackupDialog,
    choose_equalizer_conflict,
    choose_playlist_conflict,
)
from party_player.equalizer_transfer import EqualizerConflictStrategy
from party_player.overlay_transfer import OverlayConflictStrategy
from party_player.playlist_transfer import (
    PlaylistConflictStrategy,
    PlaylistTransferFormat,
)
from party_player.system_diagnostic_service import SystemDiagnosticReport
from party_player.diagnostic_export import DiagnosticExportMode
from party_player.settings_service import DependencySettings
from party_player.ui.overlay_presentation import (
    FavoritePadViewModel,
    OverlayState,
    OverlayViewModel,
    favorite_position_from_shortcut,
    collapsed_overlay_stop_visible,
    mixer_overlay_header_text,
    overlay_shortcut_allowed,
)
from party_player.overlay import OverlayRecord, OverlayRuntime, OverlayStatus
from party_player.overlay_service import OverlayCatalogSnapshot, OverlayService
from party_player.controllers.overlay_controller import OverlayController
from party_player.ui.dirty_row_scheduler import DirtyRowScheduler, RenderBatchStatistics
from party_player.performance_monitor import PerformanceMonitor
from party_player.presentation import (
    GlobalStatusState,
    LayoutDecision,
    LayoutPolicy,
    LogicalClientSize,
    PresentationPreference,
    PresentationState,
    ResolvedPresentation,
    Workspace,
    force_live_for_operational_update,
    global_status_text,
    logical_client_size,
)
from party_player.presentation_coordinator import MainWindowPresentationCoordinator
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
from party_player.capability_snapshots import CapabilitySnapshotState
from party_player.backup_restore_controller import (
    BackupRestoreController,
    BackupRestoreOperation,
    BackupRestoreUiResult,
    BackupRestoreUiState,
)
from party_player.ui.dialogs import (
    ask_silent_yes_no,
    ask_silent_yes_no_cancel,
    CuePointDialog,
    LoudnessDialog,
    NormalizationSettingsDialog,
    QueueCueDialog,
    SavedQueueTempoDialog,
    show_silent_message,
    show_tempo_analysis_help,
)
from party_player.ui.catalog_maintenance_dialog import (
    CatalogAnalysisActions,
    CatalogMaintenanceDialog,
)


def _time_text(seconds: float) -> str:
    value = max(0, round(seconds))
    return f"{value // 60:02d}:{value % 60:02d}"


def _ellipsize(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    return f"{text[: maximum - 1]}…"


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


def _automatic_help_text() -> str:
    return (
        "CD oder Playlist laden\n"
        "• Ersetzen: Alte wartende und vorbereitete Titel entfernen; ein laufender "
        "Titel bleibt erhalten.\n"
        "• Anhängen: Vorhandene Titel werden zuerst abgespielt.\n"
        "• Vollständig abspielen: Reihenfolge erhalten und Wiederholungsschutz nur "
        "für diese CD/Playlist übersteuern.\n\n"
        "Automatik starten\n"
        "• Standardmäßig beginnt sie beim ersten wartenden Titel.\n"
        "• Start ab Auswahl muss ausdrücklich bestätigt werden.\n\n"
        "Pause und Fortsetzen\n"
        "• Deck-Pause oder eine echte Crossfader-Bewegung pausiert die Automatik.\n"
        "• Deck fortsetzen beziehungsweise ▶ setzt die Automatik fort.\n\n"
        "Cue-Fallback\n"
        "Reicht die Zeit bis Cue Out nicht für einen sicheren Crossfade, endet der "
        "laufende Titel natürlich und der vorbereitete Folgetitel startet danach."
    )


_COMMON_EQUALIZER_PRESETS = frozenset(
    {"neutral", "rock", "pop", "bluesrock", "dance", "schlager", "sprache"}
)


def _compact_equalizer_labels(labels: list[str], selected: str) -> list[str]:
    """Keep the normal deck dialog focused while retaining its active preset."""
    compact = [
        label
        for label in labels
        if label in {"Vererben", "Equalizer aus"}
        or label.casefold() in _COMMON_EQUALIZER_PRESETS
        or label == selected
    ]
    return compact or [selected]


def _focus_and_break(widget: Any) -> str:
    """Focus a CustomTkinter control through its keyboard-capable child."""
    getattr(widget, "_canvas", widget).focus_set()
    return "break"


def _configure_focus_cycle(widgets: tuple[Any, ...]) -> None:
    """Install one explicit forward/backward keyboard focus cycle."""
    for index, widget in enumerate(widgets):
        focus_target = getattr(widget, "_canvas", widget)
        try:
            focus_target.configure(takefocus=True)
        except (AttributeError, TypeError):
            pass
        widget.bind(
            "<Tab>",
            lambda _event, target=widgets[(index + 1) % len(widgets)]: _focus_and_break(target),
            add="+",
        )
        widget.bind(
            "<Shift-Tab>",
            lambda _event, target=widgets[(index - 1) % len(widgets)]: _focus_and_break(target),
            add="+",
        )


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


def _duration_text(seconds: float) -> str:
    value = max(0, round(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds_part:02d}"
        if hours
        else f"{minutes:02d}:{seconds_part:02d}"
    )


def _main_layout_spacing(width: int) -> tuple[int, int, int]:
    """Return outer, inner and vertical spacing for one stable size class."""
    return (8, 4, 6) if width < 1350 else (16, 8, 8)


def _diagnostic_toggle_text(expanded: bool) -> str:
    """Return the stable disclosure label for the optional diagnostic controls."""
    return "Diagnose und Analyse ausblenden ▲" if expanded else "Diagnose und Analyse anzeigen ▼"


def _track_details_text(track: Track, loudness: LoudnessEditorState | None) -> str:
    """Build complete catalog details, including cached loudness resolution."""
    year = str(track.year) if track.year is not None else "—"
    original_year = (
        str(track.original_release_year) if track.original_release_year is not None else "—"
    )
    duration = _duration_text(track.duration_seconds or 0.0)
    loudness_text = ""
    if loudness is not None:
        loudness_text = (
            "\n\nLautstärkeanpassung:\n"
            f"Quelle: {loudness.source_text}\n"
            f"Metadatenstatus: {loudness.metadata_status_text}\n"
            f"Angefordert: {loudness.resolved.requested_gain_db:+.2f} dB\n"
            f"Effektiv: {loudness.resolved.effective_gain_db:+.2f} dB\n"
            f"{loudness.clip_protection_text}"
        )
    return (
        f"Titel: {track.title}\n"
        f"Interpret: {track.artist or '—'}\n"
        f"Album: {track.album or '—'}\n"
        f"Genre: {track.genre or '—'}\n"
        f"Jahr: {year}\n"
        f"Ursprüngliches Erscheinungsjahr: {original_year}\n"
        f"Dauer: {duration}"
        f"{loudness_text}\n\n"
        f"Datei: {track.file_path}"
    )


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


def _equalizer_effective_text(name: str, source: str) -> str:
    """Build the compact German effective-preset status shown in the dialog."""
    return f"Effektiv: {name} · {_equalizer_source_text(source)}"


def _equalizer_target_choices(
    state: EqualizerDialogState,
) -> tuple[tuple[str, str, bool, str], ...]:
    """Return stable target availability and explanations for the deck dialog."""
    has_playlist = state.saved_queue_id is not None
    has_genre = bool(state.genre.strip())
    return (
        ("preview", "Nur vorübergehend testen", True, ""),
        ("title", "Für diesen Titel speichern", True, ""),
        (
            "playlist",
            "Für diese Playlist speichern",
            has_playlist,
            "" if has_playlist else "Keine gespeicherte Playlist ausgewählt",
        ),
        (
            "genre",
            "Als Genrezuweisung speichern",
            has_genre,
            "" if has_genre else "Der Titel besitzt kein Genre-Metadatum",
        ),
        ("queue", "Für die aktuelle Queue", True, ""),
    )


def _initial_catalog_pool_target(total: int, existing: int, initial: int = 10) -> int:
    """Keep the initial widget pool bounded while retaining reusable rows."""
    return min(total, max(existing, initial))


def _queue_pool_size(
    available_height: int,
    *,
    row_height: int = 40,
    minimum: int = 10,
    overscan: int = 4,
    maximum: int = 20,
) -> int:
    """Derive the bounded physical row pool from the visible queue height."""
    visible = max(1, available_height // max(1, row_height))
    return min(maximum, max(minimum, visible + overscan))


def _queue_model_count(pool_target: int, existing_rows: int) -> int:
    """Keep surplus pooled rows addressable so a smaller page can hide them."""
    return max(pool_target, existing_rows)


def _optionmenu_changes(
    previous: tuple[tuple[str, ...], str] | None,
    values: list[str],
    selected: str,
) -> tuple[bool, bool]:
    """Return whether values and selection require separate CTk redraws."""
    current_values = tuple(values)
    return (
        previous is None or previous[0] != current_values,
        previous is None or previous[1] != selected,
    )


DECK_STATE_TEXT = {
    DeckState.EMPTY: "LEER",
    DeckState.LOADED: "GELADEN",
    DeckState.PLAYING: "WIEDERGABE",
    DeckState.PAUSED: "PAUSIERT",
    DeckState.STOPPED: "GESTOPPT",
    DeckState.FINISHED: "BEENDET",
    DeckState.ERROR: "FEHLER",
}


class SmoothScrollableFrame(ctk.CTkScrollableFrame):  # type: ignore[misc]
    """Limit expensive canvas scrolling so audio-control timers stay responsive."""

    _SCROLL_INTERVAL_SECONDS = 1 / 60
    _WINDOWS_UNITS_PER_NOTCH = 3

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._last_scroll_at = 0.0
        self._scroll_callback: Callable[[int], None] | None = None
        super().__init__(*args, **kwargs)

    def set_scroll_callback(self, callback: Callable[[int], None]) -> None:
        """Notify the owner after a bounded user scroll, e.g. for lazy row growth."""
        self._scroll_callback = callback

    def _mouse_wheel_all(self, event: Any) -> str | None:
        if not self.check_if_master_is_canvas(event.widget):
            return None
        now = monotonic()
        if now - self._last_scroll_at < self._SCROLL_INTERVAL_SECONDS:
            return "break"
        self._last_scroll_at = now
        delta = float(event.delta)
        if sys.platform.startswith("win"):
            units = -round(delta / 120) * self._WINDOWS_UNITS_PER_NOTCH
        else:
            units = -round(delta)
        units = max(-self._WINDOWS_UNITS_PER_NOTCH, min(self._WINDOWS_UNITS_PER_NOTCH, units))
        if units:
            canvas = self._parent_canvas
            if self._shift_pressed and canvas.xview() != (0.0, 1.0):
                canvas.xview_scroll(units, "units")
            elif not self._shift_pressed and canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(units, "units")
            if self._scroll_callback is not None:
                self._scroll_callback(units)
        return "break"


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


class MainWindow(ctk.CTk):  # type: ignore[misc]
    """Party-focused two-deck desktop window."""

    _CATALOG_INITIAL_POOL_SIZE = 10
    _CATALOG_POOL_GROWTH = 8
    _QUEUE_MINIMUM_POOL_SIZE = 10
    _QUEUE_OVERSCAN_ROWS = 4
    _QUEUE_MAXIMUM_POOL_SIZE = 20
    _QUEUE_ESTIMATED_ROW_HEIGHT = 40

    def __init__(
        self,
        performance_monitor: PerformanceMonitor | None = None,
        callback_state: GuiCallbackState | None = None,
        *,
        saved_geometry: str | None = None,
        save_geometry: Callable[[str], None] | None = None,
        display_provider: DisplayProvider | None = None,
        presentation_preference: PresentationPreference = PresentationPreference.AUTO,
        presentation_workspace: Workspace = Workspace.LIVE,
        save_presentation_preference: Callable[[PresentationPreference], None] | None = None,
        save_presentation_workspace: Callable[[Workspace], None] | None = None,
    ) -> None:
        super().__init__()
        self._performance = performance_monitor or PerformanceMonitor()
        self._callback_state = callback_state or GuiCallbackState()
        self._logger = logging.getLogger(__name__)
        self._display_provider = display_provider
        self._save_window_geometry = save_geometry
        self._display_fingerprint: tuple[object, ...] | None = None
        self._window_geometry_after_id: str | None = None
        self._save_presentation_preference = save_presentation_preference
        self._save_presentation_workspace = save_presentation_workspace
        self._presentation_initial_preference = presentation_preference
        self._presentation_initial_workspace = presentation_workspace
        self._presentation_coordinator: MainWindowPresentationCoordinator | None = None
        self._presentation_startup_guard = True
        self._presentation_status = GlobalStatusState()
        self._latest_decks: dict[str, Deck] = {}
        self._compact_layout_active = False
        self._compact_layout_apply_count = 0
        self._compact_widget_tree_creation_count = 0
        self._compact_overlays_expanded = False
        self._compact_analysis_expanded = False
        self._compact_playlist_expanded = False
        self._catalog_analysis_active = False
        self._loudness_analysis_active = False
        self._metadata_analysis_active = False
        self._presentation_layout_signature: tuple[ResolvedPresentation, Workspace] | None = None
        self._controller: MainController | None = None
        self._system_diagnostic_report: SystemDiagnosticReport | None = None
        self._system_diagnostic_check: Callable[[], SystemDiagnosticReport] | None = None
        self._system_diagnostic_dialog: SystemDiagnosticDialog | None = None
        self._system_diagnostic_export: (
            Callable[[SystemDiagnosticReport, DiagnosticExportMode], object] | None
        ) = None
        self._external_program_dialog: ExternalProgramsDialog | None = None
        self._external_program_binding: tuple[object, ...] | None = None
        self._backup_restore_controller: BackupRestoreController | None = None
        self._database_backup_dialog_generation = 0
        self._database_operation_generation: int | None = None
        self._default_backup_directory: Path | None = None
        self._database_backup_dialog: DatabaseBackupDialog | None = None
        self._restart_requested = False
        self._cue_controller: CuePointController | None = None
        self._loudness_controller: LoudnessController | None = None
        self._metadata_analysis: MetadataAnalysisService | None = None
        self._queue_tooltips: list[Tooltip] = []
        self._queue_rows: list[QueueRowView] = []
        self._queue_tooltip_manager = SharedTooltipManager()
        self._queue_view_models: list[QueueEntryViewModel | None] = []
        self._queue_row_index_by_id: dict[int, int] = {}
        self._catalog_tooltips: list[Tooltip] = []
        self._catalog_rows: list[CatalogRowView] = []
        self._catalog_view_models: list[CatalogEntryViewModel] = []
        self._catalog_render_started_at = monotonic()
        self._catalog_first_row_recorded = False
        self._catalog_initial_rows_recorded = False
        self._catalog_pool_target = 0
        self._layout_refresh_pending = {"catalog": False, "queue": False}
        self._focus_pending_roots: list[Any] = []
        self._focus_pending_widgets: list[Any] = []
        self._focus_callback_pending = False
        self._responsive_layout_pending = False
        self._responsive_layout_spacing: tuple[int, int, int] | None = None
        self._cursor_restore_after_id: str | None = None
        self._optionmenu_cache: dict[str, tuple[tuple[str, ...], str]] = {}
        self._scheduled_after_ids: set[str] = set()
        self._static_tooltips: list[Tooltip] = []
        self._catalog_tracks: list[Track] = []
        self._queue_entries: list[QueueEntry] = []
        self._restored_queue_ids: set[int] = set()
        self._queue_cue_warnings: dict[int, str] = {}
        self._queue_revision = QueueViewRevision()
        self._queue_selected_id: int | None = None
        self._queue_tracks: dict[int, Track] = {}
        self._queue_inherited_manual_track_ids: set[int] = set()
        self._queue_render_signature: (
            tuple[tuple[QueueEntry, ...], tuple[Track, ...], tuple[int, ...]] | None
        ) = None
        self._queue_page = 0
        self._queue_page_size = self._QUEUE_MINIMUM_POOL_SIZE
        self._queue_visible_start_index = 0
        self._queue_pool_target = self._QUEUE_MINIMUM_POOL_SIZE
        self._queue_rebind_count = 0
        self._queue_widget_creation_count = 0
        self._queue_lifecycle_counters = {
            "created_widget_count": 0,
            "destroyed_widget_count": 0,
            "configured_widget_count": 0,
            "rebound_row_count": 0,
            "updated_row_count": 0,
        }
        self._queue_focus_needed = False
        self._queue_render_in_progress = False
        self._queue_scroll_restore_pending = False
        self._saved_queue_ids: dict[str, int] = {}
        self._cover_images: dict[str, ctk.CTkImage] = {}
        self._deck_on_air = {"A": False, "B": False}
        self._deck_status_cache: dict[str, tuple[str, str]] = {}
        self._on_air_summary_cache: tuple[str, str] | None = None
        self._queue_stats_text = ""
        self._audio_device_ids: dict[str, str] = {"Systemstandard": ""}
        self._equalizer_preset_keys: dict[str, str] = {
            "Vererben": "inherit",
            "Equalizer aus": "disabled",
        }
        self._updating_mixer = False
        self._mixer_render_cache: dict[str, object] = {}
        self._overlay_controller: OverlayController | None = None
        self._overlay_service: OverlayService | None = None
        self._overlay_snapshot = OverlayCatalogSnapshot((), (), (None,) * 6, frozenset())
        self._selected_overlay: OverlayRecord | None = None
        self._overlay_runtime = OverlayRuntime()
        self._overlay_management_dialog: OverlayManagementDialog | None = None
        self._overlay_tick_active = False
        self._overlay_notice_generation = 0
        self._overlay_ducking_factor = 1.0
        self._overlay_ducking_phase = "idle"
        self._render_counters = {
            "catalog_chunk_count_total": 0,
            "queue_chunk_count_total": 0,
            "widgets_created_total": 0,
            "widgets_destroyed_total": 0,
            "catalog_layout_refresh_requested_total": 0,
            "catalog_layout_refresh_executed_total": 0,
            "queue_layout_refresh_requested_total": 0,
            "queue_layout_refresh_executed_total": 0,
            "focus_request_total": 0,
            "focus_apply_total": 0,
            "scroll_position_set_total": 0,
            "scroll_restore_requested_total": 0,
            "scroll_restore_executed_total": 0,
            "scroll_restore_coalesced_total": 0,
            "optionmenu_configure_total": 0,
            "optionmenu_set_total": 0,
        }
        self._catalog_dirty_scheduler = DirtyRowScheduler(
            self.schedule,
            measured_gui_callback(
                self._performance,
                "render.catalog_render_chunk",
                self._render_catalog_row,
                callback_state=self._callback_state,
            ),
            max_rows=5,
            budget_ms=8.0,
            inter_chunk_delay_ms=10,
            on_chunk=lambda duration, rows: self._record_render_chunk("catalog", duration, rows),
            on_complete=lambda stats: self._record_render_complete("catalog", stats),
            callback_name="catalog_render_chunk",
            is_creation=lambda index: index >= len(self._catalog_rows),
            max_create_rows=1,
        )
        self._queue_dirty_scheduler = DirtyRowScheduler(
            self.schedule,
            measured_gui_callback(
                self._performance,
                "render.queue_render_chunk",
                self._render_queue_row,
                callback_state=self._callback_state,
            ),
            max_rows=5,
            budget_ms=8.0,
            inter_chunk_delay_ms=10,
            on_chunk=lambda duration, rows: self._record_render_chunk("queue", duration, rows),
            on_complete=lambda stats: self._record_render_complete("queue", stats),
            callback_name="queue_render_chunk",
            is_creation=self._queue_row_requires_creation,
            max_create_rows=1,
            split_creation_and_bind=True,
        )
        self.title(f"DeckRelay {__version__}")
        self._apply_initial_window_geometry(saved_geometry)
        ctk.set_appearance_mode("dark")
        self.protocol("WM_DELETE_WINDOW", self._request_close)
        self._bind_gui("<F11>", "fullscreen", lambda _event: self._toggle_fullscreen())
        self._bind_gui("<Escape>", "escape", lambda _event: self.set_fullscreen(False))
        self._bind_gui("<F1>", "deck_a_play", lambda _event: self._shortcut_play_pause("A"))
        self._bind_gui("<F2>", "deck_a_stop", lambda _event: self._deck_action("A", "stop"))
        self._bind_gui("<F3>", "deck_b_play", lambda _event: self._shortcut_play_pause("B"))
        self._bind_gui("<F4>", "deck_b_stop", lambda _event: self._deck_action("B", "stop"))
        self._bind_gui("<Control-f>", "search_focus", lambda _event: self._focus_search())
        self._bind_gui("<Control-m>", "mixer", lambda _event: self._toggle_mixer_panel())
        for favorite_position in range(1, 7):
            self._bind_gui(
                f"<Control-Key-{favorite_position}>",
                f"overlay_favorite_{favorite_position}",
                self._overlay_favorite_shortcut,
            )
        self._bind_gui(
            "<Delete>",
            "delete_queue_selection",
            lambda _event: self._delete_selected_queue(),
        )
        self._bind_gui(
            "<Control-Left>",
            "crossfader_left",
            lambda _event: self._move_crossfader_by_keyboard(-0.05),
        )
        self._bind_gui(
            "<Control-Right>",
            "crossfader_right",
            lambda _event: self._move_crossfader_by_keyboard(0.05),
        )

        self.grid_columnconfigure(0, weight=1, uniform="main")
        self.grid_columnconfigure(1, weight=2, uniform="main")
        self.grid_columnconfigure(2, weight=1, uniform="main")
        self.grid_rowconfigure(1, weight=1)

        title_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._title_frame = title_frame
        title_frame.grid(row=0, column=0, columnspan=3, padx=20, pady=(14, 8), sticky="w")
        ctk.CTkLabel(title_frame, text=PRODUCT_NAME, font=("Segoe UI", 28, "bold")).pack(
            side="left"
        )
        ctk.CTkLabel(
            title_frame,
            text=f"Version {__version__}",
            font=("Segoe UI", 13),
            text_color="#aaaaaa",
        ).pack(side="left", padx=(10, 0), pady=(8, 0))
        window_controls = ctk.CTkFrame(self, fg_color="transparent")
        self._window_controls = window_controls
        window_controls.grid(row=0, column=2, padx=(8, 16), pady=(8, 4), sticky="e")
        self._on_air_summary = ctk.CTkLabel(
            window_controls,
            text="ON AIR: KEINES",
            font=("Segoe UI", 16, "bold"),
            text_color="#999999",
        )
        self._on_air_summary.pack(side="left", padx=(0, 10))
        self._window_mode_button = ctk.CTkButton(
            window_controls,
            text="❐ Fenstermodus",
            width=125,
            command=self._leave_fullscreen,
        )
        self._window_mode_button.pack(side="left", padx=2)
        extras_button = ctk.CTkButton(
            window_controls,
            text="Extras ▾",
            width=76,
        )
        extras_button.configure(command=lambda: self._show_extras_menu(extras_button))
        extras_button.pack(side="left", padx=2)
        help_button = ctk.CTkButton(
            window_controls,
            text="Hilfe ▾",
            width=72,
        )
        help_button.configure(command=lambda: self._show_help_menu(help_button))
        help_button.pack(side="left", padx=2)
        ctk.CTkButton(window_controls, text="—", width=38, command=self._minimize_window).pack(
            side="left", padx=2
        )
        ctk.CTkButton(
            window_controls,
            text="✕",
            width=38,
            fg_color="#8f1f1f",
            hover_color="#b52a2a",
            command=self._request_close,
        ).pack(side="left", padx=2)
        presentation_header = ctk.CTkFrame(self, fg_color="transparent")
        self._presentation_header = presentation_header
        presentation_header.grid(row=0, column=1, padx=8, pady=(6, 3), sticky="ew")
        presentation_header.grid_columnconfigure(0, weight=1)
        self._session_summary = ctk.CTkLabel(
            presentation_header, text="Session: —", text_color="#aaaaaa"
        )
        self._session_summary.grid(row=0, column=0, columnspan=4, sticky="ew")
        workspace_controls = ctk.CTkFrame(presentation_header, fg_color="transparent")
        workspace_controls.grid(row=1, column=0, columnspan=4)
        self._workspace_live_button = ctk.CTkButton(
            workspace_controls,
            text="LIVE",
            width=72,
            height=25,
            command=lambda: self._select_workspace(Workspace.LIVE),
        )
        self._workspace_live_button.pack(side="left", padx=3)
        self._workspace_preparation_button = ctk.CTkButton(
            workspace_controls,
            text="VORBEREITUNG",
            width=112,
            height=25,
            command=lambda: self._select_workspace(Workspace.PREPARATION),
        )
        self._workspace_preparation_button.pack(side="left", padx=3)
        self._presentation_mode_button = ctk.CTkButton(
            workspace_controls,
            text="Ansicht: AUTO ▾",
            width=110,
            height=25,
            fg_color=theme.SURFACE_RAISED,
            command=self._show_presentation_menu,
        )
        self._presentation_mode_button.pack(side="left", padx=3)
        self._global_status_label = ctk.CTkLabel(
            presentation_header,
            text="A LEER · B LEER · Quelle — · Automatik bereit · Übergang 50%",
            text_color=theme.TEXT_MUTED,
            font=(theme.FONT_FAMILY, 11),
            wraplength=720,
        )
        self._global_status_label.grid(
            row=2, column=0, columnspan=4, padx=3, pady=(1, 0), sticky="ew"
        )

        self.deck_a = DeckPanel(self, "A", self._deck_action, self._performance)
        self.deck_a.grid(row=1, column=0, padx=(16, 8), pady=8, sticky="nsew")
        self.deck_b = DeckPanel(self, "B", self._deck_action, self._performance)
        self.deck_b.grid(row=1, column=2, padx=(8, 16), pady=8, sticky="nsew")

        center = ctk.CTkFrame(self, corner_radius=12)
        self._center_panel = center
        center.grid(row=1, column=1, padx=8, pady=8, sticky="nsew")
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(2, weight=50, minsize=80, uniform="list_workspace")
        center.grid_rowconfigure(9, weight=50, minsize=80, uniform="list_workspace")
        self._workspace_catalog_ratio = 0.5
        self._compact_decks_frame = ctk.CTkFrame(center, fg_color="transparent")
        self._compact_decks_frame.grid_columnconfigure(0, weight=1, uniform="compact_decks")
        self._compact_decks_frame.grid_columnconfigure(1, weight=1, uniform="compact_decks")
        self.compact_deck_a = CompactDeckPanel(
            self._compact_decks_frame, "A", self._deck_action, self._performance
        )
        self.compact_deck_a.grid(row=0, column=0, padx=(0, 4), sticky="nsew")
        self.compact_deck_b = CompactDeckPanel(
            self._compact_decks_frame, "B", self._deck_action, self._performance
        )
        self.compact_deck_b.grid(row=0, column=1, padx=(4, 0), sticky="nsew")
        self._compact_widget_tree_creation_count = 2
        self._compact_decks_frame.grid_remove()

        self._compact_preparation = ctk.CTkFrame(center, corner_radius=8)
        self._compact_preparation.grid_columnconfigure(0, weight=1)
        self._compact_preparation_status = ctk.CTkLabel(
            self._compact_preparation,
            text="A LEER · B LEER · Quelle — · Automatik bereit · Übergang 50%",
            anchor="w",
            text_color=theme.TEXT_MUTED,
            font=(theme.FONT_FAMILY, 11),
            wraplength=760,
        )
        self._compact_preparation_status.grid(row=0, column=0, padx=(8, 4), pady=4, sticky="ew")
        self._compact_on_air_stop = ctk.CTkButton(
            self._compact_preparation,
            text="■ ON AIR stoppen",
            width=128,
            height=30,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=self._stop_on_air_decks,
        )
        self._compact_on_air_stop.grid(row=0, column=1, padx=(4, 8), pady=4)
        self._compact_on_air_stop.grid_remove()
        self._compact_preparation.grid_remove()
        self._summary = ctk.CTkLabel(center, text="Katalog wird geladen …")
        self._summary.grid(row=0, column=0, padx=12, pady=(12, 4), sticky="w")
        search_frame = ctk.CTkFrame(center, fg_color="transparent")
        self._search_frame = search_frame
        search_frame.grid(row=1, column=0, padx=12, pady=4, sticky="ew")
        search_frame.grid_columnconfigure(0, weight=1)
        self._search = ctk.CTkEntry(
            search_frame, placeholder_text="Titel, Interpret oder Album suchen"
        )
        self._search.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self._search.bind("<Return>", lambda _event: self._run_search())
        self._catalog_search_button = ctk.CTkButton(
            search_frame, text="Suchen", width=80, command=self._run_search
        )
        self._catalog_search_button.grid(row=0, column=1)
        self._catalog_search_reset_button = ctk.CTkButton(
            search_frame,
            text="×",
            width=34,
            fg_color=theme.SURFACE_RAISED,
            command=self._reset_catalog_search,
        )
        self._catalog_search_reset_button.grid(row=0, column=2, padx=(4, 0))
        self._catalog_analysis_button = ctk.CTkButton(
            search_frame,
            text="Alle Cues neu",
            width=130,
            command=self._analyze_catalog,
            fg_color="transparent",
            border_width=1,
        )
        self._catalog_analysis_cancel_button = ctk.CTkButton(
            search_frame,
            text="Analyse abbrechen",
            width=120,
            fg_color="#7d3030",
            command=self._cancel_catalog_analysis,
            state="disabled",
        )
        self._outdated_analysis_button = ctk.CTkButton(
            search_frame,
            text="Neue/veraltete Cues",
            width=150,
            command=self._analyze_outdated_catalog,
        )
        self._catalog_analysis_was_cancelled = False
        self._loudness_analysis_button = ctk.CTkButton(
            search_frame,
            text="Alle Lautheiten neu",
            width=130,
            command=self._analyze_loudness_catalog,
            fg_color="transparent",
            border_width=1,
        )
        self._loudness_analysis_cancel_button = ctk.CTkButton(
            search_frame,
            text="Lautheit abbrechen",
            width=120,
            fg_color="#7d3030",
            command=self._cancel_loudness_analysis,
            state="disabled",
        )
        self._outdated_loudness_button = ctk.CTkButton(
            search_frame,
            text="Neue/veraltete Lautheit",
            width=150,
            command=lambda: self._analyze_loudness_catalog(outdated_only=True),
        )
        self._loudness_analysis_was_cancelled = False
        catalog_imports = ctk.CTkFrame(search_frame, fg_color="transparent")
        self._catalog_imports = catalog_imports
        catalog_imports.grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))
        catalog_file_button = ctk.CTkButton(
            catalog_imports,
            text="＋ Datei in Katalog",
            width=140,
            command=self._choose_catalog_file,
        )
        catalog_file_button.pack(side="left", padx=(0, 4))
        catalog_directory_button = ctk.CTkButton(
            catalog_imports,
            text="＋ Ordner in Katalog",
            width=150,
            command=self._choose_catalog_directory,
        )
        catalog_directory_button.pack(side="left")
        catalog_maintenance_button = ctk.CTkButton(
            catalog_imports,
            text="Katalogpflege …",
            width=130,
            command=self._open_catalog_maintenance,
        )
        catalog_maintenance_button.pack(side="left", padx=(4, 0))
        self._static_tooltips.extend(
            (
                Tooltip(
                    catalog_file_button,
                    "Eine MP3-/FLAC-Datei nur in den Katalog aufnehmen",
                ),
                Tooltip(
                    catalog_directory_button,
                    "Alle MP3-/FLAC-Dateien rekursiv nur in den Katalog aufnehmen",
                ),
            )
        )
        self._catalog_previous_button = ctk.CTkButton(
            search_frame,
            text="◀",
            width=34,
            command=lambda: self._change_catalog_page(-1),
        )
        self._catalog_previous_button.grid(row=0, column=3, padx=(10, 3))
        self._catalog_page_label = ctk.CTkLabel(search_frame, text="Seite 1/1", width=75)
        self._catalog_page_label.grid(row=0, column=4)
        self._catalog_next_button = ctk.CTkButton(
            search_frame,
            text="▶",
            width=34,
            command=lambda: self._change_catalog_page(1),
        )
        self._catalog_next_button.grid(row=0, column=5, padx=(3, 0))
        self._compact_preparation_tools = ctk.CTkFrame(center, fg_color="transparent")
        self._compact_preparation_tools.grid_columnconfigure(2, weight=1)
        self._compact_analysis_toggle = ctk.CTkButton(
            self._compact_preparation_tools,
            text="Audioanalyse …",
            width=142,
            height=30,
            fg_color=theme.SURFACE_RAISED,
            command=self._open_catalog_maintenance,
        )
        self._compact_analysis_toggle.grid(row=0, column=0, padx=(0, 4))
        self._compact_playlist_toggle = ctk.CTkButton(
            self._compact_preparation_tools,
            text="Playlist / Quellen anzeigen ▾",
            width=202,
            height=30,
            fg_color=theme.SURFACE_RAISED,
            command=self._toggle_compact_playlist,
        )
        self._compact_playlist_toggle.grid(row=0, column=1, padx=4)
        self._compact_preparation_live_button = ctk.CTkButton(
            self._compact_preparation_tools,
            text="Zurück zu LIVE",
            width=120,
            height=30,
            command=lambda: self._select_workspace(Workspace.LIVE),
        )
        self._compact_preparation_live_button.grid(row=0, column=3, padx=(4, 0))
        self._compact_analysis_active_label = ctk.CTkLabel(
            self._compact_preparation_tools,
            text="",
            anchor="w",
            text_color=theme.READY,
        )
        self._compact_analysis_active_label.grid(
            row=1, column=0, columnspan=3, padx=(2, 4), pady=(4, 0), sticky="ew"
        )
        self._compact_analysis_active_cancel = ctk.CTkButton(
            self._compact_preparation_tools,
            text="Analyse abbrechen",
            width=138,
            height=28,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=self._cancel_active_analysis,
        )
        self._compact_analysis_active_cancel.grid(row=1, column=3, padx=(4, 0), pady=(4, 0))
        self._compact_analysis_active_label.grid_remove()
        self._compact_analysis_active_cancel.grid_remove()
        self._compact_preparation_tools.grid_remove()
        self._catalog = SmoothScrollableFrame(center, label_text="Katalog")
        self._catalog.grid(row=2, column=0, padx=12, pady=6, sticky="nsew")
        self._catalog.set_scroll_callback(self._catalog_scrolled)
        self._catalog_empty_label = ctk.CTkLabel(
            self._catalog,
            text="⌕  Keine Titel gefunden\nSuche ändern oder Musikverzeichnis importieren",
            text_color=theme.TEXT_MUTED,
            font=(theme.FONT_FAMILY, 14),
        )

        crossfader_bar = ctk.CTkFrame(center, corner_radius=10, border_width=1)
        self._crossfader_bar = crossfader_bar
        crossfader_bar.grid(row=3, column=0, padx=12, pady=(4, 8), sticky="ew")
        crossfader_bar.grid_columnconfigure(1, weight=1)
        self._deck_status_labels: dict[str, ctk.CTkLabel] = {}
        self._deck_status_labels["A"] = ctk.CTkLabel(
            crossfader_bar,
            text="DECK A\nKeine Titel geladen",
            width=145,
            font=("Segoe UI", 13, "bold"),
            text_color="#999999",
        )
        self._deck_status_labels["A"].grid(row=0, column=0, padx=(12, 8), pady=8)
        fader_frame = ctk.CTkFrame(crossfader_bar, fg_color="transparent")
        fader_frame.grid(row=0, column=1, padx=4, pady=6, sticky="ew")
        fader_frame.grid_columnconfigure(0, weight=1)
        self._crossfader_label = ctk.CTkLabel(
            fader_frame, text="Crossfader · 50%", font=("Segoe UI", 14, "bold")
        )
        self._crossfader_label.grid(row=0, column=0, pady=(0, 2))
        self._crossfader = ctk.CTkSlider(fader_frame, from_=0, to=1, command=self._crossfade)
        self._crossfader.grid(row=1, column=0, sticky="ew")
        self._crossfader._canvas.configure(takefocus=True)
        self._crossfader.bind("<Button-1>", lambda _event: self._crossfader.focus_set())
        self._crossfader.bind("<Left>", lambda _event: self._move_crossfader_by_keyboard(-0.05))
        self._crossfader.bind("<Right>", lambda _event: self._move_crossfader_by_keyboard(0.05))
        self._crossfader.bind("<Home>", lambda _event: self._set_crossfader_by_keyboard(0.0))
        self._crossfader.bind("<End>", lambda _event: self._set_crossfader_by_keyboard(1.0))
        self._crossfader.bind(
            "<FocusIn>",
            lambda _event: self._crossfader.configure(border_color="#55aaff", border_width=2),
        )
        self._crossfader.bind(
            "<FocusOut>",
            lambda _event: self._crossfader.configure(border_width=0),
        )
        self._deck_status_labels["B"] = ctk.CTkLabel(
            crossfader_bar,
            text="DECK B\nKeine Titel geladen",
            width=145,
            font=("Segoe UI", 13, "bold"),
            text_color="#999999",
        )
        self._deck_status_labels["B"].grid(row=0, column=2, padx=(8, 12), pady=8)

        workspace_splitter = ctk.CTkFrame(
            center,
            height=34,
            corner_radius=6,
            fg_color=theme.SURFACE_RAISED,
            cursor="sb_v_double_arrow",
        )
        workspace_splitter.grid(row=4, column=0, padx=12, pady=(0, 4), sticky="ew")
        workspace_splitter.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            workspace_splitter,
            text="↕ Ziehen",
            text_color=theme.TEXT_MUTED,
        ).grid(row=0, column=0, padx=(8, 4), pady=3)
        split_actions = ctk.CTkFrame(workspace_splitter, fg_color="transparent")
        split_actions.grid(row=0, column=1, pady=2)
        for column, (label, ratio) in enumerate(
            (("Katalog groß", 0.8), ("50/50", 0.5), ("Queue groß", 0.2))
        ):
            ctk.CTkButton(
                split_actions,
                text=label,
                width=92,
                height=26,
                fg_color="transparent",
                border_width=1,
                command=lambda selected=ratio: self._set_workspace_split(selected),
            ).grid(row=0, column=column, padx=2)
        for widget in (workspace_splitter, *workspace_splitter.winfo_children()):
            widget.bind("<B1-Motion>", self._drag_workspace_split)
            widget.bind("<ButtonRelease-1>", self._finish_workspace_split_drag)
        self._workspace_splitter = workspace_splitter

        queue_header = ctk.CTkFrame(center, fg_color="transparent")
        self._queue_header = queue_header
        queue_header.grid(row=5, column=0, padx=12, pady=(8, 2), sticky="ew")
        ctk.CTkLabel(queue_header, text="Party-Queue", font=("Segoe UI", 17, "bold")).pack(
            side="left"
        )
        self._queue_warning_generation = 0
        self._queue_warning = ctk.CTkLabel(
            queue_header,
            text="",
            text_color=theme.WARNING,
            font=(theme.FONT_FAMILY, 12),
        )
        self._queue_warning.pack(side="left", padx=(14, 0))
        self._queue_stats = ctk.CTkLabel(queue_header, text="0 Titel · 00:00")
        self._queue_stats.pack(side="right")
        self._queue_stats_tooltip = Tooltip(
            self._queue_stats,
            "Gesamtlaufzeit 00:00 · verbleibende Laufzeit 00:00",
        )
        self._static_tooltips.append(self._queue_stats_tooltip)
        self._queue_source_button = ctk.CTkButton(
            queue_header,
            text="Quelle hinzufügen ▾",
            width=190,
            height=30,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=self._show_queue_source_menu,
        )
        self._queue_source_button.pack(side="right", padx=(8, 0))
        self._queue_next_button = ctk.CTkButton(
            queue_header,
            text="▶",
            width=theme.ICON_BUTTON_SIZE,
            height=30,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=self._queue_next_page,
        )
        self._queue_next_button.pack(side="right", padx=(4, 8))
        self._queue_page_label = ctk.CTkLabel(queue_header, text="Seite 1/1")
        self._queue_page_label.pack(side="right")
        self._queue_previous_button = ctk.CTkButton(
            queue_header,
            text="◀",
            width=theme.ICON_BUTTON_SIZE,
            height=30,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=self._queue_previous_page,
        )
        self._queue_previous_button.pack(side="right", padx=4)
        queue_toolbar = ctk.CTkFrame(center, fg_color="transparent")
        self._queue_toolbar = queue_toolbar
        queue_toolbar.grid(row=6, column=0, padx=12, pady=2, sticky="ew")
        self._duplicate_switch = ctk.CTkSwitch(
            queue_toolbar,
            text="⧉",
            width=48,
            command=self._duplicate_policy_changed,
        )
        self._duplicate_switch.select()
        self._effective_duration_switch = ctk.CTkSwitch(
            queue_toolbar,
            text="⏱",
            width=48,
            command=self._queue_duration_mode_changed,
        )
        self._artist_repetition_switch = ctk.CTkSwitch(
            queue_toolbar,
            text="Interpretenschutz",
            command=self._queue_artist_repetition_changed,
        )
        self._artist_repetition_switch.select()
        self._artist_repetition_switch.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(queue_toolbar, text="Queue-EQ:").pack(side="left", padx=(10, 4))
        self._queue_equalizer_menu = ctk.CTkOptionMenu(
            queue_toolbar,
            values=["Vererben", "Equalizer aus"],
            width=120,
            command=self._queue_equalizer_changed,
        )
        self._queue_equalizer_menu.set("Vererben")
        self._queue_equalizer_menu.pack(side="left")
        directory_button = ctk.CTkButton(
            queue_toolbar,
            text="📁＋",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=self._choose_queue_directory,
        )
        shuffle_button = ctk.CTkButton(
            queue_toolbar,
            text="Mischen",
            width=82,
            height=32,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=self._shuffle_waiting_queue,
        )
        shuffle_button.pack(side="left", padx=(8, 2))
        clear_button = ctk.CTkButton(
            queue_toolbar,
            text="⌛×",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=self._clear_waiting_queue,
        )
        clear_complete_button = ctk.CTkButton(
            queue_toolbar,
            text="🗑",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            corner_radius=theme.CONTROL_CORNER_RADIUS,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=self._clear_complete_queue,
        )
        self._queue_actions_button = ctk.CTkButton(
            queue_toolbar,
            text="⋮",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=self._show_queue_actions_menu,
        )
        self._queue_actions_button.pack(side="right")
        self._automatic_queue_active = False
        self._automatic_queue_button = ctk.CTkButton(
            queue_toolbar,
            text="▶",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            command=self._toggle_automatic_queue,
        )
        self._automatic_queue_button.pack(side="right", padx=(6, 4))
        self._automatic_status_label = ctk.CTkLabel(
            queue_toolbar,
            text="Automatik bereit",
            text_color=theme.TEXT_MUTED,
            anchor="e",
        )
        self._automatic_status_label.pack(side="right", fill="x", expand=True, padx=(8, 2))
        self._queue_source_tooltip = Tooltip(
            self._queue_source_button,
            "Aktuelle Herkunft anzeigen und weitere Titel zur Queue hinzufügen",
        )
        self._static_tooltips.extend(
            (
                Tooltip(
                    self._duplicate_switch,
                    "Mehrfache aktive Einträge desselben Titels erlauben",
                ),
                Tooltip(
                    self._effective_duration_switch,
                    "Effektive Cue-Dauer für die Queue-Statistik verwenden",
                ),
                Tooltip(self._queue_previous_button, "Vorherigen Queue-Ausschnitt anzeigen"),
                Tooltip(self._queue_next_button, "Nächsten Queue-Ausschnitt anzeigen"),
                self._queue_source_tooltip,
                Tooltip(
                    directory_button,
                    "Alle MP3-/FLAC-Dateien eines Ordners in Katalog und Queue aufnehmen",
                ),
                Tooltip(shuffle_button, "Wartende Titel zufällig neu anordnen"),
                Tooltip(clear_button, "Alle wartenden Titel nach Bestätigung entfernen"),
                Tooltip(clear_complete_button, "Alle Queue-Einträge vollständig entfernen"),
                Tooltip(self._queue_actions_button, "Weitere Queue- und Playlist-Befehle"),
                Tooltip(
                    self._automatic_queue_button,
                    "Automatische Wiedergabe starten, fortsetzen oder stoppen",
                ),
            )
        )
        self._directory_progress_frame = ctk.CTkFrame(center, fg_color="transparent")
        self._directory_progress_frame.grid(row=7, column=0, padx=12, pady=2, sticky="ew")
        self._directory_progress_frame.grid_columnconfigure(0, weight=1)
        self._directory_progress = ctk.CTkProgressBar(self._directory_progress_frame)
        self._directory_progress.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self._directory_progress_label = ctk.CTkLabel(
            self._directory_progress_frame,
            text="Verzeichnis wird eingelesen …",
            width=180,
        )
        self._directory_progress_label.grid(row=0, column=1)
        self._directory_progress_frame.grid_remove()
        self._directory_progress_visible = False
        saved_toolbar = ctk.CTkFrame(center, fg_color="transparent")
        saved_toolbar.grid(row=8, column=0, padx=12, pady=2, sticky="ew")
        ctk.CTkLabel(saved_toolbar, text="Titel hinzufügen aus Playlist:").pack(
            side="left", padx=(0, 6)
        )
        self._saved_queue_menu = ctk.CTkOptionMenu(
            saved_toolbar,
            values=["Keine gespeichert"],
            command=self._saved_queue_selected,
        )
        self._saved_queue_menu.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(saved_toolbar, text="Playlist-Vorlage-EQ:").pack(side="left", padx=(8, 4))
        self._playlist_equalizer_menu = ctk.CTkOptionMenu(
            saved_toolbar,
            values=["Vererben", "Equalizer aus"],
            width=120,
            command=self._playlist_equalizer_changed,
        )
        self._playlist_equalizer_menu.set("Vererben")
        self._playlist_equalizer_menu.pack(side="left")
        self._playlist_shuffle = ctk.CTkSwitch(saved_toolbar, text="Mischen", width=80)
        self._playlist_shuffle.pack(side="left", padx=(6, 0))
        save_queue_button = ctk.CTkButton(
            saved_toolbar,
            text="💾",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            command=self._save_current_queue,
        )
        save_queue_button.pack(side="left", padx=4)
        load_queue_button = ctk.CTkButton(
            saved_toolbar,
            text="↥",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            command=self._load_saved_queue,
        )
        load_queue_button.pack(side="left")
        show_playlist_button = ctk.CTkButton(
            saved_toolbar,
            text="☷",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            command=self._show_saved_queue,
        )
        show_playlist_button.pack(side="left", padx=(4, 0))
        automatic_help_button = ctk.CTkButton(
            saved_toolbar,
            text="?",
            width=theme.ICON_BUTTON_SIZE,
            height=32,
            fg_color=theme.SURFACE_RAISED,
            hover_color=theme.SURFACE_HOVER,
            command=self._show_automatic_help,
        )
        automatic_help_button.pack(side="right", padx=(6, 0))
        self._static_tooltips.extend(
            (
                Tooltip(save_queue_button, "Aktuelle Queue als Playlist speichern"),
                Tooltip(
                    load_queue_button,
                    "Ausgewählte Playlist in die aktuelle Queue laden",
                ),
                Tooltip(show_playlist_button, "Titel der ausgewählten Playlist anzeigen"),
                Tooltip(automatic_help_button, "Hilfe zu Queue, Playlist und Automatik"),
            )
        )
        self._saved_toolbar = saved_toolbar
        self._saved_toolbar.grid_remove()
        self._saved_toolbar_visible = False
        self._queue = SmoothScrollableFrame(center)
        self._queue.grid(row=9, column=0, padx=12, pady=(2, 12), sticky="nsew")
        self._queue.set_scroll_callback(self._queue_scrolled)
        self._queue_empty_label = ctk.CTkLabel(
            self._queue,
            text="♫  Die Party-Queue ist leer\nTitel aus dem Katalog oder einem Ordner hinzufügen",
            text_color=theme.TEXT_MUTED,
            font=(theme.FONT_FAMILY, 14),
        )

        self._compact_overlay_frame = ctk.CTkFrame(center, corner_radius=8)
        self._compact_overlay_frame.grid_columnconfigure(1, weight=1)
        self._compact_overlay_toggle = ctk.CTkButton(
            self._compact_overlay_frame,
            text="Jingles anzeigen ▾",
            width=130,
            height=32,
            fg_color=theme.SURFACE_RAISED,
            command=self._toggle_compact_overlays,
        )
        self._compact_overlay_toggle.grid(row=0, column=0, padx=(6, 4), pady=4)
        self._compact_overlay_status = ctk.CTkLabel(
            self._compact_overlay_frame, text="Kein Jingle aktiv", anchor="w"
        )
        self._compact_overlay_status.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self._compact_overlay_stop = ctk.CTkButton(
            self._compact_overlay_frame,
            text="■ Jingle stoppen",
            width=126,
            height=32,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=self._stop_overlay,
        )
        self._compact_overlay_stop.grid(row=0, column=2, padx=(4, 6), pady=4)
        self._compact_overlay_stop.grid_remove()
        self._compact_overlay_pads = ctk.CTkFrame(
            self._compact_overlay_frame, fg_color="transparent"
        )
        self._compact_overlay_pads.grid(
            row=1, column=0, columnspan=3, padx=5, pady=(0, 5), sticky="ew"
        )
        self._compact_overlay_pad_buttons: list[ctk.CTkButton] = []
        self._compact_overlay_pad_tooltips: list[Tooltip] = []
        for position in range(1, 7):
            self._compact_overlay_pads.grid_columnconfigure(position - 1, weight=1)
            button = ctk.CTkButton(
                self._compact_overlay_pads,
                text=f"{position} · frei",
                height=32,
                command=lambda selected=position: self._start_overlay_favorite(selected),
            )
            button.grid(row=0, column=position - 1, padx=2, sticky="ew")
            self._compact_overlay_pad_buttons.append(button)
            tooltip = Tooltip(button, f"Favoritenplatz {position} ist nicht belegt")
            self._compact_overlay_pad_tooltips.append(tooltip)
            self._static_tooltips.append(tooltip)
        self._compact_overlay_pads.grid_remove()
        self._compact_overlay_frame.grid_remove()

        mixer_container = ctk.CTkFrame(self, corner_radius=12)
        self._mixer_container = mixer_container
        mixer_container.grid(row=2, column=0, columnspan=3, padx=16, pady=(8, 16), sticky="ew")
        mixer_container.grid_columnconfigure(0, weight=1)
        self._mixer_toggle = ctk.CTkButton(
            mixer_container,
            text="Mixer einblenden ▼",
            height=30,
            fg_color="transparent",
            command=self._toggle_mixer_panel,
        )
        self._mixer_toggle.grid(row=0, column=0, padx=8, pady=4, sticky="ew")
        self._mixer_overlay_stop = ctk.CTkButton(
            mixer_container,
            text="■ Stop",
            width=84,
            height=30,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            command=self._stop_overlay,
        )
        self._mixer_overlay_stop.grid(row=0, column=1, padx=(0, 8), pady=4)
        self._mixer_overlay_stop.grid_remove()
        self._static_tooltips.extend(
            (
                Tooltip(
                    self._mixer_toggle,
                    "Mixer öffnen oder schließen; aktive Jingles bleiben hier sichtbar",
                ),
                Tooltip(
                    self._mixer_overlay_stop,
                    "Aktiven Jingle mit kurzem Sicherheitsfade sofort stoppen",
                ),
            )
        )
        self._mixer_panel = ctk.CTkFrame(mixer_container, fg_color="transparent")
        self._mixer_panel.grid(row=1, column=0, sticky="ew")
        mixer = self._mixer_panel
        mixer.grid_columnconfigure(0, weight=1, uniform="mixer_groups")
        mixer.grid_columnconfigure(1, weight=1, uniform="mixer_groups")

        playback_group = ctk.CTkFrame(mixer, corner_radius=8)
        playback_group.grid(row=0, column=0, padx=(12, 6), pady=(4, 6), sticky="nsew")
        playback_group.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            playback_group,
            text="WIEDERGABE UND MIXER",
            font=(theme.FONT_FAMILY, 13, "bold"),
        ).grid(row=0, column=0, columnspan=4, padx=12, pady=(10, 6), sticky="w")
        self._master = ctk.CTkSlider(playback_group, from_=0, to=1, command=self._master_changed)
        self._master.grid(row=1, column=0, columnspan=2, padx=(12, 6), pady=5, sticky="ew")
        self._master_label = ctk.CTkLabel(playback_group, text="Master 80%", width=82)
        self._master_label.grid(row=1, column=2, padx=4, pady=5)
        self._mute_button = ctk.CTkButton(
            playback_group, text="Stumm", width=72, command=self._toggle_mute
        )
        self._mute_button.grid(row=1, column=3, padx=(4, 12), pady=5)
        self._player_mode = ctk.CTkSegmentedButton(
            playback_group,
            values=["MANUELL", "HALBAUTOMATISCH", "AUTOMATISCH"],
            command=self._player_mode_changed,
        )
        self._player_mode.set("HALBAUTOMATISCH")
        self._player_mode.grid(row=2, column=0, columnspan=4, padx=12, pady=5, sticky="ew")
        ctk.CTkLabel(playback_group, text="Fade-Dauer").grid(row=3, column=0, padx=(12, 4), pady=5)
        self._fade_duration = ctk.CTkSlider(
            playback_group,
            from_=1,
            to=30,
            number_of_steps=29,
            command=self._fade_duration_changed,
        )
        self._fade_duration.set(5)
        self._fade_duration.grid(row=3, column=1, padx=4, pady=5, sticky="ew")
        self._fade_duration_label = ctk.CTkLabel(playback_group, text="5 s", width=44)
        self._fade_duration_label.grid(row=3, column=2, padx=4, pady=5)
        self._fade_stop_switch = ctk.CTkSwitch(
            playback_group,
            text="Nach Fade-out stoppen",
            command=self._fade_stop_changed,
        )
        self._fade_stop_switch.grid(
            row=4, column=0, columnspan=2, padx=12, pady=(5, 10), sticky="w"
        )
        self._fullscreen_start_switch = ctk.CTkSwitch(
            playback_group,
            text="Vollbild beim Start",
            command=self._fullscreen_start_changed,
        )
        self._fullscreen_start_switch.grid(
            row=4, column=2, columnspan=2, padx=(4, 12), pady=(5, 10), sticky="w"
        )

        options_group = ctk.CTkFrame(mixer, corner_radius=8)
        options_group.grid(row=0, column=1, padx=(6, 12), pady=(4, 6), sticky="nsew")
        options_group.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            options_group,
            text="PROGRAMM- UND STARTOPTIONEN",
            font=(theme.FONT_FAMILY, 13, "bold"),
        ).grid(row=0, column=0, columnspan=3, padx=12, pady=(10, 6), sticky="w")
        self._restore_session_switch = ctk.CTkSwitch(
            options_group,
            text="Letzte Session wiederherstellen",
            command=self._restore_session_changed,
        )
        self._restore_session_switch.grid(
            row=1, column=0, columnspan=3, padx=12, pady=5, sticky="w"
        )
        self._file_browser_switch = ctk.CTkSwitch(
            options_group,
            text="Dateibrowser anzeigen",
            command=self._file_browser_changed,
        )
        self._file_browser_switch.grid(row=2, column=0, columnspan=3, padx=12, pady=5, sticky="w")
        self._production_mode_switch = ctk.CTkSwitch(
            options_group,
            text="Produktionsmodus – Diagnostik und Analyse aus (nach Neustart)",
            command=self._production_mode_changed,
        )
        self._production_mode_switch.grid(
            row=3, column=0, columnspan=3, padx=12, pady=5, sticky="w"
        )
        audio_device_frame = ctk.CTkFrame(options_group, fg_color="transparent")
        audio_device_frame.grid(row=4, column=0, columnspan=3, padx=12, pady=5, sticky="ew")
        ctk.CTkLabel(audio_device_frame, text="Audioausgabe:").pack(side="left", padx=(0, 8))
        self._audio_device_menu = ctk.CTkOptionMenu(
            audio_device_frame,
            values=["Systemstandard"],
            command=self._audio_device_changed,
        )
        self._audio_device_menu.pack(side="left", fill="x", expand=True)
        self._audio_device_retry_button = ctk.CTkButton(
            audio_device_frame,
            text="Erneut anwenden",
            width=120,
            state="disabled",
            command=self._retry_audio_output_device,
        )
        self._audio_device_retry_button.pack(side="left", padx=(10, 0))
        self._audio_device_confirm_button = ctk.CTkButton(
            audio_device_frame,
            text="Ausgabe bestätigen",
            width=130,
            state="disabled",
            command=self._confirm_audio_output_device,
        )
        self._audio_device_confirm_button.pack(side="left", padx=(6, 0))
        ctk.CTkButton(
            audio_device_frame,
            text="Normalisierung…",
            width=130,
            command=self._edit_normalization_settings,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkButton(
            options_group,
            text="System / Externe Programme…",
            command=self._show_external_program_settings,
        ).grid(row=10, column=0, columnspan=3, padx=12, pady=(6, 10), sticky="ew")
        self._audio_device_recovery_label = ctk.CTkLabel(
            options_group,
            text="Audioausgabe bereit",
            anchor="w",
        )
        self._audio_device_recovery_label.grid(
            row=5, column=0, columnspan=2, padx=12, pady=(0, 8), sticky="ew"
        )
        global_recovery_frame = ctk.CTkFrame(
            options_group,
            border_width=2,
            border_color=("#B91C1C", "#EF4444"),
        )
        global_recovery_frame.grid(row=5, column=2, padx=(6, 12), pady=(0, 8), sticky="e")
        ctk.CTkLabel(
            global_recovery_frame,
            text="⚠ GLOBALE MASSNAHME",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=("#991B1B", "#FCA5A5"),
        ).pack(padx=8, pady=(5, 2))
        self._global_audio_recovery_button = ctk.CTkButton(
            global_recovery_frame,
            text="Globale Audio-Reparatur…",
            width=170,
            fg_color=("#B91C1C", "#DC2626"),
            hover_color=("#991B1B", "#B91C1C"),
            command=self._request_global_audio_recovery,
        )
        self._global_audio_recovery_button.pack(padx=8, pady=(0, 7))
        self._recovery_return_requirements_label = ctk.CTkLabel(
            options_group,
            text="",
            anchor="w",
            justify="left",
            font=(theme.FONT_FAMILY, 12),
        )
        self._recovery_return_requirements_label.grid(
            row=6, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="ew"
        )
        self._recovery_return_requirements_label.grid_remove()
        self._recovery_resume_button = ctk.CTkButton(
            options_group,
            text="Automatik sicher fortsetzen…",
            state="disabled",
            command=self._request_recovery_automatic_resume,
        )
        self._recovery_resume_button.grid(
            row=7, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="ew"
        )
        self._recovery_resume_button.grid_remove()
        self._unresolved_incident_frame = ctk.CTkFrame(
            options_group,
            border_width=2,
            border_color=("#B45309", "#F59E0B"),
        )
        self._unresolved_incident_frame.grid_columnconfigure(0, weight=1)
        self._unresolved_incident_title = ctk.CTkLabel(
            self._unresolved_incident_frame,
            text="",
            anchor="w",
            font=(theme.FONT_FAMILY, 13, "bold"),
            text_color=("#991B1B", "#FCA5A5"),
        )
        self._unresolved_incident_title.grid(row=0, column=0, padx=10, pady=(8, 2), sticky="ew")
        self._unresolved_incident_summary = ctk.CTkLabel(
            self._unresolved_incident_frame,
            text="",
            anchor="w",
            justify="left",
        )
        self._unresolved_incident_summary.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")
        ctk.CTkButton(
            self._unresolved_incident_frame,
            text="Als geprüft schließen…",
            width=150,
            command=self._review_unresolved_incident,
        ).grid(row=0, column=1, padx=10, pady=(8, 3))
        ctk.CTkButton(
            self._unresolved_incident_frame,
            text="Nur ausblenden",
            width=150,
            fg_color="transparent",
            command=self._unresolved_incident_frame.grid_remove,
        ).grid(row=1, column=1, padx=10, pady=(3, 8))
        self._unresolved_incident_frame.grid(
            row=7, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="ew"
        )
        self._unresolved_incident_frame.grid_remove()
        self._emergency_profile_labels = {
            "Alles stumm": EmergencyActionProfile.MUTE_ALL,
            "Beide Decks stoppen": EmergencyActionProfile.STOP_ALL,
            "Notfalltitel abspielen": EmergencyActionProfile.PLAY_EMERGENCY,
            "Sicher zurücksetzen": EmergencyActionProfile.SAFE_RESET,
        }
        emergency_action_frame = ctk.CTkFrame(
            options_group,
            border_width=2,
            border_color=("#991B1B", "#EF4444"),
        )
        emergency_action_frame.grid(
            row=8, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="ew"
        )
        emergency_action_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            emergency_action_frame,
            text="NOTFALLAKTION",
            font=(theme.FONT_FAMILY, 13, "bold"),
            text_color=("#991B1B", "#FCA5A5"),
        ).grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self._emergency_profile_menu = ctk.CTkOptionMenu(
            emergency_action_frame,
            values=list(self._emergency_profile_labels),
            command=self._emergency_profile_changed,
        )
        self._emergency_profile_menu.grid(row=0, column=1, padx=8, pady=10, sticky="ew")
        self._emergency_hold_button = ctk.CTkButton(
            emergency_action_frame,
            text="1 Sekunde halten",
            width=150,
            fg_color=("#B91C1C", "#DC2626"),
            hover_color=("#991B1B", "#B91C1C"),
        )
        self._emergency_hold_button.grid(row=0, column=2, padx=10, pady=10)
        self._emergency_hold_after_id: str | None = None
        self._emergency_hold_triggered = False
        self._emergency_hold_button.bind("<ButtonPress-1>", self._begin_emergency_hold, add="+")
        self._emergency_hold_button.bind("<ButtonRelease-1>", self._cancel_emergency_hold, add="+")
        self._emergency_dashboard_frame = ctk.CTkFrame(options_group)
        self._emergency_dashboard_frame.grid(
            row=9, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="ew"
        )
        self._emergency_dashboard_label = ctk.CTkLabel(
            self._emergency_dashboard_frame,
            text="NOTFALLSTATUS wird geladen…",
            anchor="w",
            justify="left",
            font=(theme.FONT_FAMILY, 12),
        )
        self._emergency_dashboard_label.pack(fill="x", padx=10, pady=8)
        emergency_recovery_actions = ctk.CTkFrame(
            self._emergency_dashboard_frame, fg_color="transparent"
        )
        emergency_recovery_actions.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(
            emergency_recovery_actions,
            text="DECKBEZOGENE MASSNAHMEN",
            font=(theme.FONT_FAMILY, 11, "bold"),
        ).pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            emergency_recovery_actions,
            text="Deck A reparieren…",
            command=lambda: self._request_deck_recovery("A"),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            emergency_recovery_actions,
            text="Deck B reparieren…",
            command=lambda: self._request_deck_recovery("B"),
        ).pack(side="left")
        for text, media_type, loop in (
            ("Pausenmusik (Schleife)…", EmergencyMediaType.BREAK_MUSIC, True),
            ("Jingle…", EmergencyMediaType.JINGLE, False),
            ("Ansage…", EmergencyMediaType.ANNOUNCEMENT, False),
        ):
            ctk.CTkButton(
                emergency_recovery_actions,
                text=text,
                command=lambda kind=media_type, repeat=loop: self._request_emergency_media(
                    kind, loop=repeat
                ),
            ).pack(side="left", padx=(6, 0))
        immediate_replace_actions = ctk.CTkFrame(
            self._emergency_dashboard_frame,
            fg_color=("#FEE2E2", "#450A0A"),
            border_width=1,
            border_color=("#B91C1C", "#EF4444"),
        )
        immediate_replace_actions.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(
            immediate_replace_actions,
            text="UNZUMUTBARE AUSGABE SOFORT ERSETZEN",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=("#991B1B", "#FCA5A5"),
        ).pack(side="left", padx=8, pady=7)
        for deck_id in ("A", "B"):
            ctk.CTkButton(
                immediate_replace_actions,
                text=f"Deck {deck_id} sofort ersetzen…",
                fg_color=("#B91C1C", "#DC2626"),
                hover_color=("#991B1B", "#B91C1C"),
                command=lambda selected=deck_id: self._request_immediate_replace(selected),
            ).pack(side="left", padx=(0, 6), pady=6)

        diagnostic_group = ctk.CTkFrame(mixer, corner_radius=8)
        diagnostic_group.grid(row=1, column=0, columnspan=2, padx=12, pady=6, sticky="ew")
        diagnostic_group.grid_columnconfigure(0, weight=1)
        self._diagnostic_expanded = False
        self._diagnostic_toggle = ctk.CTkButton(
            diagnostic_group,
            text=_diagnostic_toggle_text(False),
            height=30,
            fg_color="transparent",
            command=self._toggle_diagnostic_panel,
        )
        self._diagnostic_toggle.grid(row=0, column=0, padx=8, pady=4, sticky="ew")
        diagnostic_frame = ctk.CTkFrame(diagnostic_group, fg_color="transparent")
        self._diagnostic_frame = diagnostic_frame
        diagnostic_frame.grid(row=1, column=0, padx=12, pady=(2, 10), sticky="ew")
        ctk.CTkLabel(
            diagnostic_frame,
            text=(
                "Hinweis: Laufzeitanalysen benötigen keine Administratorrechte. "
                "Produktionsmodus in den Einstellungen ausschalten und DeckRelay neu starten. "
                "Die portable Version muss in einem beschreibbaren Ordner liegen."
            ),
            anchor="w",
            justify="left",
            wraplength=900,
            text_color=("#7C2D12", "#FDBA74"),
        ).pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(diagnostic_frame, text="Diagnoseszenario:").pack(side="left", padx=(0, 8))
        self._diagnostic_context_labels = {
            "Leerlauf": "idle",
            "Normale Wiedergabe": "normal_playback",
            "Crossfade": "crossfade",
            "Queue-Stresstest": "queue_stress",
            "NAS-Wiedergabe": "nas_playback",
            "Cue-Vorschau": "cue_preview",
            "Verzeichnisimport": "directory_import",
            "Datenbankverzögerung": "database_delay",
            "Speicher-Langzeittest": "memory_stress",
        }
        self._diagnostic_context = ctk.CTkOptionMenu(
            diagnostic_frame, values=list(self._diagnostic_context_labels)
        )
        self._diagnostic_context.set("Leerlauf")
        self._diagnostic_context.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._database_delay_entry = ctk.CTkEntry(
            diagnostic_frame, width=72, placeholder_text="1000 ms"
        )
        self._database_delay_entry.insert(0, "1000")
        self._database_delay_entry.pack(side="left", padx=(0, 6))
        self._diagnostic_start_button = ctk.CTkButton(
            diagnostic_frame,
            text="Test starten/reset",
            width=115,
            command=self._begin_diagnostic_scenario,
        )
        self._diagnostic_start_button.pack(side="left", padx=(0, 6))
        self._diagnostic_stop_button = ctk.CTkButton(
            diagnostic_frame,
            text="Test beenden + Bericht",
            command=self._save_diagnostic_report,
        )
        self._diagnostic_stop_button.pack(side="left")
        self._diagnostic_frame.grid_remove()

        def manage_overlays() -> None:
            self._manage_overlays()

        self._overlay_panel = OverlayPanel(
            mixer,
            on_start=self._start_selected_overlay,
            on_fade_out=self._fade_out_overlay,
            on_stop=self._stop_overlay,
            on_manage=manage_overlays,
            on_favorite=self._start_overlay_favorite,
            on_edit_favorite=self._edit_overlay_favorite,
            on_remove_favorite=self._remove_overlay_favorite,
            on_category=self._overlay_category_changed,
            on_selection=self._overlay_selection_changed,
            favorite_hosts=(self.deck_a.overlay_pad_host, self.deck_b.overlay_pad_host),
        )
        self._overlay_panel.grid(
            row=2,
            column=0,
            columnspan=2,
            padx=12,
            pady=(6, 10),
            sticky="ew",
        )
        self._overlay_panel.render(OverlayViewModel())
        ctk.CTkLabel(
            options_group,
            text=(
                "Tasten: F1/F2 Deck A · F3/F4 Deck B · "
                "Strg+←/→ Crossfader · Strg+F Suche · F11 Vollbild"
            ),
            text_color="#aaaaaa",
            wraplength=580,
            justify="left",
        ).grid(row=5, column=0, columnspan=3, padx=12, pady=(5, 10), sticky="w")
        self._mixer_panel.grid_remove()
        self._bind_gui("<Configure>", "responsive_layout", self._window_resized)
        initial_state = PresentationState(
            preference=self._presentation_initial_preference,
            workspace=self._presentation_initial_workspace,
            compact_content_available=True,
        )
        self._presentation_coordinator = MainWindowPresentationCoordinator(
            initial_state,
            LayoutPolicy(),
            self.schedule,
            self._apply_presentation_state,
            self._presentation_interaction_active,
        )
        self._bind_gui(
            "<FocusIn>",
            "presentation_interaction_end",
            lambda _event: (
                self._presentation_coordinator.interaction_ended()
                if self._presentation_coordinator is not None
                else None
            ),
        )
        self._presentation_coordinator.reevaluate(
            self._logical_client_size(self.winfo_width(), self.winfo_height()),
            reason="startup",
        )
        self.schedule(2000, self._finish_presentation_startup)
        self.schedule(2000, self._poll_display_environment)
        self._request_focus_setup(self)

    def _display_snapshot(self) -> DisplaySnapshot:
        if self._display_provider is not None:
            return self._display_provider.snapshot(self.winfo_id())
        if sys.platform.startswith("win"):
            return WindowsDisplayProvider().snapshot(self.winfo_id())
        return DisplaySnapshot(
            (
                MonitorGeometry(
                    Rect(0, 0, self.winfo_screenwidth(), self.winfo_screenheight()),
                    Rect(0, 0, self.winfo_screenwidth(), self.winfo_screenheight()),
                    1.0,
                    True,
                ),
            )
        )

    def _logical_client_size(self, width: int, height: int) -> LogicalClientSize:
        return logical_client_size(width, height, self._get_window_scaling())

    @staticmethod
    def _snapshot_fingerprint(snapshot: DisplaySnapshot) -> tuple[object, ...]:
        return tuple(
            (
                monitor.bounds,
                monitor.work_area,
                round(monitor.dpi_scale, 4),
                monitor.primary,
            )
            for monitor in snapshot.monitors
        ) + (snapshot.insets,)

    def _apply_initial_window_geometry(self, saved_geometry: str | None) -> None:
        snapshot = self._display_snapshot()
        resolved = resolve_window_geometry(saved_geometry, snapshot)
        self.minsize(resolved.minimum_width, resolved.minimum_height)
        self.geometry(resolved.tk_geometry)
        self._display_fingerprint = self._snapshot_fingerprint(snapshot)
        self._log_window_geometry("startup", saved_geometry, snapshot, resolved)

    def _current_stored_geometry(self, snapshot: DisplaySnapshot) -> StoredWindowGeometry | None:
        current = parse_tk_geometry(self.geometry(), 1.0)
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

    def _ensure_window_in_work_area(self, trigger: str) -> None:
        if bool(self.attributes("-fullscreen")):
            return
        try:
            snapshot = self._display_snapshot()
        except OSError:
            self._logger.exception("Fensterarbeitsfläche konnte nicht aktualisiert werden")
            return
        current = self._current_stored_geometry(snapshot)
        if current is None:
            return
        resolved = resolve_window_geometry(current.serialize(), snapshot)
        self._display_fingerprint = self._snapshot_fingerprint(snapshot)
        if resolved.reasons:
            self.minsize(resolved.minimum_width, resolved.minimum_height)
            self.geometry(resolved.tk_geometry)
            self._log_window_geometry(trigger, current.serialize(), snapshot, resolved)

    def _poll_display_environment(self) -> None:
        try:
            snapshot = self._display_snapshot()
        except OSError:
            self._logger.exception("Fensterarbeitsfläche konnte nicht abgefragt werden")
        else:
            fingerprint = self._snapshot_fingerprint(snapshot)
            if fingerprint != self._display_fingerprint:
                self._ensure_window_in_work_area("display_change")
                if self._presentation_coordinator is not None:
                    self._presentation_coordinator.reevaluate(
                        self._logical_client_size(self.winfo_width(), self.winfo_height()),
                        reason="display-change",
                    )
        self.schedule(2000, self._poll_display_environment)

    def _schedule_window_geometry_save(self) -> None:
        if self._save_window_geometry is None:
            return
        pending = self._window_geometry_after_id
        if pending is not None:
            try:
                self.after_cancel(pending)
            except TclError:
                pass
            self._scheduled_after_ids.discard(pending)

        def save() -> None:
            self._window_geometry_after_id = None
            self._ensure_window_in_work_area("configure")
            self._persist_window_geometry()

        self._window_geometry_after_id = str(self.schedule(400, save))

    def _persist_window_geometry(self) -> None:
        if self._save_window_geometry is None or bool(self.attributes("-fullscreen")):
            return
        try:
            snapshot = self._display_snapshot()
            geometry = self._current_stored_geometry(snapshot)
        except OSError:
            return
        if geometry is not None:
            self._save_window_geometry(geometry.serialize())

    def _log_window_geometry(
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
                        m.bounds.left,
                        m.bounds.top,
                        m.bounds.right,
                        m.bounds.bottom,
                    ),
                    "work": (
                        m.work_area.left,
                        m.work_area.top,
                        m.work_area.right,
                        m.work_area.bottom,
                    ),
                    "dpi_scale": round(m.dpi_scale, 3),
                    "primary": m.primary,
                }
                for m in snapshot.monitors
            ],
            stored,
            resolved.tk_geometry,
            resolved.reasons or ("unchanged",),
        )

    def bind_controller(self, controller: MainController) -> None:
        self._controller = controller
        self._set_workspace_split(controller.workspace_catalog_ratio(), persist=False)
        selected_profile = controller.emergency_action_profile()
        selected_label = next(
            label
            for label, profile in self._emergency_profile_labels.items()
            if profile == selected_profile
        )
        self._emergency_profile_menu.set(selected_label)
        for preset_key, name in controller.equalizer_presets():
            self._equalizer_preset_keys[name] = preset_key
        self._queue_equalizer_menu.configure(values=list(self._equalizer_preset_keys))
        self._playlist_equalizer_menu.configure(values=list(self._equalizer_preset_keys))
        self.deck_a.bind_controls(
            controller.seek,
            controller.set_deck_volume,
            controller.fade,
            controller.cancel_fade,
            controller.import_file,
            self._open_deck_equalizer,
        )
        self.deck_b.bind_controls(
            controller.seek,
            controller.set_deck_volume,
            controller.fade,
            controller.cancel_fade,
            controller.import_file,
            self._open_deck_equalizer,
        )

    def _set_workspace_split(self, catalog_ratio: float, *, persist: bool = True) -> None:
        """Resize the catalog/queue list rows while keeping both usable."""
        ratio = min(0.8, max(0.2, float(catalog_ratio)))
        self._workspace_catalog_ratio = ratio
        catalog_weight = max(1, round(ratio * 100))
        self._center_panel.grid_rowconfigure(
            2, weight=catalog_weight, minsize=80, uniform="list_workspace"
        )
        self._center_panel.grid_rowconfigure(
            9, weight=100 - catalog_weight, minsize=80, uniform="list_workspace"
        )
        if persist and self._controller is not None:
            self._controller.set_workspace_catalog_ratio(ratio)

    def _drag_workspace_split(self, event: Any) -> None:
        center = self._center_panel
        height = max(1, center.winfo_height())
        relative_y = event.y_root - center.winfo_rooty()
        self._set_workspace_split(relative_y / height, persist=False)

    def _finish_workspace_split_drag(self, _event: Any) -> None:
        if self._controller is not None:
            self._controller.set_workspace_catalog_ratio(self._workspace_catalog_ratio)

    def bind_system_diagnostics(
        self,
        initial_report: SystemDiagnosticReport,
        check: Callable[[], SystemDiagnosticReport],
        export: Callable[[SystemDiagnosticReport, DiagnosticExportMode], object],
    ) -> None:
        self._system_diagnostic_report = initial_report
        self._system_diagnostic_check = check
        self._system_diagnostic_export = export

    def bind_external_program_settings(
        self,
        settings: Callable[[], DependencySettings],
        initial_report: SystemDiagnosticReport,
        check: Callable[[], SystemDiagnosticReport],
        select_vlc: Callable[[str], SystemDiagnosticReport],
        select_ffmpeg: Callable[[str], SystemDiagnosticReport],
        reset_vlc: Callable[[], SystemDiagnosticReport],
        reset_ffmpeg: Callable[[], SystemDiagnosticReport],
        can_change_vlc: Callable[[], bool],
        can_change_ffmpeg: Callable[[], bool],
        capability_snapshots: CapabilitySnapshotState,
    ) -> None:
        self._external_program_binding = (
            settings,
            initial_report,
            check,
            select_vlc,
            select_ffmpeg,
            reset_vlc,
            reset_ffmpeg,
            can_change_vlc,
            can_change_ffmpeg,
            capability_snapshots,
        )

    def _show_external_program_settings(self) -> None:
        if self._external_program_binding is None:
            return
        current = self._external_program_dialog
        if current is not None:
            try:
                if current.winfo_exists():
                    current.focus_force()
                    return
            except (RuntimeError, TclError):
                pass
        self._external_program_dialog = ExternalProgramsDialog(
            self,
            *self._external_program_binding,  # type: ignore[arg-type]
        )

    def _show_help_menu(self, button: Any) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Tempoanalyse…", command=lambda: show_tempo_analysis_help(self))
        menu.add_separator()
        menu.add_command(label="Systemdiagnose", command=self._show_system_diagnostics)
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def _show_extras_menu(self, button: Any) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Datenbank und Sicherung…", command=self._show_database_backup)
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def bind_backup_restore(
        self, controller: BackupRestoreController, default_backup_directory: Path
    ) -> None:
        self._backup_restore_controller = controller
        self._default_backup_directory = default_backup_directory

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    def _show_database_backup(self) -> None:
        if self._backup_restore_controller is None:
            return
        current = self._database_backup_dialog
        if current is not None:
            try:
                if current.winfo_exists():
                    current.focus_force()
                    return
            except (RuntimeError, TclError):
                pass
        controller = self._backup_restore_controller
        self._database_backup_dialog_generation += 1
        generation = self._database_backup_dialog_generation

        def close() -> None:
            if self._database_backup_dialog_generation == generation:
                self._database_backup_dialog_generation += 1
                self._database_backup_dialog = None

        self._database_backup_dialog = DatabaseBackupDialog(
            self,
            lambda: self._start_database_operation(self._request_default_backup),
            lambda: self._start_database_operation(self._request_backup),
            lambda: self._start_database_operation(self._request_restore),
            lambda: self._start_database_operation(self._request_playlist_export),
            self._request_playlist_music_directory,
            lambda: self._start_database_operation(self._request_playlist_import_preview),
            lambda: self._start_database_operation(self._request_equalizer_export),
            lambda: self._start_database_operation(self._request_equalizer_import_preview),
            lambda: self._start_database_operation(self._request_overlay_export),
            lambda: self._start_database_operation(self._request_overlay_import_preview),
            lambda: self._start_database_operation(self._request_media_path_remap_preview),
            lambda: self._start_database_operation(controller.start_quick_check),
            lambda: self._start_database_operation(controller.start_integrity_check),
            lambda: self._start_database_operation(controller.start_analyze),
            lambda: self._start_database_operation(self._request_vacuum),
            lambda: self._start_database_operation(self._request_reindex),
            controller.destructive_maintenance_safety,
            controller.last_manual_backup(),
            close,
        )

    def _start_database_operation(self, action: Callable[[], bool]) -> bool:
        started = action()
        if started:
            self._database_operation_generation = self._database_backup_dialog_generation
        return started

    def _request_playlist_export(self) -> bool:
        controller = self._backup_restore_controller
        menu = self.__dict__.get("_saved_queue_menu")
        selected_id = self._saved_queue_ids.get(menu.get()) if menu is not None else None
        if controller is None or selected_id is None:
            show_silent_message(
                self,
                "Keine Playlist ausgewählt",
                "Bitte zuerst eine gespeicherte Playlist auswählen.",
                error=True,
            )
            return False
        destination = filedialog.asksaveasfilename(
            title="Playlist exportieren",
            defaultextension=".json",
            filetypes=(
                ("DeckRelay-Playlist", "*.json"),
                ("M3U8-Playlist", "*.m3u8"),
            ),
        )
        if not destination:
            return False
        path = Path(destination)
        format = (
            PlaylistTransferFormat.M3U8
            if path.suffix.casefold() in {".m3u", ".m3u8"}
            else PlaylistTransferFormat.JSON
        )
        return controller.start_playlist_export(selected_id, path, format)

    def _request_playlist_import_preview(self) -> bool:
        controller = self._backup_restore_controller
        if controller is None:
            return False
        selected = filedialog.askopenfilename(
            title="Playlist importieren und prüfen",
            filetypes=(
                ("Playlistdateien", "*.json *.m3u *.m3u8"),
                ("DeckRelay-Playlist", "*.json"),
                ("M3U/M3U8-Playlist", "*.m3u *.m3u8"),
            ),
        )
        if not selected:
            return False
        source = Path(selected)
        format = (
            PlaylistTransferFormat.M3U8
            if source.suffix.casefold() in {".m3u", ".m3u8"}
            else PlaylistTransferFormat.JSON
        )
        return controller.start_playlist_import_preview(source, format)

    def _request_equalizer_export(self) -> bool:
        controller = self._backup_restore_controller
        choices = {
            name: key
            for name, key in self._equalizer_preset_keys.items()
            if key not in {"inherit", "disabled"}
        }
        if controller is None or not choices:
            return False
        name = simpledialog.askstring(
            "Equalizer-Preset exportieren",
            "Presetname eingeben:\n\n" + "\n".join(choices),
            parent=self,
        )
        if name is None:
            return False
        preset_key = choices.get(name.strip())
        if preset_key is None:
            show_silent_message(
                self,
                "Equalizer-Preset nicht gefunden",
                "Bitte einen Namen exakt aus der angezeigten Liste eingeben.",
                error=True,
            )
            return False
        destination = filedialog.asksaveasfilename(
            title="Equalizer-Preset exportieren",
            defaultextension=".json",
            filetypes=(("DeckRelay-Equalizer", "*.json"),),
        )
        return bool(destination) and controller.start_equalizer_export(
            preset_key, Path(destination)
        )

    def _request_playlist_music_directory(self) -> bool:
        """Start catalog/queue preparation without marking the database dialog busy."""
        self._choose_queue_directory()
        return False

    def _request_equalizer_import_preview(self) -> bool:
        controller = self._backup_restore_controller
        if controller is None:
            return False
        selected = filedialog.askopenfilename(
            title="Equalizer-Preset importieren und prüfen",
            filetypes=(("DeckRelay-Equalizer", "*.json"),),
        )
        return bool(selected) and controller.start_equalizer_import_preview(Path(selected))

    def _request_overlay_export(self) -> bool:
        controller = self._backup_restore_controller
        if controller is None:
            return False
        destination = filedialog.asksaveasfilename(
            title="Overlays und Jingles exportieren",
            defaultextension=".json",
            filetypes=(("DeckRelay-Overlays", "*.json"),),
        )
        return bool(destination) and controller.start_overlay_export(Path(destination))

    def _request_overlay_import_preview(self) -> bool:
        controller = self._backup_restore_controller
        if controller is None:
            return False
        selected = filedialog.askopenfilename(
            title="Overlays und Jingles importieren und prüfen",
            filetypes=(("DeckRelay-Overlays", "*.json"),),
        )
        return bool(selected) and controller.start_overlay_import_preview(Path(selected))

    def _request_media_path_remap_preview(self) -> bool:
        controller = self._backup_restore_controller
        if controller is None:
            return False
        old_base = simpledialog.askstring(
            "Alte Medienbasis",
            "Bisheriger absoluter Windows- oder UNC-Basispfad\n(z. B. D:\\Musik):",
            parent=self,
        )
        if old_base is None:
            return False
        new_base = simpledialog.askstring(
            "Neue Medienbasis",
            "Neuer absoluter Windows- oder UNC-Basispfad\n(z. B. E:\\Musik):",
            parent=self,
        )
        if new_base is None:
            return False
        return controller.start_media_path_remap_preview(old_base, new_base)

    def _request_vacuum(self) -> bool:
        controller = self._backup_restore_controller
        if controller is None:
            return False
        if not ask_silent_yes_no(
            self,
            "VACUUM wirklich ausführen?",
            "VACUUM baut die SQLite-Datenbank vollständig neu auf. Beide Decks müssen "
            "gestoppt und alle Audioaktionen beendet sein. Vorher wird automatisch ein "
            "validiertes Safety-Backup erstellt.\n\nVACUUM jetzt starten?",
        ):
            return False
        return controller.start_vacuum()

    def _request_reindex(self) -> bool:
        controller = self._backup_restore_controller
        if controller is None:
            return False
        if not ask_silent_yes_no(
            self,
            "REINDEX wirklich ausführen?",
            "REINDEX erstellt alle SQLite-Indizes neu. Beide Decks müssen gestoppt und "
            "alle Audioaktionen beendet sein. Vorher wird automatisch ein validiertes "
            "Safety-Backup erstellt.\n\nREINDEX jetzt starten?",
        ):
            return False
        return controller.start_reindex()

    def _request_backup(self) -> bool:
        if self._backup_restore_controller is None:
            return False
        selected = filedialog.askdirectory(title="Zielordner für DeckRelay-Backup wählen")
        if selected:
            return self._backup_restore_controller.start_backup(Path(selected))
        return False

    def _request_default_backup(self) -> bool:
        if self._backup_restore_controller is None or self._default_backup_directory is None:
            return False
        return self._backup_restore_controller.start_backup(self._default_backup_directory)

    def _request_restore(self) -> bool:
        if self._backup_restore_controller is None:
            return False
        selected = filedialog.askopenfilename(
            title="DeckRelay-Backup wiederherstellen",
            filetypes=(
                ("DeckRelay-Backup", "*.partyplayer-backup"),
                ("Alle Dateien", "*.*"),
            ),
        )
        if not selected:
            return False
        if not ask_silent_yes_no(
            self,
            "Backup wirklich wiederherstellen?",
            "Beide Decks müssen gestoppt und alle Audioaktionen beendet sein. "
            "Unmittelbar vor dem Austausch wird automatisch ein Sicherheitsbackup erstellt.\n\n"
            "Nach erfolgreichem Restore muss DeckRelay neu gestartet werden.",
        ):
            return False
        safety_directory = Path(selected).resolve().parent / "safety-backups"
        return self._backup_restore_controller.start_restore(Path(selected), safety_directory)

    def show_backup_restore_result(self, result: BackupRestoreUiResult) -> None:
        dialog = self.__dict__.get("_database_backup_dialog")
        dialog_generation = self.__dict__.get("_database_backup_dialog_generation", 0)
        operation_generation = self.__dict__.get("_database_operation_generation", 0)
        current_dialog = dialog is not None and operation_generation == dialog_generation
        if current_dialog and dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.complete(result)
            except (RuntimeError, TclError):
                pass
        if result.operation is BackupRestoreOperation.PLAYLIST_IMPORT_PREVIEW:
            self._database_operation_generation = None
            if current_dialog and dialog is not None:
                self._handle_playlist_import_preview(result, dialog)
            return
        if result.operation is BackupRestoreOperation.MEDIA_PATH_REMAP_PREVIEW:
            self._database_operation_generation = None
            if current_dialog and dialog is not None:
                self._handle_media_path_remap_preview(result, dialog)
            return
        if result.operation is BackupRestoreOperation.EQUALIZER_IMPORT_PREVIEW:
            self._database_operation_generation = None
            if current_dialog and dialog is not None:
                self._handle_equalizer_import_preview(result, dialog)
            return
        if result.operation is BackupRestoreOperation.OVERLAY_IMPORT_PREVIEW:
            self._database_operation_generation = None
            if current_dialog and dialog is not None:
                self._handle_overlay_import_preview(result, dialog)
            return
        if result.state is not BackupRestoreUiState.BUSY:
            self._database_operation_generation = None
        if result.state is BackupRestoreUiState.RESTART_REQUIRED:
            restore = result.operation is BackupRestoreOperation.RESTORE
            safety = f"\n\nSicherheitsbackup: {result.path}" if restore and result.path else ""
            title = (
                "Restore abgeschlossen – Neustart erforderlich"
                if restore
                else "Pfad-Neuzuordnung abgeschlossen – Neustart erforderlich"
            )
            if ask_silent_yes_no(
                self,
                title,
                result.message + safety + "\n\nDeckRelay jetzt kontrolliert neu starten?",
            ):
                self._restart_requested = True
                self._dispose_resources()
                self.destroy()
            return
        if result.state is BackupRestoreUiState.COMPLETED:
            title = {
                BackupRestoreOperation.BACKUP: "Sicherung erfolgreich",
                BackupRestoreOperation.MAINTENANCE: "Datenbankwartung abgeschlossen",
                BackupRestoreOperation.PLAYLIST_EXPORT: "Playlist exportiert",
                BackupRestoreOperation.PLAYLIST_IMPORT: "Playlist importiert",
                BackupRestoreOperation.MEDIA_PATH_REMAP: "Medienpfade neu zugeordnet",
                BackupRestoreOperation.EQUALIZER_EXPORT: "Equalizer-Preset exportiert",
                BackupRestoreOperation.EQUALIZER_IMPORT: "Equalizer-Preset importiert",
                BackupRestoreOperation.OVERLAY_EXPORT: "Overlays/Jingles exportiert",
                BackupRestoreOperation.OVERLAY_IMPORT: "Overlays/Jingles importiert",
            }.get(result.operation, "Datenoperation abgeschlossen")
        else:
            title = "Backup/Restore/Wartung nicht ausgeführt"
        path = f"\n\nDatei: {result.path}" if result.path else ""
        message = result.message
        if (
            result.operation is BackupRestoreOperation.BACKUP
            and result.state is BackupRestoreUiState.COMPLETED
        ):
            message = "Die komplette Veranstaltungssicherung wurde erfolgreich erstellt."
        show_silent_message(
            self,
            title,
            message + path,
            error=result.state is not BackupRestoreUiState.COMPLETED,
        )
        if (
            result.operation is BackupRestoreOperation.EQUALIZER_IMPORT
            and result.state is BackupRestoreUiState.COMPLETED
        ):
            self._refresh_equalizer_presets()
        if (
            result.operation is BackupRestoreOperation.OVERLAY_IMPORT
            and result.state is BackupRestoreUiState.COMPLETED
        ):
            self.refresh_overlays()

    def _refresh_equalizer_presets(self) -> None:
        controller = self._controller
        if controller is None:
            return
        self._equalizer_preset_keys = {
            "Vererben": "inherit",
            "Equalizer aus": "disabled",
        }
        for preset_key, name in controller.equalizer_presets():
            self._equalizer_preset_keys[name] = preset_key
        self._queue_equalizer_menu.configure(values=list(self._equalizer_preset_keys))
        self._playlist_equalizer_menu.configure(values=list(self._equalizer_preset_keys))

    def _handle_equalizer_import_preview(
        self, result: BackupRestoreUiResult, dialog: DatabaseBackupDialog
    ) -> None:
        preview = result.equalizer_preview
        if preview is None or not preview.valid or preview.preset is None:
            show_silent_message(
                self,
                "Equalizer-Preset kann nicht geprüft werden",
                result.message,
                error=True,
            )
            return
        preset = preview.preset
        summary = (
            f"Preset: {preset.name}\n"
            f"Schlüssel: {preset.preset_id}\n"
            f"Vorverstärkung: {preset.preamp_db:g} dB\n"
            f"Bänder: {len(preset.curve)}\n"
            f"Konflikte: {len(preview.conflicts)}"
        )
        strategy: EqualizerConflictStrategy | None
        if preview.has_conflict:
            strategy = choose_equalizer_conflict(self, preview)
        else:
            strategy = (
                EqualizerConflictStrategy.ERROR
                if ask_silent_yes_no(
                    self,
                    "Equalizer-Preset importieren?",
                    summary + "\n\nDieses Preset jetzt importieren?",
                )
                else None
            )
        controller = self._backup_restore_controller
        if strategy is None or controller is None:
            return
        dialog.start_followup(
            "Equalizer-Preset importieren",
            lambda: self._start_database_operation(
                lambda: controller.start_equalizer_import(preview, strategy)
            ),
        )

    def _handle_overlay_import_preview(
        self, result: BackupRestoreUiResult, dialog: DatabaseBackupDialog
    ) -> None:
        preview = result.overlay_preview
        if preview is None or not preview.can_import:
            show_silent_message(
                self,
                "Overlay-Konfiguration kann nicht geprüft werden",
                result.message,
                error=True,
            )
            return
        favorites = sum(record.favorite_position is not None for record in preview.records)
        summary = (
            f"Definitionen: {len(preview.records)}\n"
            f"Favoriten/Shortcuts: {favorites}\n"
            f"Konflikte mit bestehendem Bestand: {len(preview.conflicts)}\n\n"
            "Audiodateien werden nicht kopiert. Gespeicherte Dateipfade bleiben Referenzen."
        )
        if preview.conflicts:
            answer = ask_silent_yes_no_cancel(
                self,
                "Overlay-Konflikte behandeln",
                summary
                + "\n\nJa: betroffene bestehende Definitionen ausdrücklich ersetzen."
                + "\nNein: bestehende Definitionen, Favoriten und Shortcuts behalten."
                + "\nAbbrechen: nichts importieren.",
            )
            strategy = (
                OverlayConflictStrategy.REPLACE_EXISTING
                if answer is True
                else OverlayConflictStrategy.KEEP_EXISTING if answer is False else None
            )
        else:
            strategy = (
                OverlayConflictStrategy.KEEP_EXISTING
                if ask_silent_yes_no(
                    self,
                    "Overlays/Jingles importieren?",
                    summary + "\n\nDiese Konfiguration jetzt importieren?",
                )
                else None
            )
        controller = self._backup_restore_controller
        if strategy is None or controller is None:
            return
        dialog.start_followup(
            "Overlays/Jingles importieren",
            lambda: self._start_database_operation(
                lambda: controller.start_overlay_import(preview, strategy)
            ),
        )

    def _handle_playlist_import_preview(
        self, result: BackupRestoreUiResult, dialog: DatabaseBackupDialog
    ) -> None:
        preview = result.playlist_preview
        if preview is None or not preview.valid:
            show_silent_message(
                self, "Playlist kann nicht geprüft werden", result.message, error=True
            )
            return
        summary = (
            f"Playlist: {preview.name}\n"
            f"Einträge: {preview.entry_count}\n"
            f"Duplikate: {preview.duplicate_count}\n"
            f"Unbekannte Pfade: {preview.unknown_path_count}\n"
            f"Namenskonflikt: {'ja' if preview.name_conflict else 'nein'}"
        )
        if not preview.can_import:
            examples = "\n".join(preview.unknown_path_examples)
            show_silent_message(
                self,
                "Zuerst Musikordner einlesen",
                summary + "\n\nDie Playlist verweist auf Musikdateien, die noch nicht unter "
                "demselben vollständigen Pfad im Katalog stehen.\n\n"
                "1. Schließen Sie diesen Hinweis.\n"
                "2. Wählen Sie „Musikordner jetzt einlesen (MP3/FLAC)…“.\n"
                "3. Importieren Sie danach die Playlist erneut.\n\n"
                "Liegt die Musik auf einem anderen Laufwerk oder unter einem anderen "
                "Basisordner, verwenden Sie zusätzlich „Medienpfade nach Rechnerwechsel "
                "neu zuordnen…“."
                + (f"\n\nNicht gefundene Beispielpfade:\n{examples}" if examples else ""),
                error=True,
            )
            return
        strategy: PlaylistConflictStrategy | None
        if preview.name_conflict:
            strategy = choose_playlist_conflict(self, preview)
        else:
            strategy = (
                PlaylistConflictStrategy.ERROR
                if ask_silent_yes_no(
                    self,
                    "Playlist importieren?",
                    summary + "\n\nDiese Playlist jetzt importieren?",
                )
                else None
            )
        controller = self._backup_restore_controller
        if strategy is None or controller is None:
            return
        dialog.start_followup(
            "Playlist importieren",
            lambda: self._start_database_operation(
                lambda: controller.start_playlist_import(preview, strategy)
            ),
        )

    def _handle_media_path_remap_preview(
        self, result: BackupRestoreUiResult, dialog: DatabaseBackupDialog
    ) -> None:
        preview = result.media_path_preview
        if preview is None or not preview.valid:
            show_silent_message(
                self,
                "Pfad-Neuzuordnung kann nicht geprüft werden",
                result.message,
                error=True,
            )
            return
        summary = (
            f"Alte Basis: {preview.old_base_path}\n"
            f"Neue Basis: {preview.new_base_path}\n\n"
            f"Katalogtitel: {preview.track_count}\n"
            f"Overlays: {preview.overlay_count}\n"
            f"Notfallhistorie: {preview.emergency_history_count}\n"
            f"Gesamt: {preview.affected_count}"
        )
        if not preview.can_commit:
            collisions = "\n".join(preview.collisions)
            detail = (
                f"\n\nZielkollisionen:\n{collisions}"
                if collisions
                else "\n\nKeine passenden Pfade."
            )
            show_silent_message(
                self,
                "Pfad-Neuzuordnung ist blockiert",
                summary + detail,
                error=True,
            )
            return
        examples = "\n".join(
            f"{change.old_path}  →  {change.new_path}" for change in preview.examples
        )
        if not ask_silent_yes_no(
            self,
            "Medienpfade wirklich neu zuordnen?",
            summary
            + (f"\n\nBeispiele:\n{examples}" if examples else "")
            + "\n\nEs werden ausschließlich Datenbankpfade geändert. "
            "Medien werden nicht verschoben oder kopiert. Jetzt ausführen?",
        ):
            return
        controller = self._backup_restore_controller
        if controller is None:
            return
        dialog.start_followup(
            "Medienpfade neu zuordnen",
            lambda: self._start_database_operation(
                lambda: controller.start_media_path_remap(preview)
            ),
        )

    def _show_system_diagnostics(self) -> None:
        if (
            self._system_diagnostic_report is None
            or self._system_diagnostic_check is None
            or self._system_diagnostic_export is None
        ):
            return
        current = self._system_diagnostic_dialog
        if current is not None:
            try:
                if current.winfo_exists():
                    current.focus_force()
                    return
            except (RuntimeError, TclError):
                pass
        self._system_diagnostic_dialog = SystemDiagnosticDialog(
            self,
            self._system_diagnostic_report,
            self._system_diagnostic_check,
            self._system_diagnostic_export,
        )

    def bind_overlay(
        self,
        controller: OverlayController,
        service: OverlayService,
    ) -> None:
        """Connect the already-created panel and publish one catalog snapshot."""

        self._overlay_controller = controller
        self._overlay_service = service
        self.refresh_overlays()

    def refresh_overlays(self) -> None:
        """Refresh configuration only after explicit repository changes."""

        if self._overlay_service is None:
            return
        self._overlay_snapshot = self._overlay_service.snapshot()
        categories = ("Alle", *self._overlay_snapshot.categories)
        self._overlay_panel.set_favorites(
            tuple(
                (
                    FavoritePadViewModel(
                        name=record.definition.name,
                        category=record.definition.category,
                        ducking_db=(
                            record.definition.ducking_db
                            if record.definition.ducking_enabled
                            else None
                        ),
                        shortcut=record.keyboard_shortcut or "",
                        missing_file=(
                            record.definition.overlay_id in self._overlay_snapshot.missing_file_ids
                        ),
                        enabled=record.enabled,
                    )
                    if record is not None
                    else FavoritePadViewModel()
                )
                for record in self._overlay_snapshot.favorites
            )
        )
        current_name = (
            self._selected_overlay.definition.name if self._selected_overlay is not None else ""
        )
        available = self._overlay_snapshot.records_for_category("Alle")
        self._selected_overlay = next(
            (record for record in available if record.definition.name == current_name),
            available[0] if available else None,
        )
        names = tuple(record.definition.name for record in available)
        self._overlay_panel.set_choices(categories, names)
        self._overlay_panel.select(
            category="Alle",
            overlay=(
                self._selected_overlay.definition.name
                if self._selected_overlay is not None
                else "Keine Jingles"
            ),
        )
        self._refresh_compact_overlay_pads()
        self._render_overlay()

    def _refresh_compact_overlay_pads(self) -> None:
        for position, button in enumerate(self._compact_overlay_pad_buttons, start=1):
            record = self._overlay_snapshot.favorites[position - 1]
            if record is None:
                text = f"{position} · frei"
                description = f"Favoritenplatz {position} ist nicht belegt"
                enabled = True
            else:
                text = f"{position} · {record.definition.name}"
                description = f"Jingle starten: {record.definition.name} (Strg+{position})"
                enabled = (
                    record.enabled
                    and record.definition.overlay_id not in self._overlay_snapshot.missing_file_ids
                )
            button.configure(text=text, state="normal" if enabled else "disabled")
            self._compact_overlay_pad_tooltips[position - 1].set_text(description)

    def _toggle_compact_overlays(self) -> None:
        expanded = self._compact_overlays_expanded
        if expanded:
            self._compact_overlay_pads.grid_remove()
            self._compact_overlays_expanded = False
            if self._compact_layout_active and _compact_mixer_visible(False):
                self._mixer_container.grid(**_mixer_container_grid_options(True))
        else:
            self._compact_overlay_pads.grid()
            self._compact_overlays_expanded = True
            if self._compact_layout_active:
                self._mixer_container.grid_remove()
        self._compact_overlay_toggle.configure(
            text="Jingles ausblenden ▴" if not expanded else "Jingles anzeigen ▾"
        )

    def _open_deck_equalizer(self, deck_id: str) -> None:
        """Open one compact deck-local assignment dialog."""
        if self._controller is None:
            return
        controller = self._controller
        try:
            state = controller.equalizer_dialog_state(deck_id)
        except (RuntimeError, ValueError) as exc:
            self.show_error("Equalizer", str(exc))
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Equalizer · Deck {deck_id}")
        dialog.geometry("500x520")
        dialog.transient(self)
        dialog.grab_set()
        dialog_tooltips: list[Tooltip] = []
        ctk.CTkLabel(
            dialog,
            text=f"Deck {deck_id}: {state.track_title}",
            font=(theme.FONT_FAMILY, 17, "bold"),
            wraplength=450,
        ).pack(fill="x", padx=18, pady=(18, 4))
        effective = ctk.CTkLabel(
            dialog,
            text=_equalizer_effective_text(state.effective_name, state.effective_source),
            text_color=theme.TEXT_MUTED,
        )
        effective.pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkLabel(dialog, text="Preset").pack(anchor="w", padx=18)
        selected_label = self._equalizer_label_for_state(state)
        preset_line = ctk.CTkFrame(dialog, fg_color="transparent")
        preset_line.pack(fill="x", padx=18, pady=(4, 14))
        preset_line.grid_columnconfigure(0, weight=1)
        preset_menu = ctk.CTkOptionMenu(
            preset_line,
            values=_compact_equalizer_labels(
                list(self._equalizer_preset_keys),
                selected_label,
            ),
        )
        preset_menu.set(selected_label)
        preset_menu.grid(row=0, column=0, sticky="ew")

        def manage_presets() -> None:
            manager = ctk.CTkToplevel(dialog)
            manager.title("Equalizer-Presets verwalten")
            manager.geometry("420x190")
            manager.transient(dialog)
            manager.grab_set()
            ctk.CTkLabel(
                manager,
                text="Alle eingebauten und benutzerdefinierten Presets",
            ).pack(fill="x", padx=18, pady=(18, 8))
            all_presets = ctk.CTkOptionMenu(
                manager,
                values=list(self._equalizer_preset_keys),
            )
            all_presets.set(preset_menu.get())
            all_presets.pack(fill="x", padx=18, pady=6)

            def close_manager() -> None:
                manager.destroy()
                dialog.grab_set()

            def select_managed_preset() -> None:
                label = all_presets.get()
                compact = _compact_equalizer_labels(
                    list(self._equalizer_preset_keys),
                    label,
                )
                preset_menu.configure(values=compact)
                preset_menu.set(label)
                close_manager()

            def edit_managed_preset() -> None:
                preset_key = self._equalizer_preset_keys[all_presets.get()]
                close_manager()
                self._edit_equalizer(deck_id, preset_key)

            manager_buttons = ctk.CTkFrame(manager, fg_color="transparent")
            manager_buttons.pack(fill="x", padx=18, pady=(10, 18))
            ctk.CTkButton(
                manager_buttons,
                text="Bearbeiten…",
                command=edit_managed_preset,
            ).pack(side="left")
            ctk.CTkButton(
                manager_buttons,
                text="Auswählen",
                command=select_managed_preset,
            ).pack(side="right")
            ctk.CTkButton(
                manager_buttons,
                text="Abbrechen",
                command=close_manager,
            ).pack(side="right", padx=8)
            manager.protocol("WM_DELETE_WINDOW", close_manager)
            all_presets.focus_set()

        manage_presets_button = ctk.CTkButton(
            preset_line,
            text="Presets verwalten…",
            width=132,
            command=manage_presets,
        )
        manage_presets_button.grid(row=0, column=1, padx=(8, 0))
        dialog_tooltips.append(
            Tooltip(
                manage_presets_button,
                "Alle eingebauten und benutzerdefinierten Presets anzeigen",
            )
        )
        target = ctk.StringVar(value="preview")
        target_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        target_frame.pack(fill="x", padx=18)
        target_buttons: list[Any] = []
        for value, text, available, unavailable_reason in _equalizer_target_choices(state):
            button = ctk.CTkRadioButton(
                target_frame,
                text=text,
                variable=target,
                value=value,
                state="normal" if available else "disabled",
            )
            button.pack(anchor="w", pady=4)
            target_buttons.append(button)
            if not available:
                dialog_tooltips.append(Tooltip(button, unavailable_reason))
        if state.saved_queue_id is None:
            ctk.CTkLabel(
                target_frame,
                text="Playlistzuweisung benötigt eine ausgewählte gespeicherte Playlist.",
                text_color=theme.TEXT_MUTED,
            ).pack(anchor="w", padx=(26, 0))
        if not state.genre.strip():
            ctk.CTkLabel(
                target_frame,
                text="Genrezuweisung ist ohne Genre-Metadatum nicht verfügbar.",
                text_color=theme.TEXT_MUTED,
            ).pack(anchor="w", padx=(26, 0))
        feedback = ctk.CTkLabel(dialog, text="", text_color=theme.WARNING)
        feedback.pack(fill="x", padx=18, pady=8)
        preview_active = False

        def destroy_dialog() -> None:
            for tooltip in dialog_tooltips:
                tooltip.close()
            dialog.destroy()

        def selected_key() -> str:
            return self._equalizer_preset_keys[preset_menu.get()]

        def preview() -> None:
            nonlocal preview_active
            try:
                controller.preview_equalizer(deck_id, selected_key())
            except (RuntimeError, ValueError) as exc:
                feedback.configure(text=str(exc))
                return
            preview_active = True
            effective.configure(text=_equalizer_effective_text(preset_menu.get(), "PREVIEW"))
            feedback.configure(text="Vorschau aktiv – noch nicht gespeichert")

        def save() -> None:
            nonlocal preview_active
            key = selected_key()
            assignment = None if key == "inherit" else key
            try:
                match target.get():
                    case "preview":
                        preview()
                        return
                    case "title":
                        controller.save_track_equalizer(deck_id, assignment)
                    case "playlist":
                        controller.save_playlist_equalizer(assignment)
                    case "genre":
                        controller.save_genre_equalizer(deck_id, assignment)
                    case "queue":
                        controller.set_current_queue_equalizer(assignment)
            except (RuntimeError, ValueError) as exc:
                feedback.configure(text=str(exc))
                return
            preview_active = False
            destroy_dialog()

        def discard() -> None:
            nonlocal preview_active
            if preview_active:
                controller.discard_equalizer_preview(deck_id)
                preview_active = False
            destroy_dialog()

        def request_close() -> None:
            if not preview_active:
                destroy_dialog()
                return
            choice = ask_silent_yes_no_cancel(
                dialog,
                "Equalizer-Vorschau",
                (
                    "Die Equalizer-Vorschau ist noch nicht gespeichert.\n\n"
                    "Ja: mit dem gewählten Wirkungsziel speichern\n"
                    "Nein: Vorschau verwerfen\n"
                    "Abbrechen: Dialog geöffnet lassen"
                ),
            )
            if choice is None:
                return
            if choice:
                if target.get() == "preview":
                    target.set("title")
                save()
                return
            discard()

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(8, 18))
        discard_button = ctk.CTkButton(buttons, text="Verwerfen", command=discard)
        discard_button.pack(side="right")
        save_button = ctk.CTkButton(buttons, text="Speichern", command=save)
        save_button.pack(side="right", padx=8)
        preview_button = ctk.CTkButton(buttons, text="Anwenden", command=preview)
        preview_button.pack(side="right")
        edit_button = ctk.CTkButton(
            buttons,
            text="Preset bearbeiten…",
            command=lambda: self._edit_equalizer(deck_id, selected_key()),
        )
        edit_button.pack(side="left")
        dialog_tooltips.extend(
            (
                Tooltip(
                    preview_button,
                    "Ausgewähltes Preset vorübergehend auf dem Deck testen",
                ),
                Tooltip(save_button, "Preset für das gewählte Wirkungsziel speichern"),
                Tooltip(discard_button, "Vorschau verwerfen und Dialog schließen"),
            )
        )
        _configure_focus_cycle(
            (
                preset_menu,
                manage_presets_button,
                *target_buttons,
                edit_button,
                preview_button,
                save_button,
                discard_button,
            )
        )
        dialog.bind("<Escape>", lambda _event: request_close())
        dialog.protocol("WM_DELETE_WINDOW", request_close)
        preset_menu.focus_set()

    def _equalizer_label_for_state(self, state: EqualizerDialogState) -> str:
        key = {
            "TITLE": state.title_preset_key,
            "QUEUE": state.queue_preset_key,
            "PLAYLIST": state.playlist_preset_key,
            "GENRE": state.genre_preset_key,
        }.get(state.effective_source)
        if state.effective_source == "DISABLED":
            key = "disabled"
        if key is None:
            return "Vererben"
        return self._equalizer_label_for_key(key)

    def _equalizer_label_for_key(self, key: str | None) -> str:
        if key is None:
            return "Vererben"
        return next(
            (
                label
                for label, preset_key in self._equalizer_preset_keys.items()
                if preset_key == key
            ),
            "Vererben",
        )

    def bind_cue_controller(self, controller: CuePointController) -> None:
        self._cue_controller = controller
        available, message = controller.analysis_availability()
        if not available:
            self._catalog_analysis_button.configure(state="disabled")
            self._outdated_analysis_button.configure(state="disabled")
            Tooltip(self._catalog_analysis_button, message)
            Tooltip(self._outdated_analysis_button, message)

    def bind_loudness_controller(self, controller: LoudnessController) -> None:
        self._loudness_controller = controller
        available, message = controller.analysis_availability()
        if not available:
            self._loudness_analysis_button.configure(state="disabled")
            self._outdated_loudness_button.configure(state="disabled")
            Tooltip(self._loudness_analysis_button, message)
            Tooltip(self._outdated_loudness_button, message)

    def bind_metadata_analysis(self, service: MetadataAnalysisService) -> None:
        self._metadata_analysis = service

    def show_metadata_analysis_progress(self, event: str, job_id: str, detail: str) -> None:
        """Show catalog tempo batches globally, even after their dialog closes."""
        service = self._metadata_analysis
        if service is None:
            return
        progress = service.global_batch_progress(job_id)
        if progress is None:
            return
        self._metadata_analysis_active = progress.completed < progress.total
        state = "BPM-Analyse"
        if progress.state == "PAUSED":
            state += " pausiert"
        elif event == "BLOCKED":
            state += " wartet"
        elif not self._metadata_analysis_active:
            state += " abgeschlossen"
        text = (
            f"{state}: {progress.completed}/{progress.total} · "
            f"Erfolgreich: {progress.successful} · Ohne BPM: {progress.without_bpm} · "
            f"Prüfung: {progress.review_required} · Fehler: {progress.failed} · "
            f"Abgebrochen: {progress.cancelled}"
        )
        if progress.current_title:
            text += f" · Aktuell: {progress.current_title}"
        if progress.reason:
            text += f" · {progress.reason}"
        self._summary.configure(text=text)
        if self._metadata_analysis_active:
            self._show_compact_active_analysis(text)
        else:
            self._refresh_compact_analysis_toggle_state()
            self._hide_compact_active_analysis_if_complete()

    def _run_equalizer_action(self, action: Callable[[], None]) -> None:
        try:
            action()
        except (RuntimeError, ValueError) as exc:
            self.show_error("Equalizer", str(exc))

    def _edit_equalizer(self, deck_id: str, preset_key: str) -> None:
        assert self._controller is not None
        controller = self._controller
        frequencies = controller.equalizer_band_frequencies(deck_id)
        if not frequencies:
            self.show_error("Equalizer", f"Deck {deck_id} unterstützt keinen Equalizer")
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Equalizer-Preset · Deck {deck_id}")
        dialog.geometry("560x680")
        dialog.transient(self)
        dialog.grab_set()
        source_name, source_preamp, source_gains = controller.equalizer_editor_values(
            deck_id, preset_key
        )
        name_entry = ctk.CTkEntry(dialog, placeholder_text="Name des neuen Presets")
        name_entry.insert(0, f"Kopie von {source_name}")
        name_entry.pack(fill="x", padx=18, pady=(18, 8))
        preamp_label = ctk.CTkLabel(dialog, text="Preamp: -3.0 dB")
        preamp_label.pack()
        preamp = ctk.CTkSlider(dialog, from_=-20, to=0, number_of_steps=80)
        preamp.set(source_preamp)
        preamp_label.configure(text=f"Preamp: {source_preamp:.1f} dB")
        preamp.configure(
            command=lambda value: preamp_label.configure(text=f"Preamp: {float(value):.1f} dB")
        )
        preamp.pack(fill="x", padx=18, pady=(0, 10))
        band_frame = ctk.CTkScrollableFrame(dialog)
        band_frame.pack(fill="both", expand=True, padx=18, pady=8)
        sliders: list[ctk.CTkSlider] = []
        labels: list[ctk.CTkLabel] = []
        for index, frequency in enumerate(frequencies):
            row = ctk.CTkFrame(band_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            frequency_text = (
                f"{frequency / 1000:.1f} kHz" if frequency >= 1000 else f"{frequency:.0f} Hz"
            )
            ctk.CTkLabel(row, text=frequency_text, width=75).pack(side="left")
            slider = ctk.CTkSlider(row, from_=-12, to=12, number_of_steps=96)
            slider.set(source_gains[index])
            slider.pack(side="left", fill="x", expand=True, padx=8)
            value_label = ctk.CTkLabel(row, text=f"{source_gains[index]:+.1f} dB", width=62)
            value_label.pack(side="left")
            slider.configure(
                command=lambda value, label=value_label: label.configure(
                    text=f"{float(value):+.1f} dB"
                )
            )
            sliders.append(slider)
            labels.append(value_label)
        warning = ctk.CTkLabel(dialog, text="", text_color="#ffb347")
        warning.pack(padx=18, pady=4)

        def update_warning() -> None:
            boost = max((0.0, *(float(slider.get()) for slider in sliders)))
            safe = float(preamp.get()) <= -boost
            warning.configure(
                text=(
                    "Pegelreserve ausreichend"
                    if safe
                    else f"Clipping-Gefahr: Preamp auf höchstens {-boost:.1f} dB setzen"
                ),
                text_color="#7fd18b" if safe else "#ffb347",
            )

        for slider in sliders:
            slider.bind("<ButtonRelease-1>", lambda _event: update_warning())
        preamp.bind("<ButtonRelease-1>", lambda _event: update_warning())

        def save() -> None:
            name = name_entry.get().strip()
            if not name:
                warning.configure(text="Bitte einen Presetnamen eingeben")
                return
            try:
                new_key = controller.save_custom_equalizer(
                    name,
                    float(preamp.get()),
                    frequencies,
                    tuple(float(slider.get()) for slider in sliders),
                )
            except (RuntimeError, ValueError) as exc:
                warning.configure(text=str(exc))
                return
            self._equalizer_preset_keys[name] = new_key
            self._queue_equalizer_menu.configure(values=list(self._equalizer_preset_keys))
            self._playlist_equalizer_menu.configure(values=list(self._equalizer_preset_keys))
            dialog.destroy()

        def reset_sliders() -> None:
            preamp.set(0.0)
            preamp_label.configure(text="Preamp: 0.0 dB")
            for slider, label in zip(sliders, labels, strict=True):
                slider.set(0.0)
                label.configure(text="0.0 dB")
            update_warning()

        def reset_custom() -> None:
            if not preset_key.startswith("custom-"):
                return
            if not ask_silent_yes_no(
                dialog,
                "Preset zurücksetzen?",
                (
                    f"„{source_name}“ dauerhaft auf eine neutrale Kurve zurücksetzen?\n\n"
                    "Bestehende Titel-, Queue- und Genrezuweisungen bleiben erhalten."
                ),
            ):
                return
            try:
                controller.reset_custom_equalizer(preset_key)
            except (RuntimeError, ValueError) as exc:
                warning.configure(text=str(exc))
                return
            reset_sliders()
            warning.configure(
                text="Benutzerdefiniertes Preset wurde dauerhaft zurückgesetzt",
                text_color=theme.SUCCESS,
            )

        def rename() -> None:
            if not preset_key.startswith("custom-"):
                warning.configure(text="Eingebaute Presets sind nur über eine Kopie änderbar")
                return
            new_name = simpledialog.askstring(
                "Preset umbenennen",
                "Neuer Name:",
                initialvalue=source_name,
                parent=dialog,
            )
            if not new_name:
                return
            try:
                controller.rename_custom_equalizer(preset_key, new_name)
            except (RuntimeError, ValueError) as exc:
                warning.configure(text=str(exc))
                return
            self._equalizer_preset_keys.pop(source_name, None)
            self._equalizer_preset_keys[new_name] = preset_key
            self._queue_equalizer_menu.configure(values=list(self._equalizer_preset_keys))
            self._playlist_equalizer_menu.configure(values=list(self._equalizer_preset_keys))
            dialog.destroy()

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(4, 18))
        ctk.CTkButton(buttons, text="Abbrechen", command=dialog.destroy).pack(side="right")
        ctk.CTkButton(buttons, text="Als neues Preset speichern", command=save).pack(
            side="right", padx=8
        )
        custom_preset = preset_key.startswith("custom-")
        ctk.CTkButton(
            buttons,
            text="Preset zurücksetzen",
            width=125,
            command=reset_custom,
            state="normal" if custom_preset else "disabled",
        ).pack(side="left")
        ctk.CTkButton(
            buttons,
            text="Umbenennen",
            width=95,
            command=rename,
            state="normal" if custom_preset else "disabled",
        ).pack(side="left", padx=8)
        update_warning()

    def show_catalog(self, tracks: list[Track], summary: str) -> None:
        with self._performance.measure("gui.catalog_render.schedule", warning_threshold_ms=25.0):
            self._show_catalog_impl(tracks, summary)

    def _show_catalog_impl(self, tracks: list[Track], summary: str) -> None:
        self._catalog_render_started_at = monotonic()
        self._catalog_first_row_recorded = False
        self._catalog_initial_rows_recorded = False
        with self._performance.measure(
            "gui.catalog_render.fetch_view_models", warning_threshold_ms=25.0
        ):
            self._catalog_tracks = tracks
            cue_track_ids = (
                self._cue_controller.manual_track_ids([track.id for track in tracks])
                if self._cue_controller is not None
                else set()
            )
            self._catalog_view_models = [
                CatalogEntryViewModel(track, track.id in cue_track_ids) for track in tracks
            ]
        if tracks:
            self._catalog_empty_label.pack_forget()
        else:
            self._catalog_empty_label.pack(padx=16, pady=32)
        with self._performance.measure("gui.catalog_render.layout", warning_threshold_ms=10.0):
            self._summary.configure(text=summary)
        self._catalog_pool_target = _initial_catalog_pool_target(
            len(self._catalog_view_models),
            len(self._catalog_rows),
            self._CATALOG_INITIAL_POOL_SIZE,
        )
        dirty_count = max(len(self._catalog_rows), self._catalog_pool_target)
        self._catalog_dirty_scheduler.replace(list(range(dirty_count)))
        self._request_layout_refresh("catalog")
        self._publish_layout_state()

    def _render_catalog_row(self, index: int) -> None:
        if index >= len(self._catalog_rows):
            if index >= len(self._catalog_view_models):
                return
            with self._performance.measure(
                "gui.catalog_render.create_rows", warning_threshold_ms=25.0
            ):
                row_view = CatalogRowView(
                    self._catalog,
                    self._catalog_row_callbacks(),
                    self._performance,
                    self._callback_state,
                )
                self._catalog_rows.append(row_view)
                self._request_focus_setup(row_view.focus_root)
                self._performance.record("gui.catalog_render.created_row_count", 1.0, 100.0)
                self._render_counters["widgets_created_total"] += 6
        model = self._catalog_view_models[index] if index < len(self._catalog_view_models) else None
        row = self._catalog_rows[index]
        old_id = row.track_id
        with self._performance.measure("gui.catalog_render.bind_rows", warning_threshold_ms=10.0):
            changed = row.bind_entry(model)
        elapsed_ms = (monotonic() - self._catalog_render_started_at) * 1000.0
        if index == 0 and not self._catalog_first_row_recorded:
            self._catalog_first_row_recorded = True
            self._performance.record("gui.catalog_render.first_row_visible", elapsed_ms, 250.0)
        initial_target = min(15, len(self._catalog_view_models))
        if (
            initial_target
            and index == initial_target - 1
            and not self._catalog_initial_rows_recorded
        ):
            self._catalog_initial_rows_recorded = True
            self._performance.record(
                "gui.catalog_render.initial_visible_rows_complete",
                elapsed_ms,
                1000.0,
            )
        if changed and old_id != row.track_id:
            self._performance.record("gui.catalog_render.rebound_row_count", 1.0, 100.0)
        self._publish_layout_state()

    def _catalog_row_callbacks(self) -> dict[str, Callable[[Track], None]]:
        return {
            "deck_a": lambda track: self._load_catalog(track.id, "A"),
            "deck_b": lambda track: self._load_catalog(track.id, "B"),
            "queue": lambda track: self._add_queue(track.id),
            "cue": self._edit_cue_points,
            "loudness": self._edit_loudness,
            "details": self._show_track_details,
            "equalizer": self._assign_track_equalizer,
            "equalizer_remove": lambda track: self._remove_track_equalizer(track.id),
            "remove": self._remove_catalog,
        }

    def _assign_track_equalizer(self, track: Track) -> None:
        title = f"{track.artist or 'Unbekannt'} — {track.title}"
        self._show_track_equalizer_assignment(track.id, title)

    def _assign_queue_track_equalizer(self, queue_id: int) -> None:
        entry = next(
            (entry for entry in self._queue_entries if entry.queue_id == queue_id),
            None,
        )
        if entry is None:
            return
        track = self._queue_tracks.get(entry.track_id)
        title = (
            f"{track.artist or 'Unbekannt'} — {track.title}"
            if track is not None
            else f"Titel #{entry.track_id}"
        )
        self._show_track_equalizer_assignment(entry.track_id, title)

    def _show_track_equalizer_assignment(self, track_id: int, title: str) -> None:
        controller = self._controller
        if controller is None:
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title("Equalizer für Titel zuweisen")
        dialog.geometry("460x230")
        dialog.transient(self)
        dialog.grab_set()
        ctk.CTkLabel(
            dialog,
            text=title,
            font=(theme.FONT_FAMILY, 16, "bold"),
            wraplength=420,
        ).pack(fill="x", padx=18, pady=(18, 5))
        ctk.CTkLabel(
            dialog,
            text=(
                "Die Zuweisung gilt dauerhaft für diesen Titel – auch in anderen "
                "Queues und Playlists."
            ),
            text_color=theme.TEXT_MUTED,
            wraplength=420,
        ).pack(fill="x", padx=18, pady=(0, 12))
        preset_menu = ctk.CTkOptionMenu(
            dialog,
            values=list(self._equalizer_preset_keys),
        )
        preset_menu.set("Vererben")
        preset_menu.pack(fill="x", padx=18)
        feedback = ctk.CTkLabel(dialog, text="", text_color=theme.WARNING)
        feedback.pack(fill="x", padx=18, pady=5)

        def save() -> None:
            key = self._equalizer_preset_keys[preset_menu.get()]
            assignment = None if key == "inherit" else key
            try:
                controller.save_track_equalizer_by_id(track_id, assignment)
            except (RuntimeError, ValueError) as exc:
                feedback.configure(text=str(exc))
                return
            dialog.destroy()

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(8, 18))
        ctk.CTkButton(buttons, text="Abbrechen", command=dialog.destroy).pack(side="right")
        ctk.CTkButton(buttons, text="Speichern", command=save).pack(side="right", padx=8)
        preset_menu.focus_set()

    def _remove_track_equalizer(self, track_id: int) -> None:
        controller = self._controller
        if controller is None:
            return
        self._run_equalizer_action(lambda: controller.save_track_equalizer_by_id(track_id, None))

    def _remove_queue_track_equalizer(self, queue_id: int) -> None:
        entry = next(
            (entry for entry in self._queue_entries if entry.queue_id == queue_id),
            None,
        )
        if entry is not None:
            self._remove_track_equalizer(entry.track_id)

    def show_catalog_paging(self, page: int, page_count: int) -> None:
        self._catalog_page_label.configure(text=f"Seite {page}/{page_count}")
        self._catalog_previous_button.configure(state="normal" if page > 1 else "disabled")
        self._catalog_next_button.configure(state="normal" if page < page_count else "disabled")

    def show_session(self, session: PartySession) -> None:
        statuses = {
            "active": "AKTIV",
            "paused": "PAUSIERT",
            "recovered": "WIEDERHERGESTELLT",
            "finished": "BEENDET",
        }
        self._session_summary.configure(
            text=f"Session: {session.name} · {statuses.get(session.status.value, session.status.value)}"
        )
        if session.status.value in {"recovered", "paused"}:
            self._force_live_workspace(f"session-{session.status.value}")

    def show_start_settings(self, restore_session: bool, fullscreen: bool) -> None:
        (
            self._restore_session_switch.select
            if restore_session
            else self._restore_session_switch.deselect
        )()
        (
            self._fullscreen_start_switch.select
            if fullscreen
            else self._fullscreen_start_switch.deselect
        )()

    def show_file_browser_setting(self, enabled: bool) -> None:
        (self._file_browser_switch.select if enabled else self._file_browser_switch.deselect)()
        self.deck_a.show_file_browser(enabled)
        self.deck_b.show_file_browser(enabled)

    def show_production_mode(self, enabled: bool) -> None:
        (
            self._production_mode_switch.select
            if enabled
            else self._production_mode_switch.deselect
        )()

    def show_diagnostic_saved(self, path: Path) -> None:
        show_silent_message(
            self,
            "Diagnosebericht gespeichert",
            f"Der Diagnosebericht wurde gespeichert unter:\n\n{path}",
        )

    def show_diagnostic_state(self, state: str, context: str) -> None:
        label = next(
            (
                display
                for display, stored_context in self._diagnostic_context_labels.items()
                if stored_context == context
            ),
            context,
        )
        if state == "running":
            self._diagnostic_start_button.configure(
                text=f"Test läuft: {label}",
                fg_color="#2e7d32",
                hover_color="#256628",
            )
            self._diagnostic_stop_button.configure(
                text="Test beenden + Bericht",
                state="normal",
                fg_color="#8f1f1f",
                hover_color="#741919",
            )
            return
        if state == "stopping":
            self._diagnostic_start_button.configure(
                text="Test wird beendet …",
                fg_color="#9a6700",
                hover_color="#7d5400",
            )
            self._diagnostic_stop_button.configure(
                text="Bericht wird erstellt …",
                state="disabled",
            )
            return
        self._diagnostic_start_button.configure(
            text="Test beendet ✓",
            fg_color="#3b6e8f",
            hover_color="#315c78",
        )
        self._diagnostic_stop_button.configure(
            text="Test beenden + Bericht",
            state="normal",
            fg_color="#3b6e8f",
            hover_color="#315c78",
        )

    def widget_diagnostics(self) -> dict[str, int]:
        """Return counters and current GUI gauges for a manual diagnostic report.

        Recursive Tk widget counting is intentionally performed only when a report
        is requested, never from the normal status tick.
        """
        tooltip_stats = Tooltip.statistics()

        def count_widgets(root: Any) -> int:
            children = root.winfo_children()
            return len(children) + sum(count_widgets(child) for child in children)

        presentation = (
            self._presentation_coordinator.diagnostics()
            if self._presentation_coordinator is not None
            else None
        )
        return {
            "catalog_row_views": len(self._catalog_rows),
            "queue_row_views": len(self._queue_rows),
            "queue_virtualization.logical_row_count": len(self._queue_entries),
            "queue_virtualization.pool_size": len(self._queue_rows),
            "queue_virtualization.visible_start_index": self._queue_visible_start_index,
            "queue_virtualization.visible_end_index": min(
                len(self._queue_entries),
                self._queue_visible_start_index + self._queue_pool_target,
            ),
            "queue_virtualization.overscan_rows": self._QUEUE_OVERSCAN_ROWS,
            "queue_virtualization.rebind_count": self._queue_rebind_count,
            "queue_virtualization.widget_creation_count": self._queue_widget_creation_count,
            **self._queue_lifecycle_counters,
            "tooltip_manager.registered_target_count": (
                self._queue_tooltip_manager.registered_target_count
            ),
            "tooltip_manager.window_count": self._queue_tooltip_manager.window_count,
            "tooltip_instances_current": tooltip_stats.current,
            "tooltip_instances_created_total": tooltip_stats.created_total,
            "tooltip_instances_destroyed_total": tooltip_stats.destroyed_total,
            "tk_widget_count": count_widgets(self),
            "large_deck_widget_count": count_widgets(self.deck_a) + count_widgets(self.deck_b),
            "compact_deck_widget_count": (
                count_widgets(self.compact_deck_a) + count_widgets(self.compact_deck_b)
            ),
            "compact_preparation_specific_widget_count": (
                count_widgets(self._compact_preparation)
                + count_widgets(self._compact_preparation_tools)
                + 1  # Search reset button added for the compact preparation workflow.
            ),
            "compact_widget_tree_creation_count": self._compact_widget_tree_creation_count,
            "presentation.layout_applications": self._compact_layout_apply_count,
            "catalog_dirty_rows": self._catalog_dirty_scheduler.pending_count,
            "queue_dirty_rows": self._queue_dirty_scheduler.pending_count,
            "overlay.active": int(
                self._overlay_runtime.status
                not in {
                    OverlayStatus.IDLE,
                    OverlayStatus.FINISHED,
                    OverlayStatus.FAILED,
                }
            ),
            "overlay.generation": self._overlay_runtime.generation,
            "overlay.position_ms": self._overlay_runtime.position_ms or 0,
            "overlay.ducking_active": int(self._overlay_ducking_factor < 0.999),
            "presentation.client_width": (presentation.client_size.width if presentation else 0),
            "presentation.client_height": (presentation.client_size.height if presentation else 0),
            "presentation.resize_events": (presentation.resize_events if presentation else 0),
            "presentation.evaluations": presentation.evaluations if presentation else 0,
            "presentation.applied_changes": (presentation.applied_changes if presentation else 0),
            "presentation.resolved_compact": int(
                presentation is not None
                and presentation.state.resolved is ResolvedPresentation.COMPACT
            ),
            "presentation.workspace_preparation": int(
                presentation is not None and presentation.state.workspace is Workspace.PREPARATION
            ),
            "presentation.pending_switch": int(
                presentation is not None and presentation.state.pending_mode is not None
            ),
            **self._render_counters,
        }

    def memory_gauges(self) -> dict[str, int]:
        """Return cheap gauges suitable for the periodic GUI-thread sampler."""
        return {
            "cover_cache_size": len(self._cover_images),
            "registered_widget_count": max(
                0,
                self._render_counters["widgets_created_total"]
                - self._render_counters["widgets_destroyed_total"],
            ),
            "active_preview_count": (
                self._cue_controller.active_preview_count if self._cue_controller is not None else 0
            ),
        }

    def show_audio_devices(self, devices: list[tuple[str, str]], selected_device: str) -> None:
        self._audio_device_ids = {"Systemstandard": ""}
        for device_id, description in devices:
            label = description
            suffix = 2
            while label in self._audio_device_ids:
                label = f"{description} ({suffix})"
                suffix += 1
            self._audio_device_ids[label] = device_id
        values = list(self._audio_device_ids)
        selected_label = next(
            (
                label
                for label, device_id in self._audio_device_ids.items()
                if device_id == selected_device
            ),
            "Systemstandard",
        )
        self._update_optionmenu("audio_devices", self._audio_device_menu, values, selected_label)

    def show_audio_device_recovery(self, state: str, message: str) -> None:
        """Render the explicit device-loss recovery steps and their valid actions."""
        lost = state == "device_lost"
        ready = state == "ready_for_confirmation"
        self._audio_device_recovery_label.configure(
            text=message,
            text_color=(("#B45309", "#FBBF24") if lost or ready else ("#166534", "#86EFAC")),
        )
        self._audio_device_retry_button.configure(state="normal" if lost else "disabled")
        self._audio_device_confirm_button.configure(state="normal" if ready else "disabled")
        self._audio_device_menu.configure(state="disabled" if lost or ready else "normal")
        self._presentation_status = replace(
            self._presentation_status, warning=message if lost or ready else ""
        )
        self._render_global_status()
        if lost or ready:
            self._force_live_workspace(f"audio-recovery-{state}")

    def show_recovery_return_requirements(
        self, requirements: tuple[RecoveryReturnRequirement, ...], visible: bool
    ) -> None:
        """Show every recovery-return prerequisite instead of only the first blocker."""
        if not visible:
            self._recovery_return_requirements_label.grid_remove()
            self._recovery_resume_button.grid_remove()
            return
        lines = ["RÜCKKEHRPRÜFUNG"]
        lines.extend(
            f"{'✓' if requirement.fulfilled else '✕'}  {requirement.label}"
            for requirement in requirements
        )
        all_fulfilled = all(requirement.fulfilled for requirement in requirements)
        self._recovery_return_requirements_label.configure(
            text="\n".join(lines),
            text_color=(("#166534", "#86EFAC") if all_fulfilled else ("#991B1B", "#FCA5A5")),
        )
        self._recovery_return_requirements_label.grid()
        self._recovery_resume_button.configure(state="normal" if all_fulfilled else "disabled")
        self._recovery_resume_button.grid()

    def _request_recovery_automatic_resume(self) -> None:
        if self._controller is None:
            return
        if not ask_silent_yes_no(
            self,
            "Automatik sicher fortsetzen",
            "Alle Rückkehrbedingungen werden unmittelbar erneut geprüft. Erst danach "
            "wird die Notfallsperre aufgehoben und die Automatik fortgesetzt.\n\n"
            "Automatik jetzt fortsetzen?",
        ):
            return
        if not self._controller.resume_automatic_after_recovery():
            show_silent_message(
                self,
                "Automatik bleibt gesperrt",
                "Mindestens eine Sicherheitsbedingung ist nicht mehr erfüllt.",
            )

    def show_unresolved_emergency_incident(self, incident_id: int, summary: str) -> None:
        """Keep a prior unresolved audio incident prominent without auto-resolving it."""
        self._unresolved_incident_title.configure(text=f"⚠ UNGELÖSTER AUDIOVORFALL #{incident_id}")
        self._unresolved_incident_summary.configure(text=summary)
        self._unresolved_incident_frame.grid()
        self._presentation_status = replace(
            self._presentation_status, warning=f"Audiovorfall #{incident_id}"
        )
        self._render_global_status()
        self._force_live_workspace("unresolved-emergency")

    def show_emergency_dashboard(self, dashboard: EmergencyDashboardViewModel) -> None:
        """Render cached emergency readiness without initiating any storage checks."""
        media = "BEREIT" if dashboard.media_ready else "NICHT BEREIT"
        text = (
            f"NOTFALLSTATUS · {dashboard.system_state}\n"
            f"Grund: {dashboard.reason}\n"
            f"Deck A: {dashboard.deck_a_health}   ·   Deck B: {dashboard.deck_b_health}\n"
            f"Quelle A: {dashboard.deck_a_source}\n"
            f"Quelle B: {dashboard.deck_b_source}\n"
            f"Audio: {dashboard.audio_state}   ·   Notfallmedien: {media}\n"
            f"{dashboard.media_summary}\n"
            f"Aktuelle Aktion: {dashboard.current_action}\n"
            f"Letztes Ergebnis: {dashboard.last_result}"
        )
        healthy = (
            dashboard.system_state == "NORMAL"
            and dashboard.deck_a_health == "HEALTHY"
            and dashboard.deck_b_health == "HEALTHY"
            and dashboard.audio_state == "normal"
        )
        self._emergency_dashboard_label.configure(
            text=text,
            text_color=("#166534", "#86EFAC") if healthy else ("#991B1B", "#FCA5A5"),
        )

    def _request_deck_recovery(self, deck_id: str) -> None:
        if self._controller is None:
            return
        assessment = self._controller.can_restart_deck_independently(deck_id)
        if not assessment.allowed:
            show_silent_message(
                self,
                "Einzeldeck-Recovery nicht möglich",
                assessment.message or assessment.error_code,
            )
            return
        confirmed = ask_silent_yes_no(
            self,
            f"Deck {deck_id} reparieren",
            f"Nur das Backend von Deck {deck_id} wird ersetzt. Titel, sichere Position "
            "und Loudness werden wiederhergestellt. Das Deck bleibt danach stumm.\n\n"
            "Recovery jetzt starten?",
        )
        if confirmed:
            self._controller.start_deck_recovery_action(deck_id)

    def _request_emergency_media(
        self, media_type: EmergencyMediaType, *, loop: bool = False
    ) -> None:
        if self._controller is None:
            return
        label = {
            EmergencyMediaType.BREAK_MUSIC: "Pausenmusik",
            EmergencyMediaType.JINGLE: "Jingle",
            EmergencyMediaType.ANNOUNCEMENT: "Ansage",
            EmergencyMediaType.PRIMARY: "Notfalltitel",
        }[media_type]
        detail = " in Schleife" if loop else ""
        if ask_silent_yes_no(
            self,
            f"{label} starten",
            f"Das geprüfte lokale Medium wird sicher vorbereitet und{detail} gestartet.\n\n"
            "Aktion jetzt ausführen?",
        ):
            started = self._controller.start_emergency_media_action(media_type, loop=loop)
            if not started:
                show_silent_message(
                    self,
                    "Notfallaktion nicht gestartet",
                    "Eine andere Notfallaktion läuft bereits oder die Wiedergabe ist nicht konfiguriert.",
                )

    def _request_immediate_replace(self, deck_id: str) -> None:
        if self._controller is None:
            return
        if ask_silent_yes_no(
            self,
            f"Deck {deck_id} sofort ersetzen",
            f"Deck {deck_id} wird sofort hart stummgeschaltet. Danach wird der lokale "
            "Notfalltitel auf einem gesunden Deck stumm gestartet, bestätigt und kurz "
            "eingeblendet. Die Automatik bleibt pausiert.\n\n"
            "Nur bei unzumutbarer Audioausgabe fortfahren?",
        ):
            started = self._controller.start_immediate_replace_action(deck_id)
            if not started:
                show_silent_message(
                    self,
                    "Sofortersatz nicht gestartet",
                    "Eine andere Notfallaktion läuft bereits oder die Wiedergabe ist nicht konfiguriert.",
                )

    def hide_unresolved_emergency_incident(self) -> None:
        self._unresolved_incident_frame.grid_remove()
        self._presentation_status = replace(self._presentation_status, warning="")
        self._render_global_status()

    def _review_unresolved_incident(self) -> None:
        if self._controller is None:
            return
        confirmed = ask_silent_yes_no(
            self,
            "Audiovorfall als geprüft schließen",
            "Der historische Vorfall wird als vom Bediener geprüft geschlossen. "
            "Sein letzter technischer Zustand und alle Ereignisse bleiben erhalten.\n\n"
            "Vorfall jetzt schließen?",
        )
        if confirmed:
            self._controller.resolve_unresolved_emergency_incident()

    def set_fullscreen(self, enabled: bool) -> None:
        self.attributes("-fullscreen", bool(enabled))
        self._window_mode_button.configure(
            text="❐ Fenstermodus" if enabled else "□ Vollbild",
            command=self._leave_fullscreen if enabled else self._enter_fullscreen,
        )

    def _enter_fullscreen(self) -> None:
        self.set_fullscreen(True)

    def _leave_fullscreen(self) -> None:
        self.set_fullscreen(False)

    def _minimize_window(self) -> None:
        self.set_fullscreen(False)
        self.iconify()

    def _toggle_fullscreen(self) -> None:
        self.set_fullscreen(not bool(self.attributes("-fullscreen")))

    def _window_resized(self, event: Any) -> None:
        if event.widget is not self:
            return
        self._schedule_cursor_restore()
        self._schedule_window_geometry_save()
        if self._presentation_coordinator is not None:
            self._presentation_coordinator.resize(
                self._logical_client_size(event.width, event.height), reason="configure"
            )
        if self._responsive_layout_pending:
            return
        self._responsive_layout_pending = True

        def apply() -> None:
            self._responsive_layout_pending = False
            spacing = _main_layout_spacing(self.winfo_width())
            if spacing == self._responsive_layout_spacing:
                return
            self._responsive_layout_spacing = spacing
            outer, inner, vertical = spacing
            self.deck_a.grid_configure(padx=(outer, inner), pady=vertical)
            self._center_panel.grid_configure(padx=inner, pady=vertical)
            self.deck_b.grid_configure(padx=(inner, outer), pady=vertical)
            self._mixer_container.grid_configure(
                padx=outer,
                pady=(vertical, outer),
            )
            self._overlay_panel.relayout(max(0, self.winfo_width() - (outer * 2)))

        self.schedule(80, apply)

    def _presentation_interaction_active(self) -> bool:
        """Guard future destructive layout switches during active modal interaction."""
        try:
            return self.grab_current() is not None
        except TclError:
            return False

    def _show_presentation_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        for preference, label in (
            (PresentationPreference.AUTO, "Automatisch"),
            (PresentationPreference.LARGE, "Groß"),
            (PresentationPreference.COMPACT, "Kompakt"),
        ):
            menu.add_command(
                label=label,
                command=partial(self._select_presentation_preference, preference),
            )
        self._post_button_menu(menu, self._presentation_mode_button)

    def _select_presentation_preference(self, preference: PresentationPreference) -> None:
        coordinator = self._presentation_coordinator
        if coordinator is None:
            return
        coordinator.set_preference(preference)
        if self._save_presentation_preference is not None:
            self._save_presentation_preference(preference)
        self._refresh_presentation_header(coordinator.state)

    def _select_workspace(self, selected: Workspace) -> None:
        coordinator = self._presentation_coordinator
        if coordinator is None or not coordinator.set_workspace(selected):
            return
        if self._save_presentation_workspace is not None:
            self._save_presentation_workspace(selected)

    def _force_live_workspace(self, reason: str) -> None:
        coordinator = self._presentation_coordinator
        if coordinator is not None and coordinator.set_workspace(Workspace.LIVE, reason=reason):
            if self._save_presentation_workspace is not None:
                self._save_presentation_workspace(Workspace.LIVE)

    def _finish_presentation_startup(self) -> None:
        """Allow manual workspace choice after initial controller state has rendered."""
        self._presentation_startup_guard = False
        coordinator = self._presentation_coordinator
        if coordinator is None:
            return
        diagnostic = coordinator.diagnostics()
        thresholds = LayoutPolicy().thresholds
        self._logger.info(
            "Präsentationsdiagnose client_logical=%sx%s preference=%s resolved=%s "
            "workspace=%s reason=%s resize_events=%s evaluations=%s applied_changes=%s "
            "large_min=%sx%s hysteresis=%sx%s pending=%s",
            diagnostic.client_size.width,
            diagnostic.client_size.height,
            diagnostic.state.preference.value,
            diagnostic.state.resolved.value,
            diagnostic.state.workspace.value,
            diagnostic.state.last_reason,
            diagnostic.resize_events,
            diagnostic.evaluations,
            diagnostic.applied_changes,
            thresholds.large_min_width,
            thresholds.large_min_height,
            thresholds.width_hysteresis,
            thresholds.height_hysteresis,
            diagnostic.state.pending_reason,
        )

    def _apply_presentation_state(self, state: PresentationState, decision: LayoutDecision) -> None:
        """Switch prebuilt view trees without changing any domain state."""
        self._refresh_presentation_header(state)
        self._apply_presentation_layout(state)
        self._focus_workspace(state.workspace)
        capabilities = decision.capabilities
        client_size = self._logical_client_size(self.winfo_width(), self.winfo_height())
        self._logger.info(
            "Präsentation preference=%s resolved=%s workspace=%s client=%sx%s "
            "large_fits=%s compact_fits=%s reason=%s pending=%s compact_content=%s",
            state.preference.value,
            state.resolved.value,
            state.workspace.value,
            client_size.width,
            client_size.height,
            capabilities.large_fits,
            capabilities.compact_fits,
            state.last_reason,
            state.pending_reason,
            state.compact_content_available,
        )

    def _apply_presentation_layout(self, state: PresentationState) -> None:
        signature = (state.resolved, state.workspace)
        if signature == self._presentation_layout_signature:
            return
        self._presentation_layout_signature = signature
        self._compact_layout_apply_count += 1
        if state.resolved is ResolvedPresentation.COMPACT:
            self._show_compact_layout(state.workspace)
        else:
            self._show_large_layout()

    def _hide_center_content(self) -> None:
        for widget in (
            self._summary,
            self._search_frame,
            self._catalog,
            self._crossfader_bar,
            self._workspace_splitter,
            self._queue_header,
            self._queue_toolbar,
            self._directory_progress_frame,
            self._saved_toolbar,
            self._queue,
            self._compact_decks_frame,
            self._compact_overlay_frame,
            self._compact_preparation,
            self._compact_preparation_tools,
        ):
            widget.grid_remove()

    def _reset_center_rows(self) -> None:
        for row in range(10):
            self._center_panel.grid_rowconfigure(row, weight=0, minsize=0, uniform="")

    def _configure_catalog_search_layout(self, compact: bool) -> None:
        """Recompose the existing search/actions tree without creating widgets."""
        for widget in (
            self._search,
            self._catalog_search_button,
            self._catalog_search_reset_button,
            self._catalog_previous_button,
            self._catalog_page_label,
            self._catalog_next_button,
            self._catalog_imports,
            self._outdated_analysis_button,
            self._catalog_analysis_cancel_button,
            self._catalog_analysis_button,
            self._outdated_loudness_button,
            self._loudness_analysis_cancel_button,
            self._loudness_analysis_button,
        ):
            widget.grid_remove()
        self._search.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self._catalog_search_button.grid(row=0, column=1)
        self._catalog_search_reset_button.grid(row=0, column=2, padx=(4, 0))
        self._catalog_previous_button.grid(row=0, column=3, padx=(10, 3))
        self._catalog_page_label.grid(row=0, column=4)
        self._catalog_next_button.grid(row=0, column=5, padx=(3, 0))
        self._catalog_imports.grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

    def _toggle_compact_analysis(self) -> None:
        """Retained callback compatibility: analysis controls now live in catalog maintenance."""
        self._open_catalog_maintenance()

    def _toggle_compact_playlist(self) -> None:
        self._compact_playlist_expanded = not self._compact_playlist_expanded
        self._compact_playlist_toggle.configure(
            text=(
                "Playlist / Quellen ausblenden ▴"
                if self._compact_playlist_expanded
                else "Playlist / Quellen anzeigen ▾"
            )
        )
        if not self._compact_layout_active:
            return
        coordinator = self._presentation_coordinator
        if coordinator is None or coordinator.state.workspace is not Workspace.PREPARATION:
            return
        if self._compact_playlist_expanded:
            self._saved_toolbar.grid(
                row=_compact_preparation_rows()["playlist"],
                column=0,
                padx=8,
                pady=(0, 4),
                sticky="ew",
            )
        else:
            self._saved_toolbar.grid_remove()

    def _show_large_layout(self) -> None:
        self._compact_layout_active = False
        self.grid_columnconfigure(0, weight=1, uniform="main")
        self.grid_columnconfigure(1, weight=2, uniform="main")
        self.grid_columnconfigure(2, weight=1, uniform="main")
        self._title_frame.grid(row=0, column=0, padx=20, pady=(14, 8), sticky="w")
        self._presentation_header.grid(**_presentation_header_grid_options(False))
        self._window_controls.grid(
            row=0,
            column=2,
            columnspan=1,
            padx=(8, 16),
            pady=(8, 4),
            sticky="e",
        )
        self._hide_center_content()
        self._reset_center_rows()
        self._configure_catalog_search_layout(False)
        self.deck_a.grid(row=1, column=0, padx=(16, 8), pady=8, sticky="nsew")
        self.deck_b.grid(row=1, column=2, padx=(8, 16), pady=8, sticky="nsew")
        self._center_panel.grid(**_center_panel_grid_options(False))
        self._mixer_container.grid(**_mixer_container_grid_options(False))
        self._summary.grid(row=0, column=0, padx=12, pady=(12, 4), sticky="w")
        self._search_frame.grid(row=1, column=0, padx=12, pady=4, sticky="ew")
        self._catalog.grid(row=2, column=0, padx=12, pady=6, sticky="nsew")
        self._crossfader_bar.grid(row=3, column=0, padx=12, pady=(4, 8), sticky="ew")
        self._workspace_splitter.grid(row=4, column=0, padx=12, pady=(0, 4), sticky="ew")
        self._queue_header.grid(row=5, column=0, padx=12, pady=(8, 2), sticky="ew")
        self._queue_toolbar.grid(row=6, column=0, padx=12, pady=2, sticky="ew")
        if self._directory_progress_visible:
            self._directory_progress_frame.grid(row=7, column=0, padx=12, pady=2, sticky="ew")
        if self._saved_toolbar_visible:
            self._saved_toolbar.grid(row=8, column=0, padx=12, pady=2, sticky="ew")
        self._queue.grid(row=9, column=0, padx=12, pady=(2, 12), sticky="nsew")
        self._set_workspace_split(self._workspace_catalog_ratio, persist=False)
        for deck_id, deck in self._latest_decks.items():
            (self.deck_a if deck_id == "A" else self.deck_b).render(deck)

    def _show_compact_layout(
        self, workspace: Workspace, *, schedule_reassertion: bool = True
    ) -> None:
        self._compact_layout_active = True
        self.grid_columnconfigure(0, weight=0, uniform="")
        self.grid_columnconfigure(1, weight=1, uniform="")
        self.grid_columnconfigure(2, weight=0, uniform="")
        self._title_frame.grid_remove()
        self._presentation_header.grid(**_presentation_header_grid_options(True))
        self._window_controls.grid(
            row=0,
            column=2,
            columnspan=1,
            padx=(4, 12),
            pady=(4, 2),
            sticky="e",
        )
        self._hide_center_content()
        self._reset_center_rows()
        self.deck_a.grid_remove()
        self.deck_b.grid_remove()
        if workspace is Workspace.PREPARATION or not _compact_mixer_visible(
            self._compact_overlays_expanded
        ):
            self._mixer_container.grid_remove()
        else:
            self._mixer_container.grid(**_mixer_container_grid_options(True))
        self._center_panel.grid(**_center_panel_grid_options(True))
        if workspace is Workspace.PREPARATION:
            rows = _compact_preparation_rows()
            self._configure_catalog_search_layout(True)
            self._center_panel.grid_rowconfigure(rows["catalog"], weight=1, minsize=120)
            self._compact_preparation.grid(
                row=rows["live_status"], column=0, padx=8, pady=(6, 2), sticky="ew"
            )
            self._search_frame.grid(row=rows["search"], column=0, padx=8, pady=2, sticky="ew")
            self._summary.grid(row=rows["summary"], column=0, padx=10, pady=(1, 0), sticky="w")
            self._catalog.grid(row=rows["catalog"], column=0, padx=8, pady=3, sticky="nsew")
            self._compact_preparation_tools.grid(
                row=rows["tools"], column=0, padx=8, pady=(2, 4), sticky="ew"
            )
            if self._directory_progress_visible:
                self._directory_progress_frame.grid(
                    row=rows["progress"], column=0, padx=8, pady=(0, 3), sticky="ew"
                )
            if self._compact_playlist_expanded:
                self._saved_toolbar.grid(
                    row=rows["playlist"], column=0, padx=8, pady=(0, 4), sticky="ew"
                )
            if schedule_reassertion:
                self.schedule(50, self._ensure_compact_layout_exclusive)
                self.schedule(250, self._ensure_compact_layout_exclusive)
            return
        rows = _compact_live_rows()
        self._compact_decks_frame.grid(
            row=rows["decks"], column=0, padx=8, pady=(6, 3), sticky="ew"
        )
        for deck_id, deck in self._latest_decks.items():
            (self.compact_deck_a if deck_id == "A" else self.compact_deck_b).render(deck)
        self._crossfader_bar.grid(row=rows["crossfader"], column=0, padx=8, pady=3, sticky="ew")
        self._compact_overlay_frame.grid(
            row=rows["overlays"], column=0, padx=8, pady=(2, 2), sticky="ew"
        )
        self._queue_header.grid(
            row=rows["queue_header"], column=0, padx=8, pady=(2, 0), sticky="ew"
        )
        self._queue_toolbar.grid(row=rows["queue_toolbar"], column=0, padx=8, pady=0, sticky="ew")
        if self._directory_progress_visible:
            self._directory_progress_frame.grid(
                row=rows["directory_progress"],
                column=0,
                padx=8,
                pady=1,
                sticky="ew",
            )
        self._center_panel.grid_rowconfigure(rows["queue"], weight=1, minsize=80)
        self._queue.grid(row=rows["queue"], column=0, padx=8, pady=(2, 6), sticky="nsew")
        # Startup catalog/queue population can finish after the first presentation
        # decision. Reassert the mutually exclusive compact tree after those idle
        # callbacks without touching any domain state.
        if schedule_reassertion:
            self.schedule(50, self._ensure_compact_layout_exclusive)
            self.schedule(250, self._ensure_compact_layout_exclusive)

    def _ensure_compact_layout_exclusive(self) -> None:
        coordinator = self._presentation_coordinator
        if (
            not self._compact_layout_active
            or coordinator is None
            or coordinator.state.resolved is not ResolvedPresentation.COMPACT
        ):
            return
        self._show_compact_layout(
            coordinator.state.workspace,
            schedule_reassertion=False,
        )

    def _focus_workspace(self, selected: Workspace) -> None:
        """Move keyboard focus without changing selection or domain state."""
        if selected is Workspace.LIVE:
            target = self._automatic_queue_button
        elif self.__dict__.get("_compact_layout_active", False):
            target = self._search
        else:
            target = self._search
        target.focus_set()

    def _refresh_presentation_header(self, state: PresentationState) -> None:
        live_selected = state.workspace is Workspace.LIVE
        self._workspace_live_button.configure(
            fg_color="#1f6aa5" if live_selected else theme.SURFACE_RAISED
        )
        self._workspace_preparation_button.configure(
            fg_color="#1f6aa5" if not live_selected else theme.SURFACE_RAISED
        )
        mode_text = {
            PresentationPreference.AUTO: "AUTO",
            PresentationPreference.LARGE: "GROSS",
            PresentationPreference.COMPACT: "KOMPAKT",
        }[state.preference]
        resolved_note = ""
        if state.resolved is ResolvedPresentation.COMPACT:
            resolved_note = " · KOMPAKT"
        self._presentation_mode_button.configure(text=f"Ansicht: {mode_text} ▾")
        self._render_global_status(resolved_note)

    def _render_global_status(self, resolved_note: str = "") -> None:
        if not resolved_note and self._presentation_coordinator is not None:
            if self._presentation_coordinator.state.resolved is ResolvedPresentation.COMPACT:
                resolved_note = " · KOMPAKT"
        status = self._presentation_status
        text = global_status_text(status, resolved_note)
        color = theme.WARNING if status.warning else theme.TEXT_MUTED
        self._global_status_label.configure(text=text, text_color=color)
        self._compact_preparation_status.configure(text=text, text_color=color)

    def _schedule_cursor_restore(self) -> None:
        """Restore the pointer after Windows leaves its native move/resize loop."""

        pending = self._cursor_restore_after_id
        if pending is not None:
            try:
                self.after_cancel(pending)
            except TclError:
                pass
            self._scheduled_after_ids.discard(pending)

        def restore_window_cursor() -> None:
            self._cursor_restore_after_id = None
            if not bool(self.attributes("-fullscreen")):
                self.configure(cursor="arrow")

        self._cursor_restore_after_id = str(self.schedule(120, restore_window_cursor))

    def _focus_search(self) -> None:
        coordinator = self._presentation_coordinator
        if (
            coordinator is not None
            and coordinator.state.resolved is ResolvedPresentation.COMPACT
            and coordinator.state.workspace is not Workspace.PREPARATION
        ):
            self._select_workspace(Workspace.PREPARATION)
            self.schedule(0, self._search.focus_set)
            return
        self._search.focus_set()

    def show_overlay_status(self, runtime: OverlayRuntime) -> None:
        """Render one event-driven overlay snapshot on the Tk thread."""

        self._overlay_runtime = runtime
        polling_statuses = {
            OverlayStatus.PREPARING,
            OverlayStatus.FADING_IN,
            OverlayStatus.PLAYING,
            OverlayStatus.FADING_OUT,
            OverlayStatus.STOPPING,
        }
        if runtime.status in polling_statuses and not self._overlay_tick_active:
            self._overlay_tick_active = True
            self.schedule(100, self._overlay_position_tick)
        self._render_overlay()
        if runtime.status == OverlayStatus.FAILED:
            self._show_overlay_notice(runtime.error or "Jingle konnte nicht gestartet werden")
        if runtime.status == OverlayStatus.FINISHED:
            generation = runtime.generation

            def show_ready_after_finish() -> None:
                if (
                    self._overlay_runtime.generation == generation
                    and self._overlay_runtime.status == OverlayStatus.FINISHED
                ):
                    self._overlay_panel.render(
                        self._overlay_view_model(state_override=OverlayState.READY)
                    )

            self.schedule(1200, show_ready_after_finish)

    def show_ducking_status(self, factor: float, phase: str) -> None:
        """Apply a worker-originated ducking event to both deck indicators."""

        self._overlay_ducking_factor = factor
        self._overlay_ducking_phase = phase
        self.deck_a.show_ducking(factor, phase)
        self.deck_b.show_ducking(factor, phase)

    def _render_overlay(self) -> None:
        model = self._overlay_view_model()
        self._overlay_panel.render(model)
        expanded = bool(self._mixer_panel.winfo_ismapped())
        self._mixer_toggle.configure(
            text=mixer_overlay_header_text(
                expanded=expanded,
                state=model.state,
                name=model.active_name or model.selected_name,
            )
        )
        if collapsed_overlay_stop_visible(expanded=expanded, state=model.state):
            self._mixer_overlay_stop.grid()
        else:
            self._mixer_overlay_stop.grid_remove()
        active = model.state in {
            OverlayState.PREPARING,
            OverlayState.FADING_IN,
            OverlayState.PLAYING,
            OverlayState.FADING_OUT,
        }
        active_name = model.active_name or model.selected_name
        self._compact_overlay_status.configure(
            text=(f"Aktiver Jingle: {active_name}" if active else "Kein Jingle aktiv"),
            text_color=theme.WARNING if active else theme.TEXT_MUTED,
        )
        if active:
            self._compact_overlay_stop.grid()
        else:
            self._compact_overlay_stop.grid_remove()

    def _overlay_view_model(
        self,
        *,
        state_override: OverlayState | None = None,
    ) -> OverlayViewModel:
        """Build one immutable presentation snapshot without changing runtime state."""

        selected_name = (
            self._selected_overlay.definition.name if self._selected_overlay is not None else ""
        )
        definition = self._overlay_runtime.definition
        status_map = {
            OverlayStatus.IDLE: OverlayState.READY,
            OverlayStatus.PREPARING: OverlayState.PREPARING,
            OverlayStatus.READY: OverlayState.READY,
            OverlayStatus.FADING_IN: OverlayState.FADING_IN,
            OverlayStatus.PLAYING: OverlayState.PLAYING,
            OverlayStatus.FADING_OUT: OverlayState.FADING_OUT,
            OverlayStatus.STOPPING: OverlayState.FADING_OUT,
            OverlayStatus.FINISHED: OverlayState.FINISHED,
            OverlayStatus.FAILED: OverlayState.ERROR,
        }

        active_name = definition.name if definition is not None else ""
        ducking_db = (
            definition.ducking_db
            if definition is not None
            and definition.ducking_enabled
            and self._overlay_runtime.status
            in {
                OverlayStatus.FADING_IN,
                OverlayStatus.PLAYING,
                OverlayStatus.FADING_OUT,
            }
            else None
        )
        playback = self._overlay_runtime.playback
        display_definition = (
            self._selected_overlay.definition
            if state_override is not None and self._selected_overlay is not None
            else (
                definition
                if definition is not None
                else (
                    self._selected_overlay.definition
                    if self._selected_overlay is not None
                    else None
                )
            )
        )
        visible_position_ms = (
            max(0, self._overlay_runtime.position_ms - playback.cue_in_ms)
            if playback is not None and self._overlay_runtime.position_ms is not None
            else None
        )
        if visible_position_ms is not None:
            visible_position_ms = (visible_position_ms // 1000) * 1000
        return OverlayViewModel(
            state=state_override or status_map[self._overlay_runtime.status],
            selected_name=selected_name,
            active_name=active_name if state_override is None else "",
            category=(
                self._selected_overlay.definition.category
                if self._selected_overlay is not None
                else "Alle"
            ),
            duration_ms=(
                playback.cue_out_ms - playback.cue_in_ms
                if playback is not None and state_override is None
                else None
            ),
            position_ms=visible_position_ms if state_override is None else None,
            volume_percent=(
                display_definition.volume_percent if display_definition is not None else None
            ),
            ducking_db=ducking_db if state_override is None else None,
            error_message=self._overlay_runtime.error if state_override is None else "",
        )

    def overlay_diagnostics(self) -> dict[str, object]:
        """Return the current immutable overlay state for explicit reports."""

        definition = self._overlay_runtime.definition
        playback = self._overlay_runtime.playback
        controller_values = (
            self._overlay_controller.diagnostics() if self._overlay_controller is not None else {}
        )
        return {
            "status": self._overlay_runtime.status.value,
            "generation": self._overlay_runtime.generation,
            "overlay_id": definition.overlay_id if definition is not None else 0,
            "name": definition.name if definition is not None else "",
            "position_ms": self._overlay_runtime.position_ms or 0,
            "volume_percent": (definition.volume_percent if definition is not None else 0),
            "fade_in_ms": playback.fade_in_ms if playback is not None else 0,
            "fade_out_ms": playback.fade_out_ms if playback is not None else 0,
            "ducking_target_db": (
                definition.ducking_db
                if definition is not None and definition.ducking_enabled
                else 0.0
            ),
            "ducking_factor": self._overlay_ducking_factor,
            "ducking_phase": self._overlay_ducking_phase,
            "position_timer_active": self._overlay_tick_active,
            "error": self._overlay_runtime.error,
            **controller_values,
        }

    def _overlay_category_changed(self, category: str) -> None:
        records = self._overlay_snapshot.records_for_category(category)
        self._selected_overlay = records[0] if records else None
        names = tuple(record.definition.name for record in records)
        self._overlay_panel.set_choices(("Alle", *self._overlay_snapshot.categories), names)
        self._overlay_panel.select(
            category=category,
            overlay=(
                self._selected_overlay.definition.name
                if self._selected_overlay is not None
                else "Keine Jingles"
            ),
        )
        self._render_overlay()

    def _overlay_selection_changed(self, name: str) -> None:
        self._selected_overlay = next(
            (record for record in self._overlay_snapshot.records if record.definition.name == name),
            None,
        )
        self._render_overlay()

    def _start_selected_overlay(self) -> None:
        if self._overlay_controller is not None and self._selected_overlay is not None:
            self._overlay_controller.start(self._selected_overlay.definition)

    def _start_overlay_favorite(self, position: int) -> None:
        record = self._overlay_snapshot.favorites[position - 1]
        if record is None:
            self.show_queue_warning(
                f"Favoritenplatz {position} ist noch nicht belegt – über Verwalten zuweisen"
            )
            dialog = self._manage_overlays()
            if dialog is not None:
                dialog.assign_favorite(position)
            return
        if record.definition.overlay_id in self._overlay_snapshot.missing_file_ids:
            self._show_overlay_notice(
                f"„{record.definition.name}“ kann nicht gestartet werden: Datei fehlt"
            )
            self._logger.warning(
                "Overlay-Favorit %s nicht gestartet: Datei fehlt (%s)",
                position,
                record.definition.file_path,
            )
            return
        if not record.enabled:
            self._show_overlay_notice(
                f"„{record.definition.name}“ ist deaktiviert und kann nicht gestartet werden"
            )
            self._logger.warning(
                "Overlay-Favorit %s nicht gestartet: Overlay %s ist deaktiviert",
                position,
                record.definition.overlay_id,
            )
            return
        self._selected_overlay = record
        self._overlay_panel.select(
            category=record.definition.category or "Alle",
            overlay=record.definition.name,
        )
        if self._overlay_controller is not None:
            self._overlay_controller.start(record.definition)

    def _show_overlay_notice(self, message: str, *, error: bool = True) -> None:
        """Keep overlay failures local, expiring, and independent from queue UI."""

        self._overlay_notice_generation += 1
        generation = self._overlay_notice_generation
        self._overlay_panel.show_notice(message, error=error)

        def clear_notice() -> None:
            if generation == self._overlay_notice_generation:
                self._overlay_panel.show_notice("")

        self.schedule(5000, clear_notice)

    def _overlay_favorite_shortcut(self, event: Any) -> str | None:
        position = favorite_position_from_shortcut(
            str(getattr(event, "keysym", "")),
            True,
        )
        if position is None:
            return None
        focused = self.focus_get()
        top_level = focused.winfo_toplevel() if focused is not None else self
        dialog_active = self.grab_current() is not None or top_level is not self
        if not overlay_shortcut_allowed(focused, dialog_active=dialog_active):
            return None
        self._start_overlay_favorite(position)
        return "break"

    def _fade_out_overlay(self) -> None:
        if self._overlay_controller is not None:
            self._overlay_controller.fade_out()

    def _stop_overlay(self) -> None:
        if self._overlay_controller is not None:
            self._overlay_controller.stop()

    def _manage_overlays(self) -> OverlayManagementDialog | None:
        dialog = self._overlay_management_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.focus_existing()
            return dialog
        if self._overlay_service is None:
            return None
        self._overlay_management_dialog = OverlayManagementDialog(
            self,
            self._overlay_service,
            on_changed=self.refresh_overlays,
            on_preview=self._preview_overlay,
            active_overlay_id=self._active_overlay_id,
        )
        return self._overlay_management_dialog

    def _edit_overlay_favorite(self, position: int) -> None:
        record = self._overlay_snapshot.favorites[position - 1]
        if record is None:
            return
        dialog = self._manage_overlays()
        if dialog is not None:
            dialog.focus_record(record.definition.overlay_id)

    def _remove_overlay_favorite(self, position: int) -> None:
        record = self._overlay_snapshot.favorites[position - 1]
        if record is None or self._overlay_service is None:
            return
        if not ask_silent_yes_no(
            self,
            "Favoritenbelegung entfernen?",
            f"„{record.definition.name}“ von Favoritenplatz {position} entfernen?",
        ):
            return
        try:
            self._overlay_service.save(
                replace(record, favorite_position=None, keyboard_shortcut=None)
            )
        except (ValueError, KeyError) as exc:
            self._logger.warning("Favoritenbelegung konnte nicht entfernt werden: %s", exc)
            self.show_queue_warning(str(exc))
            return
        self.refresh_overlays()

    def _active_overlay_id(self) -> int | None:
        definition = self._overlay_runtime.definition
        if definition is None or self._overlay_runtime.status in {
            OverlayStatus.IDLE,
            OverlayStatus.FINISHED,
            OverlayStatus.FAILED,
        }:
            return None
        return definition.overlay_id

    def _preview_overlay(self, record: OverlayRecord) -> None:
        active_id = self._active_overlay_id()
        if (
            active_id is not None
            and active_id != record.definition.overlay_id
            and not ask_silent_yes_no(
                self,
                "Laufendes Jingle ersetzen?",
                "Die Vorhörfunktion verwendet den echten Overlaykanal. "
                "Das aktuell laufende Jingle wirklich ersetzen?",
            )
        ):
            return
        if self._overlay_controller is not None:
            self._overlay_controller.start(record.definition)

    def _overlay_position_tick(self) -> None:
        """Poll only VLC position/end state on a dedicated lightweight timer."""

        if not self._overlay_tick_active:
            return
        if self._overlay_runtime.status not in {
            OverlayStatus.PREPARING,
            OverlayStatus.FADING_IN,
            OverlayStatus.PLAYING,
            OverlayStatus.FADING_OUT,
            OverlayStatus.STOPPING,
        }:
            self._overlay_tick_active = False
            return
        if self._overlay_controller is not None:
            self._overlay_controller.update_position()
        if self._overlay_tick_active:
            self.schedule(100, self._overlay_position_tick)

    def _enable_keyboard_focus(self, root: Any) -> None:
        """Make interactive CustomTkinter controls reachable and visibly focused."""
        if not hasattr(root, "winfo_children"):
            return
        for widget in root.winfo_children():
            self._enable_keyboard_focus_control(widget)
            self._enable_keyboard_focus(widget)

    def _enable_keyboard_focus_control(self, widget: Any) -> None:
        """Configure one interactive control without recursively walking its subtree."""

        if not isinstance(widget, (ctk.CTkButton, ctk.CTkEntry, ctk.CTkSwitch)) or getattr(
            widget, "_party_focus_ready", False
        ):
            return
        setattr(widget, "_party_focus_ready", True)
        if isinstance(widget, ctk.CTkEntry):
            widget._entry.configure(takefocus=True)
        elif hasattr(widget, "_canvas"):
            widget._canvas.configure(takefocus=True)
        original_color = widget.cget("border_color")
        original_width = widget.cget("border_width")
        widget.bind(
            "<FocusIn>",
            lambda _event, item=widget, width=original_width: item.configure(
                border_color=theme.DECK_ACCENTS["A"],
                border_width=max(2, width),
            ),
            add="+",
        )
        widget.bind(
            "<FocusOut>",
            lambda _event, item=widget, color=original_color, width=original_width: item.configure(
                border_color=color, border_width=width
            ),
            add="+",
        )
        if isinstance(widget, ctk.CTkButton):
            widget.bind("<Return>", lambda _event, item=widget: item.invoke(), add="+")
            widget.bind("<space>", lambda _event, item=widget: item.invoke(), add="+")
        elif isinstance(widget, ctk.CTkSwitch):
            widget.bind("<Return>", lambda _event, item=widget: item.toggle(), add="+")
            widget.bind("<space>", lambda _event, item=widget: item.toggle(), add="+")

    def _request_layout_refresh(self, area: str) -> None:
        """Coalesce many row geometry changes into one measured idle boundary."""
        self._render_counters[f"{area}_layout_refresh_requested_total"] += 1
        if self._layout_refresh_pending[area]:
            return
        self._layout_refresh_pending[area] = True
        self._publish_layout_state()

        def apply_layout_refresh() -> None:
            self._layout_refresh_pending[area] = False
            with self._performance.measure(f"gui.layout.{area}_refresh", warning_threshold_ms=25.0):
                self._render_counters[f"{area}_layout_refresh_executed_total"] += 1
            self._publish_layout_state()

        apply_layout_refresh.__name__ = f"{area}_layout_refresh"
        self.schedule(25, apply_layout_refresh)

    def _request_focus_setup(self, root: Any) -> None:
        """Defer focus bindings and visit each newly created subtree only once."""
        self._render_counters["focus_request_total"] += 1
        if not any(pending is root for pending in self._focus_pending_roots):
            self._focus_pending_roots.append(root)
        if self._focus_callback_pending:
            return
        self._focus_callback_pending = True
        self._publish_layout_state()
        self.schedule(50, self._apply_focus_setup)

    def _apply_focus_setup(self) -> None:
        if (
            self._catalog_dirty_scheduler.pending_count
            or self._queue_dirty_scheduler.pending_count
            or any(self._layout_refresh_pending.values())
        ):
            self.schedule(25, self._apply_focus_setup)
            return
        if self._focus_pending_roots:
            self._focus_pending_widgets.extend(self._focus_pending_roots)
            self._focus_pending_roots = []
        with self._performance.measure("gui.layout.focus_apply", warning_threshold_ms=25.0):
            processed = 0
            while self._focus_pending_widgets and processed < 4:
                widget = self._focus_pending_widgets.pop()
                if getattr(widget, "_party_focus_walk_ready", False):
                    continue
                setattr(widget, "_party_focus_walk_ready", True)
                interactive = isinstance(
                    widget,
                    (ctk.CTkButton, ctk.CTkEntry, ctk.CTkSwitch),
                )
                self._enable_keyboard_focus_control(widget)
                # Internal Canvas/Label children of one CTk control are rendering
                # details, never additional tab stops.
                layout_container = widget is self or isinstance(
                    widget,
                    (ctk.CTkFrame, ctk.CTkScrollableFrame),
                )
                if not interactive and layout_container and hasattr(widget, "winfo_children"):
                    self._focus_pending_widgets.extend(widget.winfo_children())
                processed += 1
            self._render_counters["focus_apply_total"] += 1
        if self._focus_pending_widgets or self._focus_pending_roots:
            self.schedule(15, self._apply_focus_setup)
        else:
            self._focus_callback_pending = False
        self._publish_layout_state()

    def show_track_cues_changed(self, track_id: int, has_manual_cues: bool) -> None:
        """Mark matching catalog and queue rows dirty without rebuilding either list."""
        index = next(
            (
                index
                for index, model in enumerate(self._catalog_view_models)
                if model.track.id == track_id
            ),
            None,
        )
        if index is not None:
            current = self._catalog_view_models[index]
            updated = replace(current, has_manual_cues=has_manual_cues)
            if updated != current:
                self._catalog_view_models[index] = updated
                self._catalog_dirty_scheduler.mark([index])
        if has_manual_cues:
            self._queue_inherited_manual_track_ids.add(track_id)
        else:
            self._queue_inherited_manual_track_ids.discard(track_id)
        for entry in self._queue_entries:
            if entry.track_id == track_id:
                self.show_queue_entry(entry, self._queue_tracks.get(track_id))

    def show_track_metadata_changed(self, track: Track) -> None:
        """Replace one track snapshot and dirty only matching visible rows."""
        for index, current in enumerate(self._catalog_view_models):
            if current.track.id == track.id:
                self._catalog_view_models[index] = replace(current, track=track)
                self._catalog_dirty_scheduler.mark([index])
        if track.id in self._queue_tracks:
            self._queue_tracks[track.id] = track
        for entry in self._queue_entries:
            if entry.track_id == track.id:
                self.show_queue_entry(entry, track)

    def _grow_catalog_pool(self) -> None:
        """Create the next small row reserve only when the user scrolls."""
        previous = self._catalog_pool_target
        self._catalog_pool_target = min(
            len(self._catalog_view_models), previous + self._CATALOG_POOL_GROWTH
        )
        if self._catalog_pool_target <= previous:
            return
        self._catalog_dirty_scheduler.mark(list(range(previous, self._catalog_pool_target)))
        self._request_layout_refresh("catalog")
        self._publish_layout_state()

    def _catalog_scrolled(self, _units: int = 0) -> None:
        """Measure the user-scroll application and request lazy catalog growth."""
        with self._performance.measure("gui.layout.scroll_apply", warning_threshold_ms=10.0):
            self._render_counters["scroll_position_set_total"] += 1
            self._grow_catalog_pool()

    def _queue_scrolled(self, units: int) -> None:
        """Rebind the fixed row pool to the next logical queue window."""
        with self._performance.measure("gui.layout.scroll_apply", warning_threshold_ms=10.0):
            self._render_counters["scroll_position_set_total"] += 1
            maximum_start = max(0, len(self._queue_entries) - self._queue_pool_target)
            next_start = max(
                0,
                min(maximum_start, self._queue_visible_start_index + units),
            )
            if next_start == self._queue_visible_start_index:
                return
            self._apply_queue_page_change(next_start)

    def _apply_queue_page_change(self, visible_start_index: int) -> None:
        """Route local queue navigation through the revisioned event model."""
        event = QueueViewEvent(
            QueueViewEventType.PAGE_CHANGED,
            None,
            self._queue_revision.current,
            visible_start_index,
        )
        if not self._queue_revision.accepts(event):
            return
        self._queue_visible_start_index = visible_start_index
        self._queue_page = visible_start_index // max(1, self._queue_pool_target)
        self._render_queue_page()

    def _update_optionmenu(self, key: str, menu: Any, values: list[str], selected: str) -> None:
        """Avoid CTkOptionMenu redraws when values and selection are unchanged."""
        state = (tuple(values), selected)
        previous = self._optionmenu_cache.get(key)
        if previous == state:
            return
        configure_values, set_selection = _optionmenu_changes(previous, values, selected)
        with self._performance.measure("gui.layout.optionmenu_update", warning_threshold_ms=10.0):
            if configure_values:
                menu.configure(values=values)
                self._render_counters["optionmenu_configure_total"] += 1
            if set_selection:
                menu.set(selected)
                self._render_counters["optionmenu_set_total"] += 1
        self._optionmenu_cache[key] = state

    def _publish_layout_state(self) -> None:
        self._callback_state.update_layout_state(
            pending_layout_refreshes=sum(self._layout_refresh_pending.values()),
            pending_focus_request=self._focus_callback_pending,
            pending_catalog_chunks=self._catalog_dirty_scheduler.pending_count,
            pending_queue_chunks=self._queue_dirty_scheduler.pending_count,
            catalog_rows_created=len(self._catalog_rows),
            queue_rows_created=len(self._queue_rows),
        )

    def show_queue(self, entries: list[QueueEntry], tracks: dict[int, Track]) -> None:
        """Measure and schedule one complete synchronous queue-view update."""
        with self._performance.measure("gui.queue_render.total", warning_threshold_ms=100.0):
            self._show_queue_impl(entries, tracks)

    def show_restored_queue_entries(self, queue_ids: set[int]) -> None:
        """Mark queue rows that were present when a session was recovered."""
        normalized = set(queue_ids)
        if normalized == self._restored_queue_ids:
            return
        self._restored_queue_ids = normalized
        self._queue_render_signature = None
        if self._queue_entries:
            self._render_queue_page()

    def show_queue_cue_warnings(self, warnings: dict[int, str]) -> None:
        """Publish cue risks separately from persisted queue state."""
        normalized = dict(warnings)
        if normalized == self._queue_cue_warnings:
            return
        self._queue_cue_warnings = normalized
        self._queue_render_signature = None

    def _show_queue_impl(self, entries: list[QueueEntry], tracks: dict[int, Track]) -> None:
        with self._performance.measure(
            "gui.queue_render.fetch_view_models", warning_threshold_ms=25.0
        ):
            self._queue_entries = entries
            self._queue_tracks = tracks
            if self._queue_selected_id not in {entry.queue_id for entry in entries}:
                self._queue_selected_id = None
            self._update_delete_selected_queue_button()
            self._queue_inherited_manual_track_ids = (
                self._cue_controller.manual_track_ids(list(tracks))
                if self._cue_controller is not None
                else set()
            )
            signature = (
                tuple(entries),
                tuple(tracks[track_id] for track_id in sorted(tracks)),
                tuple(sorted(self._queue_inherited_manual_track_ids)),
            )
        if signature == self._queue_render_signature:
            return
        self._queue_render_signature = signature
        page_count = max(1, (len(entries) + self._queue_page_size - 1) // self._queue_page_size)
        self._queue_page = min(self._queue_page, page_count - 1)
        self._render_queue_page()

    def show_queue_entry(self, entry: QueueEntry, track: Track | None) -> None:
        """Update one visible queue row instead of rebuilding the current page."""
        index = self._queue_row_index_by_id.get(entry.queue_id)
        if index is None:
            return
        with self._performance.measure(
            "transition_completion.queue.build_view_models",
            warning_threshold_ms=5.0,
        ):
            self._queue_view_models[index] = QueueEntryViewModel(
                entry,
                track,
                not entry.has_cue_overrides
                and entry.track_id in self._queue_inherited_manual_track_ids,
                entry.queue_id == self._queue_selected_id,
                entry.request_count,
                entry.queue_id in self._restored_queue_ids,
                self._queue_cue_warnings.get(entry.queue_id, ""),
            )
        self._queue_dirty_scheduler.mark([index])
        self._publish_layout_state()

    def show_queue_events(
        self,
        events: tuple[QueueViewEvent, ...],
        entries: list[QueueEntry],
        tracks: dict[int, Track],
    ) -> None:
        """Apply revisioned queue changes, targeting rows when structure is stable."""
        accepted = tuple(event for event in events if self._queue_revision.accepts(event))
        if not accepted:
            return
        for event in accepted:
            if event.event_type != QueueViewEventType.SELECTION_CHANGED:
                continue
            if event.selected:
                self._queue_selected_id = event.queue_entry_id
            elif self._queue_selected_id == event.queue_entry_id:
                self._queue_selected_id = None
        self._update_delete_selected_queue_button()
        structural = {
            QueueViewEventType.ENTRY_ADDED,
            QueueViewEventType.ENTRY_REMOVED,
            QueueViewEventType.ENTRY_MOVED,
            QueueViewEventType.PAGE_CHANGED,
            QueueViewEventType.RESET,
        }
        if any(event.event_type in structural for event in accepted):
            self.show_queue(entries, tracks)
            return
        self._queue_entries = entries
        self._queue_tracks = tracks
        entries_by_id = {entry.queue_id: entry for entry in entries}
        for event in accepted:
            if event.queue_entry_id is None:
                continue
            if event.event_type == QueueViewEventType.SELECTION_CHANGED:
                index = self._queue_row_index_by_id.get(event.queue_entry_id)
                if index is not None:
                    model = self._queue_view_models[index]
                    assert model is not None
                    selected = bool(event.selected)
                    self._queue_view_models[index] = replace(model, selected=selected)
                    if index < len(self._queue_rows):
                        row = self._queue_rows[index]
                        configured_before = row.configured_widget_count
                        if row.update_selection(selected):
                            self._queue_lifecycle_counters["configured_widget_count"] += (
                                row.configured_widget_count - configured_before
                            )
                            self._queue_lifecycle_counters["updated_row_count"] += 1
                continue
            entry = entries_by_id.get(event.queue_entry_id)
            if event.event_type == QueueViewEventType.ENTRY_STATUS_CHANGED and entry is not None:
                index = self._queue_row_index_by_id.get(event.queue_entry_id)
                if index is not None:
                    model = self._queue_view_models[index]
                    assert model is not None
                    self._queue_view_models[index] = replace(model, entry=entry)
                    if index < len(self._queue_rows):
                        row = self._queue_rows[index]
                        configured_before = row.configured_widget_count
                        if row.update_status(entry.status):
                            self._queue_lifecycle_counters["configured_widget_count"] += (
                                row.configured_widget_count - configured_before
                            )
                            self._queue_lifecycle_counters["updated_row_count"] += 1
                continue
            if event.event_type == QueueViewEventType.ENTRY_CONTENT_CHANGED and entry is not None:
                index = self._queue_row_index_by_id.get(event.queue_entry_id)
                if index is not None:
                    model = self._queue_view_models[index]
                    assert model is not None
                    request_only = replace(model.entry, request_count=entry.request_count) == entry
                    if request_only:
                        self._queue_view_models[index] = replace(
                            model,
                            entry=entry,
                            request_count=entry.request_count,
                        )
                        if index < len(self._queue_rows):
                            row = self._queue_rows[index]
                            configured_before = row.configured_widget_count
                            if row.update_request_count(entry.request_count):
                                self._queue_lifecycle_counters["configured_widget_count"] += (
                                    row.configured_widget_count - configured_before
                                )
                                self._queue_lifecycle_counters["updated_row_count"] += 1
                        continue
            if entry is not None:
                self.show_queue_entry(entry, tracks.get(entry.track_id))

    def _render_queue_page(self) -> None:
        with self._performance.measure("gui.queue_render.schedule", warning_threshold_ms=25.0):
            self._render_queue_page_impl()

    def _render_queue_page_impl(self) -> None:
        if self._queue_entries:
            self._queue_empty_label.pack_forget()
        else:
            self._queue_empty_label.pack(padx=16, pady=32)
        available_height = max(1, int(self._queue.winfo_height()))
        self._queue_pool_target = _queue_pool_size(
            available_height,
            row_height=self._QUEUE_ESTIMATED_ROW_HEIGHT,
            minimum=self._QUEUE_MINIMUM_POOL_SIZE,
            overscan=self._QUEUE_OVERSCAN_ROWS,
            maximum=self._QUEUE_MAXIMUM_POOL_SIZE,
        )
        self._queue_page_size = self._queue_pool_target
        maximum_start = max(0, len(self._queue_entries) - self._queue_pool_target)
        self._queue_visible_start_index = min(self._queue_visible_start_index, maximum_start)
        with self._performance.measure("gui.queue_render.layout", warning_threshold_ms=15.0):
            logical_end = min(
                len(self._queue_entries),
                self._queue_visible_start_index + self._queue_pool_target,
            )
            self._queue_page_label.configure(
                text=(
                    f"{self._queue_visible_start_index + 1}–{logical_end}"
                    f"/{len(self._queue_entries)}"
                    if self._queue_entries
                    else "Queue leer"
                )
            )
            self._queue_previous_button.configure(
                state="normal" if self._queue_visible_start_index else "disabled"
            )
            self._queue_next_button.configure(
                state="normal" if logical_end < len(self._queue_entries) else "disabled"
            )
        start = self._queue_visible_start_index
        visible = self._queue_entries[start : start + self._queue_pool_target]
        self._queue_view_models = []
        self._queue_row_index_by_id = {}
        model_count = _queue_model_count(self._queue_pool_target, len(self._queue_rows))
        for index in range(model_count):
            entry = (
                visible[index] if index < self._queue_pool_target and index < len(visible) else None
            )
            if entry is not None:
                self._queue_row_index_by_id[entry.queue_id] = index
            self._queue_view_models.append(
                QueueEntryViewModel(
                    entry,
                    self._queue_tracks.get(entry.track_id),
                    not entry.has_cue_overrides
                    and entry.track_id in self._queue_inherited_manual_track_ids,
                    entry.queue_id == self._queue_selected_id,
                    entry.request_count,
                    entry.queue_id in self._restored_queue_ids,
                    self._queue_cue_warnings.get(entry.queue_id, ""),
                )
                if entry is not None
                else None
            )
        dirty_count = len(self._queue_view_models)
        if self._queue_scroll_restore_pending:
            self._render_counters["scroll_restore_coalesced_total"] += 1
        self._queue_scroll_restore_pending = True
        self._queue_render_in_progress = True
        self._queue_dirty_scheduler.replace(list(range(dirty_count)))
        self._render_counters["scroll_restore_requested_total"] += 1
        self._request_layout_refresh("queue")
        self._publish_layout_state()

    def _record_render_chunk(self, area: str, duration_ms: float, rows: int) -> None:
        """Record active GUI work for one catalog or queue render chunk."""
        self._render_counters[f"{area}_chunk_count_total"] += 1
        self._performance.record(f"gui.{area}_render.chunk", duration_ms, 8.0, {"rows": rows})

    def _record_render_complete(self, area: str, statistics: RenderBatchStatistics) -> None:
        """Record complete render wall time, including intentional inter-chunk gaps."""
        self._performance.record(
            f"gui.{area}_render.wall_clock",
            statistics.wall_clock_duration_ms,
            250.0,
            {
                "chunk_count": statistics.chunk_count,
                "maximum_chunk_duration_ms": statistics.maximum_chunk_duration_ms,
                "average_chunk_duration_ms": statistics.average_chunk_duration_ms,
                "maximum_rows_per_chunk": statistics.maximum_rows_per_chunk,
                "average_rows_per_chunk": statistics.average_rows_per_chunk,
                "maximum_gap_between_chunks_ms": statistics.maximum_gap_between_chunks_ms,
            },
        )
        if area == "catalog":
            self._performance.record(
                "gui.catalog_render.full_page_complete",
                (monotonic() - self._catalog_render_started_at) * 1000.0,
                6000.0,
            )
        else:
            self._queue_render_in_progress = False
            if self._queue_scroll_restore_pending:
                self._queue_scroll_restore_pending = False
                self._queue._parent_canvas.yview_moveto(0.0)
                self._render_counters["scroll_position_set_total"] += 1
                self._render_counters["scroll_restore_executed_total"] += 1
            if self._queue_focus_needed:
                self._queue_focus_needed = False
                self._request_focus_setup(self._queue)

    def _render_queue_row(self, index: int) -> None:
        created = False
        if index >= len(self._queue_rows):
            if index >= len(self._queue_view_models) or self._queue_view_models[index] is None:
                return
            with self._performance.measure(
                "gui.queue_render.create_rows", warning_threshold_ms=25.0
            ):
                row_view = QueueRowView(
                    self._queue,
                    self._queue_row_callbacks(),
                    self._performance,
                    self._callback_state,
                    self._queue_tooltip_manager,
                )
                self._queue_rows.append(row_view)
                self._queue_focus_needed = True
                self._performance.record("gui.queue_render.created_widget_count", 1.0, 50.0)
                self._render_counters["widgets_created_total"] += 6
                self._queue_widget_creation_count += 1
                self._queue_lifecycle_counters["created_widget_count"] += 6
                created = True
        if created:
            self._publish_layout_state()
            return
        row = self._queue_rows[index]
        old_id = row.entry_id
        configured_before = row.configured_widget_count
        with self._performance.measure("gui.queue_render.bind_rows", warning_threshold_ms=10.0):
            with self._performance.measure(
                "gui.queue_render.configure_widgets", warning_threshold_ms=10.0
            ):
                changed = row.bind_entry(self._queue_view_models[index])
        self._queue_lifecycle_counters["configured_widget_count"] += (
            row.configured_widget_count - configured_before
        )
        if changed:
            self._queue_rebind_count += 1
            lifecycle_key = "rebound_row_count" if old_id != row.entry_id else "updated_row_count"
            self._queue_lifecycle_counters[lifecycle_key] += 1
            operation = (
                "gui.queue_render.rebound_row_count"
                if old_id != row.entry_id
                else "gui.queue_render.updated_row_count"
            )
            self._performance.record(operation, 1.0, 50.0)
        self._publish_layout_state()

    def _queue_row_requires_creation(self, index: int) -> bool:
        """Distinguish occupied queue slots from intentionally empty pool placeholders."""
        return (
            index >= len(self._queue_rows)
            and index < len(self._queue_view_models)
            and self._queue_view_models[index] is not None
        )

    def _queue_row_callbacks(self) -> dict[str, Callable[[int], None]]:
        return {
            "cue": self._edit_queue_cues,
            "deck_a": lambda queue_id: self._load_queue(queue_id, "A"),
            "deck_b": lambda queue_id: self._load_queue(queue_id, "B"),
            "up": lambda queue_id: self._move_queue(queue_id, -1),
            "down": lambda queue_id: self._move_queue(queue_id, 1),
            "top": self._move_queue_top,
            "end": self._move_queue_end,
            "priority": self._set_queue_priority,
            "lock": self._toggle_queue_lock,
            "equalizer": self._assign_queue_track_equalizer,
            "equalizer_remove": self._remove_queue_track_equalizer,
            "played": self._mark_queue_played,
            "skip": self._mark_queue_skipped,
            "retry": self._retry_queue,
            "override_skip": self._play_repetition_skipped_queue,
            "reset": self._reset_queue_played,
            "remove": self._remove_queue,
            "remove_prepared": self._remove_prepared_queue,
            "move_prepared_up": lambda queue_id: self._move_prepared_queue(queue_id, -1),
            "move_prepared_down": lambda queue_id: self._move_prepared_queue(queue_id, 1),
            "select": self._select_queue,
        }

    def _queue_previous_page(self) -> None:
        if self._queue_visible_start_index > 0:
            self._apply_queue_page_change(
                max(0, self._queue_visible_start_index - self._queue_pool_target)
            )

    def _queue_next_page(self) -> None:
        maximum_start = max(0, len(self._queue_entries) - self._queue_pool_target)
        if self._queue_visible_start_index < maximum_start:
            self._apply_queue_page_change(
                min(
                    maximum_start,
                    self._queue_visible_start_index + self._queue_pool_target,
                )
            )

    def show_queue_stats(self, stats: QueueStats) -> None:
        text = f"{stats.total_tracks} Titel · {_duration_text(stats.total_duration)}"
        if text != self._queue_stats_text:
            self._queue_stats.configure(text=text)
            self._queue_stats_text = text
        self._queue_stats_tooltip.set_text(
            f"Gesamtlaufzeit {_duration_text(stats.total_duration)} · "
            f"verbleibende Laufzeit {_duration_text(stats.remaining_duration)}"
        )

    def show_queue_origin(self, text: str) -> None:
        self._queue_source_button.configure(text=f"Quelle: {_ellipsize(text, 28)} ▾")
        self._queue_source_tooltip.set_text(f"Aktive Queue-Quelle: {text}")
        self._presentation_status = replace(self._presentation_status, source=text)
        self._render_global_status()

    def show_deck(self, deck: Deck) -> None:
        self._latest_decks[deck.deck_id] = deck
        if self._compact_layout_active:
            (self.compact_deck_a if deck.deck_id == "A" else self.compact_deck_b).render(deck)
        else:
            (self.deck_a if deck.deck_id == "A" else self.deck_b).render(deck)
        self._deck_on_air[deck.deck_id] = deck.is_on_air
        if deck.is_on_air:
            status_text = "ON AIR"
            status_color = theme.ON_AIR
        elif deck.loaded_track is not None:
            status_text = "Wartend"
            status_color = theme.READY
        else:
            status_text = "Keine Titel geladen"
            status_color = theme.TEXT_MUTED
        self._presentation_status = replace(
            self._presentation_status,
            **{f"deck_{deck.deck_id.casefold()}": compact_deck_presentation(deck).state},
        )
        self._render_global_status()
        if force_live_for_operational_update(
            startup_guard=self._presentation_startup_guard,
            active=deck.is_on_air,
        ):
            self._force_live_workspace("active-playback")
        deck_status = (f"DECK {deck.deck_id}\n{status_text}", status_color)
        if self._deck_status_cache.get(deck.deck_id) != deck_status:
            self._deck_status_labels[deck.deck_id].configure(
                text=deck_status[0],
                text_color=deck_status[1],
            )
            self._deck_status_cache[deck.deck_id] = deck_status
        active = [deck_id for deck_id in ("A", "B") if self._deck_on_air[deck_id]]
        if active:
            self._compact_on_air_stop.grid()
        else:
            self._compact_on_air_stop.grid_remove()
        label = " + ".join(active) if active else "KEINES"
        summary = (
            f"ON AIR: {label}",
            theme.ON_AIR if active else theme.TEXT_MUTED,
        )
        if summary != self._on_air_summary_cache:
            self._on_air_summary.configure(text=summary[0], text_color=summary[1])
            self._on_air_summary_cache = summary

    def show_deck_cover(self, deck_id: str, image_data: object | None) -> None:
        """Create and apply the Tk-specific part of an already prepared cover."""
        with (
            self._callback_state.track("cover_apply"),
            self._performance.measure(
                "gui.cover_apply.total",
                warning_threshold_ms=100.0,
                context={"deck": deck_id},
            ),
        ):
            self._show_deck_cover_impl(deck_id, image_data)

    def _show_deck_cover_impl(self, deck_id: str, image_data: object | None) -> None:
        """Apply a worker-prepared PIL image, retaining byte fallback compatibility."""
        panel = self.deck_a if deck_id == "A" else self.deck_b
        with self._performance.measure("gui.cover_apply.prepare_result", warning_threshold_ms=50.0):
            if not image_data:
                self._clear_deck_cover(deck_id, "Kein Cover")
                return
            if isinstance(image_data, Image.Image):
                canvas = image_data
            else:
                if not isinstance(image_data, (bytes, bytearray)):
                    self._clear_deck_cover(deck_id, "Cover nicht lesbar")
                    return
                try:
                    with Image.open(BytesIO(bytes(image_data))) as source:
                        fitted = ImageOps.contain(source.convert("RGB"), (190, 160))
                        canvas = Image.new("RGB", (190, 160), "#20242b")
                        offset = ((190 - fitted.width) // 2, (160 - fitted.height) // 2)
                        canvas.paste(fitted, offset)
                except (OSError, TypeError, ValueError):
                    self._clear_deck_cover(deck_id, "Cover nicht lesbar")
                    return
        with self._performance.measure(
            "gui.cover_apply.create_tk_image", warning_threshold_ms=50.0
        ):
            cover = ctk.CTkImage(light_image=canvas, dark_image=canvas, size=(190, 160))
        previous_cover = self._cover_images.get(deck_id)
        with self._performance.measure(
            "gui.cover_apply.configure_widget", warning_threshold_ms=25.0
        ):
            panel._cover.configure(image=cover, text="")
        with self._performance.measure("gui.cover_apply.layout", warning_threshold_ms=10.0):
            self._cover_images[deck_id] = cover
        with self._performance.measure(
            "gui.cover_apply.release_old_reference", warning_threshold_ms=10.0
        ):
            del previous_cover

    def _clear_deck_cover(self, deck_id: str, text: str) -> None:
        """Clear both CTkImage state and its retained native Tk image name."""
        panel = self.deck_a if deck_id == "A" else self.deck_b
        panel._cover.configure(image=None, text=text)
        # CTkLabel._update_image() does not clear the native label for None.
        panel._cover._label.configure(image="")
        self._cover_images.pop(deck_id, None)

    def show_mixer(self, crossfader: float, master: float) -> None:
        self._updating_mixer = True
        self.show_crossfader(crossfader)
        master_percent = round(master * 100)
        if self._mixer_render_cache.get("master_percent") != master_percent:
            self._master.set(master)
            self._master_label.configure(text=f"Master {master_percent}%")
            self._mixer_render_cache["master_percent"] = master_percent
        mute_text = "Ton an" if master == 0 else "Stumm"
        if self._mixer_render_cache.get("mute_text") != mute_text:
            self._mute_button.configure(text=mute_text)
            self._mixer_render_cache["mute_text"] = mute_text
        self._updating_mixer = False

    def show_crossfader(self, crossfader: float) -> None:
        """Update only the visible crossfade fields and skip identical percentages."""
        percent = round(crossfader * 100)
        if self._mixer_render_cache.get("crossfade_percent") == percent:
            return
        self._updating_mixer = True
        self._crossfader.set(crossfader)
        self._crossfader_label.configure(text=f"Crossfader · {percent}%")
        self._presentation_status = replace(
            self._presentation_status, transition=f"Übergang {percent}%"
        )
        self._render_global_status()
        self._mixer_render_cache["crossfade_percent"] = percent
        self._updating_mixer = False

    def show_fade_settings(self, duration: float, stop_after: bool) -> None:
        self._fade_duration.set(duration)
        self._fade_duration_label.configure(text=f"{duration:.0f} s")
        if stop_after:
            self._fade_stop_switch.select()
        else:
            self._fade_stop_switch.deselect()

    def show_player_mode(self, mode: str) -> None:
        labels = {
            "manual": "MANUELL",
            "semi_automatic": "HALBAUTOMATISCH",
            "automatic": "AUTOMATISCH",
        }
        self._player_mode.set(labels.get(mode, "MANUELL"))

    def show_automatic_playback(self, active: bool) -> None:
        self._automatic_queue_active = active
        self._automatic_queue_button.configure(
            text="■" if active else "▶",
            fg_color="#8f1f1f" if active else "#1f6aa5",
        )
        if force_live_for_operational_update(
            startup_guard=self._presentation_startup_guard,
            active=active,
        ):
            self._force_live_workspace("active-automation")

    def show_automatic_status(self, state: str, detail: str = "") -> None:
        labels = {
            "ready": ("Automatik bereit", theme.TEXT_MUTED),
            "running": ("Automatik aktiv", theme.SUCCESS),
            "transition": ("Übergang läuft", theme.READY),
            "paused": ("Automatik nicht aktiv", theme.WARNING),
            "stopped": ("Automatik beendet", theme.ERROR),
            "completed": ("Automatik abgeschlossen", theme.SUCCESS),
        }
        text, color = labels.get(state, ("Automatik bereit", theme.TEXT_MUTED))
        self._presentation_status = replace(self._presentation_status, automatic=text)
        self._render_global_status()
        if detail:
            text = f"{text} · {detail}"
        self._automatic_status_label.configure(text=text, text_color=color)
        if force_live_for_operational_update(
            startup_guard=self._presentation_startup_guard,
            active=state in {"running", "transition", "paused", "stopped"},
        ):
            self._force_live_workspace(f"automatic-{state}")

    def show_queue_duplicate_policy(self, policy: str) -> None:
        if policy == "allow":
            self._duplicate_switch.select()
        else:
            self._duplicate_switch.deselect()

    def show_queue_duration_mode(self, use_effective_cues: bool) -> None:
        if use_effective_cues:
            self._effective_duration_switch.select()
        else:
            self._effective_duration_switch.deselect()

    def show_queue_artist_repetition(self, enabled: bool) -> None:
        if enabled:
            self._artist_repetition_switch.select()
        else:
            self._artist_repetition_switch.deselect()

    def show_directory_import_result(self, added: int, skipped: int, failed: int) -> None:
        show_silent_message(
            self,
            "Verzeichnis importiert",
            f"Hinzugefügt: {added}\nÜbersprungen: {skipped}\nFehler: {failed}",
        )

    def show_catalog_import_result(self, created: int, updated: int, failed: int) -> None:
        show_silent_message(
            self,
            "Katalogimport abgeschlossen",
            f"Neue Titel: {created}\nVorhandene aktualisiert: {updated}\nFehler: {failed}",
            error=failed > 0,
        )

    def show_directory_import_progress(
        self, processed: int, total: int | None, active: bool
    ) -> None:
        if not active:
            self._directory_progress_visible = False
            self._directory_progress.stop()
            self._directory_progress_frame.grid_remove()
            return
        self._directory_progress_visible = True
        if self._compact_layout_active:
            workspace = (
                self._presentation_coordinator.state.workspace
                if self._presentation_coordinator is not None
                else Workspace.LIVE
            )
            self._directory_progress_frame.grid(
                row=(
                    _compact_preparation_rows()["progress"]
                    if workspace is Workspace.PREPARATION
                    else _compact_live_rows()["directory_progress"]
                ),
                column=0,
                padx=8,
                pady=1,
                sticky="ew",
            )
        else:
            self._directory_progress_frame.grid(row=7, column=0, padx=12, pady=2, sticky="ew")
        if total is None:
            self._directory_progress.configure(mode="indeterminate")
            self._directory_progress.start()
            self._directory_progress_label.configure(text="Dateien werden gesucht …")
            return
        self._directory_progress.stop()
        self._directory_progress.configure(mode="determinate")
        self._directory_progress.set(processed / total if total else 1.0)
        self._directory_progress_label.configure(text=f"Lade Titel {processed} von {total}")

    def show_saved_queues(self, queues: list[SavedQueue]) -> None:
        self._saved_queue_ids = {queue.name: queue.saved_queue_id for queue in queues}
        names = list(self._saved_queue_ids) or ["Keine gespeichert"]
        self._update_optionmenu("saved_queues", self._saved_queue_menu, names, names[0])
        self._sync_playlist_equalizer_menu()

    def select_saved_queue(self, saved_queue_id: int) -> None:
        name = next(
            (
                name
                for name, queue_id in self._saved_queue_ids.items()
                if queue_id == saved_queue_id
            ),
            None,
        )
        if name is not None:
            self._update_optionmenu(
                "saved_queues",
                self._saved_queue_menu,
                list(self._saved_queue_ids) or [name],
                name,
            )
            self._sync_playlist_equalizer_menu()

    def show_saved_queue_load_result(self, added: int, skipped: int) -> None:
        show_silent_message(
            self,
            "Queue geladen",
            f"Hinzugefügt: {added}\nWegen Duplikatregel übersprungen: {skipped}",
        )

    def show_playlist(self, playlist: SavedQueue, tracks: list[Track]) -> None:
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Playlist – {playlist.name}")
        dialog.geometry("820x560")
        dialog.transient(self)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(
            dialog,
            text=f"{playlist.name} · {len(tracks)} Titel",
            font=("Segoe UI", 18, "bold"),
        ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
        playlist_search = ctk.CTkEntry(
            dialog,
            placeholder_text="Playlist nach Titel, Interpret oder Album durchsuchen",
        )
        playlist_search.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        content = SmoothScrollableFrame(dialog)
        content.grid(row=2, column=0, padx=12, pady=(0, 12), sticky="nsew")
        content.grid_columnconfigure(0, weight=1)

        def render_playlist(query: str = "") -> None:
            for child in content.winfo_children():
                child.destroy()
            normalized = query.strip().casefold()
            visible = [
                (position, track, playlist.entries[position - 1])
                for position, track in enumerate(tracks, start=1)
                if not normalized
                or normalized
                in " ".join(
                    (track.title, track.artist, track.album, track.genre, track.file_path)
                ).casefold()
            ]
            if not visible:
                ctk.CTkLabel(content, text="Keine passenden Playlist-Titel gefunden").grid(
                    row=0, column=0, padx=8, pady=20
                )
                return
            for display_row, (position, track, entry) in enumerate(visible):
                row = ctk.CTkFrame(content)
                row.grid(row=display_row, column=0, pady=2, sticky="ew")
                row.grid_columnconfigure(0, weight=1)
                label = (
                    f"{position:02d}. {track.artist} – {track.title}"
                    if track.artist
                    else f"{position:02d}. {track.title}"
                )
                identity = ctk.CTkLabel(
                    row,
                    text=f"{label}\n{track_version_text(track)}",
                    anchor="w",
                    justify="left",
                )
                identity.grid(row=0, column=0, padx=8, pady=5, sticky="ew")
                Tooltip(identity, f"Playlist-Datei:\n{track.file_path}")
                for column, (text, command) in enumerate(
                    (
                        (
                            "A",
                            lambda item=track: self._load_playlist_track(item.id, "A"),
                        ),
                        (
                            "B",
                            lambda item=track: self._load_playlist_track(item.id, "B"),
                        ),
                        (
                            "+ Queue",
                            lambda item=track: self._add_playlist_track(item.id),
                        ),
                        (
                            "Tempo…",
                            lambda item=entry: self._edit_saved_queue_tempo(
                                item.saved_queue_entry_id
                            ),
                        ),
                    ),
                    start=1,
                ):
                    ctk.CTkButton(row, text=text, width=65, command=command).grid(
                        row=0, column=column, padx=(2, 4), pady=4
                    )
            dialog.after_idle(lambda: self._enable_keyboard_focus(dialog))

        playlist_search.bind("<KeyRelease>", lambda _event: render_playlist(playlist_search.get()))
        playlist_search.focus_set()
        render_playlist()

    def _edit_saved_queue_tempo(self, entry_id: int | None) -> None:
        if entry_id is None or self._metadata_analysis is None or self._controller is None:
            show_silent_message(
                self,
                "Playlist-Tempo",
                "Dieser Playlist-Eintrag besitzt noch keine stabile Eintrags-ID.",
                error=True,
            )
            return
        controller = self._controller

        def submit(
            task: Callable[[], object],
            completed: Callable[[object], None],
            failed: Callable[[Exception], None],
        ) -> bool:
            return controller.load_track_editor_view_model(task, completed, failed)

        SavedQueueTempoDialog(self, entry_id, self._metadata_analysis, submit)

    def show_queue_shuffle_result(self, shuffled: int) -> None:
        message = (
            f"{shuffled} wartende Titel wurden neu angeordnet."
            if shuffled >= 2
            else "Zum Mischen werden mindestens zwei wartende Titel benötigt."
        )
        show_silent_message(self, "Queue mischen", message)

    def show_error(self, title: str, message: str) -> None:
        self._presentation_status = replace(self._presentation_status, warning=title)
        self._render_global_status()
        self._force_live_workspace("error")
        show_silent_message(self, title, message, error=True)

    def show_queue_warning(self, message: str) -> None:
        """Show an expiring warning without pausing playback or grabbing focus."""
        self._queue_warning_generation += 1
        generation = self._queue_warning_generation
        self._queue_warning.configure(text=f"⚠ {message}")

        def clear_warning() -> None:
            if generation == self._queue_warning_generation:
                self._queue_warning.configure(text="")

        self.schedule(8000, clear_warning)

    def confirm_replace(self, deck_id: str) -> bool:
        return ask_silent_yes_no(
            self,
            "Laufenden Titel ersetzen?",
            f"Deck {deck_id} spielt gerade. Soll der Titel wirklich ersetzt werden?",
        )

    def confirm_queue_cue_change(self, status: str) -> bool:
        return ask_silent_yes_no(
            self,
            "Aktiven Queue-Eintrag ändern?",
            f"Der Eintrag ist bereits {status}. Soll die Cue-Änderung gespeichert werden?\n\n"
            "Ein laufender Übergang bleibt unverändert. Zu späte Änderungen gelten erst "
            "bei der nächsten Wiedergabe.",
        )

    def schedule(self, delay_ms: int, callback: object) -> object:
        """Schedule an application callback through the common timing wrapper."""
        if not callable(callback):
            raise TypeError("callback muss aufrufbar sein")
        name = getattr(callback, "__name__", callback.__class__.__name__)
        if name == "<lambda>":
            name = "anonymous_callback"
        after_id = ""

        def run_scheduled() -> object:
            self._scheduled_after_ids.discard(after_id)
            return callback()

        run_scheduled.__name__ = name
        after_id = self.after(
            delay_ms,
            measured_gui_callback(
                self._performance,
                f"after.{name}",
                run_scheduled,
                callback_state=self._callback_state,
            ),
        )
        self._scheduled_after_ids.add(after_id)
        return after_id

    def _bind_gui(self, sequence: str, name: str, callback: Callable[[Any], object]) -> None:
        """Register one named, measured top-level keyboard binding."""
        self.bind(
            sequence,
            measured_gui_callback(
                self._performance,
                f"binding.{name}",
                callback,
                callback_state=self._callback_state,
            ),
        )

    def _schedule_idle(self, name: str, callback: Callable[[], object]) -> object:
        """Schedule named application idle work with slow-callback diagnostics."""
        after_id = ""

        def run_idle() -> object:
            self._scheduled_after_ids.discard(after_id)
            return callback()

        after_id = self.after_idle(
            measured_gui_callback(
                self._performance,
                f"after_idle.{name}",
                run_idle,
                callback_state=self._callback_state,
            )
        )
        self._scheduled_after_ids.add(after_id)
        return after_id

    @staticmethod
    def _clear(frame: ctk.CTkScrollableFrame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _run_search(self) -> None:
        if self._controller is not None:
            self._controller.search(self._search.get())

    def _reset_catalog_search(self) -> None:
        self._search.delete(0, "end")
        self._run_search()

    def _change_catalog_page(self, direction: int) -> None:
        if self._controller is not None:
            self._controller.change_catalog_page(direction)

    def _deck_action(self, deck_id: str, action: str) -> None:
        if self._controller is not None:
            self._controller.deck_action(deck_id, action)

    def _stop_on_air_decks(self) -> None:
        """Delegate the compact safety action for explicitly on-air decks."""
        for deck_id in ("A", "B"):
            if self._deck_on_air.get(deck_id, False):
                self._deck_action(deck_id, "stop")

    def _load_catalog(self, track_id: int, deck_id: str) -> None:
        if self._controller is not None:
            self._controller.load_catalog_track(track_id, deck_id)

    def _add_queue(self, track_id: int) -> None:
        if self._controller is not None:
            self._controller.add_catalog_track_to_queue(track_id)

    def _remove_catalog(self, track: Track) -> None:
        if self._controller is None:
            return
        if ask_silent_yes_no(
            self,
            "Titel aus Katalog entfernen?",
            f"{track.artist or 'Unbekannt'} — {track.title}\n\n"
            f"Ausgewählte Version: {track_version_text(track)}\n"
            f"Dateipfad: {track.file_path}\n\n"
            "Nur der Katalogeintrag wird ausgeblendet. Die Musikdatei bleibt unverändert.",
        ):
            self._controller.remove_catalog_track(track.id)

    def _show_track_details(self, track: Track) -> None:
        loudness = (
            self._loudness_controller.state(track.id)
            if self._loudness_controller is not None
            else None
        )
        show_silent_message(
            self,
            "Titeldetails",
            _track_details_text(track, loudness),
        )

    def _open_catalog_maintenance(self) -> None:
        if self._controller is None:
            return
        controller = self._controller

        def submit(
            task: Callable[[], object],
            completed: Callable[[object], None],
            failed: Callable[[Exception], None],
        ) -> bool:
            return controller.load_track_editor_view_model(task, completed, failed)

        def open_track(track_id: int) -> None:
            controller.load_track_editor_view_model(
                lambda: controller.library_track(track_id),
                lambda track: self._edit_cue_points(track) if track is not None else None,
                lambda error: self.show_error("Titel konnte nicht geöffnet werden", str(error)),
            )

        CatalogMaintenanceDialog(
            self,
            controller.catalog_maintenance_service,
            submit,
            open_track,
            CatalogAnalysisActions(
                self._analyze_outdated_catalog,
                self._analyze_catalog,
                self._cancel_catalog_analysis,
                lambda: self._analyze_loudness_catalog(outdated_only=True),
                self._analyze_loudness_catalog,
                self._cancel_loudness_analysis,
            ),
            self._metadata_analysis,
        )

    def _edit_cue_points(self, track: Track) -> None:
        if self._cue_controller is None or self._controller is None:
            return
        cue_controller = self._cue_controller
        main_controller = self._controller

        def submit_editor_task(
            task: Callable[[], object],
            completed: Callable[[object], None],
            failed: Callable[[Exception], None],
        ) -> bool:
            return main_controller.load_track_editor_view_model(task, completed, failed)

        editor_controller = TrackEditorController(
            cue_controller,
            self._loudness_controller,
            main_controller.track_editor_equalizer_state,
            self._performance,
            main_controller.metadata_editor_service,
            submit_editor_task,
            self._metadata_analysis,
        )

        def refresh_changed_track(view_model: TrackEditorViewModel) -> None:
            cue = view_model.cue
            has_manual_cues = any(
                value is not None
                for value in (
                    cue.manual_cue_in,
                    cue.manual_cue_out,
                    cue.manual_fade_duration,
                )
            )
            main_controller.track_cues_changed(track.id, has_manual_cues)
            main_controller.track_metadata_changed(track.id)

        def open_dialog(view_model: TrackEditorViewModel) -> None:
            if not self._window_is_alive():
                return
            CuePointDialog(
                self,
                cue_controller,
                track.id,
                track=track,
                editor_controller=editor_controller,
                view_model=view_model,
                on_saved=refresh_changed_track,
            )

        def load_failed(error: Exception) -> None:
            if self._window_is_alive():
                show_silent_message(self, "Titel bearbeiten", str(error), error=True)

        accepted = main_controller.load_track_editor_view_model(
            lambda: editor_controller.build_view_model(track),
            open_dialog,
            load_failed,
        )
        if not accepted:
            show_silent_message(
                self,
                "Titel bearbeiten",
                "Ein anderer Titel-Editor wird gerade vorbereitet.",
                error=True,
            )

    def _window_is_alive(self) -> bool:
        """Safely reject callbacks delivered after the Tk interpreter was destroyed."""
        try:
            return bool(self.winfo_exists())
        except (RuntimeError, TclError):
            return False

    def _edit_loudness(self, track: Track) -> None:
        if self._loudness_controller is None:
            return
        try:
            dialog = LoudnessDialog(self, self._loudness_controller, track.id)
            self.wait_window(dialog)
        except (ValueError, RuntimeError) as exc:
            show_silent_message(self, "Lautstärkeanpassung", str(exc), error=True)

    def _edit_normalization_settings(self) -> None:
        if self._loudness_controller is None:
            return
        try:
            dialog = NormalizationSettingsDialog(self, self._loudness_controller)
            self.wait_window(dialog)
        except (ValueError, RuntimeError) as exc:
            show_silent_message(self, "Normalisierung", str(exc), error=True)

    def _load_queue(self, queue_id: int, deck_id: str) -> None:
        if self._controller is not None:
            self._controller.load_queue_track(queue_id, deck_id)

    def _select_queue(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.select_queue_entry(queue_id)

    def _remove_queue(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.remove_queue_track(queue_id)

    def _delete_selected_queue(self) -> None:
        if self._controller is None or self._queue_selected_id is None:
            return
        entry = next(
            (
                candidate
                for candidate in self._queue_entries
                if candidate.queue_id == self._queue_selected_id
            ),
            None,
        )
        if entry is None:
            return
        if entry.status.value == "playing":
            show_silent_message(
                self,
                "Titel kann nicht gelöscht werden",
                "Der aktuell spielende Titel bleibt bis zum Ende in der Queue.",
                error=True,
            )
            return
        if ask_silent_yes_no(
            self,
            "Markierten Titel löschen?",
            "Soll der markierte Titel aus der aktuellen Queue entfernt werden?",
        ):
            self._controller.remove_selected_queue_track()

    def _update_delete_selected_queue_button(self) -> None:
        button = getattr(self, "_delete_selected_queue_button", None)
        if button is None:
            return
        selected = next(
            (
                entry
                for entry in getattr(self, "_queue_entries", ())
                if entry.queue_id == self._queue_selected_id
            ),
            None,
        )
        button.configure(
            state=(
                "normal"
                if selected is not None and selected.status.value != "playing"
                else "disabled"
            )
        )

    def _remove_prepared_queue(self, queue_id: int) -> None:
        if self._controller is None:
            return
        if ask_silent_yes_no(
            self,
            "Vorbereiteten Titel entfernen?",
            "Die laufende Vorbereitung wird abgebrochen beziehungsweise das inaktive "
            "Deck wird entladen. Danach wird der Titel aus der Queue entfernt.\n\n"
            "Fortfahren?",
        ):
            self._controller.remove_prepared_queue_track(queue_id)

    def _move_prepared_queue(self, queue_id: int, direction: int) -> None:
        if self._controller is None:
            return
        if ask_silent_yes_no(
            self,
            "Vorbereiteten Titel verschieben?",
            "Die laufende Vorbereitung wird abgebrochen beziehungsweise das inaktive "
            "Deck wird entladen. Danach wird der Titel als wartend verschoben.\n\n"
            "Fortfahren?",
        ):
            self._controller.move_prepared_queue_track(queue_id, direction)

    def _edit_queue_cues(self, queue_id: int) -> None:
        if self._controller is None:
            return
        try:
            dialog = QueueCueDialog(self, self._controller, queue_id)
            self.wait_window(dialog)
        except ValueError as exc:
            show_silent_message(self, "Queue-Cues", str(exc), error=True)

    def _move_queue(self, queue_id: int, direction: int) -> None:
        if self._controller is not None:
            self._controller.move_queue_track(queue_id, direction)

    def _move_queue_top(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.move_queue_track_to_top(queue_id)

    def _move_queue_end(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.move_queue_track_to_end(queue_id)

    def _set_queue_priority(self, queue_id: int) -> None:
        if self._controller is None:
            return
        priority = simpledialog.askinteger(
            "Queue-Priorität",
            "Priorität von 0 bis 999:",
            parent=self,
            minvalue=0,
            maxvalue=999,
        )
        if priority is not None:
            self._controller.set_queue_track_priority(queue_id, priority)

    def _toggle_queue_lock(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.toggle_queue_track_lock(queue_id)

    def _mark_queue_played(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.mark_queue_track_played(queue_id)

    def _mark_queue_skipped(self, queue_id: int) -> None:
        if self._controller is None:
            return
        reason = simpledialog.askstring(
            "Titel überspringen",
            "Grund (optional):",
            parent=self,
        )
        self._controller.mark_queue_track_skipped(queue_id, reason)

    def _retry_queue(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.retry_queue_track(queue_id)

    def _play_repetition_skipped_queue(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.play_repetition_skipped_queue_track(queue_id)

    def _reset_queue_played(self, queue_id: int) -> None:
        if self._controller is not None:
            self._controller.reset_played_queue_track(queue_id)

    def _toggle_mixer_panel(self) -> None:
        if self._mixer_panel.winfo_ismapped():
            self._mixer_panel.grid_remove()
        else:
            self._mixer_panel.grid()
        self._render_overlay()

    def _toggle_diagnostic_panel(self) -> None:
        self._diagnostic_expanded = not self._diagnostic_expanded
        if self._diagnostic_expanded:
            self._diagnostic_frame.grid()
        else:
            self._diagnostic_frame.grid_remove()
        self._diagnostic_toggle.configure(text=_diagnostic_toggle_text(self._diagnostic_expanded))

    def _clear_waiting_queue(self) -> None:
        if self._controller is None:
            return
        if ask_silent_yes_no(
            self,
            "Wartende Titel entfernen?",
            "Sollen wirklich alle wartenden Queue-Titel entfernt werden?",
        ):
            self._controller.clear_waiting_queue()

    def _clear_complete_queue(self) -> None:
        if self._controller is None:
            return
        if ask_silent_yes_no(
            self,
            "Queue vollständig leeren?",
            "Alle nicht spielenden Queue-Einträge werden entfernt und vorbereitete "
            "Decks werden entladen. Ein aktuell spielender Titel bleibt bis zu seinem "
            "Ende erhalten.\n\n"
            "Soll die komplette Queue wirklich geleert werden?",
        ):
            self._controller.clear_complete_queue()

    def _shuffle_waiting_queue(self) -> None:
        if self._controller is None:
            return
        if ask_silent_yes_no(
            self,
            "Queue mischen?",
            "Sollen alle wartenden Titel zufällig neu angeordnet werden?",
        ):
            self._controller.shuffle_waiting_queue()

    def _duplicate_policy_changed(self) -> None:
        if self._controller is not None:
            policy = "allow" if self._duplicate_switch.get() else "prevent"
            self._controller.set_queue_duplicate_policy(policy)

    def _queue_duration_mode_changed(self) -> None:
        if self._controller is not None:
            self._controller.set_queue_stats_use_effective_cues(
                bool(self._effective_duration_switch.get())
            )

    def _queue_artist_repetition_changed(self) -> None:
        if self._controller is not None:
            self._controller.set_queue_artist_repetition_enabled(
                bool(self._artist_repetition_switch.get())
            )

    def _queue_equalizer_changed(self, label: str) -> None:
        if self._controller is None:
            return
        controller = self._controller
        key = self._equalizer_preset_keys[label]
        self._run_equalizer_action(
            lambda: controller.set_current_queue_equalizer(None if key == "inherit" else key)
        )

    def _show_queue_source_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Dateien hinzufügen…", command=self._choose_queue_files)
        menu.add_command(label="Verzeichnis hinzufügen…", command=self._choose_queue_directory)
        playlist_menu = tk.Menu(menu, tearoff=False)
        if self._saved_queue_ids:
            for name in self._saved_queue_ids:
                playlist_menu.add_command(
                    label=name,
                    command=partial(self._load_playlist_from_source_menu, name),
                )
        else:
            playlist_menu.add_command(label="Keine Playlist gespeichert", state="disabled")
        menu.add_cascade(label="Playlist hinzufügen", menu=playlist_menu)
        menu.add_separator()
        menu.add_command(
            label="Katalogauswahl hinzufügen: + am Titel",
            command=self._focus_search,
        )
        self._post_button_menu(menu, self._queue_source_button)

    def _show_queue_actions_menu(self) -> None:
        menu = tk.Menu(self, tearoff=False)
        duplicate_label, duration_label = self._queue_toggle_menu_labels(
            bool(self._duplicate_switch.get()),
            bool(self._effective_duration_switch.get()),
        )
        menu.add_command(
            label=duplicate_label,
            command=lambda: self._toggle_hidden_switch(
                self._duplicate_switch, self._duplicate_policy_changed
            ),
        )
        menu.add_command(
            label=duration_label,
            command=lambda: self._toggle_hidden_switch(
                self._effective_duration_switch, self._queue_duration_mode_changed
            ),
        )
        menu.add_separator()
        menu.add_command(
            label="Playlist-Werkzeuge ein-/ausblenden",
            command=self._toggle_saved_toolbar,
        )
        menu.add_command(
            label="Aktuelle Queue als Playlist speichern…",
            command=self._save_current_queue,
        )
        menu.add_separator()
        menu.add_command(label="Wartende Titel entfernen…", command=self._clear_waiting_queue)
        menu.add_command(label="Gesamte Queue leeren…", command=self._clear_complete_queue)
        self._post_button_menu(menu, self._queue_actions_button)

    @staticmethod
    def _queue_toggle_menu_labels(
        duplicates_allowed: bool, effective_cue_duration: bool
    ) -> tuple[str, str]:
        return (
            f"Duplikate erlauben: {'aktiv' if duplicates_allowed else 'inaktiv'}",
            f"Cue-Restlaufzeit: {'aktiv' if effective_cue_duration else 'inaktiv'}",
        )

    @staticmethod
    def _post_button_menu(menu: tk.Menu, button: Any) -> None:
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    @staticmethod
    def _toggle_hidden_switch(widget: Any, callback: Callable[[], None]) -> None:
        widget.toggle()
        callback()

    def _toggle_saved_toolbar(self) -> None:
        if self._saved_toolbar_visible:
            self._saved_toolbar_visible = False
            self._saved_toolbar.grid_remove()
        else:
            self._saved_toolbar_visible = True
            if not self._compact_layout_active:
                self._saved_toolbar.grid(row=8, column=0, padx=12, pady=2, sticky="ew")

    def _load_playlist_from_source_menu(self, name: str) -> None:
        self._saved_queue_menu.set(name)
        self._sync_playlist_equalizer_menu()
        self._load_saved_queue()

    def _saved_queue_selected(self, _name: str) -> None:
        self._sync_playlist_equalizer_menu()

    def _sync_playlist_equalizer_menu(self) -> None:
        if self._controller is None:
            return
        saved_queue_id = self._saved_queue_ids.get(self._saved_queue_menu.get())
        key = (
            self._controller.saved_queue_equalizer_key(saved_queue_id)
            if saved_queue_id is not None
            else None
        )
        self._playlist_equalizer_menu.set(self._equalizer_label_for_key(key))
        self._playlist_equalizer_menu.configure(
            state="normal" if saved_queue_id is not None else "disabled"
        )

    def _playlist_equalizer_changed(self, label: str) -> None:
        if self._controller is None:
            return
        controller = self._controller
        saved_queue_id = self._saved_queue_ids.get(self._saved_queue_menu.get())
        if saved_queue_id is None:
            self.show_error("Equalizer", "Keine gespeicherte Playlist ausgewählt")
            return
        key = self._equalizer_preset_keys[label]
        self._run_equalizer_action(
            lambda: controller.save_saved_queue_equalizer(
                saved_queue_id, None if key == "inherit" else key
            )
        )

    def _choose_queue_directory(self) -> None:
        if self._controller is None:
            return
        directory = filedialog.askdirectory(title="Musikverzeichnis für die Party-Queue wählen")
        if directory:
            self._controller.import_directory_to_queue(directory)

    def _choose_queue_files(self) -> None:
        if self._controller is None:
            return
        paths = filedialog.askopenfilenames(
            title="Audiodateien zur Party-Queue hinzufügen",
            filetypes=(("MP3 und FLAC", "*.mp3 *.flac"), ("Alle Dateien", "*.*")),
        )
        for path in paths:
            self._controller.import_file_to_queue(path)

    def _choose_catalog_file(self) -> None:
        if self._controller is None:
            return
        file_path = filedialog.askopenfilename(
            title="Audiodatei in den Katalog aufnehmen",
            filetypes=(("MP3 und FLAC", "*.mp3 *.flac"), ("Alle Dateien", "*.*")),
        )
        if file_path:
            self._controller.import_file_to_catalog(file_path)

    def _choose_catalog_directory(self) -> None:
        if self._controller is None:
            return
        directory = filedialog.askdirectory(title="Musikordner in den Katalog aufnehmen")
        if directory:
            self._controller.import_directory_to_catalog(directory)

    def _analyze_catalog(self) -> None:
        if self._cue_controller is None:
            return
        if not ask_silent_yes_no(
            self,
            "Gesamten Katalog analysieren?",
            "Diese Sonderfunktion berechnet auch bereits aktuelle Cue-Analysen erneut. "
            "Für den normalen Betrieb genügt „Neue/veraltete Cues“.\n\n"
            "Alle Katalogtitel werden nacheinander im Hintergrund analysiert. "
            "Die Wiedergabe bleibt verfügbar.\n\nAnalyse starten?",
        ):
            return
        try:
            self._catalog_analysis_was_cancelled = False
            self._catalog_analysis_active = True
            self._expand_compact_analysis_for_active_job()
            self._show_compact_active_analysis("Cue-Analyse wird gestartet …")
            self._catalog_analysis_button.configure(state="disabled")
            self._outdated_analysis_button.configure(state="disabled")
            self._catalog_analysis_cancel_button.configure(state="normal")
            self._cue_controller.analyze_catalog(
                self._show_catalog_analysis_progress,
                self._show_catalog_analysis_completed,
            )
        except (ValueError, RuntimeError) as exc:
            self._catalog_analysis_active = False
            self._refresh_compact_analysis_toggle_state()
            self._hide_compact_active_analysis_if_complete()
            self._catalog_analysis_button.configure(state="normal")
            self._outdated_analysis_button.configure(state="normal")
            self._catalog_analysis_cancel_button.configure(state="disabled")
            show_silent_message(self, "Cue-Analyse", str(exc), error=True)

    def _cancel_catalog_analysis(self) -> None:
        if self._cue_controller is None:
            return
        self._catalog_analysis_was_cancelled = True
        self._cue_controller.cancel_batch_analysis()
        self._catalog_analysis_cancel_button.configure(state="disabled")
        self._summary.configure(text="Cue-Analyse: Abbruch angefordert …")
        self._show_compact_active_analysis("Cue-Analyse: Abbruch angefordert …")

    def _analyze_outdated_catalog(self) -> None:
        if self._cue_controller is None:
            return
        try:
            self._catalog_analysis_was_cancelled = False
            self._catalog_analysis_active = True
            self._expand_compact_analysis_for_active_job()
            self._show_compact_active_analysis("Cue-Analyse wird gestartet …")
            self._catalog_analysis_button.configure(state="disabled")
            self._outdated_analysis_button.configure(state="disabled")
            self._catalog_analysis_cancel_button.configure(state="normal")
            self._cue_controller.analyze_outdated_catalog(
                self._show_catalog_analysis_progress,
                self._show_catalog_analysis_completed,
            )
        except (ValueError, RuntimeError) as exc:
            self._catalog_analysis_active = False
            self._refresh_compact_analysis_toggle_state()
            self._hide_compact_active_analysis_if_complete()
            self._catalog_analysis_button.configure(state="normal")
            self._outdated_analysis_button.configure(state="normal")
            self._catalog_analysis_cancel_button.configure(state="disabled")
            show_silent_message(self, "Cue-Analyse", str(exc), error=True)

    def _show_catalog_analysis_progress(
        self, processed: int, total: int, succeeded: int, failed: int
    ) -> None:
        text = f"Cue-Analyse: {processed}/{total} · Erfolgreich: {succeeded} · Fehler: {failed}"
        self._summary.configure(text=text)
        self._show_compact_active_analysis(text)

    def _show_catalog_analysis_completed(self, succeeded: int, failed: int) -> None:
        self._catalog_analysis_active = False
        state = "abgebrochen" if self._catalog_analysis_was_cancelled else "abgeschlossen"
        self._summary.configure(
            text=f"Cue-Analyse {state} · Erfolgreich: {succeeded} · Fehler: {failed}"
        )
        self._catalog_analysis_button.configure(state="normal")
        self._outdated_analysis_button.configure(state="normal")
        self._catalog_analysis_cancel_button.configure(state="disabled")
        self._refresh_compact_analysis_toggle_state()
        self._hide_compact_active_analysis_if_complete()

    def _analyze_loudness_catalog(self, *, outdated_only: bool = False) -> None:
        if self._loudness_controller is None:
            return
        if not outdated_only and not ask_silent_yes_no(
            self,
            "Gesamten Katalog auf Lautheit analysieren?",
            "Diese Sonderfunktion berechnet auch bereits aktuelle Lautheitsanalysen erneut. "
            "Für den normalen Betrieb genügt „Neue/veraltete Lautheit“.\n\n"
            "Alle Katalogtitel werden einzeln im Hintergrund nach EBU R128 analysiert. "
            "Die Wiedergabe bleibt verfügbar.\n\nAnalyse starten?",
        ):
            return
        try:
            self._loudness_analysis_was_cancelled = False
            self._loudness_analysis_active = True
            self._expand_compact_analysis_for_active_job()
            self._show_compact_active_analysis("Lautheitsanalyse wird gestartet …")
            self._loudness_analysis_button.configure(state="disabled")
            self._outdated_loudness_button.configure(state="disabled")
            self._loudness_analysis_cancel_button.configure(state="normal")
            self._loudness_controller.analyze_catalog(
                self._show_loudness_analysis_progress,
                self._show_loudness_analysis_completed,
                outdated_only=outdated_only,
            )
        except (ValueError, RuntimeError) as exc:
            self._loudness_analysis_active = False
            self._refresh_compact_analysis_toggle_state()
            self._hide_compact_active_analysis_if_complete()
            self._loudness_analysis_button.configure(state="normal")
            self._outdated_loudness_button.configure(state="normal")
            self._loudness_analysis_cancel_button.configure(state="disabled")
            show_silent_message(self, "Lautheitsanalyse", str(exc), error=True)

    def _cancel_loudness_analysis(self) -> None:
        if self._loudness_controller is None:
            return
        self._loudness_analysis_was_cancelled = True
        self._loudness_controller.cancel_batch_analysis()
        self._loudness_analysis_cancel_button.configure(state="disabled")
        self._summary.configure(text="Lautheitsanalyse: Abbruch angefordert …")
        self._show_compact_active_analysis("Lautheitsanalyse: Abbruch angefordert …")

    def _show_loudness_analysis_progress(
        self, processed: int, total: int, succeeded: int, failed: int
    ) -> None:
        text = (
            f"Lautheitsanalyse: {processed}/{total} · Erfolgreich: {succeeded} · Fehler: {failed}"
        )
        self._summary.configure(text=text)
        self._show_compact_active_analysis(text)

    def _show_loudness_analysis_completed(self, succeeded: int, failed: int) -> None:
        self._loudness_analysis_active = False
        state = "abgebrochen" if self._loudness_analysis_was_cancelled else "abgeschlossen"
        self._summary.configure(
            text=f"Lautheitsanalyse {state} · Erfolgreich: {succeeded} · Fehler: {failed}"
        )
        self._loudness_analysis_button.configure(state="normal")
        self._outdated_loudness_button.configure(state="normal")
        self._loudness_analysis_cancel_button.configure(state="disabled")
        self._refresh_compact_analysis_toggle_state()
        self._hide_compact_active_analysis_if_complete()

    def _expand_compact_analysis_for_active_job(self) -> None:
        """Active progress is already shown in the dedicated compact status row."""

    def _refresh_compact_analysis_toggle_state(self) -> None:
        self._compact_analysis_toggle.configure(state="normal")

    def _show_compact_active_analysis(self, text: str) -> None:
        self._compact_analysis_active_label.configure(text=text)
        self._compact_analysis_active_label.grid()
        self._compact_analysis_active_cancel.grid()

    def _hide_compact_active_analysis_if_complete(self) -> None:
        if (
            self._catalog_analysis_active
            or self._loudness_analysis_active
            or self._metadata_analysis_active
        ):
            return
        self._compact_analysis_active_label.grid_remove()
        self._compact_analysis_active_cancel.grid_remove()

    def _cancel_active_analysis(self) -> None:
        if self._catalog_analysis_active:
            self._cancel_catalog_analysis()
        if self._loudness_analysis_active:
            self._cancel_loudness_analysis()
        if self._metadata_analysis_active and self._metadata_analysis is not None:
            self._metadata_analysis.cancel_all()
            text = "BPM-Analyse: Abbruch angefordert …"
            self._summary.configure(text=text)
            self._show_compact_active_analysis(text)

    def _save_current_queue(self) -> None:
        if self._controller is None:
            return
        name = simpledialog.askstring("Queue speichern", "Name der Queue:", parent=self)
        if not name or not name.strip():
            return
        normalized = name.strip()
        if normalized in self._saved_queue_ids and not ask_silent_yes_no(
            self,
            "Queue überschreiben?",
            f"Die Queue „{normalized}“ existiert bereits. Soll sie überschrieben werden?",
        ):
            return
        snapshot_cues = ask_silent_yes_no(
            self,
            "Cue-Werte einfrieren?",
            "Sollen die aktuell wirksamen Cue- und Überblendwerte als "
            "Veranstaltungs-Snapshot gespeichert werden?\n\n"
            "Ja: Spätere Katalogänderungen beeinflussen diese Queue nicht.\n"
            "Nein: Beim Laden werden die dann aktuellen Titelwerte verwendet.",
        )
        self._controller.save_current_queue(normalized, snapshot_cues)

    def _load_saved_queue(self) -> None:
        if self._controller is None:
            return
        name = self._saved_queue_menu.get()
        saved_queue_id = self._saved_queue_ids.get(name)
        if saved_queue_id is None:
            return
        restored_count = len(
            self._restored_queue_ids.intersection(entry.queue_id for entry in self._queue_entries)
        )
        restored_notice = (
            f"\n\nHinweis: {restored_count} wiederhergestellte Alt-Titel stehen bereits "
            "in der Queue. Beim Anhängen werden sie vor den neuen Titeln abgespielt."
            if restored_count
            else ""
        )
        choice = ask_silent_yes_no_cancel(
            self,
            "Playlist laden",
            "Sollen aktuelle wartende Titel ersetzt werden?\n\n"
            "Ja = ersetzen\nNein = anhängen\nAbbrechen = nichts ändern"
            f"{restored_notice}",
        )
        if choice is None:
            return
        use_saved_cues = ask_silent_yes_no(
            self,
            "Cue-Werte laden?",
            "Sollen die gespeicherten Veranstaltungswerte verwendet werden?\n\n"
            "Ja: Gespeicherte Cue- und Überblendwerte verwenden.\n"
            "Nein: Die aktuell gültigen Katalogwerte verwenden.",
        )

        play_all_in_order = ask_silent_yes_no(
            self,
            "Vollständig abspielen?",
            "Soll diese Playlist vollständig in ihrer Reihenfolge "
            "abgespielt werden?\n\n"
            "Ja: Wiederholungsschutz für diese geladenen Einträge übersteuern.\n"
            "Nein: Normale Party-Regeln anwenden; Titel können übersprungen werden.",
        )
        self._controller.load_saved_queue(
            saved_queue_id,
            replace_waiting=choice,
            shuffle_tracks=bool(self._playlist_shuffle.get()),
            use_saved_cues=use_saved_cues,
            play_all_in_order=play_all_in_order,
        )

    def _show_saved_queue(self) -> None:
        if self._controller is None:
            return
        saved_queue_id = self._saved_queue_ids.get(self._saved_queue_menu.get())
        if saved_queue_id is not None:
            self._controller.show_saved_queue(saved_queue_id)

    def _load_playlist_track(self, track_id: int, deck_id: str) -> None:
        if self._controller is not None:
            self._controller.load_playlist_track(track_id, deck_id)

    def _add_playlist_track(self, track_id: int) -> None:
        if self._controller is not None:
            self._controller.add_playlist_track_to_queue(track_id)

    def _crossfade(self, value: float) -> None:
        if not self._updating_mixer and self._controller is not None:
            self._controller.set_crossfader(float(value))

    def _move_crossfader_by_keyboard(self, change: float) -> str:
        return self._set_crossfader_by_keyboard(self._crossfader.get() + change)

    def _set_crossfader_by_keyboard(self, value: float) -> str:
        position = max(0.0, min(float(value), 1.0))
        self._crossfader.set(position)
        self._crossfader_label.configure(text=f"Crossfader · {position:.0%}")
        if self._controller is not None:
            self._controller.set_crossfader(position)
        return "break"

    def _master_changed(self, value: float) -> None:
        if not self._updating_mixer and self._controller is not None:
            self._controller.set_master_volume(float(value))

    def _audio_device_changed(self, label: str) -> None:
        if self._controller is not None:
            self._controller.set_audio_output_device(self._audio_device_ids.get(label, ""))

    def _retry_audio_output_device(self) -> None:
        if self._controller is not None:
            self._controller.retry_audio_output_device()

    def _confirm_audio_output_device(self) -> None:
        if self._controller is not None:
            self._controller.confirm_audio_output_device_recovered()

    def _request_global_audio_recovery(self) -> None:
        if self._controller is None:
            return
        if self._controller.global_audio_recovery_ready_for_release():
            confirmed = ask_silent_yes_no(
                self,
                "Audioausgabe freigeben",
                "Die Sicherheits-Stummschaltung beider Decks wird aufgehoben. "
                "Die Automatik bleibt pausiert.\n\nAudioausgabe jetzt freigeben?",
            )
            if confirmed:
                self._controller.release_global_audio_recovery_mute()
                self._refresh_global_audio_recovery_button()
            return
        confirmed = ask_silent_yes_no(
            self,
            "Globale Audio-Reparatur",
            "Beide Audio-Backends werden ersetzt. Die Wiedergabe wird pausiert und "
            "bleibt anschließend stumm, bis die Ausgabe bewusst freigegeben wird.\n\n"
            "Globale Reparatur jetzt starten?",
        )
        if self._controller is not None and confirmed:
            started = self._controller.start_global_audio_recovery()
            if started:
                self._global_audio_recovery_button.configure(state="disabled")
                self.after(500, self._refresh_global_audio_recovery_button)

    def _refresh_global_audio_recovery_button(self) -> None:
        if self._controller is None:
            return
        active = self._controller.global_audio_recovery_active()
        ready = self._controller.global_audio_recovery_ready_for_release()
        self._global_audio_recovery_button.configure(
            state="disabled" if active else "normal",
            text="Audio freigeben…" if ready else "Globale Audio-Reparatur…",
        )
        if active:
            self.after(500, self._refresh_global_audio_recovery_button)

    def _emergency_profile_changed(self, label: str) -> None:
        if self._controller is not None:
            self._controller.set_emergency_action_profile(self._emergency_profile_labels[label])

    def _begin_emergency_hold(self, _event: object) -> None:
        if self._controller is None or self._emergency_hold_after_id is not None:
            return
        self._emergency_hold_triggered = False
        self._emergency_hold_button.configure(text="Weiter halten…")
        self._emergency_hold_after_id = self.after(1000, self._trigger_emergency_hold)

    def _cancel_emergency_hold(self, _event: object) -> None:
        if self._emergency_hold_after_id is not None:
            self.after_cancel(self._emergency_hold_after_id)
            self._emergency_hold_after_id = None
        if not self._emergency_hold_triggered:
            self._emergency_hold_button.configure(text="1 Sekunde halten")

    def _trigger_emergency_hold(self) -> None:
        self._emergency_hold_after_id = None
        self._emergency_hold_triggered = True
        label = self._emergency_profile_menu.get()
        profile = self._emergency_profile_labels[label]
        started = self._controller is not None and self._controller.start_emergency_action(profile)
        self._emergency_hold_button.configure(
            text="Ausgelöst" if started else "Aktion bereits aktiv"
        )
        self.after(
            1500,
            lambda: self._emergency_hold_button.configure(text="1 Sekunde halten"),
        )

    def _toggle_mute(self) -> None:
        if self._controller is not None:
            self._controller.toggle_mute()

    def _fade_duration_changed(self, value: float) -> None:
        duration = round(float(value))
        self._fade_duration_label.configure(text=f"{duration} s")
        if self._controller is not None:
            self._controller.set_fade_duration(duration)

    def _fade_stop_changed(self) -> None:
        if self._controller is not None:
            self._controller.set_fade_out_stops_deck(bool(self._fade_stop_switch.get()))

    def _restore_session_changed(self) -> None:
        if self._controller is not None:
            self._controller.set_restore_last_session(bool(self._restore_session_switch.get()))

    def _fullscreen_start_changed(self) -> None:
        if self._controller is not None:
            self._controller.set_fullscreen_on_start(bool(self._fullscreen_start_switch.get()))

    def _file_browser_changed(self) -> None:
        if self._controller is not None:
            self._controller.set_file_browser_enabled(bool(self._file_browser_switch.get()))

    def _production_mode_changed(self) -> None:
        if self._controller is not None:
            self._controller.set_production_mode(bool(self._production_mode_switch.get()))

    def _save_diagnostic_report(self) -> None:
        if self._controller is None:
            return
        context = self._diagnostic_context_labels[self._diagnostic_context.get()]
        self._controller.save_performance_diagnostic(context)

    def _begin_diagnostic_scenario(self) -> None:
        if self._controller is None:
            return
        context = self._diagnostic_context_labels[self._diagnostic_context.get()]
        try:
            delay_ms = max(0, int(self._database_delay_entry.get().strip() or "1000"))
        except ValueError:
            delay_ms = 1000
            self._database_delay_entry.delete(0, "end")
            self._database_delay_entry.insert(0, "1000")
        self._controller.begin_diagnostic_scenario(context, delay_ms)

    def _shortcut_play_pause(self, deck_id: str) -> None:
        if self._controller is not None:
            self._controller.toggle_deck_play_pause(deck_id)

    def _player_mode_changed(self, value: str) -> None:
        if self._controller is not None:
            modes = {
                "MANUELL": "manual",
                "HALBAUTOMATISCH": "semi_automatic",
                "AUTOMATISCH": "automatic",
            }
            mode = modes.get(value, "manual")
            self._controller.set_player_mode(mode)

    def _toggle_automatic_queue(self) -> None:
        if self._controller is None:
            return
        if self._automatic_queue_active:
            choice = ask_silent_yes_no_cancel(
                self,
                "Automatik anhalten",
                "Soll die Automatik nur pausiert oder vollständig beendet werden?\n\n"
                "Ja = pausieren und später fortsetzen\n"
                "Nein = Automatik beenden\n"
                "Abbrechen = unverändert weiterlaufen",
            )
            if choice is None:
                return
            if choice:
                self._controller.pause_automatic_queue()
            else:
                self._controller.stop_automatic_queue()
        else:
            from_selected = False
            skip_earlier = True
            if (
                not self._controller.is_automatic_queue_paused()
                and self._controller.automatic_start_has_earlier_waiting_entries()
            ):
                choice = ask_silent_yes_no_cancel(
                    self,
                    "Automatik starten",
                    "Vor dem ausgewählten Titel warten noch Queue-Titel.\n\n"
                    "Ja = beim ersten wartenden Titel beginnen\n"
                    "Nein = ab dem ausgewählten Titel beginnen und frühere Titel "
                    "überspringen\n"
                    "Abbrechen = Automatik nicht starten",
                )
                if choice is None:
                    return
                from_selected = not choice
                if from_selected:
                    skip_earlier = ask_silent_yes_no(
                        self,
                        "Frühere Titel behandeln",
                        "Sollen die Titel vor der Auswahl übersprungen werden?\n\n"
                        "Ja = mit sichtbarem Grund überspringen\n"
                        "Nein = wartend behalten und nach dem ausgewählten Titel abspielen",
                    )
            summary = self._controller.automatic_start_summary(
                from_selected=from_selected,
                skip_earlier=skip_earlier,
            )
            if not ask_silent_yes_no(
                self,
                "Automatik starten?",
                summary + "\n\nSoll die Automatik mit diesen Einstellungen gestartet werden?",
            ):
                return
            self._controller.start_automatic_queue(
                from_selected=from_selected,
                skip_earlier=skip_earlier,
            )

    def _show_automatic_help(self) -> None:
        show_silent_message(
            self,
            "Hilfe zu Queue und Automatik",
            _automatic_help_text(),
        )

    def _request_close(self) -> None:
        if self._controller is None:
            self._persist_window_geometry()
            self.destroy()
            return
        audio_note = (
            "Mindestens ein Deck spielt und wird beim Schließen gestoppt.\n\n"
            if self._controller.is_audio_active()
            else ""
        )
        choice = ask_silent_yes_no_cancel(
            self,
            "DeckRelay schließen?",
            audio_note + "Soll die aktive Session abgeschlossen werden?\n\n"
            "Ja = Session abschließen\n"
            "Nein = Session für den nächsten Start offenlassen\n"
            "Abbrechen = DeckRelay geöffnet lassen",
        )
        if choice is None:
            return
        self._controller.close(finish_session=choice)
        self._persist_window_geometry()
        self._dispose_resources()
        self.destroy()

    def _dispose_resources(self) -> None:
        """Idempotently release GUI-owned callbacks, images and row resources."""
        self._overlay_tick_active = False
        for after_id in tuple(self._scheduled_after_ids):
            try:
                self.after_cancel(after_id)
            except (ValueError, RuntimeError):
                pass
        self._scheduled_after_ids.clear()
        for tooltip in self._static_tooltips:
            tooltip.close()
        self._static_tooltips.clear()
        self.deck_a.dispose()
        self.deck_b.dispose()
        compact_deck_a = self.__dict__.get("compact_deck_a")
        compact_deck_b = self.__dict__.get("compact_deck_b")
        if compact_deck_a is not None:
            compact_deck_a.dispose()
        if compact_deck_b is not None:
            compact_deck_b.dispose()
        for catalog_row in self._catalog_rows:
            catalog_row.dispose()
        self._catalog_rows.clear()
        for queue_row in self._queue_rows:
            queue_row.dispose()
        destroyed_queue_widgets = len(self._queue_rows) * 6
        self._queue_lifecycle_counters["destroyed_widget_count"] += destroyed_queue_widgets
        self._render_counters["widgets_destroyed_total"] += destroyed_queue_widgets
        self._queue_rows.clear()
        self._queue_tooltip_manager.close()
        overlay_panel = self.__dict__.get("_overlay_panel")
        if overlay_panel is not None:
            overlay_panel.close()
        overlay_dialog = self.__dict__.get("_overlay_management_dialog")
        if overlay_dialog is not None and overlay_dialog.winfo_exists():
            overlay_dialog.destroy()
        database_dialog = self.__dict__.get("_database_backup_dialog")
        if database_dialog is not None and database_dialog.winfo_exists():
            database_dialog.destroy()
        for deck_id in tuple(self._cover_images):
            self._clear_deck_cover(deck_id, "Kein Cover")
