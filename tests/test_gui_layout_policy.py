from party_player.ui.main_window import (
    MainWindow,
    _center_panel_grid_options,
    _compact_mixer_visible,
    _compact_live_rows,
    _compact_preparation_rows,
    _diagnostic_toggle_text,
    _automatic_help_text,
    _initial_catalog_pool_target,
    _main_layout_spacing,
    _mixer_container_grid_options,
    _optionmenu_changes,
    _presentation_header_grid_options,
    _queue_model_count,
    _queue_pool_size,
)
from party_player.ui.compact_deck_presentation import compact_deck_presentation
from party_player.enums import DeckState
from party_player.models import Deck, Track
from party_player.presentation import (
    GlobalStatusState,
    PresentationState,
    ResolvedPresentation,
    Workspace,
)


class Disposable:
    def __init__(self) -> None:
        self.dispose_count = 0

    def dispose(self) -> None:
        self.dispose_count += 1


class Closable:
    def close(self) -> None:
        pass


class GridDouble:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, int]] = {}

    def grid_rowconfigure(self, row: int, **values: int) -> None:
        self.rows[row] = values


class RemovableGridDouble:
    def __init__(self) -> None:
        self.remove_count = 0

    def grid_remove(self) -> None:
        self.remove_count += 1


class SplitControllerDouble:
    def __init__(self) -> None:
        self.saved: list[float] = []

    def set_workspace_catalog_ratio(self, ratio: float) -> None:
        self.saved.append(ratio)


class FocusDouble:
    def __init__(self) -> None:
        self.focus_count = 0

    def focus_set(self) -> None:
        self.focus_count += 1


class ConfigureDouble:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def configure(self, **values: object) -> None:
        self.values.update(values)


def test_initial_catalog_pool_is_bounded_and_reuses_existing_rows() -> None:
    assert _initial_catalog_pool_target(50, 0) == 10
    assert _initial_catalog_pool_target(10, 0) == 10


def test_main_layout_spacing_uses_only_two_stable_size_classes() -> None:
    assert _main_layout_spacing(1180) == (8, 4, 6)
    assert _main_layout_spacing(1349) == (8, 4, 6)
    assert _main_layout_spacing(1350) == (16, 8, 8)
    assert _main_layout_spacing(1920) == (16, 8, 8)
    assert _initial_catalog_pool_target(50, 24) == 24


def test_center_panel_restores_one_column_after_compact_layout() -> None:
    assert _center_panel_grid_options(True)["columnspan"] == 3
    assert _center_panel_grid_options(False) == {
        "row": 1,
        "column": 1,
        "columnspan": 1,
        "padx": 8,
        "pady": 8,
        "sticky": "nsew",
    }


def test_presentation_header_restores_one_column_after_compact_layout() -> None:
    assert _presentation_header_grid_options(True)["columnspan"] == 2
    assert _presentation_header_grid_options(False) == {
        "row": 0,
        "column": 1,
        "columnspan": 1,
        "padx": 8,
        "pady": (6, 3),
        "sticky": "ew",
    }


def test_mixer_disclosure_remains_reachable_in_compact_layout() -> None:
    compact = _mixer_container_grid_options(True)
    large = _mixer_container_grid_options(False)

    assert compact["row"] == large["row"] == 2
    assert compact["columnspan"] == large["columnspan"] == 3
    assert compact["pady"] == (0, 8)


def test_compact_jingle_pads_temporarily_use_mixer_footer_space() -> None:
    assert _compact_mixer_visible(overlays_expanded=False) is True
    assert _compact_mixer_visible(overlays_expanded=True) is False


def test_compact_jingle_disclosure_precedes_flexible_queue_rows() -> None:
    rows = _compact_live_rows()

    assert rows["crossfader"] < rows["overlays"] < rows["queue_header"]
    assert rows["queue_toolbar"] < rows["queue"]


def test_compact_preparation_keeps_catalog_as_the_flexible_center_region() -> None:
    rows = _compact_preparation_rows()

    assert rows["live_status"] < rows["search"] < rows["catalog"]
    assert rows["catalog"] < rows["tools"] < rows["playlist"]


