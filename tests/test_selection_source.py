"""Tests for behavior-neutral, explainable source resolution."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest

from party_player.enums import EmptyQueuePolicy, QueueSource, QueueStatus
from party_player.models import QueueEntry
from party_player.queue_service import QueueService
from party_player.selection_source import (
    SelectionSourceClass,
    SelectionSourceResolver,
    SourceResolutionReason,
)


def _entry(
    queue_id: int,
    *,
    source: QueueSource = QueueSource.MANUAL,
    priority: int | None = None,
    position: int = 1,
    added_at: datetime | None = None,
    requested_by: str = "",
    source_detail: str = "",
) -> QueueEntry:
    return QueueEntry(
        queue_id,
        queue_id,
        position,
        QueueStatus.WAITING,
        source=source,
        priority=source.default_priority if priority is None else priority,
        added_at=added_at,
        requested_by=requested_by,
        source_detail=source_detail,
    )


class QueueRepositoryStub:
    def __init__(self, entries: list[QueueEntry]) -> None:
        self.entries = entries
        self.list_calls = 0

    def list_queue(self, _session_id: int) -> list[QueueEntry]:
        self.list_calls += 1
        return list(self.entries)


def _service(
    entries: list[QueueEntry],
    *,
    empty_queue_policy: EmptyQueuePolicy = EmptyQueuePolicy.STOP_AFTER_CURRENT,
) -> tuple[QueueService, QueueRepositoryStub]:
    repository = QueueRepositoryStub(entries)
    return (
        QueueService(  # type: ignore[arg-type]
            repository,
            object(),
            1,
            empty_queue_policy=empty_queue_policy,
        ),
        repository,
    )


def test_queue_resolution_uses_existing_order_without_mutation_or_extra_reads() -> None:
    now = datetime(2026, 9, 5, 12, 0)
    expected = _entry(9, priority=700, position=1, added_at=now)
    entries = [
        _entry(2, priority=300, position=1, added_at=now),
        _entry(10, priority=700, position=1, added_at=now + timedelta(seconds=1)),
        expected,
    ]
    service, repository = _service(
        entries,
        empty_queue_policy=EmptyQueuePolicy.AUTOMATIC_SELECTION,
    )

    selected = service.get_next_candidate()

    assert selected == expected
    assert repository.list_calls == 1
    assert repository.entries == entries
    resolution = service.last_source_resolution
    assert resolution is not None
    assert resolution.selected_source is SelectionSourceClass.MANUAL
    assert resolution.reason is SourceResolutionReason.WAITING_QUEUE_PRECEDES_AUTOMATIC
    assert [key.name for key in resolution.sort_keys] == [
        "priority",
        "position",
        "added_at",
        "queue_id",
    ]
    assert resolution.deferred_sources == (
        SelectionSourceClass.AUTOMATIC,
        SelectionSourceClass.EMERGENCY,
    )
    assert not resolution.automatic_required


def test_every_queue_tie_key_is_deterministic_for_random_repository_order() -> None:
    now = datetime(2026, 9, 5, 12, 0)
    expected = _entry(1, priority=700, position=1, added_at=now)
    later_id = _entry(2, priority=700, position=1, added_at=now)
    later_time = _entry(3, priority=700, position=1, added_at=now + timedelta(seconds=1))
    later_position = _entry(4, priority=700, position=2, added_at=now - timedelta(days=1))
    lower_priority = _entry(5, priority=699, position=0, added_at=now - timedelta(days=1))

    forward, _ = _service([lower_priority, later_position, later_time, later_id, expected])
    reverse, _ = _service([expected, later_id, later_time, later_position, lower_priority])

    assert forward.get_next_candidate() == expected
    assert reverse.get_next_candidate() == expected


@pytest.mark.parametrize(
    ("detail", "expected_kind", "expected_label"),
    [
        (r"directory:G:\Music\Celebration", "directory", "Verzeichnis · Celebration"),
        ("saved_queue:Evening", "playlist", "Playlist · Evening"),
    ],
)
def test_playlist_processing_keeps_safe_exact_origin(
    detail: str,
    expected_kind: str,
    expected_label: str,
) -> None:
    entry = _entry(1, source=QueueSource.PLAYLIST, source_detail=detail)
    resolution = SelectionSourceResolver().describe_waiting_queue(
        entry,
        context_id="source-context",
        empty_queue_policy=EmptyQueuePolicy.AUTOMATIC_SELECTION,
        waiting_candidate_count=1,
        guest_fairness_round=None,
    )

    assert resolution.selected_source is SelectionSourceClass.PLAYLIST
    assert resolution.origin_kind == expected_kind
    assert resolution.origin_label == expected_label
    assert "G:\\Music" not in repr(resolution)


def test_guest_fairness_is_explained_without_requester_identity() -> None:
    entries = [
        _entry(1, source=QueueSource.GUEST_REQUEST, requested_by="Alice", position=1),
        _entry(2, source=QueueSource.GUEST_REQUEST, requested_by="alice", position=2),
        _entry(3, source=QueueSource.GUEST_REQUEST, requested_by="Bob", position=3),
    ]
    service, _repository = _service(entries)

    selected = service.get_next_candidate({1, 3})

    assert selected is not None and selected.queue_id == 2
    resolution = service.last_source_resolution
    assert resolution is not None
    assert [key.name for key in resolution.sort_keys] == [
        "priority",
        "guest_fairness_round",
        "added_at",
        "position",
        "queue_id",
    ]
    assert resolution.origin_kind == "guest_request"
    assert resolution.origin_label == "Gastwunsch"
    assert "Alice" not in repr(resolution)
    assert "Bob" not in repr(resolution)


def test_resolution_models_are_immutable() -> None:
    entry = _entry(1)
    resolution = SelectionSourceResolver().describe_waiting_queue(
        entry,
        context_id="immutable",
        empty_queue_policy=EmptyQueuePolicy.STOP_AFTER_CURRENT,
        waiting_candidate_count=1,
        guest_fairness_round=None,
    )

    with pytest.raises(FrozenInstanceError):
        resolution.priority = 1  # type: ignore[misc]


def test_normalized_source_priorities_remain_unchanged() -> None:
    assert {
        source: source.default_priority
        for source in (
            QueueSource.EMERGENCY,
            QueueSource.MANUAL,
            QueueSource.GUEST_REQUEST,
            QueueSource.PLAYLIST,
            QueueSource.AUTOMATIC,
        )
    } == {
        QueueSource.EMERGENCY: 999,
        QueueSource.MANUAL: 700,
        QueueSource.GUEST_REQUEST: 600,
        QueueSource.PLAYLIST: 300,
        QueueSource.AUTOMATIC: 100,
    }
