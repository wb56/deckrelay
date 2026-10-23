"""Contracts and realized geometry for the catalog filter/analysis panel."""

from tkinter import TclError
from typing import Any, cast

import customtkinter as ctk  # type: ignore[import-untyped]
import pytest

from party_player.ui.catalog_maintenance_filter_analysis_panel import (
    CatalogMaintenanceFilterAnalysisPanel,
)


EXPECTED_ANALYSIS_BUTTON_LABELS = {
    "Neue/veraltete Cues",
    "Alle Cues neu",
    "Cue-Analyse abbrechen",
    "Neue/veraltete Lautheit",
    "Alle Lautheiten neu",
    "Lautheit abbrechen",
}


class _Widget:
    def __init__(self, value: str = "") -> None:
        self.value = value
        self.visible = False
        self.options: dict[str, object] = {}

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value

    def delete(self, _first: object, _last: object) -> None:
        self.value = ""

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def grid_configure(self, **options: object) -> None:
        self.options.update(options)

    def grid_propagate(self, value: bool) -> None:
        self.options["grid_propagate"] = value

    def grid(self) -> None:
        self.visible = True

    def grid_remove(self) -> None:
        self.visible = False

    def winfo_ismapped(self) -> bool:
        return self.visible

    def update_idletasks(self) -> None:
        pass

    def winfo_height(self) -> int:
        return 100

    def winfo_reqheight(self) -> int:
        return 100


def _realized_panel(
    width: int = 1664, *, callbacks: dict[str, Any] | None = None
) -> tuple[ctk.CTk, CatalogMaintenanceFilterAnalysisPanel]:
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
    root.geometry(f"{width}x900+0+0")
    root.grid_columnconfigure(0, weight=1)
    root.update()
    supplied = callbacks or {}
    panel = CatalogMaintenanceFilterAnalysisPanel(
        root,
        title="Katalogpflege",
        work_queue_labels={},
        field_labels={},
        source_labels={},
        status_labels={},
        suggestion_labels={},
        apply_filter=lambda: None,
        reset_filters=lambda: None,
        analysis_actions=None,
        metadata_analysis_available=True,
        analysis_error=lambda _error: None,
        prepare_tempo=supplied.get("start", lambda: None),
        pause_tempo=supplied.get("pause", lambda: None),
        resume_tempo=supplied.get("resume", lambda: None),
        abort_tempo=supplied.get("abort", lambda: None),
        resume_pending_tempo=supplied.get("pending_resume", lambda: None),
        discard_pending_tempo=supplied.get("pending_discard", lambda: None),
    )
    panel.toggle_filter_details(None)
    panel.toggle_analysis()
    root.update()
    return root, panel


def _buttons_below(widget: Any) -> list[ctk.CTkButton]:
    descendants: list[ctk.CTkButton] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton):
            descendants.append(child)
        descendants.extend(_buttons_below(child))
    return descendants


def _panel() -> CatalogMaintenanceFilterAnalysisPanel:
    panel = cast(Any, object.__new__(CatalogMaintenanceFilterAnalysisPanel))
    panel._queue = _Widget("Fehlende BPM-Werte")
    panel._search = _Widget("Dance")
    panel._filter_field = _Widget("BPM")
    panel._filter_source = _Widget("Analyse")
    panel._filter_status = _Widget("Ungeprüft")
    panel._filter_value = _Widget("Mit Wert")
    panel._filter_confirmed = _Widget("Bestätigt")
    panel._filter_conflict = _Widget("Ohne Konflikt")
    panel._filter_suggestion = _Widget("Offen")
    panel._filter_confidence = _Widget("0,8")
    panel._filter_changed_from = _Widget("2026-01-01")
    panel._filter_changed_to = _Widget("2026-12-31")
    panel._filter_bpm_from = _Widget("100")
    panel._filter_bpm_to = _Widget("130")
    panel._filter_toggle = _Widget()
    panel._filter_toggle.options.update(row=3, column=0, columnspan=4)
    panel._filter_wrapper = _Widget()
    panel._filter_details = _Widget()
    panel._filter_actions = _Widget()
    panel._counts = _Widget()
    panel._counts.options.update(row=2, column=0, columnspan=4)
    panel._filter_details_visible = False
    panel._compact = False
    panel.update_idletasks = lambda: None
    panel.configure = lambda **_options: None
    panel.grid_propagate = lambda _value: None
    return panel


def test_filter_panel_reads_every_input_without_normalizing_values() -> None:
    values = _panel().read_filter_values()

    assert values.queue == "Fehlende BPM-Werte"
    assert values.search == "Dance"
    assert values.field == "BPM"
    assert values.source == "Analyse"
    assert values.status == "Ungeprüft"
    assert values.value == "Mit Wert"
    assert values.confirmed == "Bestätigt"
    assert values.conflict == "Ohne Konflikt"
    assert values.suggestion == "Offen"
    assert values.minimum_confidence == "0,8"
    assert values.minimum_bpm == "100"
    assert values.maximum_bpm == "130"
    assert values.changed_from == "2026-01-01"
    assert values.changed_to == "2026-12-31"