def test_ctrl_f_switches_compact_live_to_preparation_before_focusing() -> None:
    window = object.__new__(MainWindow)
    search = FocusDouble()
    selected: list[Workspace] = []
    scheduled: list[object] = []
    window._search = search
    window._presentation_coordinator = type(
        "Coordinator",
        (),
        {
            "state": PresentationState(
                resolved=ResolvedPresentation.COMPACT,
                workspace=Workspace.LIVE,
            )
        },
    )()
    window._select_workspace = selected.append
    window.schedule = lambda _delay, callback: scheduled.append(callback)

    MainWindow._focus_search(window)

    assert selected == [Workspace.PREPARATION]
    assert search.focus_count == 0
    scheduled[0]()
    assert search.focus_count == 1


def test_compact_safety_stop_targets_only_explicitly_on_air_decks() -> None:
    window = object.__new__(MainWindow)
    commands: list[tuple[str, str]] = []
    window._deck_on_air = {"A": False, "B": True}
    window._deck_action = lambda deck_id, action: commands.append((deck_id, action))

    MainWindow._stop_on_air_decks(window)

    assert commands == [("B", "stop")]


def test_active_analysis_does_not_reopen_removed_catalog_controls() -> None:
    window = object.__new__(MainWindow)
    toggle = ConfigureDouble()
    window._compact_analysis_expanded = False
    window._compact_analysis_toggle = toggle
    window._compact_layout_active = False
    window._presentation_coordinator = None

    MainWindow._expand_compact_analysis_for_active_job(window)

    assert window._compact_analysis_expanded is False
    assert toggle.values == {}


def test_legacy_compact_analysis_callback_opens_catalog_maintenance() -> None:
    window = object.__new__(MainWindow)
    toggle = ConfigureDouble()
    window._compact_analysis_expanded = True
    window._compact_analysis_toggle = toggle
    window._compact_layout_active = False
    opened: list[bool] = []
    window._open_catalog_maintenance = lambda: opened.append(True)

    MainWindow._toggle_compact_analysis(window)

    assert opened == [True]


def test_selected_playlist_does_not_replace_explicit_active_source() -> None:
    window = object.__new__(MainWindow)
    active_source = "Verzeichnis: Tanzmusik"
    window._presentation_status = GlobalStatusState(source=active_source)
    window._sync_playlist_equalizer_menu = lambda: None

    MainWindow._saved_queue_selected(window, "Nur zur Bearbeitung")

    assert window._presentation_status.source == active_source


def test_compact_layout_reassertion_uses_current_workspace_not_stale_callback_state() -> None:
    window = object.__new__(MainWindow)
    window._compact_layout_active = True
    window._presentation_coordinator = type(
        "Coordinator",
        (),
        {
            "state": PresentationState(
                resolved=ResolvedPresentation.COMPACT,
                workspace=Workspace.PREPARATION,
            )
        },
    )()
    applications: list[tuple[Workspace, bool]] = []
    window._show_compact_layout = lambda workspace, schedule_reassertion: applications.append(
        (workspace, schedule_reassertion)
    )

    MainWindow._ensure_compact_layout_exclusive(window)

    assert applications == [(Workspace.PREPARATION, False)]


def test_diagnostic_disclosure_label_matches_expanded_state() -> None:
    assert _diagnostic_toggle_text(False) == "Diagnose und Analyse anzeigen ▼"
    assert _diagnostic_toggle_text(True) == "Diagnose und Analyse ausblenden ▲"


def test_automatic_help_explains_safe_queue_and_playback_controls() -> None:
    text = _automatic_help_text()

    for expected in (
        "Ersetzen",
        "Anhängen",
        "Vollständig abspielen",
        "ersten wartenden Titel",
        "Deck-Pause",
        "Crossfader",
        "Cue-Fallback",
    ):
        assert expected in text


def test_optionmenu_policy_skips_identical_state() -> None:
    state = (("A", "B"), "A")

    assert _optionmenu_changes(state, ["A", "B"], "A") == (False, False)
    assert _optionmenu_changes(state, ["A", "B"], "B") == (False, True)
    assert _optionmenu_changes(state, ["A", "C"], "A") == (True, False)


