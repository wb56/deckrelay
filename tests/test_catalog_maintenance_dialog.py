"""Display-independent lifecycle tests for the catalog-maintenance dialog."""

from tkinter import TclError
from types import SimpleNamespace
from typing import Any, cast

import customtkinter as ctk  # type: ignore[import-untyped]
import pytest

from party_player.catalog_maintenance import (
    BatchAction,
    BatchExample,
    BatchPreview,
    BatchResult,
    MaintenanceFilter,
    MetadataBatchRequest,
    SelectionDescription,
)
from party_player.ui.catalog_maintenance_filter_analysis_panel import (
    CatalogMaintenanceFilterAnalysisPanel,
)
from party_player.metadata_rules import MetadataFieldKey
from party_player.metadata_analysis_service import TempoBatchProgress
from party_player.track_suitability import TrackSuitabilityStatus
from party_player.ui.catalog_maintenance_dialog import (
    CatalogAnalysisActions,
    CATALOG_DIALOG_MINIMUM_SIZE,
    CATALOG_DIALOG_PREFERRED_SIZE,
    CatalogMaintenanceDialog,
    WORKFLOW_SECTIONS,
    ask_filter_selection_strategy,
    high_risk_confirmation_text,
    parse_bpm_filter,
    parse_batch_input,
)


def test_catalog_maintenance_layout_declares_workflow_and_resizable_geometry() -> None:
    assert WORKFLOW_SECTIONS == (
        "1. Titel finden und Ergebnis eingrenzen",
        "2. Titel auswählen",
        "3. Aktion festlegen",
        "4. Prüfen und ausführen",
    )
    assert CATALOG_DIALOG_PREFERRED_SIZE == (1180, 760)
    assert CATALOG_DIALOG_MINIMUM_SIZE == (760, 560)
    assert all(
        minimum < preferred
        for minimum, preferred in zip(CATALOG_DIALOG_MINIMUM_SIZE, CATALOG_DIALOG_PREFERRED_SIZE)
    )


def test_metadata_target_value_is_enabled_only_for_value_actions() -> None:
    class Action:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

    class Value:
        def __init__(self) -> None:
            self.options: dict[str, object] = {}

        def configure(self, **options: object) -> None:
            self.options.update(options)

    class Dialog:
        _action = Action("Wert setzen")
        _value = Value()

    dialog = Dialog()
    CatalogMaintenanceDialog._action_changed(cast(Any, dialog))
    assert dialog._value.options["state"] == "normal"

    dialog._action.value = "Vorhandenen Wert bestätigen"
    CatalogMaintenanceDialog._action_changed(cast(Any, dialog))
    assert dialog._value.options["state"] == "disabled"


def test_late_page_result_does_not_touch_destroyed_dialog() -> None:
    class Dialog:
        def _active(self) -> bool:
            return False

        def _show_page(self, _page: object) -> None:
            raise AssertionError("destroyed widgets must not be accessed")

    CatalogMaintenanceDialog._page_loaded(cast(Any, Dialog()), object())


def test_close_is_idempotent_and_releases_dialog(monkeypatch: Any) -> None:
    from threading import Event

    released: list[object] = []
    monkeypatch.setattr(
        "party_player.ui.catalog_maintenance_dialog.release_dialog", released.append
    )
    cancelled_callbacks: list[str] = []

    class Dialog:
        _closed = False
        _cancel_event = Event()
        _layout_after_id = "layout-id"
        destroyed = 0
        cancelled = cancelled_callbacks
        _filter_analysis_panel = SimpleNamespace(
            _cancel_pending_analysis_scroll=lambda: cancelled_callbacks.append("analysis")
        )

        def after_cancel(self, after_id: str) -> None:
            self.cancelled.append(after_id)

        def destroy(self) -> None:
            self.destroyed += 1

    dialog = Dialog()
    CatalogMaintenanceDialog._close(cast(Any, dialog))
    CatalogMaintenanceDialog._close(cast(Any, dialog))

    assert released == [dialog]
    assert dialog.destroyed == 1
    assert dialog._cancel_event.is_set()
    assert dialog._load_generation == 1
    assert dialog.cancelled == ["layout-id", "analysis"]
    assert dialog._layout_after_id is None


def _preview(key: MetadataFieldKey, selected: int = 3) -> BatchPreview:
    request = MetadataBatchRequest(
        SelectionDescription.for_filter(MaintenanceFilter()).select(1),
        frozenset({key}),
        BatchAction.SET,
        ((key, "Gemeinsamer Wert"),),
    )
    return BatchPreview(
        "token",
        request,
        selected,
        selected,
        0,
        0,
        0,
        0,
        0,
        (BatchExample(1, key, "Vorher", "Gemeinsamer Wert"),),
        1,
    )


@pytest.mark.parametrize(
    "key", [MetadataFieldKey.TITLE, MetadataFieldKey.ARTIST, MetadataFieldKey.ALBUM]
)
def test_identity_batches_require_explicit_second_confirmation(
    key: MetadataFieldKey,
) -> None:
    text = high_risk_confirmation_text(_preview(key, 184))

    assert text is not None
    assert "184 ausgewählten" in text
    assert "Tatsächlich änderbar: 184" in text
    assert "Gemeinsamer Wert" in text
    assert "Vorher" in text
    assert "wirklich für alle angezeigten Titel" in text
    assert "Musikdateien und Tags werden nicht verändert" in text


