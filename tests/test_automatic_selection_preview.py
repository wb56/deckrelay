"""State-neutral multi-step automatic-selection preview tests."""

from dataclasses import dataclass
from datetime import datetime
import random
from threading import Event, Thread

import pytest

from party_player.automatic_selection import AutomaticSelectionService
from party_player.models import Track
from party_player.repetition_policy import PersistentRepetitionService
from party_player.selection_preview import SelectionPreviewCompletion
from party_player.selection_decision import CandidateDecisionCategory, SelectionOutcome
from party_player.track_selection import RepetitionService, SelectionDecision, TrackSelectionService


def _track(track_id: int, artist: str = "Artist") -> Track:
    return Track(track_id, f"ignored-{track_id}.mp3", f"Track {track_id}", artist, "", 180.0)


@dataclass
class _Tracks:
    values: tuple[Track, ...]
    calls: int = 0

    def automatic_candidates(self) -> list[Track]:
        self.calls += 1
        return list(self.values)


@dataclass
class _HistorySnapshot:
    counts: dict[int, int]
    recent: list[int]

    def play_counts(self) -> dict[int, int]:
        return dict(self.counts)

    def recent_track_ids(self, limit: int) -> set[int]:
        return set(self.recent[:limit])

    def record_played(self, track: Track) -> None:
        self.counts[track.id] = self.counts.get(track.id, 0) + 1
        self.recent.insert(0, track.id)


@dataclass
class _History:
    counts: dict[int, int]
    recent: list[int]
    snapshot_calls: int = 0

    def play_counts(self) -> dict[int, int]:
        return dict(self.counts)

    def recent_track_ids(self, limit: int) -> set[int]:
        return set(self.recent[:limit])

    def preview_snapshot(self) -> _HistorySnapshot:
        self.snapshot_calls += 1
        return _HistorySnapshot(dict(self.counts), list(self.recent))


def test_preview_is_repeatable_and_does_not_change_real_selection() -> None:
    tracks = _Tracks((_track(1, "A"), _track(2, "B"), _track(3, "C")))
    history = _History({}, [])
    selector = AutomaticSelectionService(
        tracks, history, recent_track_limit=0, randomizer=random.Random(37)
    )
    rules = TrackSelectionService()

    first = selector.preview(rules, 3)
    second = selector.preview(rules, 3)
    real = selector.select(rules)
    control = AutomaticSelectionService(
        _Tracks(tracks.values),
        _History({}, []),
        recent_track_limit=0,
        randomizer=random.Random(37),
    ).select(TrackSelectionService())

    assert [step.track_id for step in first.steps] == [step.track_id for step in second.steps]
    assert first.steps[0].track_id == real.id == control.id
    assert len({step.track_id for step in first.steps}) == 3
    assert tracks.calls == 3  # two snapshots plus the one real selection
    assert history.snapshot_calls == 2


def test_preview_simulates_track_artist_and_play_count_without_mutating_rules() -> None:
    tracks = _Tracks((_track(1, "A"), _track(2, "A"), _track(3, "B")))
    history = _History({}, [])
    repetition = RepetitionService(track_window_size=1, artist_window_size=1)
    rules = TrackSelectionService((repetition,))
    selector = AutomaticSelectionService(
        tracks, history, recent_track_limit=0, randomizer=random.Random(5)
    )

    preview = selector.preview(rules, 2)

    assert preview.achieved_depth == 2
    assert preview.steps[0].artist != preview.steps[1].artist
    assert preview.steps[0].track_id != preview.steps[1].track_id
    assert history.counts == {}
    assert not repetition._recent_track_ids
    assert not repetition._recent_artists


def test_preview_keeps_productive_last_state_and_builds_consistent_rationales() -> None:
    selector = AutomaticSelectionService(
        _Tracks((_track(1, "A"), _track(2, "B"))),
        _History({}, []),
        recent_track_limit=0,
        randomizer=random.Random(9),
    )
    selector.last_relaxation_stage = "SENTINEL"
    selector.last_rationale = None

    preview = selector.preview(TrackSelectionService(), 2)

    assert selector.last_relaxation_stage == "SENTINEL"
    assert selector.last_rationale is None
    assert preview.schema_version == 1
    assert preview.requested_depth == preview.achieved_depth == 2
    for position, step in enumerate(preview.steps, 1):
        assert step.position == position
        assert step.rationale.outcome is SelectionOutcome.ACCEPTED
        assert step.rationale.context_id == step.rationale.source_resolution.context.context_id
        assert step.rationale.selected_candidate.track_id == step.track_id
        assert (
            sum(
                item.decision_category is CandidateDecisionCategory.SELECTED
                for item in step.rationale.evaluated_candidates
            )
            == 1
        )
        assert "ignored-" not in repr(step.rationale)


def test_preview_stops_without_a_safe_candidate() -> None:
    class RejectAll:
        def evaluate(self, _entry, _track):
            return SelectionDecision.reject("BLOCKED_TRACK")

    selector = AutomaticSelectionService(
        _Tracks((_track(1),)), _History({}, []), recent_track_limit=0
    )

    preview = selector.preview(TrackSelectionService((RejectAll(),)), 3)

    assert preview.steps == ()
    assert preview.achieved_depth == 0
    assert preview.completion_reason is SelectionPreviewCompletion.NO_SAFE_CANDIDATE


