"""Display-independent tests for the custom loudness dialog actions."""

from typing import Any, cast

import pytest

from party_player.ui.loudness_dialogs import (
    _parse_gain_db as new_parse_gain_db,
    LoudnessDialog as NewLoudnessDialog,
    NormalizationSettingsDialog as NewNormalizationSettingsDialog,
)
from party_player.ui.dialogs import (
    _parse_gain_db,
    LoudnessDialog,
    NormalizationSettingsDialog,
)
from party_player.controllers.loudness_controller import LoudnessEditorState
from party_player.loudness import ResolvedLoudnessSettings
from party_player.models import Deck, Track
from party_player.ui.main_window import _deck_loudness_text, _track_details_text


class _Entry:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _ErrorLabel:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, *, text: str) -> None:
        self.text = text


class _Controller:
    def __init__(self) -> None:
        self.saved: list[tuple[int, float | None]] = []

    def save_manual_gain(self, track_id: int, gain: float | None) -> None:
        if gain is not None and not -12.0 <= gain <= 12.0:
            raise ValueError("außerhalb des Bereichs")
        self.saved.append((track_id, gain))


class _DialogDouble:
    def __init__(self, value: str = "") -> None:
        self._controller = _Controller()
        self._track_id = 42
        self._gain = _Entry(value)
        self._error = _ErrorLabel()
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True

    @staticmethod
    def _number(entry: _Entry) -> float:
        return float(entry.get().strip().replace(",", "."))


def test_dialogs_module_reexports_loudness_dialog_symbols() -> None:
    assert LoudnessDialog is NewLoudnessDialog
    assert NormalizationSettingsDialog is NewNormalizationSettingsDialog
    assert _parse_gain_db is new_parse_gain_db
    assert new_parse_gain_db(" −4,5 dB ") == -4.5


def test_save_accepts_decimal_comma_and_closes_dialog() -> None:
    dialog = _DialogDouble("-2,50")

    LoudnessDialog._save(cast(Any, dialog))

    assert dialog._controller.saved == [(42, -2.5)]
    assert dialog.destroyed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-4 dB", -4.0),
        ("−4 DB", -4.0),
        (" -4,5db ", -4.5),
        ("+2.25", 2.25),
    ],
)
def test_gain_parser_accepts_user_friendly_db_notation(raw: str, expected: float) -> None:
    assert _parse_gain_db(raw) == expected


def test_invalid_value_keeps_dialog_open_and_shows_inline_error() -> None:
    dialog = _DialogDouble("13")

    LoudnessDialog._save(cast(Any, dialog))

    assert dialog._controller.saved == []
    assert not dialog.destroyed
    assert dialog._error.text == "außerhalb des Bereichs"


def test_reset_removes_manual_gain_and_cancel_does_not_save() -> None:
    reset_dialog = _DialogDouble("4")
    cancel_dialog = _DialogDouble("4")

    LoudnessDialog._reset(cast(Any, reset_dialog))
    LoudnessDialog._cancel(cast(Any, cancel_dialog))

    assert reset_dialog._controller.saved == [(42, None)]
    assert reset_dialog.destroyed
    assert cancel_dialog._controller.saved == []
    assert cancel_dialog.destroyed


def test_track_details_include_resolved_loudness_information() -> None:
    track = Track(7, "music/song.flac", "Song", "Artist", "Album", 125.0)
    resolved = ResolvedLoudnessSettings(
        requested_gain_db=4.0,
        effective_gain_db=1.5,
        linear_gain_factor=1.1885,
        source="REPLAYGAIN_TAG",
        peak_limited=True,
        normalization_mode="TRACK",
    )
    state = LoudnessEditorState(
        track.id,
        "Artist — Song",
        None,
        resolved,
        "ReplayGain",
        "Clip-Schutz aktiv",
    )

    details = _track_details_text(track, state)

    assert "Lautstärkeanpassung:" in details
    assert "Quelle: ReplayGain" in details
    assert "Angefordert: +4.00 dB" in details
    assert "Effektiv: +1.50 dB" in details
    assert "Clip-Schutz aktiv" in details


def test_deck_loudness_text_shows_complete_runtime_state() -> None:
    track = Track(7, "music/song.flac", "Song", "Artist", "Album", 125.0)
    deck = Deck(
        deck_id="A",
        loaded_track=track,
        loudness_requested_gain_db=4.0,
        loudness_effective_gain_db=1.5,
        loudness_source="REPLAYGAIN_TAG",
        loudness_peak_limited=True,
    )

    assert _deck_loudness_text(deck) == "Gain: +4.00 → +1.50 dB · ReplayGain · Clip-Schutz aktiv"


def test_normalization_settings_dialog_saves_all_values_transactionally() -> None:
    saved: list[dict[str, object]] = []

    class Controller:
        def update_normalization_settings(self, **values: object) -> None:
            saved.append(values)

    dialog = _DialogDouble()
    dialog._controller = Controller()  # type: ignore[assignment]
    dialog._enabled = _Entry("1")  # type: ignore[attr-defined]
    dialog._clip_protection = _Entry("1")  # type: ignore[attr-defined]
    dialog._mode = _Entry("Album")  # type: ignore[attr-defined]
    dialog._mode_labels = {"Titel": "TRACK", "Album": "ALBUM", "Aus": "OFF"}  # type: ignore[attr-defined]
    dialog._target_loudness = _Entry("-14")  # type: ignore[attr-defined]
    dialog._maximum_positive = _Entry("5,5")  # type: ignore[attr-defined]
    dialog._maximum_negative = _Entry("-10")  # type: ignore[attr-defined]
    dialog._maximum_peak = _Entry("-0,5")  # type: ignore[attr-defined]
    dialog._headroom = _Entry("1,5")  # type: ignore[attr-defined]
    dialog._fallback = _Entry("2")  # type: ignore[attr-defined]
    dialog._smoothing = _Entry("0,75")  # type: ignore[attr-defined]

    NormalizationSettingsDialog._save(cast(Any, dialog))

    assert saved == [
        {
            "enabled": True,
            "clip_protection_enabled": True,
            "mode": "ALBUM",
            "target_loudness_lufs": -14.0,
            "maximum_positive_gain_db": 5.5,
            "maximum_negative_gain_db": -10.0,
            "maximum_output_peak_db": -0.5,
            "headroom_db": 1.5,
            "fallback_positive_gain_db": 2.0,
            "smoothing_seconds": 0.75,
        }
    ]
    assert dialog.destroyed
