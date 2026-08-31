"""Focused tests for the frequently executed deck render path."""

from party_player.controllers.main_controller import EqualizerDialogState
from party_player.enums import DeckState
from party_player.models import Deck, Track
from party_player.metadata_analysis_service import TempoBatchProgress
from party_player.performance_monitor import PerformanceMonitor
from party_player.ui.main_window import (
    DeckPanel,
    MainWindow,
    _compact_equalizer_labels,
    _configure_focus_cycle,
    _equalizer_effective_text,
    _equalizer_source_text,
    _equalizer_target_choices,
)


class FakeWidget:
    def __init__(self) -> None:
        self.configure_calls = 0
        self.set_calls = 0

    def configure(self, **_values: object) -> None:
        self.configure_calls += 1

    def set(self, _value: float) -> None:
        self.set_calls += 1

    def set_color(self, _color: str) -> None:
        self.configure_calls += 1


def test_compact_equalizer_list_keeps_common_and_selected_presets() -> None:
    labels = [
        "Vererben",
        "Equalizer aus",
        "Neutral",
        "Rock",
        "Klassik",
        "Mein Saal",
    ]

    assert _compact_equalizer_labels(labels, "Mein Saal") == [
        "Vererben",
        "Equalizer aus",
        "Neutral",
        "Rock",
        "Mein Saal",
    ]


def test_compact_equalizer_list_hides_unselected_uncommon_presets() -> None:
    labels = ["Vererben", "Equalizer aus", "Neutral", "Klassik", "Jazz"]

    assert _compact_equalizer_labels(labels, "Neutral") == [
        "Vererben",
        "Equalizer aus",
        "Neutral",
    ]


def test_equalizer_sources_are_presented_in_german() -> None:
    assert _equalizer_source_text("TITLE") == "Titel"
    assert _equalizer_source_text("QUEUE") == "aktuelle Queue"
    assert _equalizer_source_text("PLAYLIST") == "Playlist"
    assert _equalizer_source_text("GENRE") == "Genre"
    assert _equalizer_source_text("PREVIEW") == "Vorschau"
    assert _equalizer_effective_text("Rock", "PREVIEW") == "Effektiv: Rock · Vorschau"


def test_equalizer_targets_explain_unavailable_playlist_and_genre() -> None:
    unavailable = EqualizerDialogState(
        "A",
        "Song",
        "",
        "Aus",
        "DISABLED",
        None,
        None,
        None,
        None,
        None,
    )
    choices = {
        key: (available, reason)
        for key, _label, available, reason in _equalizer_target_choices(unavailable)
    }

    assert choices["preview"] == (True, "")
    assert choices["playlist"] == (False, "Keine gespeicherte Playlist ausgewählt")
    assert choices["genre"] == (False, "Der Titel besitzt kein Genre-Metadatum")

    available = EqualizerDialogState(
        "B",
        "Song",
        "Rock",
        "Rock",
        "GENRE",
        None,
        None,
        "dance",
        "rock",
        7,
    )
    enabled = {
        key: is_available
        for key, _label, is_available, _reason in _equalizer_target_choices(available)
    }
    assert enabled["playlist"]
    assert enabled["genre"]


def test_equalizer_focus_cycle_supports_forward_and_backward_navigation() -> None:
    class FocusWidget:
        def __init__(self) -> None:
            self.bindings: dict[str, object] = {}
            self.focused = False
            self.takefocus = False

        def configure(self, *, takefocus: bool) -> None:
            self.takefocus = takefocus

        def bind(self, event: str, callback: object, *, add: str) -> None:
            assert add == "+"
            self.bindings[event] = callback

        def focus_set(self) -> None:
            self.focused = True

    first, second, third = FocusWidget(), FocusWidget(), FocusWidget()
    _configure_focus_cycle((first, second, third))

    assert all(widget.takefocus for widget in (first, second, third))
    assert first.bindings["<Tab>"](object()) == "break"  # type: ignore[operator]
    assert second.focused
    assert first.bindings["<Shift-Tab>"](object()) == "break"  # type: ignore[operator]
    assert third.focused


