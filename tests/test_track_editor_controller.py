"""Tests for the phase-A track-editor composition layer."""

from dataclasses import replace
from typing import Any, cast

import pytest

from party_player.controllers.cue_point_controller import CuePointEditorState
from party_player.controllers.track_editor_controller import (
    TrackEditorChanges,
    TrackEditorController,
)
from party_player.cue_points import ResolvedTrackBoundaries
from party_player.models import Track
from party_player.performance_monitor import PerformanceMonitor
from party_player.analysis import AudioFileInfo


def _state() -> CuePointEditorState:
    return CuePointEditorState(
        track_id=7,
        title="Artist — Titel",
        manual_cue_in=None,
        manual_cue_out=None,
        manual_fade_duration=None,
        resolved=ResolvedTrackBoundaries(
            0.0, 180.0, 7.0, "FILE_BOUNDARY", "FILE_BOUNDARY", "GLOBAL"
        ),
    )


class _CueController:
    def __init__(self) -> None:
        self.saved: tuple[int, float | None, float | None, float | None] | None = None

    def state(self, _track_id: int) -> CuePointEditorState:
        return _state()

    def save(
        self, track_id: int, cue_in: float | None, cue_out: float | None, fade: float | None
    ) -> CuePointEditorState:
        self.saved = (track_id, cue_in, cue_out, fade)
        return _state()

    def save_async(
        self,
        track_id: int,
        cue_in: float | None,
        cue_out: float | None,
        fade: float | None,
        completed: Any,
        _failed: Any,
        *,
        discard_automatic: bool = False,
        changed_fields: frozenset[str] | None = None,
    ) -> None:
        self.saved = (track_id, cue_in, cue_out, fade)
        self.discarded = discard_automatic
        self.changed_fields = changed_fields
        completed(_state())


class _LoudnessController:
    def __init__(self) -> None:
        self.error: str | None = None

    def state(self, track_id: int) -> object:
        return {"track_id": track_id, "integrated_lufs": None}

    def analysis_availability(self) -> tuple[bool, str]:
        return True, "FFmpeg ist verfügbar."

    def analyze_track(self, track_id: int, completed: Any) -> object:
        completed(None, self.error)
        return object()


def _controller() -> tuple[TrackEditorController, _CueController]:
    cue = _CueController()
    return TrackEditorController(cast(Any, cue)), cue


def _track() -> Track:
    return Track(7, "C:/music/song.mp3", "Titel", "Artist", "Album", 180.0, year=2001)


def test_view_model_preserves_real_metadata_and_missing_values() -> None:
    controller, _ = _controller()

    model = controller.build_view_model(_track())

    assert model.heading == "Artist — Titel"
    assert model.album == "Album"
    assert model.original_release_year == 2001
    assert model.duration_seconds == 180.0
    assert model.cue.manual_cue_in is None
    assert model.loudness is None
    assert model.equalizer_preset_name is None


def test_track_editor_runs_single_loudness_analysis_and_refreshes_state() -> None:
    loudness = _LoudnessController()
    controller = TrackEditorController(cast(Any, _CueController()), cast(Any, loudness))
    completed: list[object] = []
    failed: list[Exception] = []

    assert controller.loudness_analysis_availability() == (True, "FFmpeg ist verfügbar.")
    controller.analyze_loudness(7, completed.append, failed.append)

    assert completed == [{"track_id": 7, "integrated_lufs": None}]
    assert failed == []


def test_track_editor_reports_single_loudness_analysis_failure() -> None:
    loudness = _LoudnessController()
    loudness.error = "FFmpeg-Analyse fehlgeschlagen"
    controller = TrackEditorController(cast(Any, _CueController()), cast(Any, loudness))
    completed: list[object] = []
    failed: list[Exception] = []

    controller.analyze_loudness(7, completed.append, failed.append)

    assert completed == []
    assert len(failed) == 1
    assert str(failed[0]) == "FFmpeg-Analyse fehlgeschlagen"


def test_view_model_composes_existing_loudness_and_equalizer_sources() -> None:
    cue = _CueController()
    controller = TrackEditorController(
        cast(Any, cue),
        cast(Any, _LoudnessController()),
        lambda _track: ("party", "Party", "TITLE"),
    )

    model = controller.build_view_model(_track())

    assert model.loudness == {"track_id": 7, "integrated_lufs": None}
    assert model.equalizer_preset_key == "party"
    assert model.equalizer_preset_name == "Party"
    assert model.equalizer_source == "TITLE"


def test_technical_audio_info_uses_existing_async_analysis_service() -> None:
    cue = _CueController()
    expected = AudioFileInfo(180.0, 44_100, 2, "flac", bits_per_sample=16)

    class Analysis:
        def technical_audio_info(self, track_id: int) -> AudioFileInfo:
            assert track_id == 7
            return expected

    def submit(task: Any, completed: Any, _failed: Any) -> bool:
        completed(task())
        return True

    controller = TrackEditorController(
        cast(Any, cue),
        background_submit=submit,
        metadata_analysis=cast(Any, Analysis()),
    )
    loaded: list[AudioFileInfo] = []

    assert controller.load_technical_audio_info_async(7, loaded.append, pytest.fail)
    assert loaded == [expected]


