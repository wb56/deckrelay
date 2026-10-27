"""Enumerations used by the DeckRelay domain."""

from __future__ import annotations

from enum import StrEnum


class DeckState(StrEnum):
    EMPTY = "empty"
    LOADED = "loaded"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    FINISHED = "finished"
    ERROR = "error"


class QueueStatus(StrEnum):
    WAITING = "waiting"
    PREPARING = "preparing"
    READY = "ready"
    PLAYING = "playing"
    PLAYED = "played"
    SKIPPED = "skipped"
    FAILED = "failed"
    REMOVED = "removed"
    # Transitional aliases keep callers source-compatible while persistence
    # and user-visible state use the explicit new lifecycle names.
    LOADED = "ready"
    ERROR = "failed"


class QueueSource(StrEnum):
    MANUAL = "MANUAL"
    GUEST_REQUEST = "GUEST_REQUEST"
    AUTOMATIC = "AUTOMATIC"
    PLAYLIST = "PLAYLIST"
    EMERGENCY = "EMERGENCY"

    @property
    def default_priority(self) -> int:
        return {
            QueueSource.EMERGENCY: 999,
            QueueSource.MANUAL: 700,
            QueueSource.GUEST_REQUEST: 600,
            QueueSource.PLAYLIST: 300,
            QueueSource.AUTOMATIC: 100,
        }[self]

    @classmethod
    def normalize(cls, value: str | "QueueSource") -> "QueueSource":
        if isinstance(value, cls):
            return value
        normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
        if normalized.startswith(("saved_queue:", "directory:")):
            return cls.PLAYLIST
        aliases = {
            "manual": cls.MANUAL,
            "catalog": cls.MANUAL,
            "queue": cls.MANUAL,
            "guest": cls.GUEST_REQUEST,
            "guest_request": cls.GUEST_REQUEST,
            "request": cls.GUEST_REQUEST,
            "automatic": cls.AUTOMATIC,
            "auto": cls.AUTOMATIC,
            "playlist": cls.PLAYLIST,
            "directory": cls.PLAYLIST,
            "saved_queue": cls.PLAYLIST,
            "emergency": cls.EMERGENCY,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(f"Unbekannte Queue-Quelle: {value}") from exc


class GuestPriority(StrEnum):
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    VIP = "VIP"

    @property
    def queue_priority(self) -> int:
        return {
            GuestPriority.NORMAL: 600,
            GuestPriority.HIGH: 650,
            GuestPriority.VIP: 690,
        }[self]

    @classmethod
    def normalize(cls, value: str | "GuestPriority") -> "GuestPriority":
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().upper())
        except ValueError as exc:
            raise ValueError("Gastpriorität muss NORMAL, HIGH oder VIP sein") from exc


class ShortTrackPolicy(StrEnum):
    ALLOW = "ALLOW"
    MANUAL_ONLY = "MANUAL_ONLY"
    SKIP_AUTOMATICALLY = "SKIP_AUTOMATICALLY"
    USE_REDUCED_FADE = "USE_REDUCED_FADE"


class EmptyQueuePolicy(StrEnum):
    STOP_AFTER_CURRENT = "STOP_AFTER_CURRENT"
    AUTOMATIC_SELECTION = "AUTOMATIC_SELECTION"
    EMERGENCY_PLAYLIST = "EMERGENCY_PLAYLIST"
    REPEAT_CURRENT_PLAYLIST = "REPEAT_CURRENT_PLAYLIST"


class PlayerMode(StrEnum):
    MANUAL = "manual"
    SEMI_AUTOMATIC = "semi_automatic"
    AUTOMATIC = "automatic"


class DeckOrigin(StrEnum):
    """Persisted origin of a queue entry's deck assignment."""

    MANUAL = "MANUAL"
    AUTO = "AUTO"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    FINISHED = "finished"
    RECOVERED = "recovered"


class CompletionStatus(StrEnum):
    PLAYED = "PLAYED"
    PARTIALLY_PLAYED = "PARTIALLY_PLAYED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"

    # Backwards-compatible names for callers using the original API.
    COMPLETED = "PLAYED"
    STOPPED = "ABORTED"
    ERROR = "FAILED"


class HistoryReasonCode(StrEnum):
    OPERATOR_SKIP = "OPERATOR_SKIP"
    DECK_EJECT = "DECK_EJECT"
    DECK_STOP = "DECK_STOP"
    APPLICATION_SHUTDOWN = "APPLICATION_SHUTDOWN"
    TRACK_REPLACED = "TRACK_REPLACED"
    PLAYBACK_ERROR = "PLAYBACK_ERROR"
    UNSPECIFIED = "UNSPECIFIED"
    LEGACY_REASON = "LEGACY_REASON"