def test_filter_panel_reset_restores_all_defaults() -> None:
    panel = _panel()

    panel.reset_filter_inputs()

    assert panel.queue.get() == "Alle Arbeitsvorräte"
    assert panel.filter_field.get() == "Alle Felder"
    assert panel.filter_source.get() == "Alle Quellen"
    assert panel.filter_status.get() == "Alle Prüfstatus"
    assert panel.filter_value.get() == "Wert egal"
    assert panel.filter_confirmed.get() == "Bestätigung egal"
    assert panel.filter_conflict.get() == "Konflikt egal"
    assert panel.filter_suggestion.get() == "Vorschlag egal"
    assert panel.search.get() == ""
    assert panel.filter_confidence.get() == ""
    assert panel.filter_changed_from.get() == ""
    assert panel.filter_changed_to.get() == ""
    assert panel.filter_bpm_from.get() == ""
    assert panel.filter_bpm_to.get() == ""


def test_filter_panel_disclosure_preserves_values_and_compact_layout() -> None:
    panel = _panel()
    results = _Widget()

    panel.toggle_filter_details(results)
    panel.set_compact_layout(True, results)
    panel.toggle_filter_details(results)

    assert panel.search.get() == "Dance"
    assert panel.filter_confidence.get() == "0,8"
    assert panel.filter_actions.options["row"] == 2
    assert panel.counts.options["row"] == 3
    assert panel.filter_toggle.options["text"] == "Weitere Filter anzeigen ▾"


def test_filter_toggle_and_counts_use_distinct_grid_rows_at_979_and_980_pixels() -> None:
    panel = _panel()
    results = _Widget()

    panel.set_compact_layout(True, results)
    compact_rows = (panel.filter_toggle.options["row"], panel.counts.options["row"])
    panel.set_compact_layout(False, results)
    wide_rows = (panel.filter_toggle.options["row"], panel.counts.options["row"])

    assert compact_rows == (4, 3)
    assert wide_rows == (3, 2)


def test_realized_filter_toggle_and_counts_do_not_share_grid_area() -> None:
    root, panel = _realized_panel()
    results = _Widget()
    try:
        panel.set_compact_layout(True, results)
        root.update()
        compact_rows = (panel.filter_toggle.grid_info()["row"], panel.counts.grid_info()["row"])
        panel.set_compact_layout(False, results)
        root.update()
        wide_rows = (panel.filter_toggle.grid_info()["row"], panel.counts.grid_info()["row"])

        assert compact_rows == (4, 3)
        assert wide_rows == (3, 2)
    finally:
        root.destroy()


def test_analysis_disclosure_preserves_filter_values() -> None:
    panel = _panel()
    panel._analysis_panel = _Widget()
    panel._analysis_toggle = _Widget()

    panel.toggle_analysis()
    panel.toggle_analysis()

    assert panel.search.get() == "Dance"
    assert not panel._analysis_panel.visible
    assert panel._analysis_toggle.options["text"] == "Audioanalyse anzeigen ▾"


def test_nested_filter_and_analysis_columns_expand() -> None:
    root, panel = _realized_panel()
    try:
        assert panel._filter_details.grid_columnconfigure(0)["weight"] == 1
        assert panel._analysis_host.grid_columnconfigure(0)["weight"] == 1
    finally:
        root.destroy()


def test_realized_wide_panel_propagates_width_without_tempo_clipping() -> None:
    root, panel = _realized_panel()
    try:
        root_width = root.winfo_width()
        panel_width = panel.winfo_width()
        filter_wrapper_width = panel._filter_wrapper.winfo_width()
        filter_details_width = panel._filter_details.winfo_width()
        analysis_host_width = panel._analysis_host.winfo_width()

        assert panel_width == root_width - 24
        assert filter_wrapper_width == panel_width
        if root_width >= 1624:
            assert panel_width >= 1600
        assert filter_details_width >= panel_width - 30
        assert analysis_host_width >= filter_details_width - 30
        tempo_right = panel._tempo_start.winfo_rootx() + panel._tempo_start.winfo_width()
        details_right = panel._filter_details.winfo_rootx() + filter_details_width
        assert tempo_right <= details_right
    finally:
        root.destroy()