def test_editor_records_path_free_build_save_and_lifecycle_operations() -> None:
    cue = _CueController()
    performance = PerformanceMonitor()
    controller = TrackEditorController(
        cast(Any, cue),
        equalizer_state=lambda _track: ("party", "Party", "TITLE"),
        performance_monitor=performance,
    )
    model = controller.build_view_model(_track())

    controller.save_async(
        model,
        TrackEditorChanges(1.0, 170.0, 5.0),
        lambda _model: None,
        lambda error: pytest.fail(str(error)),
    )
    controller.record_event("track_editor.open")

    statistics = performance.statistics()
    assert statistics["track_editor.build_view_model"].count == 1
    assert statistics["track_editor.save"].count == 1
    assert statistics["track_editor.open"].count == 1
    assert statistics["track_editor.equalizer_resolve"].count == 1


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (TrackEditorChanges(-0.1, None, None), "Cue In"),
        (TrackEditorChanges(None, 181.0, None), "Dateiende"),
        (TrackEditorChanges(20.0, 10.0, None), "hinter Cue In"),
        (TrackEditorChanges(10.0, 20.0, -1.0), "nicht negativ"),
        (TrackEditorChanges(10.0, 20.0, 11.0), "hörbare Titelbereich"),
    ],
)
def test_cue_validation_rejects_invalid_boundaries(
    changes: TrackEditorChanges, message: str
) -> None:
    controller, _ = _controller()
    model = controller.build_view_model(_track())

    with pytest.raises(ValueError, match=message):
        controller.validate_changes(model, changes)


def test_save_validates_then_delegates_once_to_existing_cue_controller() -> None:
    controller, cue = _controller()
    model = controller.build_view_model(_track())

    controller.save(model, TrackEditorChanges(None, 170.0, 5.0))

    assert cue.saved == (7, None, 170.0, 5.0)


def test_unchanged_values_do_not_delegate_to_persistence() -> None:
    controller, cue = _controller()
    model = controller.build_view_model(_track())

    result = controller.save(model, TrackEditorChanges(None, None, None))

    assert result is model
    assert cue.saved is None


def test_async_save_delegates_once_and_returns_updated_model() -> None:
    controller, cue = _controller()
    model = controller.build_view_model(_track())
    completed: list[object] = []

    submitted = controller.save_async(
        model,
        TrackEditorChanges(1.0, 170.0, 5.0),
        completed.append,
        lambda error: pytest.fail(str(error)),
    )

    assert submitted
    assert cue.saved == (7, 1.0, 170.0, 5.0)
    assert cue.changed_fields == frozenset({"cue_in", "cue_out", "fade_duration"})
    assert len(completed) == 1


def test_discarding_automatic_suggestion_is_part_of_async_save() -> None:
    controller, cue = _controller()
    model = controller.build_view_model(_track())
    model = replace(
        model,
        cue=replace(
            model.cue,
            automatic_cue_in=1.0,
            automatic_cue_out=175.0,
            automatic_fade_duration=6.0,
        ),
    )

    submitted = controller.save_async(
        model,
        TrackEditorChanges(None, None, None, discard_automatic=True),
        lambda _model: None,
        lambda error: pytest.fail(str(error)),
    )

    assert submitted
    assert cue.discarded
    assert cue.changed_fields == frozenset()


def test_automatic_suggestion_is_staged_without_persistence() -> None:
    controller, cue = _controller()
    model = controller.build_view_model(_track())
    model = replace(
        model,
        cue=replace(
            model.cue,
            automatic_cue_in=1.5,
            automatic_cue_out=175.0,
            automatic_fade_duration=6.0,
        ),
    )

    suggestion = controller.automatic_suggestion(model)

    assert suggestion == TrackEditorChanges(1.5, 175.0, 6.0)
    assert cue.saved is None


def test_analysis_state_distinguishes_none_suggested_and_adopted() -> None:
    controller, _ = _controller()
    model = controller.build_view_model(_track())
    assert model.analysis_state == "NONE"

    suggested_cue = replace(
        model.cue,
        automatic_cue_in=1.5,
        automatic_cue_out=175.0,
        automatic_fade_duration=6.0,
    )
    suggested = replace(model, cue=suggested_cue)
    assert suggested.analysis_state == "SUGGESTED"

    adopted = replace(
        suggested,
        cue=replace(
            suggested_cue,
            manual_cue_in=1.5,
            manual_cue_out=175.0,
            manual_fade_duration=6.0,
        ),
    )
    assert adopted.analysis_state == "ADOPTED"


def test_analysis_state_rejects_incomplete_suggestion_for_adoption() -> None:
    controller, _ = _controller()
    model = controller.build_view_model(_track())
    incomplete = replace(model, cue=replace(model.cue, automatic_cue_in=1.5))

    assert incomplete.analysis_state == "INCOMPLETE"
    with pytest.raises(ValueError, match="vollständiger Vorschlag"):
        controller.automatic_suggestion(incomplete)


def test_effective_play_duration_uses_resolved_boundaries() -> None:
    controller, _ = _controller()
    model = controller.build_view_model(_track())

    assert model.effective_play_duration == 180.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("   ", None),
        ("12,5", 12.5),
        ("12.5", 12.5),
        (" −1,25 ", -1.25),
    ],
)
def test_optional_seconds_parser_accepts_german_and_invariant_numbers(
    raw: str, expected: float | None
) -> None:
    assert TrackEditorController.parse_optional_seconds(raw, "Cue In") == expected


def test_optional_seconds_parser_reports_the_field_in_german() -> None:
    with pytest.raises(ValueError, match="Cue Out ist keine gültige Zahl"):
        TrackEditorController.parse_optional_seconds("spät", "Cue Out")
