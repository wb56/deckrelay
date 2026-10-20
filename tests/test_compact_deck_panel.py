"""Behavioral tests for the compact deck panel without creating a GUI."""

from collections.abc import Callable
from types import SimpleNamespace
import pytest

from party_player.enums import DeckState
from party_player.models import Deck, Track
from party_player.performance_monitor import PerformanceMonitor
from party_player.ui import compact_deck_actions, deck_panels, theme
from party_player.ui.deck_panels import CompactDeckPanel


class WidgetDouble:
    """Minimal CustomTkinter-compatible widget with observable interactions."""

    created: list["WidgetDouble"] = []

    def __init__(self, master: object = None, **values: object) -> None:
        self.master = master
        self.initial_values = values
        self.configure_calls: list[dict[str, object]] = []
        self.grid_calls: list[dict[str, object]] = []
        self.grid_configure_calls: list[dict[str, object]] = []
        self.column_calls: list[tuple[int, dict[str, object]]] = []
        self.bindings: dict[str, Callable[..., object]] = {}
        self.set_calls: list[float] = []
        self.command = values.get("command")
        self.width = 200
        WidgetDouble.created.append(self)

    def configure(self, **values: object) -> None:
        self.configure_calls.append(values)

    def grid(self, **values: object) -> None:
        self.grid_calls.append(values)

    def grid_configure(self, **values: object) -> None:
        self.grid_configure_calls.append(values)

    def grid_columnconfigure(self, column: int, **values: object) -> None:
        self.column_calls.append((column, values))

    def bind(self, event: str, callback: Callable[..., object]) -> None:
        self.bindings[event] = callback

    def set(self, value: float) -> None:
        self.set_calls.append(value)

    def winfo_width(self) -> int:
        return self.width


class TooltipDouble:
    def __init__(self, widget: WidgetDouble, text: str) -> None:
        self.widget = widget
        self.text = text
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def widget_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    WidgetDouble.created = []
    monkeypatch.setattr(deck_panels.ctk.CTkFrame, "__init__", WidgetDouble.__init__)
    monkeypatch.setattr(deck_panels.ctk.CTkFrame, "configure", WidgetDouble.configure)
    monkeypatch.setattr(
        deck_panels.ctk.CTkFrame, "grid_columnconfigure", WidgetDouble.grid_columnconfigure
    )
    monkeypatch.setattr(deck_panels.ctk, "CTkLabel", WidgetDouble)
    monkeypatch.setattr(deck_panels.ctk, "CTkSlider", WidgetDouble)
    monkeypatch.setattr(deck_panels, "_SeekProgressBar", WidgetDouble)


def make_panel(deck_id: str = "A") -> CompactDeckPanel:
    return CompactDeckPanel(object(), deck_id, lambda _deck_id, _action: None)


def loaded_deck(**changes: object) -> Deck:
    deck = Deck(
        "A",
        loaded_track=Track(
            7,
            r"D:\Musik\song.mp3",
            "Song",
            "Artist",
            "Album",
            125.0,
            bpm=128.0,
        ),
        state=DeckState.PLAYING,
        volume=0.65,
        position=61.0,
        duration=125.0,
    )
    for name, value in changes.items():
        setattr(deck, name, value)
    return deck


@pytest.mark.parametrize("deck_id", ["A", "B"])
def test_construction_initializes_deck_widgets_and_callbacks(
    widget_doubles: None, deck_id: str
) -> None:
    actions: list[tuple[str, str]] = []
    monitor = PerformanceMonitor()

    panel = CompactDeckPanel(
        object(), deck_id, lambda deck, action: actions.append((deck, action)), monitor
    )

    assert panel.deck_id == deck_id
    assert panel._accent == theme.DECK_ACCENTS[deck_id]
    assert panel._performance is monitor
    assert panel._action("A", "play") is None
    assert actions == [("A", "play")]
    assert panel._seek_callback is None
    assert panel._volume_callback is None
    assert panel._fade_callback is None
    assert panel._cancel_fade_callback is None
    assert panel._updating_controls is False
    assert panel._progress_max == 1.0
    assert panel._render_cache == {}
    assert panel._tooltips == []
    assert panel._identity.initial_values["text"] == f"DECK {deck_id}"
    assert panel._identity.initial_values["text_color"] == theme.DECK_ACCENTS[deck_id]
    assert panel._progress.set_calls == [0]
    assert set(panel._progress.bindings) == {"<Button-1>", "<B1-Motion>"}
    assert panel._volume.set_calls == [1]


