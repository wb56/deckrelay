"""Application data models."""

from dataclasses import dataclass, field
from datetime import datetime
from party_player.enums import DeckState, QueueSource, QueueStatus, SessionStatus


@dataclass(frozen=True, slots=True)
class Track:
    """A read-only music catalog entry."""

    id: int
    file_path: str
    title: str
    artist: str
    album: str
    duration_seconds: float | None
    genre: str = ""
    year: int | None = None
    original_release_year: int | None = None
    bpm: float | None = None


@dataclass(slots=True)
class Deck:
    """State owned by one independently controlled deck."""

    deck_id: str
    loaded_track: Track | None = None
    state: DeckState = DeckState.EMPTY
    volume: float = 1.0
    position: float = 0.0
    duration: float = 0.0
    error_message: str = ""
    is_on_air: bool = False
    cue_in: float = 0.0
    cue_out: float = 0.0
    cue_fade_duration: float = 0.0
    cue_in_source: str = "FILE_BOUNDARY"
    cue_out_source: str = "FILE_BOUNDARY"
    cue_fade_source: str = "GLOBAL"
    cue_warning: str = ""
    automatic_crossfade_allowed: bool = True
    cue_boundaries_ready: bool = False
    loudness_requested_gain_db: float = 0.0
    loudness_effective_gain_db: float = 0.0
    loudness_source: str = "NONE"
    loudness_peak_limited: bool = False
    equalizer_preset_name: str = "Aus"
    equalizer_source: str = "DISABLED"
    equalizer_preamp_db: float = 0.0
    equalizer_band_count: int = 0
    equalizer_applied: bool = False
    equalizer_error: str = ""
    backend_state: str = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class QueueEntry:
    queue_id: int
    track_id: int
    position: int
    status: QueueStatus
    source: QueueSource = QueueSource.MANUAL
    requested_by: str = ""
    added_at: datetime | None = None
    loaded_deck: str | None = None
    played_at: datetime | None = None
    skip_reason: str | None = None
    cue_in_override: float | None = None
    cue_out_override: float | None = None
    fade_duration_override: float | None = None
    cue_override_source: str = "inherited"
    priority: int = 0
    locked: bool = False
    request_count: int = 0
    lock_source: str = "NONE"
    unique_requester_count: int = 0
    last_requested_at: datetime | None = None
    updated_at: datetime | None = None
    preparation_attempts: int = 0
    failure_code: str | None = None
    skip_code: str | None = None
    source_detail: str = ""

    @property
    def has_cue_overrides(self) -> bool:
        return self.cue_override_source in {"queue", "snapshot"} and any(
            value is not None
            for value in (
                self.cue_in_override,
                self.cue_out_override,
                self.fade_duration_override,
            )
        )


@dataclass(frozen=True, slots=True)
class PartySession:
    session_id: int
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    selected_playlist: int | None = None
    settings_snapshot: str = "{}"


@dataclass(slots=True)
class PartySettings:
    audio_backend: str = "vlc"
    default_master_volume: float = 0.8
    default_deck_volume: float = 1.0
    default_crossfader_position: float = 0.5
    fade_duration: float = 5.0
    fade_out_stops_deck: bool = False
    automatic_deck_loading: bool = True
    player_mode: str = "semi_automatic"
    fullscreen_on_start: bool = False
    restore_last_session: bool = True
    queue_duplicate_policy: str = "allow"
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SavedQueueEntry:
    track_id: int
    position: int
    cue_in: float | None = None
    cue_out: float | None = None
    fade_duration: float | None = None
    cue_source: str = "inherited"
    saved_queue_entry_id: int | None = None


@dataclass(frozen=True, slots=True)
class SavedQueue:
    saved_queue_id: int
    name: str
    entries: tuple[SavedQueueEntry, ...] = ()
    equalizer_preset_id: int | None = None

    @property
    def track_ids(self) -> tuple[int, ...]:
        return tuple(entry.track_id for entry in self.entries)


@dataclass(frozen=True, slots=True)
class QueueStats:
    total_tracks: int
    total_duration: float
    remaining_tracks: int
    remaining_duration: float