def test_harmless_or_single_item_batch_needs_no_extra_confirmation() -> None:
    assert high_risk_confirmation_text(_preview(MetadataFieldKey.RATING, 184)) is None
    assert high_risk_confirmation_text(_preview(MetadataFieldKey.TITLE, 1)) is None


@pytest.mark.parametrize(
    ("key", "raw", "message"),
    [
        (MetadataFieldKey.DANCEABILITY, "sehr gut", "Tanzbarkeit"),
        (MetadataFieldKey.ENERGY, "hoch", "Energie"),
        (MetadataFieldKey.RATING, "sechs", "Bewertung"),
        (MetadataFieldKey.BPM, "schnell", "BPM"),
    ],
)
def test_batch_input_errors_are_user_facing(key: MetadataFieldKey, raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message) as raised:
        parse_batch_input(key, BatchAction.SET, raw)

    assert "invalid literal" not in str(raised.value)


def test_batch_input_validates_ranges_and_accepts_decimal_comma() -> None:
    with pytest.raises(ValueError, match="0 bis 100"):
        parse_batch_input(MetadataFieldKey.DANCEABILITY, BatchAction.SET, "101")
    assert parse_batch_input(MetadataFieldKey.DANCEABILITY, BatchAction.SET, "85") == 85
    assert parse_batch_input(MetadataFieldKey.BPM, BatchAction.SET, "123,5") == 123.5


def test_empty_batch_value_uses_action_specific_message() -> None:
    with pytest.raises(ValueError, match="^Bitte einen Zielwert eingeben\\.$"):
        parse_batch_input(MetadataFieldKey.TITLE, BatchAction.SET, "  ")


def test_bpm_filter_accepts_open_bounds_and_decimal_comma() -> None:
    assert parse_bpm_filter("", "128,5") == (None, 128.5)
    assert parse_bpm_filter("95", "") == (95.0, None)


@pytest.mark.parametrize(
    ("minimum", "maximum", "message"),
    [("schnell", "", "Zahl"), ("19", "", "20 und 300"), ("130", "120", "größer")],
)
def test_bpm_filter_rejects_invalid_ranges(minimum: str, maximum: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_bpm_filter(minimum, maximum)


def test_stale_page_result_is_ignored() -> None:
    class Dialog:
        _load_generation = 4

        def _active(self) -> bool:
            return True

        def _show_page(self, _page: object) -> None:
            raise AssertionError("stale page must not be rendered")

    CatalogMaintenanceDialog._page_loaded(cast(Any, Dialog()), object(), 3)


def test_page_is_submitted_before_independent_counts() -> None:
    submitted: list[tuple[object, object, object]] = []

    class Repository:
        def page(self, *_args: object) -> object:
            return object()

        def counts(self) -> object:
            return object()

    class Dialog:
        _load_generation = 0
        _filter = MaintenanceFilter()
        _page = 1
        _counts = _Widget()
        _page_label = _Widget()
        _service = type("Service", (), {"repository": Repository()})()

        def _load_task(self, work: object, done: object, generation: object) -> None:
            submitted.append((work, done, generation))

    dialog = Dialog()
    CatalogMaintenanceDialog._load_counts_and_page(cast(Any, dialog))

    assert len(submitted) == 1
    assert submitted[0][0]() is not None  # type: ignore[operator]
    assert dialog._counts.options["text"] == "Arbeitsvorräte werden gezählt …"


def test_stale_statistics_and_load_errors_are_ignored() -> None:
    class Dialog:
        _load_generation = 4
        _counts = _Widget()

        def _active(self) -> bool:
            return True

        def _failed(self, _error: Exception) -> None:
            raise AssertionError("stale errors must not be displayed")

    dialog = Dialog()
    CatalogMaintenanceDialog._counts_loaded(cast(Any, dialog), (), 3)
    CatalogMaintenanceDialog._load_failed(cast(Any, dialog), RuntimeError("stale"), 3)

    assert dialog._counts.options == {}


def test_preview_validation_happens_before_worker_submission() -> None:
    class Dialog:
        _result = _Widget()

        def _request(self) -> MetadataBatchRequest:
            raise ValueError("Bitte einen Zielwert eingeben.")

        def _task(self, *_args: object) -> None:
            raise AssertionError("invalid actions must not reach a worker")

    dialog = Dialog()
    CatalogMaintenanceDialog._make_preview(cast(Any, dialog))

    assert dialog._result.options["text"] == "Fehler: Bitte einen Zielwert eingeben."


def test_execute_without_preview_performs_explicit_action_validation() -> None:
    class Dialog:
        _running = False
        _preview = None
        previews = 0

        def _make_preview(self) -> None:
            self.previews += 1

    dialog = Dialog()
    CatalogMaintenanceDialog._execute(cast(Any, dialog))

    assert dialog.previews == 1


def test_double_click_does_not_start_second_execution(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "party_player.ui.catalog_maintenance_dialog.ask_silent_yes_no",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not ask again")),
    )

    class Dialog:
        _running = True
        _preview = _preview(MetadataFieldKey.TITLE)

    CatalogMaintenanceDialog._execute(cast(Any, Dialog()))


def test_filter_callbacks_ignore_destroyed_dialog() -> None:
    class Dialog:
        def _active(self) -> bool:
            return False

        def _load_counts_and_page(self) -> None:
            raise AssertionError("destroyed widgets must not be accessed")

    CatalogMaintenanceDialog._filter_restricted(cast(Any, Dialog()), MaintenanceFilter(), ((1, 0),))
    CatalogMaintenanceDialog._filter_preserved(cast(Any, Dialog()), MaintenanceFilter(), ((1, 0),))


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([True], "KEEP"),
        ([False, True], "RESTRICT"),
        ([False, False], "DISCARD"),
        ([None], None),
        ([False, None], None),
    ],
)
def test_filter_selection_strategies(
    monkeypatch: Any, answers: list[bool | None], expected: str | None
) -> None:
    pending = iter(answers)
    monkeypatch.setattr(
        "party_player.ui.catalog_maintenance_dialog.ask_silent_yes_no_cancel",
        lambda *_args: next(pending),
    )

    assert ask_filter_selection_strategy(object()) == expected