def test_render_updates_deck_state_progress_time_and_volume(widget_doubles: None) -> None:
    panel = make_panel()

    panel.render(loaded_deck(is_on_air=True, cue_warning="Cue prüfen"))

    assert panel._title.configure_calls[-1] == {"text": "Artist – Song"}
    assert panel._source.configure_calls[-1] == {"text": "Quelle: song.mp3 · 128 BPM"}
    assert panel._state.configure_calls[-1] == {
        "text": "● ON AIR",
        "text_color": theme.ON_AIR,
    }
    assert panel._time.configure_calls[-1] == {"text": "01:01 / 02:05 · Rest 01:04"}
    assert panel.configure_calls[-1] == {
        "border_color": theme.ON_AIR,
        "border_width": 2,
    }
    assert panel._progress.set_calls[-1] == pytest.approx(61 / 125)
    assert panel._progress_max == 125.0
    assert panel._volume.set_calls[-1] == 0.65
    assert panel._volume_label.configure_calls[-1] == {"text": "Lautstärke 65%"}
    assert panel._message.configure_calls[-1] == {
        "text": "Cue prüfen",
        "text_color": theme.WARNING,
    }


def test_render_prefers_error_and_uses_error_coloring(widget_doubles: None) -> None:
    panel = make_panel()

    panel.render(
        loaded_deck(
            state=DeckState.ERROR,
            error_message="Decoderfehler",
            cue_warning="Cue prüfen",
        )
    )

    assert panel._state.configure_calls[-1] == {
        "text": "FEHLER",
        "text_color": theme.ERROR,
    }
    assert panel._message.configure_calls[-1] == {
        "text": "Decoderfehler",
        "text_color": theme.ERROR,
    }


def test_repeated_render_uses_cache_and_skips_widget_updates(widget_doubles: None) -> None:
    panel = make_panel()
    deck = loaded_deck()

    panel.render(deck)
    calls = {
        "panel": len(panel.configure_calls),
        "title": len(panel._title.configure_calls),
        "source": len(panel._source.configure_calls),
        "state": len(panel._state.configure_calls),
        "time": len(panel._time.configure_calls),
        "progress": len(panel._progress.set_calls),
        "volume": len(panel._volume.set_calls),
        "volume_text": len(panel._volume_label.configure_calls),
        "message": len(panel._message.configure_calls),
    }
    panel.render(deck)

    assert len(panel.configure_calls) == calls["panel"]
    assert len(panel._title.configure_calls) == calls["title"]
    assert len(panel._source.configure_calls) == calls["source"]
    assert len(panel._state.configure_calls) == calls["state"]
    assert len(panel._time.configure_calls) == calls["time"]
    assert len(panel._progress.set_calls) == calls["progress"]
    assert len(panel._volume.set_calls) == calls["volume"]
    assert len(panel._volume_label.configure_calls) == calls["volume_text"]
    assert len(panel._message.configure_calls) == calls["message"]


def test_progress_cache_ignores_subpixel_position_change(widget_doubles: None) -> None:
    panel = make_panel()
    deck = loaded_deck(position=10.0, duration=120.0)

    panel.render(deck)
    progress_sets = len(panel._progress.set_calls)
    deck.position = 10.1
    panel.render(deck)

    assert len(panel._progress.set_calls) == progress_sets
    assert panel._time.configure_calls[-1] == {"text": "00:10 / 02:00 · Rest 01:50"}


def test_bind_controls_and_pointer_seek_forward_existing_signatures(
    widget_doubles: None,
) -> None:
    panel = make_panel("B")
    seeks: list[tuple[str, float]] = []
    volumes: list[tuple[str, float]] = []
    fades: list[tuple[str, bool]] = []
    cancellations: list[str] = []
    panel.bind_controls(
        lambda deck, position: seeks.append((deck, position)),
        lambda deck, volume: volumes.append((deck, volume)),
        lambda deck, fade_in: fades.append((deck, fade_in)),
        cancellations.append,
    )
    panel._progress_max = 180.0

    panel._seek_from_pointer(SimpleNamespace(x=50))
    panel._seek_from_pointer(SimpleNamespace(x=-20))
    panel._seek_from_pointer(SimpleNamespace(x=400))

    assert seeks == [("B", 45.0), ("B", 0.0), ("B", 180.0)]


