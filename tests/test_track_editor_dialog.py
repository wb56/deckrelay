"""Display-independent lifecycle tests for the phase-A track editor."""

from types import SimpleNamespace
from typing import Any, cast

from pytest import MonkeyPatch

from party_player.ui import dialogs
from party_player.ui.dialogs import CuePointDialog
from party_player.controllers.track_editor_controller import TrackEditorController
from party_player.metadata_analysis_service import TempoAnalysisView
from party_player.metadata_analysis_profiles import MetadataAnalysisProfile
from party_player.analysis import AudioFileInfo


class _Controller:
    def __init__(self) -> None:
        self.preview_stops = 0
        self.analysis_cancels = 0
        self.active_preview_count = 0

    def stop_preview(self) -> None:
        self.preview_stops += 1

    def cancel_analysis(self) -> None:
        self.analysis_cancels += 1


class _Tooltip:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _DialogDouble:
    def __init__(self) -> None:
        self._controller = _Controller()
        self._editor_controller = _EditorController()
        self._closed = False
        self.closed_callbacks = 0
        self._on_closed = self._closed_callback
        self.destroyed = False
        self.grab_released = False
        self._title_tooltip = _Tooltip()
        self._path_tooltip = _Tooltip()

    def _closed_callback(self) -> None:
        self.closed_callbacks += 1

    def grab_release(self) -> None:
        self.grab_released = True

    def grab_current(self) -> object:
        return self

    def destroy(self) -> None:
        self.destroyed = True

    def _finish(self) -> None:
        CuePointDialog._finish(cast(Any, self))


class _Entry:
    def __init__(self) -> None:
        self.value = ""

    def delete(self, _start: int, _end: str) -> None:
        self.value = ""

    def insert(self, _index: int, value: str) -> None:
        self.value = value


class _Label:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, *, text: str) -> None:
        self.text = text


class _Button(_Label):
    def __init__(self) -> None:
        super().__init__()
        self.state = "normal"

    def configure(self, *, text: str, state: str = "normal") -> None:
        self.text = text
        self.state = state


class _TechnicalStatus:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, **values: object) -> None:
        self.text = str(values["text"])


class _TechnicalDialog:
    _channel_description = staticmethod(CuePointDialog._channel_description)
    _channel_layout_text = staticmethod(CuePointDialog._channel_layout_text)
    _format_audio_duration = staticmethod(CuePointDialog._format_audio_duration)
    _technical_audio_text = classmethod(CuePointDialog._technical_audio_text.__func__)
    _technical_audio_fields = classmethod(CuePointDialog._technical_audio_fields.__func__)
    _technical_audio_field_names = staticmethod(CuePointDialog._technical_audio_field_names)
    _technical_audio_unavailable_text = classmethod(
        CuePointDialog._technical_audio_unavailable_text.__func__
    )
    _technical_audio_error_text = staticmethod(CuePointDialog._technical_audio_error_text)

    def __init__(self) -> None:
        self._technical_audio_status = _TechnicalStatus()

    def _is_active(self) -> bool:
        return True


def test_technical_audio_data_are_presented_read_only_and_understandably() -> None:
    dialog = _TechnicalDialog()
    info = AudioFileInfo(
        236.466,
        44_100,
        2,
        "flac",
        "flac",
        987_000,
        None,
        16,
        "stereo",
    )

    CuePointDialog._technical_audio_loaded(cast(Any, dialog), info)

    assert "Audioformat/Codec: FLAC" in dialog._technical_audio_status.text
    assert "Abtastrate: 44.1 kHz" in dialog._technical_audio_status.text
    assert "Bittiefe: 16 Bit" in dialog._technical_audio_status.text
    assert "Kanäle: 2" in dialog._technical_audio_status.text
    assert "Kanallayout: Stereo" in dialog._technical_audio_status.text
    assert "Technische Dauer: 3:56,466" in dialog._technical_audio_status.text
    assert "Bitratenmodus: Nicht als MP3-CBR/VBR klassifiziert" in (
        dialog._technical_audio_status.text
    )