@pytest.mark.parametrize("dialog_width", (979, 980, 760))
def test_realized_tempo_controls_fit_actual_canvas_viewport(dialog_width: int) -> None:
    root, panel = _realized_panel(dialog_width)
    try:
        canvas = panel._filter_details._parent_canvas
        viewport_left = canvas.winfo_rootx()
        viewport_right = viewport_left + canvas.winfo_width()
        width_chain = (
            root.winfo_width(),
            panel.winfo_width(),
            panel._filter_wrapper.winfo_width(),
            canvas.winfo_width(),
            panel._analysis_host.winfo_width(),
            panel._analysis_panel.winfo_width(),
        )
        controls = {
            "profile": panel._tempo_profile,
            "scope": panel._tempo_scope,
            "skip-current switch": panel._tempo_skip_current,
            "start": panel._tempo_start,
            "pause": panel._tempo_pause,
            "resume": panel._tempo_resume,
            "abort": panel._tempo_abort,
            "resume pending": panel._tempo_pending_resume,
            "discard pending": panel._tempo_pending_discard,
        }

        assert root.winfo_width() == dialog_width
        assert all(width > 0 for width in width_chain)
        assert all(outer >= inner for outer, inner in zip(width_chain, width_chain[1:]))
        assert panel._filter_details.winfo_reqwidth() > 0
        assert viewport_right - viewport_left == canvas.winfo_width()
        for name, widget in controls.items():
            left = widget.winfo_rootx()
            right = left + widget.winfo_width()
            assert left >= viewport_left, name
            assert right <= viewport_right, name

        switch_label = panel._tempo_skip_current._text_label
        assert switch_label.winfo_width() >= switch_label.winfo_reqwidth()
        for button in _buttons_below(panel.analysis_panel):
            text_label = button._text_label
            assert text_label.winfo_width() >= text_label.winfo_reqwidth(), button.cget("text")
    finally:
        root.destroy()


def test_realized_analysis_action_groups_use_uniform_button_grid() -> None:
    root, panel = _realized_panel(980)
    try:
        buttons = {button.cget("text"): button for button in _buttons_below(panel.analysis_panel)}
        assert EXPECTED_ANALYSIS_BUTTON_LABELS <= buttons.keys()

        for labels in (
            ("Neue/veraltete Cues", "Alle Cues neu", "Cue-Analyse abbrechen"),
            (
                "Neue/veraltete Lautheit",
                "Alle Lautheiten neu",
                "Lautheit abbrechen",
            ),
            ("Pause", "Fortsetzen", "Gesamten Auftrag abbrechen"),
            ("Wartende Runs fortsetzen", "Wartende Runs verwerfen"),
        ):
            group = [buttons[label] for label in labels]
            assert len({button.winfo_width() for button in group}) == 1
            assert len({button.winfo_height() for button in group}) == 1
            left_edges = [button.winfo_rootx() for button in group]
            gaps = [
                right - (left + button.winfo_width())
                for left, button, right in zip(left_edges, group, left_edges[1:])
            ]
            assert len(set(gaps)) <= 1
    finally:
        root.destroy()


def test_tempo_button_callbacks_labels_and_initial_states_are_preserved() -> None:
    calls: list[str] = []
    callbacks = {
        name: (lambda current=name: calls.append(current))
        for name in ("start", "pause", "resume", "abort", "pending_resume", "pending_discard")
    }
    root, panel = _realized_panel(callbacks=callbacks)
    try:
        expected = {
            panel.tempo_start: ("Auswahl analysieren …", "normal", "start"),
            panel.tempo_pause: ("Pause", "disabled", "pause"),
            panel.tempo_resume: ("Fortsetzen", "disabled", "resume"),
            panel.tempo_abort: ("Gesamten Auftrag abbrechen", "disabled", "abort"),
            panel.tempo_pending_resume: ("Wartende Runs fortsetzen", "normal", "pending_resume"),
            panel.tempo_pending_discard: ("Wartende Runs verwerfen", "normal", "pending_discard"),
        }
        for button, (label, state, callback_name) in expected.items():
            assert button.cget("text") == label
            assert button.cget("state") == state
            button.cget("command")()
            assert calls[-1] == callback_name
    finally:
        root.destroy()


def test_analysis_callback_is_forwarded_and_errors_use_supplied_handler() -> None:
    calls: list[str] = []
    errors: list[Exception] = []
    failure = RuntimeError("Analyse konnte nicht gestartet werden")

    CatalogMaintenanceFilterAnalysisPanel.run_analysis_action(
        lambda: calls.append("ok"), errors.append
    )
    CatalogMaintenanceFilterAnalysisPanel.run_analysis_action(
        lambda: (_ for _ in ()).throw(failure), errors.append
    )

    assert calls == ["ok"]
    assert errors == [failure]


def test_tempo_presentation_updates_status_and_button_states() -> None:
    panel = _panel()
    panel._tempo_progress = _Widget()
    panel._tempo_start = _Widget()
    panel._tempo_pause = _Widget()
    panel._tempo_resume = _Widget()
    panel._tempo_abort = _Widget()

    panel.render_tempo_state(
        "Tempoanalyse läuft …",
        start="disabled",
        pause="normal",
        resume="disabled",
        abort="normal",
    )

    assert panel._tempo_progress.options["text"] == "Tempoanalyse läuft …"
    assert panel._tempo_start.options["state"] == "disabled"
    assert panel._tempo_pause.options["state"] == "normal"
    assert panel._tempo_resume.options["state"] == "disabled"
    assert panel._tempo_abort.options["state"] == "normal"