def test_volume_and_fade_callbacks_ignore_only_programmatic_volume(
    widget_doubles: None,
) -> None:
    panel = make_panel("B")
    volumes: list[tuple[str, float]] = []
    fades: list[tuple[str, bool]] = []
    cancellations: list[str] = []
    panel.bind_controls(
        lambda _deck, _position: None,
        lambda deck, value: volumes.append((deck, value)),
        lambda deck, fade_in: fades.append((deck, fade_in)),
        cancellations.append,
    )

    panel._volume_changed(0.4)
    panel._updating_controls = True
    panel._volume_changed(0.7)
    panel._fade(True)
    panel._fade(False)
    panel._cancel_fade()

    assert panel._volume_label.configure_calls[-2:] == [
        {"text": "Lautstärke 40%"},
        {"text": "Lautstärke 70%"},
    ]
    assert volumes == [("B", 0.4)]
    assert fades == [("B", True), ("B", False)]
    assert cancellations == ["B"]


def test_dispose_closes_and_removes_all_tooltips(widget_doubles: None) -> None:
    panel = make_panel()
    tooltips = [TooltipDouble(panel._title, "one"), TooltipDouble(panel._state, "two")]
    panel._tooltips.extend(tooltips)

    panel.dispose()

    assert [tooltip.close_calls for tooltip in tooltips] == [1, 1]
    assert panel._tooltips == []


def test_compact_actions_build_once_and_forward_all_commands(
    widget_doubles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = make_panel("B")
    actions: list[tuple[str, str]] = []
    fades: list[tuple[str, bool]] = []
    cancellations: list[str] = []
    panel._action = lambda deck, action: actions.append((deck, action))
    panel._fade_callback = lambda deck, fade_in: fades.append((deck, fade_in))
    panel._cancel_fade_callback = cancellations.append
    monkeypatch.setattr(compact_deck_actions.ctk, "CTkFrame", WidgetDouble)
    monkeypatch.setattr(compact_deck_actions.ctk, "CTkButton", WidgetDouble)
    monkeypatch.setattr(compact_deck_actions, "Tooltip", TooltipDouble)

    compact_deck_actions.build_compact_deck_actions(panel)
    buttons = [
        widget
        for widget in WidgetDouble.created
        if widget.master is panel._actions_host and "command" in widget.initial_values
    ]
    for button in buttons:
        assert callable(button.command)
        button.command()
    count_after_first_build = len(WidgetDouble.created)
    compact_deck_actions.build_compact_deck_actions(panel)

    assert len(buttons) == 8
    assert actions == [
        ("B", "play"),
        ("B", "pause"),
        ("B", "resume"),
        ("B", "stop"),
        ("B", "eject"),
    ]
    assert fades == [("B", True), ("B", False)]
    assert cancellations == ["B"]
    assert len(panel._tooltips) == 8
    assert len(WidgetDouble.created) == count_after_first_build


def test_bind_compact_decks_uses_controller_callbacks_and_existing_import(
    widget_doubles: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = make_panel("A"), make_panel("B")
    built: list[CompactDeckPanel] = []
    monkeypatch.setattr(compact_deck_actions, "build_compact_deck_actions", built.append)
    controller = SimpleNamespace(
        seek=lambda _deck, _position: None,
        set_deck_volume=lambda _deck, _volume: None,
        fade=lambda _deck, _fade_in: None,
        cancel_fade=lambda _deck: None,
    )

    compact_deck_actions.bind_compact_decks(controller, first, second)

    assert built == [first, second]
    for panel in (first, second):
        assert panel._seek_callback is controller.seek
        assert panel._volume_callback is controller.set_deck_volume
        assert panel._fade_callback is controller.fade
        assert panel._cancel_fade_callback is controller.cancel_fade
    assert CompactDeckPanel.__module__ == "party_player.ui.deck_panels"