def _panel() -> DeckPanel:
    panel = DeckPanel.__new__(DeckPanel)
    panel.deck_id = "B"
    panel._updating_controls = False
    panel._render_cache = {}
    panel._performance = PerformanceMonitor()
    panel._render_operation = "status_render.deck_b"
    panel._accent = "#9b6cff"
    panel._air_badge = FakeWidget()
    panel.configure = lambda **_values: None  # type: ignore[method-assign]
    panel._last_on_air = False
    panel._air_transition_after_id = None
    panel._title = FakeWidget()
    panel._metadata = FakeWidget()
    panel._cue_points = FakeWidget()
    panel._loudness = FakeWidget()
    panel._equalizer = FakeWidget()
    panel._equalizer_button = FakeWidget()
    panel._ducking_status = FakeWidget()
    panel._state = FakeWidget()
    panel._time = FakeWidget()
    panel._progress = FakeWidget()
    panel._volume = FakeWidget()
    panel._volume_label = FakeWidget()
    panel._error = FakeWidget()
    return panel


def test_deck_render_has_detailed_timings_and_skips_invisible_progress_change() -> None:
    panel = _panel()
    deck = Deck(
        "B",
        loaded_track=Track(1, "song.mp3", "Song", "Artist", "Album", 120.0),
        state=DeckState.PLAYING,
        position=10.0,
        duration=120.0,
        cue_out=120.0,
        cue_fade_duration=7.0,
        cue_boundaries_ready=True,
    )

    panel.render(deck)
    initial_progress_sets = panel._progress.set_calls
    deck.position = 10.1
    panel.render(deck)

    assert panel._progress.set_calls == initial_progress_sets
    timings = panel._performance.statistics()
    for suffix in ("text", "cues", "status", "time", "progress", "volume", "message"):
        assert f"status_render.deck_b.{suffix}" in timings


def test_deck_render_shows_effective_catalog_bpm() -> None:
    panel = _panel()
    track = Track(1, "song.mp3", "Song", "Artist", "Album", 120.0, bpm=98.5)

    panel.render(Deck("B", loaded_track=track, duration=120.0))

    assert "98.5 BPM" in str(dict(panel._render_cache["metadata"])["text"])


def test_metadata_batch_progress_is_visible_in_main_window() -> None:
    class Summary:
        text = ""

        def configure(self, **values: object) -> None:
            self.text = str(values["text"])

    class Service:
        def global_batch_progress(self, _job_id: str) -> TempoBatchProgress:
            return TempoBatchProgress(
                12, 5, 4, 1, 2, 0, 0, 0, 7, "Artist — Song", "RUNNING", "", 60.0
            )

    window = object.__new__(MainWindow)
    window._metadata_analysis = Service()
    window._summary = Summary()
    shown: list[str] = []
    window._show_compact_active_analysis = shown.append

    window.show_metadata_analysis_progress("FINISHED", "job", "SUCCESS")

    assert window._metadata_analysis_active
    assert "BPM-Analyse: 5/12" in window._summary.text
    assert "Ohne BPM: 1" in window._summary.text
    assert "Aktuell: Artist — Song" in shown[0]


def test_on_air_transition_schedules_one_short_settle_step() -> None:
    panel = _panel()
    configured: list[dict[str, object]] = []
    scheduled: list[object] = []
    panel.configure = lambda **values: configured.append(values)  # type: ignore[method-assign]

    def schedule(_delay: int, callback: object) -> str:
        scheduled.append(callback)
        return "air-transition"

    panel.after = schedule  # type: ignore[method-assign]
    panel.after_cancel = lambda _after_id: None  # type: ignore[method-assign]

    panel._animate_air_transition(True)
    panel._animate_air_transition(True)

    assert configured == [{"border_color": "#ff4d5e", "border_width": 3}]
    assert len(scheduled) == 1


def test_ducking_indicator_is_informational_and_change_detected() -> None:
    panel = _panel()

    panel.show_ducking(0.5, "attack")
    panel.show_ducking(0.5, "attack")
    assert panel._ducking_status.configure_calls == 1

    panel.show_ducking(1.0, "idle")
    assert panel._ducking_status.configure_calls == 2
