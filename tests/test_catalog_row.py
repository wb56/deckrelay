from typing import Any

from party_player.models import Track
from party_player.performance_monitor import PerformanceMonitor
from party_player.ui import catalog_row
from party_player.ui.catalog_row import CatalogEntryViewModel, CatalogRowView


class FakeWidget:
    created = 0

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        type(self).created += 1
        self.configure_count = 0

    def configure(self, **_kwargs: object) -> None:
        self.configure_count += 1

    def pack(self, **_kwargs: object) -> None:
        pass

    def pack_forget(self) -> None:
        pass

    def destroy(self) -> None:
        pass

    def winfo_pointerxy(self) -> tuple[int, int]:
        return (12, 34)


class FakeTooltip:
    created = 0

    def __init__(self, _widget: object, _text: str) -> None:
        type(self).created += 1

    def cancel(self) -> None:
        pass

    def set_text(self, _text: str) -> None:
        pass

    def close(self) -> None:
        pass


def make_track(track_id: int, title: str) -> Track:
    return Track(track_id, f"{title}.mp3", title, "Artist", "Album", 120.0)


def row_callbacks() -> dict[str, Any]:
    return {
        name: lambda _track: None
        for name in (
            "deck_a",
            "deck_b",
            "queue",
            "cue",
            "loudness",
            "details",
            "equalizer",
            "equalizer_remove",
            "remove",
        )
    }


def test_catalog_row_reuses_widgets_and_tooltips(monkeypatch) -> None:
    FakeWidget.created = 0
    FakeTooltip.created = 0
    monkeypatch.setattr(catalog_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(catalog_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(catalog_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(catalog_row, "Tooltip", FakeTooltip)
    row = CatalogRowView(object(), row_callbacks(), PerformanceMonitor())
    widgets_after_creation = FakeWidget.created
    tooltips_after_creation = FakeTooltip.created

    assert row.bind_entry(CatalogEntryViewModel(make_track(1, "One")))
    assert row.bind_entry(CatalogEntryViewModel(make_track(2, "Two"), True))

    assert FakeWidget.created == widgets_after_creation
    assert FakeTooltip.created == tooltips_after_creation == 5


def test_track_version_text_identifies_the_concrete_file() -> None:
    track = Track(7, "Mix - VBR.mp3", "Mix", "Artist", "Album", 125.2)

    assert catalog_row.track_version_text(track) == "MP3 · Mix - VBR.mp3 · 2:05"


def test_unchanged_catalog_model_does_not_configure_widgets(monkeypatch) -> None:
    monkeypatch.setattr(catalog_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(catalog_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(catalog_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(catalog_row, "Tooltip", FakeTooltip)
    row = CatalogRowView(object(), row_callbacks(), PerformanceMonitor())
    model = CatalogEntryViewModel(make_track(1, "One"))
    row.bind_entry(model)
    configure_count = sum(widget.configure_count for widget in row._buttons.values())
    configure_count += row._label.configure_count

    assert not row.bind_entry(model)
    after = sum(widget.configure_count for widget in row._buttons.values())
    after += row._label.configure_count
    assert after == configure_count


def test_more_menu_offers_equalizer_assignment_actions(monkeypatch) -> None:
    actions: list[str] = []

    class FakeMenu:
        commands: list[tuple[str, Any]] = []
        popup_position: tuple[int, int] | None = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            type(self).commands = []

        def add_command(self, *, label: str, command: Any) -> None:
            type(self).commands.append((label, command))

        def add_separator(self) -> None:
            pass

        def tk_popup(self, x: int, y: int) -> None:
            type(self).popup_position = (x, y)

    monkeypatch.setattr(catalog_row.ctk, "CTkFrame", FakeWidget)
    monkeypatch.setattr(catalog_row.ctk, "CTkLabel", FakeWidget)
    monkeypatch.setattr(catalog_row.ctk, "CTkButton", FakeWidget)
    monkeypatch.setattr(catalog_row, "Tooltip", FakeTooltip)
    monkeypatch.setattr(catalog_row.tk, "Menu", FakeMenu)
    callbacks = row_callbacks()
    callbacks["equalizer"] = lambda _track: actions.append("assign")
    callbacks["equalizer_remove"] = lambda _track: actions.append("remove")
    row = CatalogRowView(object(), callbacks, PerformanceMonitor())
    row.bind_entry(CatalogEntryViewModel(make_track(1, "One")))

    row._show_more_actions()

    commands = dict(FakeMenu.commands)
    commands["Equalizer zuweisen…"]()
    commands["Equalizer-Zuweisung entfernen"]()
    assert actions == ["assign", "remove"]
    assert FakeMenu.popup_position == (12, 34)