class _Widget:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **options: object) -> None:
        self.options.update(options)


class _FilterWidget(_Widget):
    def __init__(self, value: str = "dirty") -> None:
        super().__init__()
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def delete(self, _first: object, _last: object) -> None:
        self.value = ""


def test_reset_filters_clears_inputs_selection_and_reloads() -> None:
    class Dialog:
        _queue = _FilterWidget()
        _filter_field = _FilterWidget()
        _filter_source = _FilterWidget()
        _filter_status = _FilterWidget()
        _filter_value = _FilterWidget()
        _filter_confirmed = _FilterWidget()
        _filter_conflict = _FilterWidget()
        _filter_suggestion = _FilterWidget()
        _search = _FilterWidget("Dance")
        _filter_confidence = _FilterWidget("0.8")
        _filter_changed_from = _FilterWidget("2026-01-01")
        _filter_changed_to = _FilterWidget("2026-12-31")
        _filter_bpm_from = _FilterWidget("100")
        _filter_bpm_to = _FilterWidget("130")
        _selection_label = _Widget()
        _result = _Widget()
        _filter = MaintenanceFilter(text="Dance", minimum_bpm=100.0)
        _selection = SelectionDescription.for_filter(_filter).select_all_matches()
        _preview = object()
        _page = 4
        reloads = 0

        def _load_counts_and_page(self) -> None:
            self.reloads += 1

        def _update_selection(self) -> None:
            CatalogMaintenanceDialog._update_selection(cast(Any, self))

    dialog = Dialog()

    class Panel:
        def reset_filter_inputs(self) -> None:
            for widget, value in (
                (dialog._queue, "Alle Arbeitsvorräte"),
                (dialog._filter_field, "Alle Felder"),
                (dialog._filter_source, "Alle Quellen"),
                (dialog._filter_status, "Alle Prüfstatus"),
                (dialog._filter_value, "Wert egal"),
                (dialog._filter_confirmed, "Bestätigung egal"),
                (dialog._filter_conflict, "Konflikt egal"),
                (dialog._filter_suggestion, "Vorschlag egal"),
            ):
                widget.set(value)
            for entry in (
                dialog._search,
                dialog._filter_confidence,
                dialog._filter_changed_from,
                dialog._filter_changed_to,
                dialog._filter_bpm_from,
                dialog._filter_bpm_to,
            ):
                entry.delete(0, "end")

    dialog._filter_analysis_panel = Panel()
    CatalogMaintenanceDialog._reset_filters(cast(Any, dialog))

    assert dialog._filter == MaintenanceFilter()
    assert not dialog._selection.all_matches
    assert not dialog._selection.included_ids
    assert dialog._preview is None
    assert dialog._page == 1
    assert dialog._filter_bpm_from.value == dialog._filter_bpm_to.value == ""
    assert dialog.reloads == 1


def test_catalog_selection_actions_share_one_selection_with_suitability(
    monkeypatch: Any,
) -> None:
    class Choice:
        def get(self) -> str:
            return "Für Automatik und Wünsche geeignet"

    class Service:
        def __init__(self) -> None:
            self.persisted: tuple[int, ...] = ()

        def suitability_selection(self, selection: SelectionDescription) -> tuple[int, ...]:
            if selection.all_matches:
                return tuple(
                    track_id for track_id in range(1, 77) if track_id not in selection.excluded_ids
                )
            return tuple(sorted(selection.included_ids))

        def set_suitability(
            self, track_ids: tuple[int, ...], _status: TrackSuitabilityStatus
        ) -> int:
            self.persisted = track_ids
            return len(track_ids)

    class Dialog:
        _filter = MaintenanceFilter(text="visible")
        _selection = SelectionDescription.for_filter(_filter)
        _current = SimpleNamespace(
            total=76,
            rows=tuple(SimpleNamespace(track_id=track_id) for track_id in range(1, 13)),
        )
        _selection_label = _Widget()
        _suitability_apply_button = _Widget()
        _result = _Widget()
        _suitability = Choice()
        _running = False
        _service = Service()

        def _active(self) -> bool:
            return True

        def _show_page(self, _page: object) -> None:
            pass

        def _update_selection(self) -> None:
            CatalogMaintenanceDialog._update_selection(cast(Any, self))

        def _task(self, work: Any, done: Any) -> None:
            done(work())

        def _confirm_suitability_change(self, value: object) -> None:
            CatalogMaintenanceDialog._confirm_suitability_change(cast(Any, self), value)

        def _suitability_changed(self, value: object) -> None:
            CatalogMaintenanceDialog._suitability_changed(cast(Any, self), value)

        def _load_counts_and_page(self) -> None:
            pass

    monkeypatch.setattr(
        "party_player.ui.catalog_maintenance_dialog.ask_silent_yes_no",
        lambda *_args: True,
    )
    dialog = Dialog()

    CatalogMaintenanceDialog._toggle(cast(Any, dialog), 0)
    assert dialog._selection.included_ids == frozenset({1})
    assert dialog._selection_label.options["text"] == "1 ausgewählt"

    CatalogMaintenanceDialog._select_page(cast(Any, dialog))
    assert dialog._selection.included_ids == frozenset(range(1, 13))
    assert dialog._selection_label.options["text"] == "12 ausgewählt"

    CatalogMaintenanceDialog._select_all(cast(Any, dialog))
    assert dialog._selection.all_matches
    assert dialog._selection.filter == dialog._filter
    assert dialog._selection_label.options["text"] == "76 ausgewählt · 0 ausgeschlossen"

    CatalogMaintenanceDialog._toggle(cast(Any, dialog), 0)
    assert dialog._selection.excluded_ids == frozenset({1})
    assert dialog._selection_label.options["text"] == "75 ausgewählt · 1 ausgeschlossen"

    CatalogMaintenanceDialog._prepare_suitability_change(cast(Any, dialog))
    assert dialog._service.persisted == tuple(range(2, 77))

    CatalogMaintenanceDialog._clear_selection(cast(Any, dialog))
    assert not dialog._selection.all_matches
    assert dialog._selection.included_ids == frozenset()
    assert dialog._selection_label.options["text"] == "0 ausgewählt"


