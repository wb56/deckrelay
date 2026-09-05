"""Immutable explanations for queue, automatic and emergency source resolution."""

from dataclasses import dataclass
from enum import StrEnum

from party_player.enums import EmptyQueuePolicy, QueueSource
from party_player.models import QueueEntry
from party_player.queue_origin import entry_origin


class SelectionSourceClass(StrEnum):
    """Normalized processing class independent of persisted origin details."""

    EMERGENCY = "EMERGENCY"
    MANUAL = "MANUAL"
    GUEST_REQUEST = "GUEST_REQUEST"
    PLAYLIST = "PLAYLIST"
    AUTOMATIC = "AUTOMATIC"

    @classmethod
    def from_queue_source(cls, source: QueueSource) -> "SelectionSourceClass":
        return cls(source.value)


class SourceResolutionReason(StrEnum):
    WAITING_QUEUE_PRECEDES_AUTOMATIC = "WAITING_QUEUE_PRECEDES_AUTOMATIC"
    REPEAT_PLAYLIST_POLICY = "REPEAT_PLAYLIST_POLICY"
    AUTOMATIC_REQUIRED_EMPTY_QUEUE = "AUTOMATIC_REQUIRED_EMPTY_QUEUE"
    AUTOMATIC_EMERGENCY_FALLBACK = "AUTOMATIC_EMERGENCY_FALLBACK"
    DIRECT_EMERGENCY_POLICY = "DIRECT_EMERGENCY_POLICY"
    NO_SOURCE_AVAILABLE = "NO_SOURCE_AVAILABLE"


@dataclass(frozen=True, slots=True)
class SourceSortKey:
    name: str
    direction: str
    value: str | int


@dataclass(frozen=True, slots=True)
class SelectionSourceContext:
    context_id: str
    empty_queue_policy: EmptyQueuePolicy
    waiting_candidate_count: int
    selection_rationale_context_id: str | None = None


@dataclass(frozen=True, slots=True)
class SourceResolution:
    context: SelectionSourceContext
    selected_source: SelectionSourceClass | None
    origin_kind: str
    origin_label: str
    priority: int | None
    sort_keys: tuple[SourceSortKey, ...]
    reason: SourceResolutionReason
    deferred_sources: tuple[SelectionSourceClass, ...]
    automatic_required: bool
    emergency_fallback: bool


class SelectionSourceResolver:
    """Describe an already selected source without querying or mutating state."""

    _QUEUE_SORT_KEYS = (
        ("priority", "DESC"),
        ("position", "ASC"),
        ("added_at", "ASC"),
        ("queue_id", "ASC"),
    )

    @staticmethod
    def queue_sort_key(entry: QueueEntry) -> tuple[int, int, str, int]:
        return (
            -entry.priority,
            entry.position,
            entry.added_at.isoformat() if entry.added_at is not None else "",
            entry.queue_id,
        )

    def describe_waiting_queue(
        self,
        entry: QueueEntry,
        *,
        context_id: str,
        empty_queue_policy: EmptyQueuePolicy,
        waiting_candidate_count: int,
        guest_fairness_round: int | None,
    ) -> SourceResolution:
        sort_keys = list(self._sort_keys(entry))
        if guest_fairness_round is not None:
            sort_keys.insert(1, SourceSortKey("guest_fairness_round", "ASC", guest_fairness_round))
            sort_keys[2], sort_keys[3] = sort_keys[3], sort_keys[2]
        return SourceResolution(
            context=SelectionSourceContext(
                context_id,
                empty_queue_policy,
                waiting_candidate_count,
            ),
            selected_source=SelectionSourceClass.from_queue_source(entry.source),
            origin_kind=self._origin(entry)[0],
            origin_label=self._origin(entry)[1],
            priority=entry.priority,
            sort_keys=tuple(sort_keys),
            reason=SourceResolutionReason.WAITING_QUEUE_PRECEDES_AUTOMATIC,
            deferred_sources=self._deferred_empty_queue_sources(empty_queue_policy),
            automatic_required=False,
            emergency_fallback=False,
        )

    def describe_generated(
        self,
        entry: QueueEntry,
        *,
        context_id: str,
        rationale_context_id: str | None,
        empty_queue_policy: EmptyQueuePolicy,
        reason: SourceResolutionReason,
    ) -> SourceResolution:
        emergency_fallback = reason is SourceResolutionReason.AUTOMATIC_EMERGENCY_FALLBACK
        return SourceResolution(
            context=SelectionSourceContext(
                context_id,
                empty_queue_policy,
                0,
                rationale_context_id,
            ),
            selected_source=SelectionSourceClass.from_queue_source(entry.source),
            origin_kind=self._origin(entry)[0],
            origin_label=self._origin(entry)[1],
            priority=entry.priority,
            sort_keys=self._sort_keys(entry),
            reason=reason,
            deferred_sources=(
                (SelectionSourceClass.EMERGENCY,)
                if reason is SourceResolutionReason.AUTOMATIC_REQUIRED_EMPTY_QUEUE
                else ()
            ),
            automatic_required=reason
            in {
                SourceResolutionReason.AUTOMATIC_REQUIRED_EMPTY_QUEUE,
                SourceResolutionReason.AUTOMATIC_EMERGENCY_FALLBACK,
            },
            emergency_fallback=emergency_fallback,
        )

    @staticmethod
    def describe_unavailable(
        *,
        context_id: str,
        rationale_context_id: str | None,
        empty_queue_policy: EmptyQueuePolicy,
        automatic_required: bool,
    ) -> SourceResolution:
        return SourceResolution(
            context=SelectionSourceContext(
                context_id,
                empty_queue_policy,
                0,
                rationale_context_id,
            ),
            selected_source=None,
            origin_kind="none",
            origin_label="Keine Quelle verfügbar",
            priority=None,
            sort_keys=(),
            reason=SourceResolutionReason.NO_SOURCE_AVAILABLE,
            deferred_sources=(),
            automatic_required=automatic_required,
            emergency_fallback=False,
        )

    @classmethod
    def _sort_keys(cls, entry: QueueEntry) -> tuple[SourceSortKey, ...]:
        values: tuple[str | int, ...] = (
            entry.priority,
            entry.position,
            entry.added_at.isoformat() if entry.added_at is not None else "",
            entry.queue_id,
        )
        return tuple(
            SourceSortKey(name, direction, value)
            for (name, direction), value in zip(cls._QUEUE_SORT_KEYS, values, strict=True)
        )

    @staticmethod
    def _origin(entry: QueueEntry) -> tuple[str, str]:
        kind, name = entry_origin(entry)
        if kind == "directory":
            return kind, f"Verzeichnis · {name}" if name else "Verzeichnis"
        if kind == "playlist":
            return kind, f"Playlist · {name}" if name else "Playlist"
        if entry.source is QueueSource.GUEST_REQUEST:
            return "guest_request", "Gastwunsch"
        return kind, name

    @staticmethod
    def _deferred_empty_queue_sources(
        policy: EmptyQueuePolicy,
    ) -> tuple[SelectionSourceClass, ...]:
        if policy is EmptyQueuePolicy.AUTOMATIC_SELECTION:
            return (SelectionSourceClass.AUTOMATIC, SelectionSourceClass.EMERGENCY)
        if policy is EmptyQueuePolicy.EMERGENCY_PLAYLIST:
            return (SelectionSourceClass.EMERGENCY,)
        if policy is EmptyQueuePolicy.REPEAT_CURRENT_PLAYLIST:
            return (SelectionSourceClass.PLAYLIST,)
        return ()