def test_unavailable_technical_audio_data_use_explicit_fallback() -> None:
    dialog = _TechnicalDialog()

    CuePointDialog._technical_audio_failed(cast(Any, dialog), RuntimeError("broken"))

    assert "Status: Nicht verfügbar" in dialog._technical_audio_status.text
    assert "Audioformat/Codec: Nicht verfügbar" in dialog._technical_audio_status.text
    assert "Technische Dauer: Nicht verfügbar" in dialog._technical_audio_status.text


def test_mp3_has_no_misleading_pcm_bit_depth_and_reports_unknown_mode() -> None:
    dialog = _TechnicalDialog()
    info = AudioFileInfo(241.453, 44_100, 2, "mp3", "mp3", 245_000)

    CuePointDialog._technical_audio_loaded(cast(Any, dialog), info)

    assert "Audioformat/Codec: MP3 – MPEG Audio Layer III" in dialog._technical_audio_status.text
    assert "Bitratenmodus: Nicht zuverlässig bestimmbar" in dialog._technical_audio_status.text
    assert "Bittiefe: Nicht anwendbar" in dialog._technical_audio_status.text
    assert "Bitrate: 245 kbit/s" in dialog._technical_audio_status.text


def test_multiple_audio_streams_identify_selected_stream() -> None:
    dialog = _TechnicalDialog()
    info = AudioFileInfo(
        60.0,
        48_000,
        2,
        "flac",
        "matroska",
        bits_per_sample=24,
        audio_stream_count=2,
        selected_stream_index=4,
    )

    CuePointDialog._technical_audio_loaded(cast(Any, dialog), info)

    assert "Audiostreams: 2; verwendet wird Stream 4" in dialog._technical_audio_status.text


def test_late_technical_audio_result_for_older_generation_is_ignored() -> None:
    dialog = _TechnicalDialog()
    dialog._technical_audio_generation = 2
    dialog._technical_audio_status.text = "aktuelle Anzeige"

    CuePointDialog._technical_audio_loaded(
        cast(Any, dialog), AudioFileInfo(60.0, 44_100, 2, "flac"), generation=1
    )

    assert dialog._technical_audio_status.text == "aktuelle Anzeige"


def test_technical_audio_errors_are_mapped_to_understandable_states() -> None:
    assert CuePointDialog._technical_audio_error_text(FileNotFoundError()) == (
        "Datei fehlt oder ist nicht erreichbar."
    )
    assert CuePointDialog._technical_audio_error_text(RuntimeError("timed out")) == (
        "FFprobe hat das Zeitlimit überschritten."
    )
    assert (
        CuePointDialog._technical_audio_error_text(RuntimeError("unvollständige Audiodaten"))
        == "Datei enthält keinen verwendbaren Audiostream."
    )


class _EditorController:
    def __init__(self) -> None:
        self.events: list[str] = []

    def automatic_suggestion(self, _model: object) -> object:
        from party_player.controllers.track_editor_controller import TrackEditorChanges

        return TrackEditorChanges(1.25, 178.5, 6.0)

    def record_event(self, operation: str) -> None:
        self.events.append(operation)


class _AdoptionDialogDouble:
    def __init__(self) -> None:
        self._editor_controller = _EditorController()
        self._view_model = object()
        self._cue_in = _Entry()
        self._cue_out = _Entry()
        self._fade = _Entry()
        self._analysis_status = _Label()
        self._analysis_details = _Label()
        self._error = _Label()

    def _set_analysis_status(self, message: str) -> None:
        self._analysis_status.configure(text=message)

    def _replace(self, entry: _Entry, value: str) -> None:
        CuePointDialog._replace(entry, value)

    def _is_active(self) -> bool:
        return CuePointDialog._is_active(cast(Any, self))