def test_preview_success_updates_state_and_summary() -> None:
    class Dialog:
        _preview: BatchPreview | None = None
        _result = _Widget()

        def _active(self) -> bool:
            return True

    dialog = Dialog()
    preview = _preview(MetadataFieldKey.RATING)

    CatalogMaintenanceDialog._previewed(cast(Any, dialog), preview)

    assert dialog._preview is preview
    assert "änderbar 3" in str(dialog._result.options["text"])


def test_cancel_request_is_visible_and_sets_worker_event() -> None:
    from threading import Event

    class Dialog:
        _cancel_event = Event()
        _cancel_button = _Widget()
        _result = _Widget()

    dialog = Dialog()

    CatalogMaintenanceDialog._cancel_batch(cast(Any, dialog))

    assert dialog._cancel_event.is_set()
    assert dialog._cancel_button.options["state"] == "disabled"
    assert "Teiltransaktion endet noch" in str(dialog._result.options["text"])


def test_partial_completion_and_error_are_rendered_without_widget_rebuild() -> None:
    class Dialog:
        _running = True
        _preview = _preview(MetadataFieldKey.RATING)
        _execute_button = _Widget()
        _cancel_button = _Widget()
        _result = _Widget()
        loaded = 0

        def _active(self) -> bool:
            return True

        def _load_counts_and_page(self) -> None:
            self.loaded += 1

    dialog = Dialog()
    result = BatchResult(
        1,
        "PARTIAL",
        4,
        3,
        2,
        0,
        0,
        1,
        0,
        0,
        1,
        0.1,
        1,
        (),
    )

    CatalogMaintenanceDialog._finished(cast(Any, dialog), result)

    assert dialog._running is False
    assert dialog._preview is None
    assert "PARTIAL" in str(dialog._result.options["text"])
    assert "1 Konflikte" in str(dialog._result.options["text"])
    assert dialog.loaded == 1

    CatalogMaintenanceDialog._failed(cast(Any, dialog), RuntimeError("kaputt"))
    assert "Fehler: kaputt" == dialog._result.options["text"]


def test_progress_poll_reads_worker_state_only_on_gui_callback() -> None:
    class Dialog:
        _poll_progress = CatalogMaintenanceDialog._poll_progress
        _running = True
        _progress_state = (250, 600)
        _progress_after: str | None = None
        _result = _Widget()

        def _active(self) -> bool:
            return True

        def after(self, _milliseconds: int, callback: object) -> str:
            assert callback is not None
            return "after-1"

    dialog = Dialog()

    CatalogMaintenanceDialog._poll_progress(cast(Any, dialog))

    assert "250 von 600" in str(dialog._result.options["text"])
    assert dialog._progress_after == "after-1"


def test_audio_analysis_actions_keep_existing_callbacks_separate() -> None:
    calls: list[str] = []
    actions = CatalogAnalysisActions(
        lambda: calls.append("cues_outdated"),
        lambda: calls.append("cues_all"),
        lambda: calls.append("cues_cancel"),
        lambda: calls.append("loudness_outdated"),
        lambda: calls.append("loudness_all"),
        lambda: calls.append("loudness_cancel"),
    )

    for action in (
        actions.analyze_cues_outdated,
        actions.analyze_cues_all,
        actions.cancel_cues,
        actions.analyze_loudness_outdated,
        actions.analyze_loudness_all,
        actions.cancel_loudness,
    ):
        CatalogMaintenanceDialog._run_analysis_action(cast(Any, object()), action)

    assert calls == [
        "cues_outdated",
        "cues_all",
        "cues_cancel",
        "loudness_outdated",
        "loudness_all",
        "loudness_cancel",
    ]


def test_audio_analysis_callback_error_uses_dialog_error_path() -> None:
    errors: list[Exception] = []

    class Dialog:
        def _failed(self, error: Exception) -> None:
            errors.append(error)

    failure = RuntimeError("Analyse konnte nicht gestartet werden")

    CatalogMaintenanceDialog._run_analysis_action(
        cast(Any, Dialog()), lambda: (_ for _ in ()).throw(failure)
    )

    assert errors == [failure]


