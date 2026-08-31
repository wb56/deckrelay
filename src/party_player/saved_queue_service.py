"""Business rules for named reusable queues."""

from math import isfinite
from random import shuffle

from party_player.enums import QueueStatus
from party_player.cue_points import CuePointService
from party_player.models import SavedQueue, SavedQueueEntry, Track
from party_player.queue_service import QueueService
from party_player.repositories.saved_queue_repository import SavedQueueRepository


class SavedQueueService:
    def __init__(
        self,
        repository: SavedQueueRepository,
        queue: QueueService,
        cue_points: CuePointService | None = None,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._cue_points = cue_points

    def list(self) -> list[SavedQueue]:
        return self._repository.list_all()

    def get(self, saved_queue_id: int) -> SavedQueue:
        saved = self._repository.get(saved_queue_id)
        if saved is None:
            raise ValueError("Playlist nicht gefunden")
        return saved

    def save_current(self, name: str, snapshot_cues: bool = True) -> SavedQueue:
        entries: list[SavedQueueEntry] = []
        for entry in self._queue.entries():
            if entry.status not in {
                QueueStatus.WAITING,
                QueueStatus.LOADED,
                QueueStatus.PLAYING,
            }:
                continue
            cue_in = cue_out = fade_duration = None
            cue_source = "inherited"
            if snapshot_cues:
                track = self._queue.track(entry.track_id)
                if self._cue_points is not None and track is not None:
                    resolved = self._cue_points.resolve(track, queue_entry=entry)
                    cue_in = resolved.cue_in
                    cue_out = resolved.cue_out
                    fade_duration = resolved.fade_duration
                    cue_source = "snapshot"
                elif any(
                    value is not None
                    for value in (
                        entry.cue_in_override,
                        entry.cue_out_override,
                        entry.fade_duration_override,
                    )
                ):
                    cue_in = entry.cue_in_override
                    cue_out = entry.cue_out_override
                    fade_duration = entry.fade_duration_override
                    cue_source = "snapshot"
            entries.append(
                SavedQueueEntry(
                    entry.track_id,
                    entry.position,
                    cue_in,
                    cue_out,
                    fade_duration,
                    cue_source,
                )
            )
            self.validate_snapshot(entries[-1], self._queue.track(entry.track_id))
        return self._repository.save(name, entries)

    def load(
        self,
        saved_queue_id: int,
        replace_waiting: bool,
        shuffle_tracks: bool = False,
        use_saved_cues: bool = True,
    ) -> tuple[int, int]:
        saved = self.get(saved_queue_id)
        if use_saved_cues:
            for entry in saved.entries:
                track = self._queue.track(entry.track_id)
                if track is None:
                    raise ValueError(
                        f"Gespeicherte Queue enthält einen unbekannten Titel "
                        f"(Track-ID {entry.track_id})"
                    )
                self.validate_snapshot(entry, track)
        if replace_waiting:
            self._queue.clear_waiting()
        entries = list(saved.entries)
        if shuffle_tracks:
            shuffle(entries)
        return self._queue.add_many(
            entries,
            source=f"saved_queue:{saved.name}",
            use_saved_cues=use_saved_cues,
        )

    @staticmethod
    def validate_snapshot(entry: SavedQueueEntry, track: Track | None) -> None:
        """Reject unsafe persisted cue combinations before they reach the active queue."""
        values = (entry.cue_in, entry.cue_out, entry.fade_duration)
        if all(value is None for value in values):
            return
        if track is None:
            raise ValueError(
                f"Cue-Snapshot verweist auf einen unbekannten Titel (Track-ID {entry.track_id})"
            )
        if any(value is not None and not isfinite(float(value)) for value in values):
            raise ValueError(f"Ungültiger Cue-Snapshot für „{track.title}“")

        duration = float(track.duration_seconds or 0.0)
        cue_in = float(entry.cue_in) if entry.cue_in is not None else 0.0
        cue_out = (
            float(entry.cue_out)
            if entry.cue_out is not None
            else (duration if duration > 0 else None)
        )
        fade = float(entry.fade_duration) if entry.fade_duration is not None else None

        if cue_in < 0:
            raise ValueError(f"Cue In für „{track.title}“ darf nicht negativ sein")
        if duration > 0 and cue_in >= duration:
            raise ValueError(f"Cue In für „{track.title}“ liegt außerhalb der Titeldauer")
        if cue_out is not None and cue_out <= cue_in:
            raise ValueError(f"Cue Out für „{track.title}“ muss nach Cue In liegen")
        if duration > 0 and cue_out is not None and cue_out > duration + 0.25:
            raise ValueError(f"Cue Out für „{track.title}“ liegt außerhalb der Titeldauer")
        if fade is not None:
            if fade <= 0:
                raise ValueError(f"Überblenddauer für „{track.title}“ muss positiv sein")
            if cue_out is not None and fade >= cue_out - cue_in:
                raise ValueError(
                    f"Überblenddauer für „{track.title}“ ist für die nutzbare Dauer zu lang"
                )
