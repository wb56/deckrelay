"""History-backed track and artist repetition protection."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from party_player.database.connection import Database
from party_player.enums import QueueSource
from party_player.models import QueueEntry, Track
from party_player.selection_decision import RuleKind
from party_player.track_selection import SelectionDecision, normalize_artist_name


@dataclass(frozen=True, slots=True)
class RecentPlay:
    track_id: int
    artist: str
    finished_at: datetime


class RepetitionHistoryRepository:
    def __init__(
        self,
        database: Database,
        partial_playback_ratio_threshold: float = 0.5,
    ) -> None:
        self._database = database
        self._partial_playback_ratio_threshold = min(
            1.0, max(0.0, partial_playback_ratio_threshold)
        )

    def recent_completed(self, limit: int) -> list[RecentPlay]:
        if limit <= 0:
            return []
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT h.track_id, t.artist, h.finished_at
                   FROM play_history h
                   JOIN tracks t ON t.id = h.track_id
                   WHERE (h.completion_status = 'PLAYED'
                          OR (h.completion_status = 'PARTIALLY_PLAYED'
                              AND h.playback_ratio >= ?))
                     AND h.finished_at IS NOT NULL
                   ORDER BY h.finished_at DESC, h.id DESC
                   LIMIT ?""",
                (self._partial_playback_ratio_threshold, limit),
            ).fetchall()
        return [
            RecentPlay(
                int(row["track_id"]),
                str(row["artist"]),
                datetime.fromisoformat(str(row["finished_at"])),
            )
            for row in rows
        ]

    def completed_since(self, since: datetime) -> list[RecentPlay]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT h.track_id, t.artist, h.finished_at
                   FROM play_history h
                   JOIN tracks t ON t.id = h.track_id
                   WHERE (h.completion_status = 'PLAYED'
                          OR (h.completion_status = 'PARTIALLY_PLAYED'
                              AND h.playback_ratio >= ?))
                     AND h.finished_at >= ?
                   ORDER BY h.finished_at DESC, h.id DESC""",
                (self._partial_playback_ratio_threshold, since.isoformat()),
            ).fetchall()
        return [
            RecentPlay(
                int(row["track_id"]),
                str(row["artist"]),
                datetime.fromisoformat(str(row["finished_at"])),
            )
            for row in rows
        ]


class PersistentRepetitionService:
    """Apply independent count and elapsed-time windows to history."""

    rule_id = "selection.repetition"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION

    def __init__(
        self,
        repository: RepetitionHistoryRepository,
        *,
        track_window_size: int = 25,
        track_window_minutes: float = 120.0,
        artist_window_size: int = 5,
        artist_window_minutes: float = 20.0,
        guest_track_window_size: int | None = None,
        guest_track_window_minutes: float | None = None,
        guest_artist_window_size: int | None = None,
        guest_artist_window_minutes: float | None = None,
        automatic_track_window_size: int | None = None,
        automatic_track_window_minutes: float | None = None,
        automatic_artist_window_size: int | None = None,
        automatic_artist_window_minutes: float | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._repository = repository
        self.track_window_size = max(0, track_window_size)
        self.track_window = timedelta(minutes=max(0.0, track_window_minutes))
        self.artist_window_size = max(0, artist_window_size)
        self.artist_window = timedelta(minutes=max(0.0, artist_window_minutes))
        self._source_windows = {
            QueueSource.GUEST_REQUEST: (
                guest_track_window_size,
                guest_track_window_minutes,
                guest_artist_window_size,
                guest_artist_window_minutes,
            ),
            QueueSource.AUTOMATIC: (
                automatic_track_window_size,
                automatic_track_window_minutes,
                automatic_artist_window_size,
                automatic_artist_window_minutes,
            ),
        }
        self._clock = clock
        self._operator_overrides: set[int] = set()
        self.queue_artist_repetition_enabled = True
        self._logger = logging.getLogger(__name__)

    def allow_queue_entry(self, queue_id: int) -> None:
        self._operator_overrides.add(queue_id)
        self._logger.info(
            "Operator-Override für Wiederholungsschutz: queue_id=%s",
            queue_id,
        )

    def operator_override_applies(self, entry: QueueEntry) -> bool:
        return entry.queue_id in self._operator_overrides

    def evaluate(self, entry: QueueEntry, track: Track) -> SelectionDecision | None:
        if entry.queue_id in self._operator_overrides:
            return None
        source_windows = self._source_windows.get(entry.source, (None, None, None, None))
        track_window_size = max(
            self.track_window_size,
            source_windows[0] if source_windows[0] is not None else 0,
        )
        track_window = max(
            self.track_window,
            timedelta(
                minutes=max(0.0, source_windows[1]) if source_windows[1] is not None else 0.0
            ),
        )
        artist_window_size = max(
            self.artist_window_size,
            source_windows[2] if source_windows[2] is not None else 0,
        )
        artist_window = max(
            self.artist_window,
            timedelta(
                minutes=max(0.0, source_windows[3]) if source_windows[3] is not None else 0.0
            ),
        )
        maximum = max(track_window_size, artist_window_size, 1)
        recent = self._repository.recent_completed(maximum)
        now = self._clock()
        widest_window = max(track_window, artist_window)
        timed = self._repository.completed_since(now - widest_window) if widest_window else []
        track_plays = [play for play in timed if play.track_id == track.id]
        if (
            track_window_size
            and any(play.track_id == track.id for play in recent[:track_window_size])
        ) or (track_window and any(now - play.finished_at < track_window for play in track_plays)):
            return SelectionDecision.reject(
                "TRACK_REPETITION",
                reason="Titel liegt noch innerhalb des Wiederholungsschutzes",
            )
        artist = normalize_artist_name(track.artist)
        protect_artist = self.queue_artist_repetition_enabled or entry.source not in {
            QueueSource.MANUAL,
            QueueSource.PLAYLIST,
        }
        artist_plays = [play for play in timed if normalize_artist_name(play.artist) == artist]
        if protect_artist and (
            (
                artist_window_size
                and any(
                    normalize_artist_name(play.artist) == artist
                    for play in recent[:artist_window_size]
                )
            )
            or (
                artist_window
                and any(now - play.finished_at < artist_window for play in artist_plays)
            )
        ):
            return SelectionDecision.reject(
                "ARTIST_REPETITION",
                reason="Interpret liegt noch innerhalb des Wiederholungsschutzes",
            )
        return None