class _TempoPanel:
    def __init__(self) -> None:
        self.states: list[tuple[str | None, dict[str, str]]] = []

    def render_tempo_state(self, text: str | None, **states: str) -> None:
        self.states.append((text, states))


def test_responsive_panel_switches_exactly_below_980_pixels() -> None:
    calls: list[tuple[bool, object]] = []
    results = object()

    class Panel:
        def set_compact_layout(self, compact: bool, results_panel: object) -> None:
            calls.append((compact, results_panel))

    class Dialog:
        _filter_analysis_panel = Panel()
        _results_panel = results
        _compact_filter_layout = False
        _closed = False
        _layout_running = False
        _layout_after_id = None
        _filter_layout_size: tuple[int, int] | None = None
        width = 979
        height = 760

        def winfo_width(self) -> int:
            return self.width

        def winfo_height(self) -> int:
            return self.height

        def _request_height_layout(self) -> None:
            pass

    dialog = Dialog()
    CatalogMaintenanceDialog._on_filter_resize(cast(Any, dialog), object())
    dialog.width = 980
    CatalogMaintenanceDialog._on_filter_resize(cast(Any, dialog), object())

    assert calls == [(True, results), (False, results)]
    assert dialog._compact_filter_layout is False


def test_filter_resize_ignores_unchanged_client_size_but_tracks_real_resize() -> None:
    requests: list[None] = []

    class Panel:
        def set_compact_layout(self, _compact: bool, _results_panel: object) -> None:
            pass

    class Dialog:
        _filter_analysis_panel = Panel()
        _results_panel = object()
        _compact_filter_layout = False
        _filter_layout_size: tuple[int, int] | None = None
        width = 1000
        height = 760

        def winfo_width(self) -> int:
            return self.width

        def winfo_height(self) -> int:
            return self.height

        def _request_height_layout(self) -> None:
            requests.append(None)

    dialog = Dialog()
    CatalogMaintenanceDialog._on_filter_resize(cast(Any, dialog), object())
    CatalogMaintenanceDialog._on_filter_resize(cast(Any, dialog), object())
    dialog.height = 761
    CatalogMaintenanceDialog._on_filter_resize(cast(Any, dialog), object())

    assert len(requests) == 2


@pytest.mark.parametrize("pending", (False, True))
def test_filter_toggle_remains_explicit_while_layout_is_running_or_pending(
    pending: bool,
) -> None:
    callbacks: list[object] = []
    panel = cast(Any, object.__new__(CatalogMaintenanceFilterAnalysisPanel))
    panel._filter_details_visible = False
    panel._filter_wrapper = SimpleNamespace(grid=lambda: None, grid_remove=lambda: None)
    panel._filter_toggle = SimpleNamespace(configure=lambda **_options: None)
    panel.refresh_layout = lambda _results: None

    class Dialog:
        _closed = False
        _layout_running = not pending
        _layout_requested = False
        _layout_after_id = "pending" if pending else None
        _results_panel = object()
        _filter_analysis_panel = panel
        _request_height_layout = CatalogMaintenanceDialog._request_height_layout

        def after_idle(self, callback: object) -> str:
            callbacks.append(callback)
            return "layout"

    dialog = Dialog()
    panel._layout_changed = dialog._request_height_layout

    CatalogMaintenanceDialog._toggle_filter_details(cast(Any, dialog))
    CatalogMaintenanceDialog._toggle_filter_details(cast(Any, dialog))

    assert panel._filter_details_visible is False
    assert dialog._layout_requested is (not pending)
    assert callbacks == []


def test_height_layout_waits_until_results_body_is_initialized() -> None:
    scheduled: list[object] = []

    class Dialog:
        _closed = False
        _layout_running = False
        _layout_after_id = "initial-layout"
        _request_height_layout = CatalogMaintenanceDialog._request_height_layout
        _apply_height_layout = CatalogMaintenanceDialog._apply_height_layout

        def _active(self) -> bool:
            return True

        def after_idle(self, callback: object) -> str:
            scheduled.append(callback)
            return "completed-layout"

        def update_idletasks(self) -> None:
            raise AssertionError("layout callbacks must not process nested Tk idle work")

    dialog = Dialog()

    CatalogMaintenanceDialog._apply_height_layout(cast(Any, dialog))

    assert dialog._layout_after_id is None
    assert dialog._layout_running is False
    dialog._results_body = object()
    dialog._request_height_layout()
    assert scheduled == [dialog._apply_height_layout]