class _SaveCompletionDialogDouble:
    def __init__(self) -> None:
        self._closed = False
        self._controller = _Controller()
        self._editor_controller = _EditorController()
        self._saving = True
        self._save_had_changes = True
        self._pending_metadata_changes = SimpleNamespace(empty=True)
        self._metadata_confirmations = {object()}
        self._metadata_removals = {object(): object()}
        self._metadata_suggestion_actions = {1: object()}
        self._discard_automatic = True
        self._save_button = _Button()
        self._error = _Label()
        self._on_saved_models: list[object] = []
        self._on_saved = self._on_saved_models.append
        self.shown_states: list[object] = []

    def _is_active(self) -> bool:
        return True

    def _show_sources(self, state: object) -> None:
        self.shown_states.append(state)


def test_window_close_matches_cancel_and_releases_preview_resources() -> None:
    dialog = _DialogDouble()

    CuePointDialog._cancel(cast(Any, dialog))

    assert dialog._controller.preview_stops == 1
    assert dialog._controller.analysis_cancels == 1
    assert dialog.grab_released
    assert dialog.destroyed
    assert dialog.closed_callbacks == 1


def test_finish_is_idempotent_for_late_close_callbacks() -> None:
    dialog = _DialogDouble()
    path_tooltip = dialog._path_tooltip
    title_tooltip = dialog._title_tooltip

    CuePointDialog._finish(cast(Any, dialog))
    CuePointDialog._finish(cast(Any, dialog))

    assert dialog.closed_callbacks == 1
    assert dialog.destroyed
    assert path_tooltip.closed
    assert title_tooltip.closed
    assert dialog._path_tooltip is None
    assert dialog._title_tooltip is None


def test_reset_actions_stack_vertically_at_narrow_width(monkeypatch: MonkeyPatch) -> None:
    button_layouts: list[tuple[str, dict[str, object]]] = []

    class Frame:
        def __init__(self, _parent: object, **_kwargs: object) -> None:
            self.layout: dict[str, object] = {}

        def grid(self, **kwargs: object) -> None:
            self.layout = kwargs

        def grid_columnconfigure(self, column: int, *, weight: int) -> None:
            assert (column, weight) == (0, 1)

    class Button:
        def __init__(self, parent: Frame, *, text: str, **_kwargs: object) -> None:
            self.parent = parent
            self.text = text

        def grid(self, **kwargs: object) -> None:
            button_layouts.append((self.text, kwargs))

    monkeypatch.setattr(dialogs.ctk, "CTkFrame", Frame)
    monkeypatch.setattr(dialogs.ctk, "CTkButton", Button)
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._cue_parent = object()
    dialog._cue_in = object()
    dialog._cue_out = object()
    dialog._fade = object()
    dialog._clear = lambda _entry: None
    dialog._use_safe_defaults = lambda: None

    CuePointDialog._build_reset_buttons(dialog)

    assert [text for text, _layout in button_layouts[:3]] == [
        "Startpunkt zurücksetzen",
        "Endpunkt zurücksetzen",
        "Überblenddauer zurücksetzen",
    ]
    assert [layout["row"] for _text, layout in button_layouts[:3]] == [0, 1, 2]
    assert all(layout["sticky"] == "ew" for _text, layout in button_layouts[:3])


def test_long_editor_title_wraps_and_keeps_full_text_in_tooltip(
    monkeypatch: MonkeyPatch,
) -> None:
    labels: list[object] = []
    tooltips: list[tuple[object, str]] = []

    class Frame:
        def __init__(self, _parent: object, **_kwargs: object) -> None:
            pass

        def grid(self, **_kwargs: object) -> None:
            pass

    class Label:
        def __init__(self, _parent: object, *, text: str, **kwargs: object) -> None:
            self.text = text
            self.options = kwargs
            self.pack_options: dict[str, object] = {}
            labels.append(self)

        def pack(self, **kwargs: object) -> None:
            self.pack_options = kwargs

    class Tooltip:
        def __init__(self, widget: object, message: str) -> None:
            tooltips.append((widget, message))

    long_title = "Queen — Bohemian Rhapsody (The Original Soundtrack Remastered Edition)"
    monkeypatch.setattr(dialogs.ctk, "CTkFrame", Frame)
    monkeypatch.setattr(dialogs.ctk, "CTkLabel", Label)
    monkeypatch.setattr(dialogs, "Tooltip", Tooltip)
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._editor_content = object()
    dialog._view_model = SimpleNamespace(
        cue=SimpleNamespace(title=long_title),
        album="Album",
        original_release_year=1975,
        file_path="C:/music/Queen - Bohemian Rhapsody.mp3",
    )

    CuePointDialog._build_header(dialog)

    title_label = labels[0]
    assert title_label.text == long_title
    assert title_label.options["wraplength"] == 540
    assert title_label.pack_options["fill"] == "x"
    assert tooltips[0] == (title_label, long_title)


