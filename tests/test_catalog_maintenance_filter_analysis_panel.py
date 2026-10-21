"""Display-independent contracts for the catalog filter/analysis panel."""

from typing import Any, cast

from party_player.ui.catalog_maintenance_filter_analysis_panel import (
    CatalogMaintenanceFilterAnalysisPanel,
)


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
    panel._filter_wrapper = _Widget()
    panel._filter_details = _Widget()
    panel._filter_actions = _Widget()
    panel._counts = _Widget()
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
    assert panel.search.get() == panel.filter_bpm_from.get() == panel.filter_bpm_to.get() == ""


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


def test_analysis_disclosure_preserves_filter_values() -> None:
    panel = _panel()
    panel._analysis_panel = _Widget()
    panel._analysis_toggle = _Widget()

    panel.toggle_analysis()
    panel.toggle_analysis()

    assert panel.search.get() == "Dance"
    assert not panel._analysis_panel.visible
    assert panel._analysis_toggle.options["text"] == "Audioanalyse anzeigen ▾"


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