def test_height_layout_coalesces_request_without_recursive_idle_processing() -> None:
    callbacks: list[object] = []
    applied_heights: list[int] = []

    class ResultsBody:
        def winfo_height(self) -> int:
            return 190

    class ResultsPanel:
        def grid_bbox(self, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, 190)

        def winfo_height(self) -> int:
            return 230

    class FilterPanel:
        requested_during_layout = False
        dialog: Any = None

        def height_request(self) -> tuple[int, int]:
            if not self.requested_during_layout:
                self.requested_during_layout = True
                self.dialog._request_height_layout()
            return (120, 180)

        def apply_allocated_height(self, height: int) -> None:
            applied_heights.append(height)

        def finish_layout_update(self) -> None:
            pass

    panel = FilterPanel()

    class Dialog:
        _closed = False
        _layout_running = False
        _layout_after_id: str | None = None
        _results_body = ResultsBody()
        _results_panel = ResultsPanel()
        _filter_analysis_panel = panel
        _request_height_layout = CatalogMaintenanceDialog._request_height_layout
        _apply_height_layout = CatalogMaintenanceDialog._apply_height_layout
        callback_depth = 0
        maximum_callback_depth = 0

        def _active(self) -> bool:
            return True

        def after_idle(self, callback: object) -> str:
            callbacks.append(callback)
            return f"layout-{len(callbacks)}"

        def update_idletasks(self) -> None:
            raise AssertionError("layout callbacks must not process nested Tk idle work")

        def winfo_height(self) -> int:
            return 620

        def grid_bbox(self, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, 60)

        def grid_rowconfigure(self, *_args: int, **_kwargs: int) -> None:
            pass

    dialog = Dialog()
    panel.dialog = dialog
    dialog._request_height_layout()

    while callbacks:
        callback = callbacks.pop(0)
        dialog.callback_depth += 1
        dialog.maximum_callback_depth = max(dialog.maximum_callback_depth, dialog.callback_depth)
        callback()
        dialog.callback_depth -= 1

    assert len(applied_heights) == 2
    assert dialog.maximum_callback_depth == 1
    assert dialog._layout_after_id is None


def _realized_layout_dialog(width: int, height: int) -> tuple[ctk.CTk, CatalogMaintenanceDialog]:
    try:
        root = ctk.CTk()
    except TclError as error:
        message = str(error)
        missing_tcl_runtime = (
            "Can't find a usable" in message and ".tcl" in message
        ) or 'invalid command name "tcl_findLibrary"' in message
        if not missing_tcl_runtime:
            raise
        pytest.skip(
            "Windows-CI-Python enthaelt kein nutzbares Tcl/Tk (tk.tcl/tcl_findLibrary); "
            "reale Tk-Geometrie erfordert eine separate manuelle Windows-Abnahme"
        )
    root.geometry("1200x900+0+0")
    root.update()
    dialog = CatalogMaintenanceDialog(
        root,
        cast(Any, SimpleNamespace()),
        lambda _work, _done, _failed: False,
        lambda _track_id: None,
    )
    dialog.geometry(f"{width}x{height}+0+0")
    dialog.update()
    return root, dialog


@pytest.mark.parametrize(
    ("panel_request", "expected_panel", "expected_results_minimum"),
    [
        ((120, 180), 180, 230),
        ((300, 500), 316, 230),
    ],
)
def test_first_filter_toggle_reaches_stable_layout(
    panel_request: tuple[int, int],
    expected_panel: int,
    expected_results_minimum: int,
) -> None:
    """Exercise dialog-owned height distribution without realizing Tk widgets."""
    callbacks: list[object] = []
    row_configuration: dict[int, dict[str, int]] = {}
    applied_heights: list[int] = []

    class Geometry:
        def __init__(self, height: int) -> None:
            self.height = height

        def winfo_height(self) -> int:
            return self.height

        def grid_bbox(self, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, self.height - 40)

    class Panel:
        def height_request(self) -> tuple[int, int]:
            return panel_request

        def apply_allocated_height(self, height: int) -> None:
            applied_heights.append(height)

        def finish_layout_update(self) -> None:
            pass

    class Dialog:
        _closed = False
        _layout_running = False
        _layout_requested = False
        _layout_after_id: str | None = None
        _results_body = Geometry(190)
        _results_panel = Geometry(230)
        _filter_analysis_panel = Panel()
        _request_height_layout = CatalogMaintenanceDialog._request_height_layout
        _apply_height_layout = CatalogMaintenanceDialog._apply_height_layout

        def _active(self) -> bool:
            return True

        def after_idle(self, callback: object) -> str:
            callbacks.append(callback)
            return "height-layout"

        def winfo_height(self) -> int:
            return 620

        def grid_bbox(self, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, 20)

        def grid_rowconfigure(self, row: int, **options: int) -> None:
            row_configuration[row] = options

    dialog = Dialog()
    dialog._request_height_layout()
    assert len(callbacks) == 1

    cast(Any, callbacks.pop())()

    assert applied_heights == [expected_panel]
    assert row_configuration == {
        0: {"weight": 0, "minsize": expected_panel},
        1: {"weight": 1, "minsize": expected_results_minimum},
    }
    assert expected_results_minimum - 40 >= 190
    assert dialog._layout_after_id is None
    assert dialog._layout_running is False
    assert dialog._layout_requested is False


@pytest.mark.parametrize("panel_request", ((208, 520), (320, 520)))
def test_small_height_reserves_footer_actions_and_results_minimum(
    panel_request: tuple[int, int],
) -> None:
    row_configuration: dict[int, dict[str, int]] = {}
    applied_heights: list[int] = []

    class ResultsBody:
        def winfo_height(self) -> int:
            return 80

    class ResultsPanel:
        def grid_bbox(self, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, 80)

        def winfo_height(self) -> int:
            return 120

    class Panel:
        def height_request(self) -> tuple[int, int]:
            return panel_request

        def height_floor(self) -> int:
            return 121

        def apply_allocated_height(self, height: int) -> None:
            applied_heights.append(height)

        def finish_layout_update(self) -> None:
            pass

    class Dialog:
        _closed = False
        _layout_running = False
        _layout_requested = False
        _layout_after_id = "layout"
        _results_body = ResultsBody()
        _results_panel = ResultsPanel()
        _filter_analysis_panel = Panel()

        def _active(self) -> bool:
            return True

        def winfo_height(self) -> int:
            return 551

        def grid_bbox(self, _first_column: int, row: int, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, {2: 100, 3: 100, 4: 50}[row])

        def grid_rowconfigure(self, row: int, **options: int) -> None:
            row_configuration[row] = options

        def _request_height_layout(self) -> None:
            raise AssertionError("stable height allocation must not schedule another layout")

    CatalogMaintenanceDialog._apply_height_layout(cast(Any, Dialog()))

    assert applied_heights == [167]
    assert row_configuration == {
        0: {"weight": 0, "minsize": 167},
        1: {"weight": 1, "minsize": 120},
    }