def test_queue_pool_tracks_height_but_never_exceeds_virtualization_limits() -> None:
    assert _queue_pool_size(1) == 10
    assert _queue_pool_size(400) == 14
    assert _queue_pool_size(5000) == 20
    assert _queue_model_count(10, 20) == 20
    assert _queue_model_count(20, 10) == 20


def test_workspace_split_keeps_both_lists_visible_and_persists_choice() -> None:
    window = object.__new__(MainWindow)
    center = GridDouble()
    controller = SplitControllerDouble()
    window._center_panel = center
    window._controller = controller

    window._set_workspace_split(0.8)

    assert center.rows[2] == {
        "weight": 80,
        "minsize": 80,
        "uniform": "list_workspace",
    }
    assert center.rows[9] == {
        "weight": 20,
        "minsize": 80,
        "uniform": "list_workspace",
    }
    assert controller.saved == [0.8]

    window._set_workspace_split(1.0, persist=False)
    assert window._workspace_catalog_ratio == 0.8
    assert controller.saved == [0.8]


def test_workspace_focus_moves_to_live_action_or_preparation_search() -> None:
    window = object.__new__(MainWindow)
    live = FocusDouble()
    search = FocusDouble()
    window._automatic_queue_button = live
    window._search = search

    window._focus_workspace(Workspace.LIVE)
    assert live.focus_count == 1
    assert search.focus_count == 0

    window._focus_workspace(Workspace.PREPARATION)
    assert live.focus_count == 1
    assert search.focus_count == 1


def test_compact_deck_projects_existing_deck_state_without_mutation() -> None:
    track = Track(7, r"D:\Musik\long-title.mp3", "Long title", "Artist", "Album", 240.0, bpm=126.0)
    deck = Deck(
        "A",
        loaded_track=track,
        state=DeckState.PLAYING,
        volume=0.72,
        position=45.0,
        duration=240.0,
        is_on_air=True,
        cue_warning="Cue prüfen",
    )

    model = compact_deck_presentation(deck)

    assert model.title == "Artist – Long title"
    assert model.source == "long-title.mp3"
    assert model.state == "● ON AIR"
    assert model.progress == 45.0 / 240.0
    assert model.warning == "Cue prüfen"
    assert model.bpm == 126.0
    assert deck.position == 45.0


def test_repeated_compact_layout_decision_does_not_rebuild_or_reapply() -> None:
    window = object.__new__(MainWindow)
    window._presentation_layout_signature = None
    window._compact_layout_apply_count = 0
    applied: list[Workspace] = []
    window._show_compact_layout = applied.append
    window._show_large_layout = lambda: None
    state = PresentationState(resolved=ResolvedPresentation.COMPACT, workspace=Workspace.LIVE)

    window._apply_presentation_layout(state)
    window._apply_presentation_layout(state)

    assert applied == [Workspace.LIVE]
    assert window._compact_layout_apply_count == 1


def test_queue_dispose_counts_destroyed_widgets_once() -> None:
    window = object.__new__(MainWindow)
    rows = (Disposable(), Disposable())
    window._scheduled_after_ids = set()
    window._static_tooltips = []
    window.deck_a = Disposable()
    window.deck_b = Disposable()
    window._catalog_rows = []
    window._queue_rows = list(rows)
    window._queue_tooltip_manager = Closable()
    window._cover_images = {}
    window._queue_lifecycle_counters = {"destroyed_widget_count": 0}
    window._render_counters = {"widgets_destroyed_total": 0}

    window._dispose_resources()
    window._dispose_resources()

    assert [row.dispose_count for row in rows] == [1, 1]
    assert window._queue_lifecycle_counters["destroyed_widget_count"] == 12
    assert window._render_counters["widgets_destroyed_total"] == 12


def test_empty_queue_pool_placeholder_does_not_wait_for_widget_creation() -> None:
    window = object.__new__(MainWindow)
    window._queue_rows = [object(), object()]
    window._queue_view_models = [object(), object(), None, None]

    assert not window._queue_row_requires_creation(2)
    assert not window._queue_row_requires_creation(3)

    window._queue_view_models[2] = object()
    assert window._queue_row_requires_creation(2)