def test_adopting_analysis_only_stages_values_until_save() -> None:
    dialog = _AdoptionDialogDouble()

    CuePointDialog._adopt_analysis(cast(Any, dialog))

    assert dialog._cue_in.value == "1.250"
    assert dialog._cue_out.value == "178.500"
    assert dialog._fade.value == "6.000"
    assert "erst mit „Speichern“" in dialog._analysis_status.text
    assert dialog._error.text == ""


def test_second_save_click_is_ignored_while_persistence_is_running() -> None:
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._saving = True

    CuePointDialog._save(dialog)

    assert dialog._saving


def test_persistence_failure_keeps_dialog_open_and_reenables_save() -> None:
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._closed = False
    dialog._saving = True
    dialog._editor_controller = _EditorController()
    dialog._save_button = _Button()
    dialog._error = _Label()
    dialog.winfo_exists = lambda: True

    CuePointDialog._save_failed(dialog, RuntimeError("Datenbank gesperrt"))

    assert not dialog._saving
    assert dialog._save_button.state == "normal"
    assert dialog._save_button.text == "Speichern"
    assert "Datenbank gesperrt" in dialog._error.text


def test_discarding_analysis_is_only_staged_until_save() -> None:
    dialog = _AdoptionDialogDouble()
    dialog._view_model = type(
        "Model",
        (),
        {"analysis_state": "SUGGESTED"},
    )()
    dialog._discard_automatic = False

    CuePointDialog._discard_analysis(cast(Any, dialog))

    assert dialog._discard_automatic
    assert "erst mit „Speichern“" in dialog._analysis_details.text
    assert dialog._analysis_status.text == "Verwerfen lokal vorgemerkt."


def test_successful_save_keeps_dialog_open_and_resets_staged_state() -> None:
    dialog = _SaveCompletionDialogDouble()
    view_model = type("Model", (), {"cue": object()})()

    CuePointDialog._save_completed(cast(Any, dialog), cast(Any, view_model))

    assert dialog._controller.preview_stops == 0
    assert dialog._controller.analysis_cancels == 0
    assert dialog._on_saved_models == [view_model]
    assert not dialog._saving
    assert not dialog._save_had_changes
    assert dialog._pending_metadata_changes is None
    assert not dialog._metadata_confirmations
    assert not dialog._metadata_removals
    assert not dialog._metadata_suggestion_actions
    assert not dialog._discard_automatic
    assert dialog._save_button.state == "normal"
    assert dialog._save_button.text == "Speichern"
    assert dialog._error.text == ""


def test_destroyed_dialog_is_inactive_even_if_tk_lookup_raises() -> None:
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._closed = False

    def missing_window() -> bool:
        raise RuntimeError("application has been destroyed")

    dialog.winfo_exists = missing_window

    assert not CuePointDialog._is_active(dialog)