def test_height_budget_reserves_filter_panel_outer_padding_once() -> None:
    row_configuration: dict[int, dict[str, int]] = {}
    applied_heights: list[int] = []

    class ResultsBody:
        def winfo_height(self) -> int:
            return 80

    class ResultsPanel:
        def grid_bbox(self, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, 80)

        def winfo_height(self) -> int:
            return 120

    class Panel:
        def height_request(self) -> tuple[int, int]:
            return (208, 520)

        def height_floor(self) -> int:
            return 121

        def apply_allocated_height(self, height: int) -> None:
            applied_heights.append(height)

        def finish_layout_update(self) -> None:
            pass

    class Dialog:
        _closed = False
        _layout_running = False
        _layout_requested = False
        _layout_after_id = "layout"
        _results_body = ResultsBody()
        _results_panel = ResultsPanel()
        _filter_analysis_panel = Panel()

        def _active(self) -> bool:
            return True

        def winfo_height(self) -> int:
            return 551

        def grid_bbox(self, _first_column: int, row: int, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, {2: 100, 3: 100, 4: 50}[row])

        def grid_rowconfigure(self, row: int, **options: int) -> None:
            row_configuration[row] = options

        def _request_height_layout(self) -> None:
            raise AssertionError("stable height allocation must not schedule another layout")

    CatalogMaintenanceDialog._apply_height_layout(cast(Any, Dialog()))

    assert applied_heights == [167]
    assert row_configuration[1]["minsize"] == 120
    assert 167 + 14 + 120 + 100 + 100 + 50 == 551


@pytest.mark.parametrize("width", (760, 979, 980))
@pytest.mark.parametrize("height", (620, 820))
def test_realized_dialog_distributes_height_for_layout_states(width: int, height: int) -> None:
    root, dialog = _realized_layout_dialog(width, height)
    try:
        state_a = (
            dialog._filter_analysis_panel.winfo_height(),
            dialog._results_body.winfo_height(),
        )
        dialog._filter_toggle.invoke()
        dialog.update()
        state_b = (
            dialog._filter_analysis_panel.winfo_height(),
            dialog._results_body.winfo_height(),
        )
        dialog._analysis_toggle.invoke()
        dialog.update()
        state_c = (
            dialog._filter_analysis_panel.winfo_height(),
            dialog._results_body.winfo_height(),
        )

        assert state_a[0] < state_b[0]
        assert state_b[1] <= state_a[1]
        assert abs(state_c[0] - state_b[0]) <= 1
        assert abs(state_c[1] - state_b[1]) <= 1
        assert state_c[1] >= 80
        if height >= 820:
            assert state_c[1] >= 190
        assert dialog._filter_analysis_panel.winfo_width() <= dialog.winfo_width()
        assert dialog._results_panel.winfo_width() <= dialog.winfo_width()
        assert dialog._footer.winfo_rooty() + dialog._footer.winfo_height() <= (
            dialog.winfo_rooty() + dialog.winfo_height()
        )
    finally:
        dialog._close()
        root.destroy()


def test_layout_state_cycles_restore_list_height_selection_and_scroll() -> None:
    """Prove state restoration; realized pixel geometry remains GUI acceptance."""
    callbacks: list[object] = []
    result_heights: list[int] = []
    panel_heights: list[int] = []

    class ResultsBody:
        def winfo_height(self) -> int:
            return 190

    class ResultsPanel:
        def winfo_height(self) -> int:
            return 230

        def grid_bbox(self, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, 190)

    class Panel:
        filter_visible = False
        analysis_visible = False

        def height_request(self) -> tuple[int, int]:
            if self.analysis_visible:
                return (260, 260)
            if self.filter_visible:
                return (180, 180)
            return (120, 120)

        def apply_allocated_height(self, height: int) -> None:
            panel_heights.append(height)

        def finish_layout_update(self) -> None:
            pass

    panel = Panel()

    class Dialog:
        _closed = False
        _layout_running = False
        _layout_requested = False
        _layout_after_id: str | None = None
        _results_body = ResultsBody()
        _results_panel = ResultsPanel()
        _filter_analysis_panel = panel
        _request_height_layout = CatalogMaintenanceDialog._request_height_layout
        _apply_height_layout = CatalogMaintenanceDialog._apply_height_layout
        _selection = SelectionDescription.for_filter(MaintenanceFilter()).select(17)
        scroll_position = (0.5, 0.75)

        def _active(self) -> bool:
            return True

        def after_idle(self, callback: object) -> str:
            callbacks.append(callback)
            return f"height-layout-{len(callbacks)}"

        def winfo_height(self) -> int:
            return 620

        def grid_bbox(self, *_args: int) -> tuple[int, int, int, int]:
            return (0, 0, 100, 20)

        def grid_rowconfigure(self, row: int, **options: int) -> None:
            if row == 1:
                result_heights.append(options["minsize"])

    dialog = Dialog()

    def apply_state(*, filter_visible: bool, analysis_visible: bool) -> None:
        panel.filter_visible = filter_visible
        panel.analysis_visible = analysis_visible
        dialog._request_height_layout()
        assert len(callbacks) == 1
        cast(Any, callbacks.pop())()

    for _cycle in range(2):
        apply_state(filter_visible=False, analysis_visible=False)
        apply_state(filter_visible=True, analysis_visible=False)
        apply_state(filter_visible=True, analysis_visible=True)
        apply_state(filter_visible=True, analysis_visible=False)
        apply_state(filter_visible=False, analysis_visible=False)

    assert result_heights == [230, 230, 230, 230, 230] * 2
    assert panel_heights == [120, 180, 260, 180, 120] * 2
    assert dialog._selection.included_ids == frozenset({17})
    assert dialog.scroll_position == (0.5, 0.75)
    assert dialog._layout_after_id is None


