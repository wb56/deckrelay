"""Queue ordering and automatic free-deck loading."""

import logging
import random
from contextlib import nullcontext
from datetime import datetime, timedelta
from collections.abc import Callable

from party_player.deck_controller import DeckController
from party_player.cue_points import CuePointService, ResolvedTrackBoundaries
from party_player.enums import EmptyQueuePolicy, GuestPriority, QueueSource, QueueStatus
from party_player.models import QueueEntry, SavedQueueEntry, Track
from party_player.repositories.track_repository import TrackRepository
from party_player.repository import PartyPlayerRepository
from party_player.track_selection import SelectionDecision, TrackSelectionService
from party_player.file_availability import FileAvailabilityChecker, FileAvailabilityService
from party_player.automatic_selection import AutomaticSelectionService
from party_player.structured_logging import log_queue_event
from party_player.performance_monitor import PerformanceMonitor


class QueueService:
    _MANUAL_LOCK_SOURCES = frozenset({"MANUAL", "MANUAL_SYSTEM"})
    _SYSTEM_LOCK_SOURCES = frozenset({"SYSTEM", "MANUAL_SYSTEM"})
    _ALLOWED_TRANSITIONS: dict[QueueStatus, frozenset[QueueStatus]] = {
        QueueStatus.WAITING: frozenset(
            {
                QueueStatus.PREPARING,
                QueueStatus.READY,
                QueueStatus.PLAYED,
                QueueStatus.SKIPPED,
                QueueStatus.FAILED,
                QueueStatus.REMOVED,
            }
        ),
        QueueStatus.PREPARING: frozenset(
            {
                QueueStatus.READY,
                QueueStatus.WAITING,
                QueueStatus.SKIPPED,
                QueueStatus.FAILED,
                QueueStatus.REMOVED,
            }
        ),
        QueueStatus.READY: frozenset(
            {
                QueueStatus.PLAYING,
                QueueStatus.WAITING,
                QueueStatus.SKIPPED,
                QueueStatus.FAILED,
                QueueStatus.REMOVED,
            }
        ),
        QueueStatus.PLAYING: frozenset(
            {
                QueueStatus.READY,
                QueueStatus.PLAYED,
                QueueStatus.SKIPPED,
                QueueStatus.FAILED,
                QueueStatus.REMOVED,
            }
        ),
        QueueStatus.PLAYED: frozenset(
            {QueueStatus.PLAYING, QueueStatus.WAITING, QueueStatus.REMOVED}
        ),
        QueueStatus.SKIPPED: frozenset({QueueStatus.WAITING, QueueStatus.REMOVED}),
        QueueStatus.FAILED: frozenset({QueueStatus.WAITING, QueueStatus.REMOVED}),
        QueueStatus.REMOVED: frozenset(),
    }

    @staticmethod
    def recover_persisted_session(
        repository: PartyPlayerRepository,
        session_id: int,
    ) -> None:
        """Run restart recovery through the queue mutation boundary."""
        repository.recover_queue_after_restart(session_id)

    @staticmethod
    def copy_persisted_pending_queue(
        repository: PartyPlayerRepository,
        source_session_id: int,
        target_session_id: int,
    ) -> int:
        """Copy pending entries as one repository transaction."""
        return repository.copy_pending_queue(source_session_id, target_session_id)

    def __init__(
        self,
        repository: PartyPlayerRepository,
        tracks: TrackRepository,
        session_id: int,
        allow_duplicates: bool = True,
        cue_points: CuePointService | None = None,
        selection_service: TrackSelectionService | None = None,
        file_availability: FileAvailabilityChecker | None = None,
        guest_duplicate_policy: str = "merge",
        guest_recent_minutes: float = 120.0,
        maximum_active_guest_requests: int = 3,
        minimum_guest_request_interval_seconds: float = 0.0,
        maximum_consecutive_guest_tracks: int = 2,
        guest_popularity_enabled: bool = False,
        guest_popularity_points_per_request: int = 1,
        wall_clock: Callable[[], datetime] = datetime.now,
        empty_queue_policy: EmptyQueuePolicy = EmptyQueuePolicy.STOP_AFTER_CURRENT,
        automatic_selection: AutomaticSelectionService | None = None,
        repeat_playlist_entries: Callable[[], list[SavedQueueEntry]] | None = None,
    ) -> None:
        self._repository = repository
        self._tracks = tracks
        self.session_id = session_id
        self.allow_duplicates = allow_duplicates
        self._cue_points = cue_points
        self._selection_service = selection_service or TrackSelectionService()
        self._file_availability = file_availability or FileAvailabilityService()
        if guest_duplicate_policy not in {"reject", "merge"}:
            raise ValueError("Gastwunsch-Duplikatregel muss reject oder merge sein")
        self.guest_duplicate_policy = guest_duplicate_policy
        self.guest_recent_minutes = max(0.0, guest_recent_minutes)
        self.maximum_active_guest_requests = max(1, maximum_active_guest_requests)
        self.minimum_guest_request_interval = timedelta(
            seconds=max(0.0, minimum_guest_request_interval_seconds)
        )
        self.maximum_consecutive_guest_tracks = max(1, maximum_consecutive_guest_tracks)
        self.guest_popularity_enabled = guest_popularity_enabled
        self.guest_popularity_points_per_request = max(0, guest_popularity_points_per_request)
        self._wall_clock = wall_clock
        self.empty_queue_policy = empty_queue_policy
        self._automatic_selection = automatic_selection
        self._repeat_playlist_entries = repeat_playlist_entries
        self._logger = logging.getLogger(__name__)
        self._performance: PerformanceMonitor | None = None

    def close_cached_connection(self) -> bool:
        """Close the repository cache owned by the calling persistence worker."""
        return self._repository.close_cached_connection()

    def set_performance_monitor(self, performance: PerformanceMonitor) -> None:
        """Share the controller monitor for detailed queue-operation timings."""
        self._performance = performance

    def add(
        self,
        track_id: int,
        source: QueueSource | str = QueueSource.MANUAL,
        requested_by: str = "",
        *,
        cue_in_override: float | None = None,
        cue_out_override: float | None = None,
        fade_duration_override: float | None = None,
        cue_override_source: str = "inherited",
        priority: int | None = None,
    ) -> QueueEntry:
        if self._tracks.get(track_id) is None:
            raise ValueError("Titel nicht gefunden")
        if not self.allow_duplicates and self._repository.has_active_track(
            self.session_id, track_id
        ):
            raise ValueError("Dieser Titel befindet sich bereits in der aktiven Queue")
        entry = self._repository.add_queue_entry(
            self.session_id,
            track_id,
            source,
            requested_by,
            cue_in_override=cue_in_override,
            cue_out_override=cue_out_override,
            fade_duration_override=fade_duration_override,
            cue_override_source=cue_override_source,
            priority=priority,
        )
        self._log_event("QUEUE_ADDED", entry, "ADDED")
        return entry

    def add_many(
        self,
        entries: list[SavedQueueEntry],
        *,
        source: str,
        use_saved_cues: bool = True,
    ) -> tuple[int, int]:
        """Add a saved-queue snapshot using one database transaction."""
        added = 0
        skipped = 0
        with self._repository.transaction():
            for entry in entries:
                try:
                    self.add(
                        entry.track_id,
                        source=source,
                        cue_in_override=entry.cue_in if use_saved_cues else None,
                        cue_out_override=entry.cue_out if use_saved_cues else None,
                        fade_duration_override=(entry.fade_duration if use_saved_cues else None),
                        cue_override_source=(entry.cue_source if use_saved_cues else "inherited"),
                    )
                    added += 1
                except ValueError as exc:
                    if "bereits in der aktiven Queue" in str(exc):
                        skipped += 1
                    else:
                        raise
        return added, skipped

    def record_audit_event(
        self,
        event_code: str,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self._repository.record_session_event(
            self.session_id,
            event_code,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )

    def add_guest_request(
        self,
        track_id: int,
        requester: str = "",
        *,
        duplicate_policy: str | None = None,
        guest_priority: GuestPriority | str = GuestPriority.NORMAL,
    ) -> QueueEntry:
        policy = duplicate_policy or self.guest_duplicate_policy
        if policy not in {"reject", "merge"}:
            raise ValueError("Gastwunsch-Duplikatregel muss reject oder merge sein")
        if self._tracks.get(track_id) is None:
            raise ValueError("Titel nicht gefunden")
        normalized_priority = GuestPriority.normalize(guest_priority)
        requester_key = " ".join(requester.casefold().split())
        existing = self._repository.active_track_entry(self.session_id, track_id)
        if requester_key:
            last_request = self._repository.last_guest_request_at(self.session_id, requester_key)
            if (
                last_request is not None
                and self._wall_clock() - last_request < self.minimum_guest_request_interval
            ):
                raise ValueError("Zwischen zwei Gastwünschen ist mehr Abstand erforderlich")
            if (
                existing is None
                and self._repository.active_guest_request_count(self.session_id, requester_key)
                >= self.maximum_active_guest_requests
            ):
                raise ValueError("Maximale Anzahl aktiver Gastwünsche erreicht")
            if (
                existing is None
                and self._repository.consecutive_guest_plays(
                    requester_key, self.maximum_consecutive_guest_tracks
                )
                >= self.maximum_consecutive_guest_tracks
            ):
                raise ValueError("Zu viele aufeinanderfolgende Titel dieses Gasts")
        if existing is not None:
            if policy == "reject":
                raise ValueError("Dieser Gastwunsch ist bereits aktiv")
            merged = self._repository.register_guest_request(
                existing.queue_id, requester, self._wall_clock()
            )
            requested_priority = normalized_priority.queue_priority
            if self.guest_popularity_enabled and merged.source is QueueSource.GUEST_REQUEST:
                requested_priority = min(
                    QueueSource.MANUAL.default_priority - 1,
                    max(merged.priority, requested_priority)
                    + self.guest_popularity_points_per_request,
                )
            if requested_priority > merged.priority:
                merged = self.update_metadata(
                    merged.queue_id,
                    priority=requested_priority,
                    locked=merged.locked,
                    request_count=merged.request_count,
                    lock_source=merged.lock_source,
                )
            return merged
        if self.guest_recent_minutes and self._repository.was_track_completed_since(
            track_id,
            datetime.now() - timedelta(minutes=self.guest_recent_minutes),
        ):
            raise ValueError("Dieser Titel wurde vor Kurzem bereits gespielt")
        entry = self.add(
            track_id,
            source=QueueSource.GUEST_REQUEST,
            requested_by=requester.strip(),
            priority=normalized_priority.queue_priority,
        )
        return self._repository.register_guest_request(
            entry.queue_id, requester, self._wall_clock()
        )

    def set_cue_overrides(
        self,
        queue_id: int,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
        source: str = "queue",
    ) -> QueueEntry:
        self._repository.set_queue_cue_overrides(queue_id, cue_in, cue_out, fade_duration, source)
        entry = self.entry(queue_id)
        assert entry is not None
        return entry

    def entries(self) -> list[QueueEntry]:
        return self._repository.list_queue(self.session_id)

    @property
    def automatic_selection_stage(self) -> str:
        if self._automatic_selection is None:
            return "NONE"
        return self._automatic_selection.last_relaxation_stage

    def entry(self, queue_id: int) -> QueueEntry | None:
        return self._repository.get_queue_entry(queue_id)

    def update_metadata(
        self,
        queue_id: int,
        *,
        priority: int,
        locked: bool,
        request_count: int,
        lock_source: str | None = None,
    ) -> QueueEntry:
        if isinstance(priority, bool) or not 0 <= priority <= 999:
            raise ValueError("Queue-Priorität muss zwischen 0 und 999 liegen")
        self._repository.update_queue_metadata(
            queue_id,
            priority=priority,
            locked=locked,
            request_count=request_count,
            lock_source=lock_source,
        )
        entry = self.entry(queue_id)
        assert entry is not None
        return entry

    def track(self, track_id: int) -> Track | None:
        return self._tracks.get(track_id)

    def remove(self, queue_id: int) -> None:
        entry = self.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        if entry.status == QueueStatus.PLAYING:
            raise ValueError("Ein spielender Queue-Eintrag kann nicht bearbeitet werden")
        if entry.status in {QueueStatus.PREPARING, QueueStatus.READY}:
            raise ValueError(
                "Nur wartende oder abgeschlossene Queue-Einträge können direkt "
                "entfernt werden; vorbereitete Titel müssen zuerst vom Deck entladen werden"
            )
        if entry.status == QueueStatus.WAITING and entry.locked:
            raise ValueError("Der Queue-Eintrag ist gesperrt")
        self._transition(
            queue_id,
            QueueStatus.REMOVED,
            expected_status=entry.status,
        )
        self._normalize_positions()

    def remove_prepared(self, queue_id: int) -> None:
        entry = self.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        if entry.status not in {QueueStatus.PREPARING, QueueStatus.READY}:
            raise ValueError("Nur vorbereitete Queue-Einträge können so entfernt werden")
        self._transition(queue_id, QueueStatus.REMOVED)
        self._normalize_positions()

    def reset_prepared(self, queue_id: int) -> None:
        entry = self.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        if entry.status not in {QueueStatus.PREPARING, QueueStatus.READY}:
            raise ValueError("Queue-Eintrag ist nicht vorbereitet")
        self._validate_transition(queue_id, QueueStatus.WAITING)
        self._repository.reset_queue_entry_to_waiting(queue_id)
        self._set_lock_components(queue_id, system=False)

    def move_to_top(self, queue_id: int) -> None:
        self._require_freely_waiting(queue_id)
        entries = self.entries()
        selected = next((entry for entry in entries if entry.queue_id == queue_id), None)
        if selected is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        ordered = [selected.queue_id] + [
            entry.queue_id for entry in entries if entry.queue_id != queue_id
        ]
        self._repository.set_queue_positions(self.session_id, ordered)

    def move_to_end(self, queue_id: int) -> None:
        self._require_freely_waiting(queue_id)
        entries = self.entries()
        selected = next((entry for entry in entries if entry.queue_id == queue_id), None)
        if selected is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        ordered = [entry.queue_id for entry in entries if entry.queue_id != queue_id] + [
            selected.queue_id
        ]
        self._repository.set_queue_positions(self.session_id, ordered)

    def set_priority(self, queue_id: int, priority: int) -> QueueEntry:
        entry = self.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        return self.update_metadata(
            queue_id,
            priority=priority,
            locked=entry.locked,
            request_count=entry.request_count,
            lock_source=entry.lock_source,
        )

    def toggle_lock(self, queue_id: int) -> QueueEntry:
        entry = self.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        return self._set_lock_components(
            queue_id,
            manual=entry.lock_source not in self._MANUAL_LOCK_SOURCES,
        )

    def clear_waiting(self) -> int:
        """Remove all unlocked waiting entries in one repository transaction."""
        return self._repository.clear_waiting_queue(self.session_id)

    def clear_complete(self) -> int:
        """Remove all non-playing entries in one atomic repository transaction."""
        return self._repository.clear_complete_queue(self.session_id)

    def shuffle_waiting(self, randomizer: random.Random | None = None) -> int:
        """Shuffle waiting entries while leaving all other queue slots untouched."""
        entries = self.entries()
        waiting_ids = [
            entry.queue_id
            for entry in entries
            if entry.status == QueueStatus.WAITING and not entry.locked
        ]
        if len(waiting_ids) < 2:
            return len(waiting_ids)
        original_order = waiting_ids.copy()
        (randomizer or random.Random()).shuffle(waiting_ids)
        if waiting_ids == original_order:
            waiting_ids = waiting_ids[1:] + waiting_ids[:1]
        waiting_iterator = iter(waiting_ids)
        reordered = [
            (
                next(waiting_iterator)
                if entry.status == QueueStatus.WAITING and not entry.locked
                else entry.queue_id
            )
            for entry in entries
        ]
        self._repository.set_queue_positions(self.session_id, reordered)
        return len(waiting_ids)

    def mark_played(self, queue_id: int) -> None:
        self._transition(queue_id, QueueStatus.PLAYED)

    def mark_skipped(
        self,
        queue_id: int,
        reason: str | None = None,
        *,
        code: str = "OPERATOR_SKIPPED",
    ) -> None:
        self._transition(
            queue_id,
            QueueStatus.SKIPPED,
            skip_reason=reason or None,
            skip_code=code,
        )
        entry = self.entry(queue_id)
        if entry is not None:
            self._repository.record_session_event(
                self.session_id,
                "QUEUE_SKIPPED",
                entity_type="QUEUE",
                entity_id=queue_id,
                details={
                    "track_id": entry.track_id,
                    "source": entry.source.value,
                    "reason": reason or "",
                    "skip_code": code,
                },
            )

    def retry(self, queue_id: int) -> None:
        self._validate_transition(queue_id, QueueStatus.WAITING)
        self._repository.retry_queue_entry(queue_id)
        self._set_lock_components(queue_id, system=False)

    def restore_artist_repetition_skips(self) -> int:
        return self._repository.restore_artist_repetition_skips(self.session_id)

    def override_repetition_skip(self, queue_id: int) -> None:
        self._repository.override_repetition_skip(queue_id)

    def mark_error(self, queue_id: int, code: str = "PREPARATION_FAILED") -> None:
        self._transition(queue_id, QueueStatus.FAILED, failure_code=code)

    def reset_played(self, queue_id: int) -> None:
        self._validate_transition(queue_id, QueueStatus.WAITING)
        self._repository.reset_played_queue_entry_to_waiting(queue_id)
        self._set_lock_components(queue_id, system=False)

    def mark_loaded(self, queue_id: int, deck_id: str) -> None:
        self._transition(queue_id, QueueStatus.READY, loaded_deck=deck_id)

    def mark_preparing(self, queue_id: int, deck_id: str | None = None) -> None:
        self._transition(
            queue_id,
            QueueStatus.PREPARING,
            loaded_deck=deck_id,
        )

    def mark_playing(self, queue_id: int) -> None:
        """Mark the queue entry explicitly assigned to a deck as playing."""
        repository_measure = (
            self._performance.measure(
                "queue_service.mark_playing.repository",
                warning_threshold_ms=10.0,
            )
            if self._performance is not None
            else nullcontext()
        )
        with repository_measure:
            updated = self._repository.mark_queue_playing(queue_id)
        log_measure = (
            self._performance.measure(
                "queue_service.mark_playing.log",
                warning_threshold_ms=10.0,
            )
            if self._performance is not None
            else nullcontext()
        )
        with log_measure:
            self._log_event("QUEUE_PLAYING", updated, "PLAYING")

    def mark_finished(self, queue_id: int, status: QueueStatus) -> None:
        """Finish one known queue entry without touching other deck assignments."""
        if status not in {QueueStatus.PLAYED, QueueStatus.SKIPPED}:
            raise ValueError("Ungültiger Queue-Endstatus")
        entry = self.entry(queue_id)
        if entry is None or entry.status == status:
            return
        if entry.status != QueueStatus.PLAYING:
            logging.getLogger(__name__).info(
                "Veralteter Queue-Abschluss für Eintrag %s verworfen: %s → %s",
                queue_id,
                entry.status.value,
                status.value,
            )
            return
        self._transition(queue_id, status, expected_status=QueueStatus.PLAYING)

    def mark_playing_for_deck(self, deck_id: str, track_id: int) -> int | None:
        entries = self.entries()
        entry = next(
            (
                item
                for item in entries
                if item.loaded_deck == deck_id
                and item.track_id == track_id
                and item.status in {QueueStatus.READY, QueueStatus.PLAYING}
            ),
            None,
        )
        if entry is None:
            replay_candidates = [
                item
                for item in entries
                if item.loaded_deck == deck_id
                and item.track_id == track_id
                and item.status == QueueStatus.PLAYED
            ]
            entry = max(
                replay_candidates,
                key=lambda item: item.played_at or item.added_at or datetime.min,
                default=None,
            )
        if entry is None:
            return None
        self._transition(entry.queue_id, QueueStatus.PLAYING)
        return entry.queue_id

    def mark_finished_for_deck(
        self,
        deck_id: str,
        status: QueueStatus,
        *,
        unplayed_skip_reason: str = "Deck beendet, bevor der Titel gestartet wurde",
    ) -> None:
        """Finish a deck assignment without claiming an unstarted track was played.

        A READY entry has only been preloaded.  Mapping it directly to PLAYED would
        violate the queue state machine and falsify playback history.  When callers
        finish a deck as PLAYED, only an actually PLAYING entry receives that state;
        READY entries are closed as SKIPPED instead.
        """
        entries = [
            item
            for item in self.entries()
            if item.loaded_deck == deck_id
            and item.status in {QueueStatus.READY, QueueStatus.PLAYING}
        ]
        for entry in entries:
            target = (
                QueueStatus.SKIPPED
                if status == QueueStatus.PLAYED and entry.status == QueueStatus.READY
                else status
            )
            self._transition(
                entry.queue_id,
                target,
                skip_reason=unplayed_skip_reason if target == QueueStatus.SKIPPED else None,
            )

    def release_deck_assignments(self, deck_id: str, except_queue_id: int | None = None) -> None:
        """Close stale assignments before another track occupies a deck."""
        for entry in self.entries():
            if (
                entry.loaded_deck == deck_id
                and entry.queue_id != except_queue_id
                and entry.status in {QueueStatus.READY, QueueStatus.PLAYING}
            ):
                self._transition(entry.queue_id, QueueStatus.SKIPPED, skip_reason="Deck neu belegt")

    def release_playing_deck_assignment(self, queue_id: int, deck_id: str) -> bool:
        return self._repository.release_queue_deck_assignment(
            queue_id,
            QueueStatus.PLAYING,
            deck_id,
        )

    def reconcile_deck_assignments(self, deck_a: DeckController, deck_b: DeckController) -> bool:
        """Reset persisted assignments that are not represented by the actual decks."""
        changed = False
        for deck in (deck_a, deck_b):
            matching_kept = False
            track_id = deck.model.loaded_track.id if deck.model.loaded_track else None
            for entry in self.entries():
                if entry.loaded_deck != deck.model.deck_id or entry.status not in {
                    QueueStatus.READY,
                    QueueStatus.PLAYING,
                }:
                    continue
                if not matching_kept and track_id is not None and entry.track_id == track_id:
                    matching_kept = True
                    continue
                # Transition completion detaches the outgoing deck before its
                # PLAYING entry is persisted as PLAYED.  A concurrent preload
                # reconciliation must not mistake that short-lived state for an
                # orphaned preload or attempt the forbidden PLAYING -> WAITING
                # transition.
                if entry.status == QueueStatus.PLAYING:
                    continue
                self._validate_transition(entry.queue_id, QueueStatus.WAITING)
                self._repository.reset_queue_entry_to_waiting(entry.queue_id)
                self._set_lock_components(entry.queue_id, system=False)
                changed = True
        self._normalize_positions()
        return changed

    def move_up(self, queue_id: int) -> None:
        self._require_freely_waiting(queue_id)
        entries = self.entries()
        index = next(
            (index for index, entry in enumerate(entries) if entry.queue_id == queue_id), None
        )
        if index is not None and index > 0:
            self._repository.swap_positions(entries[index - 1].queue_id, queue_id)
            self._normalize_positions()

    def move_down(self, queue_id: int) -> None:
        self._require_freely_waiting(queue_id)
        entries = self.entries()
        index = next(
            (index for index, entry in enumerate(entries) if entry.queue_id == queue_id), None
        )
        if index is not None and index < len(entries) - 1:
            self._repository.swap_positions(queue_id, entries[index + 1].queue_id)
            self._normalize_positions()

    def load_next_into_free_deck(
        self,
        deck_a: DeckController,
        deck_b: DeckController,
        excluded_decks: set[str] | None = None,
        *,
        allow_empty_queue_selection: bool = True,
    ) -> tuple[QueueEntry, DeckController] | None:
        while candidate := self.next_load_candidate(
            deck_a,
            deck_b,
            excluded_decks,
            allow_empty_queue_selection=allow_empty_queue_selection,
        ):
            waiting, free_deck, _track = candidate
            track, decision = self.revalidate_candidate(waiting.queue_id)
            if not decision.accepted or track is None:
                self.reject_candidate(waiting.queue_id, decision)
                continue
            self.mark_preparing(waiting.queue_id, free_deck.model.deck_id)
            try:
                free_deck.load(track)
                self.apply_cues_to_deck(waiting, free_deck)
            except (OSError, ValueError, RuntimeError):
                self.mark_error(waiting.queue_id)
                continue
            self._transition(
                waiting.queue_id, QueueStatus.READY, loaded_deck=free_deck.model.deck_id
            )
            updated = self._repository.get_queue_entry(waiting.queue_id)
            assert updated is not None
            return updated, free_deck
        return None

    def apply_cues_to_deck(
        self, entry: QueueEntry, deck: DeckController
    ) -> ResolvedTrackBoundaries | None:
        track = deck.model.loaded_track
        if self._cue_points is None or track is None:
            return None
        boundaries = self._cue_points.resolve(track, queue_entry=entry)
        model = deck.model
        model.cue_in = boundaries.cue_in
        model.cue_out = boundaries.cue_out
        model.cue_fade_duration = boundaries.fade_duration
        model.cue_in_source = boundaries.cue_in_source
        model.cue_out_source = boundaries.cue_out_source
        model.cue_fade_source = boundaries.fade_source
        model.cue_warning = boundaries.warning
        model.automatic_crossfade_allowed = boundaries.automatic_crossfade_allowed
        model.cue_boundaries_ready = True
        return boundaries

    def next_load_candidate(
        self,
        deck_a: DeckController,
        deck_b: DeckController,
        excluded_decks: set[str] | None = None,
        *,
        allow_empty_queue_selection: bool = True,
    ) -> tuple[QueueEntry, DeckController, Track] | None:
        excluded = excluded_decks or set()
        free_deck = next(
            (
                deck
                for deck in (deck_a, deck_b)
                if not deck.backend.is_playing()
                and not deck.is_fading
                and deck.model.loaded_track is None
                and deck.model.deck_id not in excluded
            ),
            None,
        )
        if free_deck is None:
            return None
        examined: set[int] = set()
        while waiting := self.get_next_candidate(
            examined,
            allow_empty_queue_selection=allow_empty_queue_selection,
        ):
            examined.add(waiting.queue_id)
            track = self._tracks.get_active(waiting.track_id)
            decision = self._selection_service.evaluate(waiting, track)
            if not decision.accepted:
                self.reject_candidate(waiting.queue_id, decision)
                continue
            assert track is not None
            return waiting, free_deck, track
        return None

    def candidate_availability(
        self,
        track: Track,
        cancelled: Callable[[], bool] | None = None,
    ) -> SelectionDecision:
        """Run replaceable file I/O checks; callers choose an appropriate worker."""
        if cancelled is not None and cancelled():
            return SelectionDecision.reject("CANDIDATE_CANCELLED")
        if isinstance(self._file_availability, FileAvailabilityService):
            return self._file_availability.evaluate(
                track,
                cancelled=cancelled or (lambda: False),
            )
        return self._file_availability.evaluate(track)

    def preview_candidate_decision(self, entry: QueueEntry) -> SelectionDecision:
        """Evaluate stable business rules for a start preview without mutating the queue."""
        return self._selection_service.evaluate(entry, self._tracks.get_active(entry.track_id))

    def preview_candidate_decisions(
        self,
        entries: list[QueueEntry],
    ) -> dict[int, tuple[Track | None, SelectionDecision]]:
        """Evaluate a complete start preview on one shared SQLite connection."""

        results: dict[int, tuple[Track | None, SelectionDecision]] = {}
        with self._repository.transaction():
            for entry in entries:
                track = self._tracks.get_active(entry.track_id)
                results[entry.queue_id] = (
                    track,
                    (
                        SelectionDecision.allow()
                        if entry.status is QueueStatus.READY
                        else self._selection_service.evaluate(entry, track)
                    ),
                )
        return results

    def revalidate_candidate(
        self,
        queue_id: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[Track | None, SelectionDecision]:
        """Reload and fully validate one waiting candidate immediately before preparation."""
        if cancelled is not None and cancelled():
            return None, SelectionDecision.reject("CANDIDATE_CANCELLED")
        entry = self._repository.get_queue_entry(queue_id)
        if entry is None or entry.status is not QueueStatus.WAITING:
            decision = SelectionDecision.reject(
                "CANDIDATE_STATE_CHANGED",
                reason="Der Queue-Zustand hat sich geändert",
            )
            return None, decision
        track = self._tracks.get_active(entry.track_id)
        if cancelled is not None and cancelled():
            return None, SelectionDecision.reject("CANDIDATE_CANCELLED")
        decision = self._selection_service.evaluate(entry, track)
        if decision.accepted:
            assert track is not None
            decision = self.candidate_availability(track, cancelled)
        if cancelled is not None and cancelled():
            return None, SelectionDecision.reject("CANDIDATE_CANCELLED")
        self.record_audit_event(
            "CANDIDATE_REVALIDATED",
            entity_type="QUEUE",
            entity_id=queue_id,
            details={
                "accepted": decision.accepted,
                "code": decision.code,
                "track_id": entry.track_id,
            },
        )
        self._log_event(
            "CANDIDATE_REVALIDATED",
            entry,
            decision.code or "ACCEPTED",
        )
        return track, decision

    def reject_candidate(
        self,
        queue_id: int,
        decision: SelectionDecision,
    ) -> None:
        if decision.terminal_status == QueueStatus.FAILED:
            self.mark_error(queue_id, decision.code)
            return
        self.mark_skipped(
            queue_id,
            decision.reason or None,
            code=decision.code,
        )

    def get_next_candidate(
        self,
        excluded_queue_ids: set[int] | None = None,
        *,
        allow_empty_queue_selection: bool = True,
    ) -> QueueEntry | None:
        """Return the next ordered, unexamined waiting entry without loading it.

        Queue display order and candidate validation remain separate concerns:
        callers may reject one candidate, add its id to ``excluded_queue_ids``,
        and request the next entry without mutating positions or deck state.
        """
        excluded = excluded_queue_ids or set()
        candidate = next(
            (
                entry
                for entry in self._fair_candidate_entries()
                if entry.status == QueueStatus.WAITING and entry.queue_id not in excluded
            ),
            None,
        )
        if candidate is not None:
            return candidate
        if (
            excluded
            or not allow_empty_queue_selection
            or self.empty_queue_policy is EmptyQueuePolicy.STOP_AFTER_CURRENT
        ):
            return None
        if self.empty_queue_policy is EmptyQueuePolicy.REPEAT_CURRENT_PLAYLIST:
            return self._repeat_current_playlist()
        if self._automatic_selection is None:
            return None
        selected = (
            self._automatic_selection.select_emergency(self._selection_service)
            if self.empty_queue_policy is EmptyQueuePolicy.EMERGENCY_PLAYLIST
            else self._automatic_selection.select(self._selection_service)
        )
        stage = self._automatic_selection.last_relaxation_stage
        if stage != "NONE":
            self.record_audit_event(
                "RULE_RELAXATION",
                details={"stage": stage},
            )
            log_queue_event(
                self._logger,
                "RULE_RELAXATION",
                session_id=self.session_id,
                queue_id=None,
                track_id=selected.id if selected is not None else None,
                source=QueueSource.AUTOMATIC.value,
                status="selection",
                reason_code=stage,
            )
        if selected is None:
            return None
        source = (
            QueueSource.EMERGENCY
            if self._automatic_selection.last_relaxation_stage == "EMERGENCY_PLAYLIST"
            else QueueSource.AUTOMATIC
        )
        self.record_audit_event(
            "AUTOMATIC_SELECTION",
            entity_type="TRACK",
            entity_id=selected.id,
            details={"stage": stage, "source": source.value},
        )
        return self.add(selected.id, source=source)

    def _repeat_current_playlist(self) -> QueueEntry | None:
        if self._repeat_playlist_entries is None:
            return None
        added: list[QueueEntry] = []
        for saved in self._repeat_playlist_entries():
            try:
                added.append(
                    self.add(
                        saved.track_id,
                        source=QueueSource.PLAYLIST,
                        cue_in_override=saved.cue_in,
                        cue_out_override=saved.cue_out,
                        fade_duration_override=saved.fade_duration,
                        cue_override_source=saved.cue_source,
                    )
                )
            except ValueError:
                continue
        return added[0] if added else None

    def _fair_candidate_entries(self) -> list[QueueEntry]:
        """Round-robin equal-priority guest wishes without changing stored positions."""
        entries = self.entries()
        priorities = {
            entry.priority
            for entry in entries
            if entry.source is QueueSource.GUEST_REQUEST and entry.status == QueueStatus.WAITING
        }
        for priority in priorities:
            indexes = [
                index
                for index, entry in enumerate(entries)
                if entry.source is QueueSource.GUEST_REQUEST
                and entry.status == QueueStatus.WAITING
                and entry.priority == priority
            ]
            if len(indexes) < 2:
                continue
            occurrence_by_requester: dict[str, int] = {}
            ranked: list[tuple[int, datetime, int, int, QueueEntry]] = []
            for index in indexes:
                entry = entries[index]
                requester = " ".join(entry.requested_by.casefold().split())
                requester_key = requester or f"anonymous:{entry.queue_id}"
                round_number = occurrence_by_requester.get(requester_key, 0)
                occurrence_by_requester[requester_key] = round_number + 1
                ranked.append(
                    (
                        round_number,
                        entry.added_at or datetime.min,
                        entry.position,
                        entry.queue_id,
                        entry,
                    )
                )
            ordered = [
                item[4]
                for item in sorted(
                    ranked,
                    key=lambda item: (item[0], item[1], item[2], item[3]),
                )
            ]
            for index, entry in zip(indexes, ordered, strict=True):
                entries[index] = entry
        return entries

    def restore_deck_assignments(self, deck_a: DeckController, deck_b: DeckController) -> list[str]:
        """Restore persisted deck loads without ever starting playback."""
        restored: list[str] = []
        decks = {"A": deck_a, "B": deck_b}
        for entry in self.entries():
            if entry.status not in {QueueStatus.READY, QueueStatus.PLAYING}:
                continue
            if entry.loaded_deck not in decks:
                continue
            deck = decks[entry.loaded_deck]
            if deck.model.loaded_track is not None:
                self._validate_transition(entry.queue_id, QueueStatus.WAITING)
                self._repository.reset_queue_entry_to_waiting(entry.queue_id)
                self._set_lock_components(entry.queue_id, system=False)
                continue
            track = self._tracks.get_active(entry.track_id)
            if track is None:
                self.mark_error(entry.queue_id, "TRACK_MISSING")
                continue
            try:
                deck.load(track)
                self.apply_cues_to_deck(entry, deck)
            except (OSError, ValueError, RuntimeError):
                self.mark_error(entry.queue_id)
                continue
            self._transition(entry.queue_id, QueueStatus.READY, loaded_deck=entry.loaded_deck)
            restored.append(entry.loaded_deck)
        return restored

    def _normalize_positions(self) -> None:
        self._repository.set_queue_positions(
            self.session_id, [entry.queue_id for entry in self.entries()]
        )

    def _require_freely_waiting(self, queue_id: int) -> QueueEntry:
        entry = self.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        if entry.status == QueueStatus.PLAYING:
            raise ValueError("Ein spielender Queue-Eintrag kann nicht bearbeitet werden")
        if entry.status != QueueStatus.WAITING:
            raise ValueError("Nur wartende Queue-Einträge können direkt bearbeitet werden")
        if entry.locked:
            raise ValueError("Der Queue-Eintrag ist gesperrt")
        return entry

    def _validate_transition(self, queue_id: int, target: QueueStatus) -> QueueEntry:
        entry = self.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        if entry.status != target and target not in self._ALLOWED_TRANSITIONS[entry.status]:
            raise ValueError(
                f"Ungültiger Queue-Statusübergang: {entry.status.value} → {target.value}"
            )
        return entry

    def _transition(
        self,
        queue_id: int,
        target: QueueStatus,
        *,
        expected_status: QueueStatus | None = None,
        loaded_deck: str | None = None,
        skip_reason: str | None = None,
        failure_code: str | None = None,
        skip_code: str | None = None,
    ) -> None:
        current = self._validate_transition(queue_id, target)
        if expected_status is not None and current.status is not expected_status:
            raise ValueError("Queue-Zustand wurde gleichzeitig geändert; Operation abgebrochen")
        self._repository._update_queue_status(
            queue_id,
            target,
            expected_status=current.status,
            loaded_deck=loaded_deck,
            skip_reason=skip_reason,
            failure_code=failure_code,
            skip_code=skip_code,
        )
        self._set_lock_components(
            queue_id,
            system=target in {QueueStatus.PREPARING, QueueStatus.READY},
        )
        updated = self.entry(queue_id)
        if updated is not None:
            self._log_event(
                f"QUEUE_{target.value.upper()}",
                updated,
                failure_code or skip_code or target.value.upper(),
            )

    def _log_event(
        self,
        event_code: str,
        entry: QueueEntry,
        reason_code: str,
    ) -> None:
        log_queue_event(
            self._logger,
            event_code,
            session_id=self.session_id,
            queue_id=entry.queue_id,
            track_id=entry.track_id,
            source=entry.source.value,
            status=entry.status.value,
            reason_code=reason_code,
        )

    def _set_lock_components(
        self,
        queue_id: int,
        *,
        manual: bool | None = None,
        system: bool | None = None,
    ) -> QueueEntry:
        """Persist manual and lifecycle locks without overwriting each other."""
        entry = self.entry(queue_id)
        if entry is None:
            raise ValueError("Queue-Eintrag nicht gefunden")
        manual_locked = entry.lock_source in self._MANUAL_LOCK_SOURCES if manual is None else manual
        system_locked = entry.lock_source in self._SYSTEM_LOCK_SOURCES if system is None else system
        if manual_locked and system_locked:
            source = "MANUAL_SYSTEM"
        elif manual_locked:
            source = "MANUAL"
        elif system_locked:
            source = "SYSTEM"
        else:
            source = "NONE"
        return self.update_metadata(
            queue_id,
            priority=entry.priority,
            locked=manual_locked or system_locked,
            request_count=entry.request_count,
            lock_source=source,
        )