def test_loudness_tab_is_built_lazily_and_only_once(monkeypatch: MonkeyPatch) -> None:
    created_labels: list[object] = []

    class Tab:
        def grid_columnconfigure(self, _column: int, *, weight: int) -> None:
            assert weight == 1

    class Tabs:
        selected = "Lautheit"
        loudness = Tab()

        def get(self) -> str:
            return self.selected

        def tab(self, name: str) -> Tab:
            assert name == "Lautheit"
            return self.loudness

    class Label:
        def __init__(self, parent: Tab, *, text: str, **_kwargs: object) -> None:
            self.parent = parent
            self.text = text
            created_labels.append(self)

        def grid(self, **_kwargs: object) -> None:
            pass

    class Button(Label):
        pass

    class EditorController:
        @staticmethod
        def loudness_analysis_availability() -> tuple[bool, str]:
            return False, "Nicht verfügbar"

    monkeypatch.setattr(dialogs.ctk, "CTkLabel", Label)
    monkeypatch.setattr(dialogs.ctk, "CTkButton", Button)
    dialog = cast(Any, object.__new__(_AdoptionDialogDouble))
    dialog._tabs = Tabs()
    dialog._lazy_tabs_built = {"Cue"}
    dialog._view_model = SimpleNamespace(loudness=None)
    dialog._editor_controller = EditorController()
    dialog._loudness_text = lambda: CuePointDialog._loudness_text(dialog)
    dialog._analyze_loudness = lambda: None
    dialog._build_loudness_tab = lambda: CuePointDialog._build_loudness_tab(dialog)

    CuePointDialog._tab_changed(dialog)
    CuePointDialog._tab_changed(dialog)

    assert len(created_labels) == 3
    assert "keine Lautheitsdaten" in created_labels[0].text
    assert dialog._lazy_tabs_built == {"Cue", "Lautheit"}


def test_track_editor_exposes_only_implemented_tabs() -> None:
    source = CuePointDialog.__init__.__code__.co_consts
    tab_names = next(
        value
        for value in source
        if isinstance(value, tuple) and value == ("Cue", "Lautheit", "Metadaten")
    )

    assert tab_names == ("Cue", "Lautheit", "Metadaten")


def test_metadata_tab_is_loaded_lazily_and_only_once() -> None:
    class Tabs:
        def get(self) -> str:
            return "Metadaten"

    class Dialog:
        def __init__(self) -> None:
            self._tabs = Tabs()
            self._lazy_tabs_built = {"Cue"}
            self.loads = 0

        def _build_metadata_tab(self) -> None:
            self.loads += 1

    dialog = Dialog()

    CuePointDialog._tab_changed(cast(Any, dialog))
    CuePointDialog._tab_changed(cast(Any, dialog))

    assert dialog.loads == 1
    assert dialog._lazy_tabs_built == {"Cue", "Metadaten"}


def test_late_metadata_result_is_ignored_after_dialog_close() -> None:
    class Dialog:
        def _is_active(self) -> bool:
            return False

        def _clear_metadata_container(self) -> None:
            raise AssertionError("destroyed widgets must not be accessed")

    CuePointDialog._metadata_loaded(cast(Any, Dialog()), cast(Any, object()))


def test_metadata_places_technical_data_before_editable_fields_and_suggestions() -> None:
    calls: list[tuple[object, ...]] = []
    updated_model = object()

    class EditorController:
        @staticmethod
        def with_metadata(_view_model: object, model: object) -> object:
            assert model is updated_model
            return "updated"

    class Dialog:
        _metadata_loading = True
        _view_model = "initial"
        _editor_controller = EditorController()

        def _is_active(self) -> bool:
            return True

        def _clear_metadata_container(self) -> None:
            calls.append(("clear",))

        def _render_tempo_analysis(self) -> None:
            calls.append(("tempo", 0))

        def _render_technical_audio_info(self, row: int) -> None:
            calls.append(("technical", row))

        def _render_metadata_fields(self, model: object, *, start_row: int) -> None:
            calls.append(("fields", model, start_row))

        def _render_metadata_suggestions(self, model: object) -> int:
            calls.append(("suggestions", model))
            return 9

        def _schedule_metadata_scroll_top(self) -> None:
            calls.append(("scroll", 0))

    dialog = Dialog()

    CuePointDialog._metadata_loaded(cast(Any, dialog), cast(Any, updated_model))

    assert calls == [
        ("clear",),
        ("tempo", 0),
        ("technical", 1),
        ("fields", updated_model, 2),
        ("suggestions", updated_model),
        ("scroll", 0),
    ]


