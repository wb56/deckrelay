"""Performance-diagnostics tests for the main controller."""

from pathlib import Path

import pytest

from party_player.performance_monitor import PerformanceSettings
from tests.main_controller_test_support import build_controller


def test_diagnostic_report_is_saved_with_context_and_without_file_paths(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    controller.bind_database_diagnostic_status(
        lambda: (
            "last_manual_backup_at: 2026-08-10T12:00:00+00:00",
            "last_data_operation: MAINTENANCE",
            "last_data_operation_error: none",
        )
    )
    controller.initialize()
    controller.add_catalog_track_to_queue(1)
    controller._status_tick()

    report_path = controller.save_diagnostic_report("normal_playback", tmp_path / "diagnostics")
    report = report_path.read_text(encoding="utf-8")

    assert report_path.name.startswith("deckrelay-diagnostic-")
    assert report.startswith("DeckRelay diagnostic report")
    assert "Version: 2.0.0-beta.1" in report
    assert "Test context: normal_playback" in report
    assert "Operating mode:" in report
    assert "status_tick.total:" in report
    assert "GUI event dispatcher:" in report
    assert "published_count:" in report
    assert "Registered workers:" in report
    assert "Queue instrumentation:" in report
    assert "  complete: false" in report
    assert "gui.queue_render.total" in report
    assert "  counters_plausible: true" in report
    assert "Timings:" in report
    assert "Counters:" in report
    assert "Gauges:" in report
    assert "tracemalloc_enabled:" in report
    assert "process_rss_status:" in report
    assert "catalog_row_views: 0" in report
    assert "Backup/restore/maintenance:" in report
    assert "last_data_operation: MAINTENANCE" in report
    assert "song.mp3" not in report


def test_diagnostic_report_confirms_complete_queue_instrumentation(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    for operation in controller.QUEUE_INSTRUMENTATION_OPERATIONS:
        controller._performance.record(operation, 1.0, 100.0)

    report_path = controller.save_diagnostic_report("queue_stress", tmp_path)
    report = report_path.read_text(encoding="utf-8")

    assert "Queue instrumentation:" in report
    assert "  complete: true" in report
    assert "  missing: none" in report
    assert "  counters_plausible: true" in report


def test_diagnostic_report_classifies_track_editor_timings_and_counters(
    tmp_path: Path,
) -> None:
    controller, _view = build_controller(tmp_path)
    controller._performance.record(
        "track_editor.build_view_model",
        4.0,
        100.0,
        {"track_id": 1},
    )
    controller._performance.record("track_editor_open_total", 1.0, 100.0)

    report_path = controller.save_diagnostic_report("idle", tmp_path)
    report = report_path.read_text(encoding="utf-8")

    timings = report.split("Timings:", 1)[1].split("Counters:", 1)[0]
    counters = report.split("Counters:", 1)[1].split("Gauges:", 1)[0]
    assert "track_editor.build_view_model" in timings
    assert "track_editor_open_total" not in timings
    assert "track_editor_open_total" in counters
    assert "song.mp3" not in report


def test_save_performance_diagnostic_reports_saved_path(tmp_path: Path, monkeypatch) -> None:
    controller, view = build_controller(tmp_path)
    report_path = tmp_path / "diagnostic.txt"
    report_path.write_text("diagnostic", encoding="utf-8")
    monkeypatch.setattr(controller, "save_diagnostic_report", lambda _context: report_path)

    controller.save_performance_diagnostic("crossfade")

    assert view.diagnostic_path == report_path.resolve()
    assert view.diagnostic_state == ("stopped", "crossfade")


def test_diagnostic_report_uses_configured_runtime_directory(tmp_path: Path) -> None:
    diagnostic_dir = tmp_path / "runtime" / "diagnostics"
    controller, _view = build_controller(
        tmp_path,
        diagnostics_directory=diagnostic_dir,
    )

    report_path = controller.save_diagnostic_report("idle")

    assert report_path.parent == diagnostic_dir
    assert report_path.is_file()


def test_save_performance_diagnostic_explains_disabled_mode(tmp_path: Path, monkeypatch) -> None:
    controller, view = build_controller(tmp_path)

    def reject_report(_context: str) -> Path:
        raise RuntimeError("Performance-Diagnostik ist im Produktionsbetrieb deaktiviert")

    monkeypatch.setattr(controller, "save_diagnostic_report", reject_report)

    controller.save_performance_diagnostic("idle")

    assert view.errors == ["Performance-Diagnostik ist im Produktionsbetrieb deaktiviert"]


def test_diagnostic_report_rejects_unknown_context(tmp_path: Path) -> None:
    controller, _view = build_controller(tmp_path)
    with pytest.raises(ValueError, match="Diagnosekontext"):
        controller.save_diagnostic_report("free text", tmp_path)


def test_database_delay_cannot_be_enabled_in_production_mode(tmp_path: Path) -> None:
    controller, view = build_controller(
        tmp_path, performance_settings=PerformanceSettings(enabled=False)
    )

    controller.begin_diagnostic_scenario("database_delay", 1000)

    assert controller._diagnostic_scenario.snapshot() is None
    assert view.errors
    assert "Produktionsmodus" in view.errors[-1]
    assert "neu starten" in view.errors[-1]
    assert "Administratorrechte sind nicht erforderlich" in view.errors[-1]
