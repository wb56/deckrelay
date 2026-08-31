import sys
from types import ModuleType

from party_player import __main__ as application_entry


def test_application_entry_calls_freeze_support_before_composition(monkeypatch) -> None:
    calls: list[str] = []

    class ApplicationFake:
        def __init__(self) -> None:
            calls.append("compose")

        def run(self) -> None:
            calls.append("run")

    app_module = ModuleType("party_player.app")
    app_module.PartyPlayerApplication = ApplicationFake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "party_player.app", app_module)
    monkeypatch.setattr(
        application_entry.multiprocessing, "freeze_support", lambda: calls.append("freeze")
    )
    monkeypatch.setattr(application_entry.sys, "argv", ["DeckRelay.exe"])

    application_entry.main()

    assert calls == ["freeze", "compose", "run"]


def test_packaged_metadata_probe_does_not_compose_main_window(monkeypatch, tmp_path) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        application_entry.multiprocessing, "freeze_support", lambda: calls.append("freeze")
    )
    monkeypatch.setattr(
        application_entry,
        "_run_internal_metadata_analysis_probe",
        lambda *args: calls.append(args),
    )
    monkeypatch.setattr(
        application_entry.sys,
        "argv",
        [
            "DeckRelay.exe",
            "--internal-metadata-analysis-probe",
            str(tmp_path / "input.mp3"),
            str(tmp_path / "result.json"),
            "ffmpeg.exe",
            "ffprobe.exe",
            "30",
        ],
    )

    application_entry.main()

    assert calls[0] == "freeze"
    assert len(calls) == 2