def test_tempo_prepare_and_empty_selection_render_through_panel() -> None:
    panel = _TempoPanel()

    class Repository:
        def resolve_selection(self, _selection: object) -> tuple[tuple[int, int], ...]:
            return ()

    class Dialog:
        _metadata_analysis = object()
        _filter_analysis_panel = panel
        _service = SimpleNamespace(repository=Repository())
        _selection = object()

        def _active(self) -> bool:
            return True

        def _task(self, work: Any, done: Any) -> None:
            done(work())

        def _tempo_selection_resolved(self, value: object) -> None:
            CatalogMaintenanceDialog._tempo_selection_resolved(cast(Any, self), value)

    CatalogMaintenanceDialog._prepare_tempo_batch(cast(Any, Dialog()))

    assert panel.states == [
        (None, {"start": "disabled"}),
        ("Auswahl und Analysestand werden geprüft …", {}),
        (None, {"start": "normal"}),
        ("Bitte mindestens einen Titel auswählen.", {}),
    ]


def test_tempo_started_paused_resumed_and_aborted_render_through_panel() -> None:
    calls: list[str] = []
    panel = _TempoPanel()

    class Analysis:
        def pause(self) -> None:
            calls.append("pause")

        def resume(self) -> None:
            calls.append("resume")

        def cancel_all(self) -> None:
            calls.append("abort")

    class Dialog:
        _metadata_analysis = Analysis()
        _filter_analysis_panel = panel

        def _active(self) -> bool:
            return True

        def _schedule_tempo_progress(self) -> None:
            calls.append("schedule")

    dialog = Dialog()
    CatalogMaintenanceDialog._tempo_batch_started(
        cast(Any, dialog), (SimpleNamespace(run_id=17), SimpleNamespace(run_id=23))
    )
    CatalogMaintenanceDialog._pause_tempo_batch(cast(Any, dialog))
    CatalogMaintenanceDialog._resume_tempo_batch(cast(Any, dialog))
    CatalogMaintenanceDialog._abort_tempo_batch(cast(Any, dialog))

    assert dialog._tempo_run_ids == (17, 23)
    assert calls == ["schedule", "pause", "resume", "schedule", "abort", "schedule"]
    assert panel.states == [
        (None, {"pause": "normal", "abort": "normal"}),
        ("Serielle Tempoanalyse wurde gestartet …", {}),
        (None, {"pause": "disabled", "resume": "normal"}),
        (
            "PAUSIERT – der laufende Titel darf noch enden.",
            {},
        ),
        (None, {"pause": "normal", "resume": "disabled"}),
        (None, {"abort": "disabled"}),
        ("Tempoanalyse wurde vollständig abgebrochen.", {}),
    ]


def test_tempo_started_renders_through_dialogs_real_analysis_panel() -> None:
    panel = cast(Any, object.__new__(CatalogMaintenanceFilterAnalysisPanel))
    panel._tempo_progress = _Widget()
    panel._tempo_start = _Widget()
    panel._tempo_pause = _Widget()
    panel._tempo_resume = _Widget()
    panel._tempo_abort = _Widget()
    dialog = cast(Any, object.__new__(CatalogMaintenanceDialog))
    dialog._filter_analysis_panel = panel
    dialog._active = lambda: True
    dialog._schedule_tempo_progress = lambda: None

    CatalogMaintenanceDialog._tempo_batch_started(dialog, (SimpleNamespace(run_id=17),))

    assert isinstance(panel, CatalogMaintenanceFilterAnalysisPanel)
    assert panel._tempo_progress.options["text"] == "Serielle Tempoanalyse wurde gestartet …"
    assert panel._tempo_pause.options["state"] == "normal"


def test_tempo_batch_progress_renders_aggregates_without_rebuilding_rows() -> None:
    class Dialog:
        _tempo_skipped = 4
        _filter_analysis_panel = _TempoPanel()
        loaded = 0

        def _active(self) -> bool:
            return True

        def _load_counts_and_page(self) -> None:
            self.loaded += 1

    dialog = Dialog()
    progress = TempoBatchProgress(
        12,
        12,
        8,
        1,
        3,
        2,
        0,
        1,
        None,
        "",
        "IDLE",
        "",
        None,
    )

    CatalogMaintenanceDialog._tempo_progressed(cast(Any, dialog), progress)

    text, states = dialog._filter_analysis_panel.states[-1]
    assert "12 / 12 abgeschlossen" in text
    assert "Prüfung erforderlich 3" in text
    assert "übersprungen 4" in text
    assert states == {
        "start": "normal",
        "pause": "disabled",
        "resume": "disabled",
        "abort": "disabled",
    }
    assert dialog.loaded == 1