def test_metadata_scroll_is_reset_after_rebuilt_layout() -> None:
    callbacks: list[object] = []

    class Canvas:
        positions: list[float] = []

        def yview_moveto(self, position: float) -> None:
            self.positions.append(position)

    class Container:
        _parent_canvas = Canvas()

    class Dialog:
        _metadata_scroll_after_id = None
        _metadata_container = Container()

        def after_idle(self, callback: object) -> str:
            callbacks.append(callback)
            return "scroll-top"

        def _scroll_metadata_top(self) -> None:
            CuePointDialog._scroll_metadata_top(cast(Any, self))

        def _is_active(self) -> bool:
            return True

    dialog = Dialog()

    CuePointDialog._schedule_metadata_scroll_top(cast(Any, dialog))
    assert dialog._metadata_scroll_after_id == "scroll-top"
    cast(Any, callbacks[0])()

    assert dialog._metadata_scroll_after_id is None
    assert dialog._metadata_container._parent_canvas.positions == [0.0]


def test_metadata_scroll_callback_ignores_closed_dialog() -> None:
    class Canvas:
        def yview_moveto(self, _position: float) -> None:
            raise AssertionError("closed dialog must not touch canvas")

    dialog = SimpleNamespace(
        _metadata_scroll_after_id="pending",
        _metadata_container=SimpleNamespace(_parent_canvas=Canvas()),
        _is_active=lambda: False,
    )

    CuePointDialog._scroll_metadata_top(cast(Any, dialog))

    assert dialog._metadata_scroll_after_id is None


def test_completed_tempo_analysis_shows_proposals_warnings_and_version() -> None:
    class Widget:
        def __init__(self) -> None:
            self.values: dict[str, object] = {}

        def configure(self, **values: object) -> None:
            self.values.update(values)

    class Dialog:
        _tempo_status = Widget()
        _tempo_button = Widget()
        _tempo_cancel = Widget()

        def _is_active(self) -> bool:
            return True

    view = TempoAnalysisView(
        1,
        9,
        "COMPLETED",
        "TEMPO_AND_ENERGY_EXPERIMENTAL",
        "ffmpeg-onset-acf-v0.1",
        "ffmpeg-onset-autocorrelation",
        "2026-08-22 18:00",
        120.0,
        240.0,
        0.72,
        0.5,
        63,
        0.5,
        (
            "Halb-/Doppeltempo-Alternative vorhanden.",
            "Möglicher Tempowechsel oder instabiles Tempo.",
        ),
        "",
    )
    dialog = Dialog()

    CuePointDialog._tempo_loaded(cast(Any, dialog), view)

    text = str(dialog._tempo_status.values["text"])
    assert "BPM-Vorschlag: 120.0" in text
    assert "Alternative: 240.0" in text
    assert "Experimenteller Energievorschlag: 63 %" in text
    assert "ffmpeg-onset-acf-v0.1" in text
    assert "Tempowechsel" in text
    assert dialog._tempo_button.values["text"] == "Erneut analysieren"


def test_second_tempo_start_is_rejected_with_german_message() -> None:
    class Analysis:
        active_job_count = 1

        def block_reason(self, *, batch: bool) -> str:
            assert not batch
            return ""

    controller = object.__new__(TrackEditorController)
    controller._metadata_analysis = cast(Any, Analysis())
    controller._background_submit = cast(Any, lambda *_args: True)
    errors: list[str] = []

    accepted = controller.start_tempo_analysis_async(
        1,
        MetadataAnalysisProfile.TEMPO,
        lambda _value: None,
        lambda error: errors.append(str(error)),
    )

    assert not accepted
    assert errors == ["Es läuft bereits eine Tempoanalyse."]
