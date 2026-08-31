"""Display-independent lifecycle tests for the catalog-maintenance dialog."""

from typing import Any, cast

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
from party_player.metadata_rules import MetadataFieldKey
from party_player.metadata_analysis_service import TempoBatchProgress
from party_player.ui.catalog_maintenance_dialog import (
    CatalogAnalysisActions,
    CatalogMaintenanceDialog,
    ask_filter_selection_strategy,
    high_risk_confirmation_text,
    parse_bpm_filter,
    parse_batch_input,
)


def test_late_page_result_does_not_touch_destroyed_dialog() -> None:
    class Dialog:
        def _active(self) -> bool:
            return False

        def _show_page(self, _page: object) -> None:
            raise AssertionError("destroyed widgets must not be accessed")

    CatalogMaintenanceDialog._loaded(cast(Any, Dialog()), object())


def test_close_is_idempotent_and_releases_dialog(monkeypatch: Any) -> None:
    from threading import Event

    released: list[object] = []
    monkeypatch.setattr(
        "party_player.ui.catalog_maintenance_dialog.release_dialog", released.append
    )

    class Dialog:
        _closed = False
        _cancel_event = Event()
        destroyed = 0

        def destroy(self) -> None:
            self.destroyed += 1

    dialog = Dialog()
    CatalogMaintenanceDialog._close(cast(Any, dialog))
    CatalogMaintenanceDialog._close(cast(Any, dialog))

    assert released == [dialog]
    assert dialog.destroyed == 1
    assert dialog._cancel_event.is_set()


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

    CatalogMaintenanceDialog._loaded(cast(Any, Dialog()), object(), 3)


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
    CatalogMaintenanceDialog._reset_filters(cast(Any, dialog))

    assert dialog._filter == MaintenanceFilter()
    assert not dialog._selection.all_matches
    assert not dialog._selection.included_ids
    assert dialog._preview is None
    assert dialog._page == 1
    assert dialog._filter_bpm_from.value == dialog._filter_bpm_to.value == ""
    assert dialog.reloads == 1


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


def test_tempo_batch_progress_renders_aggregates_without_rebuilding_rows() -> None:
    class Dialog:
        _tempo_skipped = 4
        _tempo_progress = _Widget()
        _tempo_start = _Widget()
        _tempo_pause = _Widget()
        _tempo_resume = _Widget()
        _tempo_abort = _Widget()
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

    text = str(dialog._tempo_progress.options["text"])
    assert "12 / 12 abgeschlossen" in text
    assert "Prüfung erforderlich 3" in text
    assert "übersprungen 4" in text
    assert dialog.loaded == 1