def test_preview_exception_cannot_advance_productive_rng_or_last_state() -> None:
    class BrokenRule:
        def evaluate(self, _entry, _track):
            raise RuntimeError("preview failure")

    tracks = _Tracks((_track(1), _track(2)))
    history = _History({}, [])
    selector = AutomaticSelectionService(
        tracks, history, recent_track_limit=0, randomizer=random.Random(21)
    )
    control = AutomaticSelectionService(
        _Tracks(tracks.values),
        _History({}, []),
        recent_track_limit=0,
        randomizer=random.Random(21),
    )
    selector.last_relaxation_stage = "UNCHANGED"

    with pytest.raises(RuntimeError, match="preview failure"):
        selector.preview(TrackSelectionService((BrokenRule(),)), 2)

    assert selector.last_relaxation_stage == "UNCHANGED"
    assert selector.last_rationale is None
    assert selector.select(TrackSelectionService()).id == control.select(TrackSelectionService()).id
    assert history.counts == {}


def test_preview_never_performs_file_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_file_access(*_args, **_kwargs):
        raise AssertionError("Vorschau darf keine Dateioperation auslösen")

    monkeypatch.setattr("pathlib.Path.exists", fail_file_access)
    selector = AutomaticSelectionService(
        _Tracks((_track(1),)), _History({}, []), recent_track_limit=0
    )

    preview = selector.preview(TrackSelectionService(), 1)

    assert preview.achieved_depth == 1


def test_preview_advances_copied_persistent_repetition_history() -> None:
    class EmptyHistoryRepository:
        def recent_completed(self, _limit):
            return []

        def completed_since(self, _since):
            return []

    repetition = PersistentRepetitionService(
        EmptyHistoryRepository(),
        track_window_size=1,
        track_window_minutes=0,
        artist_window_size=1,
        artist_window_minutes=0,
    )
    selector = AutomaticSelectionService(
        _Tracks((_track(1, "A"), _track(2, "A"), _track(3, "B"))),
        _History({}, []),
        recent_track_limit=0,
        randomizer=random.Random(5),
    )

    preview = selector.preview(TrackSelectionService((repetition,)), 2)

    assert preview.achieved_depth == 2
    assert preview.steps[0].artist != preview.steps[1].artist
    assert repetition._preview_plays == []


def test_preview_advances_time_for_persistent_repetition_windows() -> None:
    class EmptyHistoryRepository:
        def recent_completed(self, _limit):
            return []

        def completed_since(self, _since):
            return []

    started_at = datetime(2026, 9, 6, 18, 0)
    repetition = PersistentRepetitionService(
        EmptyHistoryRepository(),
        track_window_size=0,
        track_window_minutes=2,
        artist_window_size=0,
        artist_window_minutes=2,
        clock=lambda: started_at,
    )
    selector = AutomaticSelectionService(
        _Tracks((_track(1, "A"), _track(2, "A"), _track(3, "B"))),
        _History({}, []),
        recent_track_limit=0,
        randomizer=random.Random(5),
    )

    preview = selector.preview(TrackSelectionService((repetition,)), 3)

    assert preview.achieved_depth == 3
    assert preview.steps[0].artist != preview.steps[1].artist
    assert preview.steps[2].artist == preview.steps[0].artist


def test_rng_snapshot_is_serialized_with_real_selection() -> None:
    class BlockingHistory(_History):
        def __init__(self) -> None:
            super().__init__({}, [])
            self.snapshot_started = Event()
            self.release_snapshot = Event()

        def preview_snapshot(self) -> _HistorySnapshot:
            self.snapshot_started.set()
            assert self.release_snapshot.wait(timeout=2)
            return super().preview_snapshot()

    tracks = _Tracks((_track(1, "A"), _track(2, "B"), _track(3, "C")))
    history = BlockingHistory()
    selector = AutomaticSelectionService(
        tracks, history, recent_track_limit=0, randomizer=random.Random(31)
    )
    preview_result = []
    real_result = []
    preview_thread = Thread(
        target=lambda: preview_result.append(selector.preview(TrackSelectionService(), 1))
    )
    real_thread = Thread(
        target=lambda: real_result.append(selector.select(TrackSelectionService()))
    )

    preview_thread.start()
    assert history.snapshot_started.wait(timeout=2)
    real_thread.start()
    assert real_result == []
    history.release_snapshot.set()
    preview_thread.join(timeout=2)
    real_thread.join(timeout=2)

    assert not preview_thread.is_alive() and not real_thread.is_alive()
    assert preview_result[0].steps[0].track_id == real_result[0].id


@pytest.mark.parametrize("depth", [0, -1, 11])
def test_preview_rejects_invalid_depth(depth: int) -> None:
    selector = AutomaticSelectionService(_Tracks((_track(1),)), _History({}, []))

    with pytest.raises(ValueError, match="zwischen 1 und 10"):
        selector.preview(TrackSelectionService(), depth)
