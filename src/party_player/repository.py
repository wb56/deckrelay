"""Party session, queue and playback-history persistence."""

import sqlite3
import json
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime
from typing import cast

from party_player.database.connection import Database
from party_player.enums import CompletionStatus, QueueSource, QueueStatus, SessionStatus
from party_player.models import PartySession, QueueEntry, SessionRecoverySummary


class PartyPlayerRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def close_cached_connection(self) -> bool:
        """Close a cache owned by the calling persistence worker."""
        return self._database.close_cached_connection()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group multiple repository calls into one SQLite transaction."""
        with self._database.transaction():
            yield

    def create_session(self, name: str, settings_snapshot: str = "{}") -> PartySession:
        with self._database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO party_sessions (name, settings_snapshot) VALUES (?, ?)",
                (name, settings_snapshot),
            )
            session_id = int(cursor.lastrowid or 0)
            row = connection.execute(
                """SELECT id, name, started_at, ended_at, status,
                          selected_playlist, settings_snapshot
                   FROM party_sessions WHERE id = ?""",
                (session_id,),
            ).fetchone()
        assert row is not None
        return self._session_from_row(row)

    def record_session_event(
        self,
        session_id: int,
        event_code: str,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        """Append one stable, session-owned operational audit event."""
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO session_audit_events
                   (session_id, event_code, entity_type, entity_id, details)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    event_code,
                    entity_type,
                    entity_id,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                ),
            )

    def repetition_override_queue_ids(self, session_id: int) -> set[int]:
        """Return active queue entries whose repetition override survives restart."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT e.entity_id
                   FROM session_audit_events e
                   JOIN party_queue q
                     ON q.id = e.entity_id AND q.session_id = e.session_id
                   WHERE e.session_id = ?
                     AND e.event_code = 'REPETITION_OVERRIDE'
                     AND e.entity_type = 'QUEUE'
                     AND e.entity_id IS NOT NULL
                     AND q.status IN ('waiting', 'preparing', 'ready', 'playing')""",
                (session_id,),
            ).fetchall()
        return {int(row["entity_id"]) for row in rows}

    def recover_queue_after_restart(self, session_id: int) -> SessionRecoverySummary:
        """Atomically turn volatile deck states into safe waiting entries."""
        recovered_at = datetime.now().isoformat()
        with self._database.connect() as connection:
            counts = connection.execute(
                """SELECT
                       SUM(CASE WHEN status IN ('waiting', 'preparing', 'ready') THEN 1 ELSE 0 END)
                           AS pending_entries,
                       SUM(CASE WHEN status IN ('preparing', 'ready') THEN 1 ELSE 0 END)
                           AS reset_preparations,
                       SUM(CASE WHEN status = 'playing' THEN 1 ELSE 0 END)
                           AS interrupted_playbacks
                   FROM party_queue WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO play_history
                   (session_id, track_id, deck_id, started_at, finished_at,
                    play_duration, completion_status, queue_id, skip_reason,
                    error_message, effective_duration, playback_ratio, queue_source,
                    result_code, skip_code, cue_in_override, cue_out_override,
                    fade_duration_override, cue_override_source, override_applied,
                    effective_cue_in, effective_cue_out)
                   SELECT q.session_id, q.track_id, COALESCE(q.loaded_deck, 'UNKNOWN'),
                          COALESCE(q.played_at, q.updated_at, q.added_at), ?,
                          0, 'ABORTED', q.id, NULL, NULL, t.duration_seconds, 0,
                          q.source, 'ABORTED', 'APPLICATION_SHUTDOWN',
                          q.cue_in_override, q.cue_out_override,
                          q.fade_duration_override, q.cue_override_source,
                          CASE
                              WHEN q.cue_override_source IN ('queue', 'snapshot')
                               AND (q.cue_in_override IS NOT NULL
                                    OR q.cue_out_override IS NOT NULL
                                    OR q.fade_duration_override IS NOT NULL)
                                  THEN 1
                              ELSE 0
                          END,
                          q.cue_in_override, q.cue_out_override
                   FROM party_queue q
                   JOIN tracks t ON t.id = q.track_id
                   WHERE q.session_id = ? AND q.status = 'playing'""",
                (recovered_at, session_id),
            )
            connection.execute(
                """UPDATE party_queue
                   SET status = CASE WHEN status = 'playing' THEN 'skipped' ELSE 'waiting' END,
                       loaded_deck = NULL,
                       skip_reason = CASE WHEN status = 'playing'
                           THEN 'Wiedergabe durch Neustart unterbrochen' ELSE skip_reason END,
                       skip_code = CASE WHEN status = 'playing'
                           THEN 'RECOVERY_INTERRUPTED_PLAYBACK' ELSE skip_code END,
                       locked = CASE
                           WHEN lock_source IN ('MANUAL', 'MANUAL_SYSTEM') THEN 1
                           ELSE 0
                       END,
                       lock_source = CASE
                           WHEN lock_source IN ('MANUAL', 'MANUAL_SYSTEM') THEN 'MANUAL'
                           ELSE 'NONE'
                       END,
                       updated_at = ?
                   WHERE session_id = ?
                     AND status IN ('preparing', 'ready', 'playing')""",
                (recovered_at, session_id),
            )
            connection.execute(
                """INSERT INTO session_audit_events
                   (session_id, event_code, details)
                   VALUES (?, 'SESSION_RECOVERED', ?)""",
                (
                    session_id,
                    json.dumps(
                        {
                            "audio_started": False,
                            "volatile_states_reset": True,
                            "pending_entries": int(counts["pending_entries"] or 0),
                            "reset_preparations": int(counts["reset_preparations"] or 0),
                            "interrupted_playbacks": int(counts["interrupted_playbacks"] or 0),
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return SessionRecoverySummary(
            restored_session_id=session_id,
            pending_entries=int(counts["pending_entries"] or 0),
            reset_preparations=int(counts["reset_preparations"] or 0),
            interrupted_playbacks=int(counts["interrupted_playbacks"] or 0),
        )

    def latest_unfinished_session(self) -> PartySession | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT id, name, started_at, ended_at, status,
                          selected_playlist, settings_snapshot
                   FROM party_sessions
                   WHERE status IN ('active', 'paused', 'recovered')
                   ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
        return self._session_from_row(row) if row else None

    def latest_finished_session_with_pending_queue(self) -> PartySession | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT s.id, s.name, s.started_at, s.ended_at, s.status,
                          s.selected_playlist, s.settings_snapshot
                   FROM party_sessions AS s
                   WHERE s.status = 'finished'
                     AND EXISTS (
                         SELECT 1 FROM party_queue AS q
                         WHERE q.session_id = s.id
                             AND q.status IN ('waiting', 'preparing', 'ready', 'playing')
                     )
                   ORDER BY s.started_at DESC, s.id DESC LIMIT 1"""
            ).fetchone()
        return self._session_from_row(row) if row else None

    def copy_pending_queue(self, source_session_id: int, target_session_id: int) -> int:
        with self._database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO party_queue
                       (session_id, track_id, position, status, source, requested_by,
                        cue_in_override, cue_out_override, fade_duration_override,
                        cue_override_source, priority, locked, request_count, lock_source,
                        unique_requester_count, last_requested_at, updated_at,
                        preparation_attempts, failure_code, skip_code)
                   SELECT ?, track_id,
                          ROW_NUMBER() OVER (
                              ORDER BY priority DESC, position, added_at, id
                          ),
                          'waiting', source, requested_by, cue_in_override, cue_out_override,
                          fade_duration_override, cue_override_source,
                          priority, locked, request_count, lock_source,
                          unique_requester_count, last_requested_at, updated_at,
                          preparation_attempts, failure_code, skip_code
                   FROM party_queue
                   WHERE session_id = ?
                       AND status IN ('waiting', 'preparing', 'ready', 'playing')
                   ORDER BY priority DESC, position, added_at, id""",
                (target_session_id, source_session_id),
            )
        return cursor.rowcount

    def set_session_status(self, session_id: int, status: SessionStatus) -> None:
        ended_at = datetime.now().isoformat() if status == SessionStatus.FINISHED else None
        with self._database.connect() as connection:
            connection.execute(
                "UPDATE party_sessions SET status = ?, ended_at = ? WHERE id = ?",
                (status.value, ended_at, session_id),
            )

    def set_selected_playlist(self, session_id: int, saved_queue_id: int | None) -> None:
        with self._database.connect() as connection:
            connection.execute(
                "UPDATE party_sessions SET selected_playlist = ? WHERE id = ?",
                (saved_queue_id, session_id),
            )

    def selected_playlist_id(self, session_id: int) -> int | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT selected_playlist FROM party_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None or row["selected_playlist"] is None:
            return None
        return int(row["selected_playlist"])

    def add_queue_entry(
        self,
        session_id: int,
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
        normalized_source = QueueSource.normalize(source)
        resolved_priority = (
            normalized_source.default_priority
            if priority is None
            else self._validate_priority(priority)
        )
        with self._database.connect() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT COALESCE(MAX(position), 0) + 1 AS next_position
                   FROM party_queue WHERE session_id = ? AND status != 'removed'""",
                (session_id,),
            ).fetchone()
            position = int(row["next_position"])
            cursor = connection.execute(
                """INSERT INTO party_queue
                   (session_id, track_id, position, source, requested_by, cue_in_override,
                    cue_out_override, fade_duration_override, cue_override_source, priority)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    track_id,
                    position,
                    normalized_source.value,
                    requested_by,
                    cue_in_override,
                    cue_out_override,
                    fade_duration_override,
                    cue_override_source,
                    resolved_priority,
                ),
            )
            queue_id = int(cursor.lastrowid or 0)
        entry = self.get_queue_entry(queue_id)
        assert entry is not None
        return entry

    def list_queue(self, session_id: int, include_removed: bool = False) -> list[QueueEntry]:
        clause = "" if include_removed else "AND status != 'removed'"
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""SELECT id, track_id, position, status, source, requested_by,
                           added_at, loaded_deck, played_at, skip_reason, cue_in_override,
                           cue_out_override, fade_duration_override, cue_override_source,
                           priority, locked, request_count, lock_source,
                           unique_requester_count, last_requested_at, updated_at,
                           preparation_attempts, failure_code, skip_code
                    FROM party_queue WHERE session_id = ? {clause}
                    ORDER BY priority DESC, position, added_at, id""",
                (session_id,),
            ).fetchall()
        return [self._queue_from_row(row) for row in rows]

    def get_queue_entry(self, queue_id: int) -> QueueEntry | None:
        with self._database.connect() as connection:
            row = self._get_queue_entry_row(connection, queue_id)
        return self._queue_from_row(row) if row else None

    @staticmethod
    def _get_queue_entry_row(connection: sqlite3.Connection, queue_id: int) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """SELECT id, track_id, position, status, source, requested_by,
                          added_at, loaded_deck, played_at, skip_reason, cue_in_override,
                          cue_out_override, fade_duration_override, cue_override_source,
                          priority, locked, request_count, lock_source,
                          unique_requester_count, last_requested_at, updated_at,
                          preparation_attempts, failure_code, skip_code
                   FROM party_queue WHERE id = ?""",
                (queue_id,),
            ).fetchone(),
        )

    def has_active_track(self, session_id: int, track_id: int) -> bool:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM party_queue
                       WHERE session_id = ? AND track_id = ?
                           AND status IN ('waiting', 'preparing', 'ready', 'playing')
                   ) AS present""",
                (session_id, track_id),
            ).fetchone()
        return bool(row["present"])

    def active_track_entry(self, session_id: int, track_id: int) -> QueueEntry | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT id, track_id, position, status, source, requested_by,
                          added_at, loaded_deck, played_at, skip_reason, cue_in_override,
                          cue_out_override, fade_duration_override, cue_override_source,
                          priority, locked, request_count, lock_source,
                          unique_requester_count, last_requested_at, updated_at,
                          preparation_attempts, failure_code, skip_code
                   FROM party_queue
                   WHERE session_id = ? AND track_id = ?
                     AND status IN ('waiting', 'preparing', 'ready', 'playing')
                   ORDER BY priority DESC, position, id LIMIT 1""",
                (session_id, track_id),
            ).fetchone()
        return self._queue_from_row(row) if row else None

    def register_guest_request(
        self,
        queue_id: int,
        requester: str,
        requested_at: datetime | None = None,
    ) -> QueueEntry:
        """Atomically increment wishes and count a normalized requester once."""
        requester_key = " ".join(requester.casefold().split())
        with self._database.connect() as connection:
            unique_increment = 0
            if requester_key:
                insert = connection.execute(
                    """INSERT OR IGNORE INTO queue_guest_requesters
                       (queue_id, requester_key) VALUES (?, ?)""",
                    (queue_id, requester_key),
                )
                unique_increment = int(insert.rowcount == 1)
            cursor = connection.execute(
                """UPDATE party_queue
                   SET request_count = request_count + 1,
                       unique_requester_count = unique_requester_count + ?,
                       last_requested_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (unique_increment, queue_id),
            )
            if requester_key:
                connection.execute(
                    """INSERT INTO guest_request_events
                       (session_id, queue_id, requester_key, requested_at)
                       SELECT session_id, id, ?, ? FROM party_queue WHERE id = ?""",
                    (
                        requester_key,
                        (requested_at or datetime.now()).isoformat(),
                        queue_id,
                    ),
                )
        if cursor.rowcount != 1:
            raise ValueError("Queue-Eintrag nicht gefunden")
        entry = self.get_queue_entry(queue_id)
        assert entry is not None
        return entry

    def active_guest_request_count(self, session_id: int, requester_key: str) -> int:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(DISTINCT q.id) AS total
                   FROM party_queue q
                   JOIN queue_guest_requesters r ON r.queue_id = q.id
                   WHERE q.session_id = ? AND r.requester_key = ?
                     AND q.status IN ('waiting', 'preparing', 'ready', 'playing')""",
                (session_id, requester_key),
            ).fetchone()
        return int(row["total"])

    def last_guest_request_at(
        self,
        session_id: int,
        requester_key: str,
    ) -> datetime | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT MAX(requested_at) AS requested_at
                   FROM guest_request_events
                   WHERE session_id = ? AND requester_key = ?""",
                (session_id, requester_key),
            ).fetchone()
        return (
            datetime.fromisoformat(str(row["requested_at"]))
            if row["requested_at"] is not None
            else None
        )

    def consecutive_guest_plays(self, requester_key: str, limit: int) -> int:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM queue_guest_requesters r
                       WHERE r.queue_id = h.queue_id AND r.requester_key = ?
                   ) AS requested
                   FROM play_history h
                   WHERE h.completion_status = 'PLAYED'
                   ORDER BY h.finished_at DESC, h.id DESC
                   LIMIT ?""",
                (requester_key, max(1, limit + 1)),
            ).fetchall()
        count = 0
        for row in rows:
            if not bool(row["requested"]):
                break
            count += 1
        return count

    def was_track_completed_since(self, track_id: int, since: datetime) -> bool:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM play_history
                   WHERE track_id = ? AND completion_status = 'PLAYED'
                     AND finished_at >= ?
                   LIMIT 1""",
                (track_id, since.isoformat()),
            ).fetchone()
        return row is not None

    def set_queue_cue_overrides(
        self,
        queue_id: int,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
        source: str = "queue",
    ) -> None:
        if source not in {"inherited", "queue", "snapshot"}:
            raise ValueError("Unbekannte Herkunft der Queue-Cue-Werte")
        if source == "inherited" and any(
            value is not None for value in (cue_in, cue_out, fade_duration)
        ):
            raise ValueError("Geerbte Queue-Cues dürfen keine eigenen Werte enthalten")
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE party_queue
                   SET cue_in_override = ?, cue_out_override = ?,
                       fade_duration_override = ?, cue_override_source = ?
                   WHERE id = ?""",
                (cue_in, cue_out, fade_duration, source, queue_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Queue-Eintrag nicht gefunden")

    def _update_queue_status(
        self,
        queue_id: int,
        status: QueueStatus,
        *,
        expected_status: QueueStatus,
        loaded_deck: str | None = None,
        skip_reason: str | None = None,
        failure_code: str | None = None,
        skip_code: str | None = None,
    ) -> None:
        """Persistence primitive; lifecycle validation belongs to QueueService."""
        played_at = (
            datetime.now().isoformat()
            if status in {QueueStatus.PLAYED, QueueStatus.PLAYING}
            else None
        )
        try:
            with self._database.connect() as connection:
                cursor = connection.execute(
                    """UPDATE party_queue
                       SET status = ?,
                           loaded_deck = CASE
                               WHEN ? IN ('removed', 'failed', 'skipped') THEN NULL
                               ELSE COALESCE(?, loaded_deck)
                           END,
                           skip_reason = ?,
                           played_at = COALESCE(?, played_at),
                           failure_code = ?,
                           skip_code = ?,
                           preparation_attempts = preparation_attempts
                               + CASE WHEN ? = 'preparing' THEN 1 ELSE 0 END,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND status = ?""",
                    (
                        status.value,
                        status.value,
                        loaded_deck,
                        skip_reason,
                        played_at,
                        failure_code,
                        skip_code,
                        status.value,
                        queue_id,
                        expected_status.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "Queue-Zustand wurde gleichzeitig geändert; Operation abgebrochen"
                    )
        except sqlite3.IntegrityError as error:
            raise ValueError("Das Deck ist bereits einem Queue-Eintrag zugewiesen") from error

    def mark_queue_playing(self, queue_id: int) -> QueueEntry:
        """Atomically start a prepared entry and release only its lifecycle lock."""
        played_at = datetime.now().isoformat()
        with self._database.connect_cached() as connection:
            cursor = connection.execute(
                """UPDATE party_queue
                   SET status = ?,
                       played_at = COALESCE(?, played_at),
                       locked = CASE
                           WHEN lock_source IN ('MANUAL', 'MANUAL_SYSTEM') THEN 1
                           ELSE 0
                       END,
                       lock_source = CASE
                           WHEN lock_source = 'MANUAL_SYSTEM' THEN 'MANUAL'
                           WHEN lock_source = 'SYSTEM' THEN 'NONE'
                           ELSE lock_source
                       END,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status IN (?, ?, ?)""",
                (
                    QueueStatus.PLAYING.value,
                    played_at,
                    queue_id,
                    QueueStatus.READY.value,
                    QueueStatus.PLAYING.value,
                    QueueStatus.PLAYED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Queue-Eintrag kann nicht gestartet werden")
            row = self._get_queue_entry_row(connection, queue_id)
        assert row is not None
        return self._queue_from_row(row)

    def update_queue_metadata(
        self,
        queue_id: int,
        *,
        priority: int,
        locked: bool,
        request_count: int,
        lock_source: str | None = None,
    ) -> None:
        """Persist bounded queue metadata used by incremental row rendering."""
        priority = self._validate_priority(priority)
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE party_queue
                   SET priority = ?, locked = ?, request_count = ?,
                       lock_source = COALESCE(?, lock_source),
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    priority,
                    int(locked),
                    max(0, request_count),
                    lock_source,
                    queue_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("Queue-Eintrag nicht gefunden")

    def release_queue_deck_assignment(
        self,
        queue_id: int,
        expected_status: QueueStatus,
        expected_deck: str,
    ) -> bool:
        """Release one stale deck reference if the assignment is still current.

        Completion paths can legitimately race: another callback may already
        have completed or reassigned the entry.  In that case this cleanup is
        obsolete and must not fail the GUI callback or clear the new owner.
        """
        with self._database.connect_cached() as connection:
            cursor = connection.execute(
                """UPDATE party_queue
                   SET loaded_deck = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?
                     AND status IN (?, ?)
                     AND loaded_deck = ?""",
                (
                    queue_id,
                    expected_status.value,
                    QueueStatus.PLAYED.value,
                    expected_deck,
                ),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _validate_priority(priority: int) -> int:
        if isinstance(priority, bool) or not 0 <= priority <= 999:
            raise ValueError("Queue-Priorität muss zwischen 0 und 999 liegen")
        return priority

    def swap_positions(self, first_id: int, second_id: int) -> None:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, position FROM party_queue WHERE id IN (?, ?)", (first_id, second_id)
            ).fetchall()
            positions = {int(row["id"]): int(row["position"]) for row in rows}
            if len(positions) != 2:
                raise ValueError("Warteschlangeneintrag nicht gefunden")
            connection.execute(
                "UPDATE party_queue SET position = ? WHERE id = ?",
                (positions[second_id], first_id),
            )
            connection.execute(
                "UPDATE party_queue SET position = ? WHERE id = ?",
                (positions[first_id], second_id),
            )

    def set_queue_positions(self, session_id: int, queue_ids: list[int]) -> None:
        """Persist one complete visible queue order with contiguous positions."""
        with self._database.connect() as connection:
            for position, queue_id in enumerate(queue_ids, start=1):
                connection.execute(
                    """UPDATE party_queue SET position = ?
                       WHERE id = ? AND session_id = ? AND status != 'removed'""",
                    (position, queue_id, session_id),
                )

    def clear_waiting_queue(self, session_id: int) -> int:
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE party_queue SET status = 'removed'
                   WHERE session_id = ? AND status = 'waiting' AND locked = 0""",
                (session_id,),
            )
        return max(0, cursor.rowcount)

    def clear_complete_queue(self, session_id: int) -> int:
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE party_queue SET status = 'removed'
                   WHERE session_id = ?
                     AND status NOT IN ('playing', 'removed')""",
                (session_id,),
            )
        return max(0, cursor.rowcount)

    def retry_queue_entry(self, queue_id: int) -> None:
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE party_queue
                   SET status = 'waiting', loaded_deck = NULL, played_at = NULL,
                       skip_reason = NULL, failure_code = NULL, skip_code = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'failed'""",
                (queue_id,),
            )
        if cursor.rowcount != 1:
            raise ValueError("Nur fehlgeschlagene Queue-Einträge können erneut versucht werden")

    def restore_artist_repetition_skips(self, session_id: int) -> int:
        """Reactivate explicit queue items rejected only by artist repetition."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE party_queue
                   SET status = 'waiting', loaded_deck = NULL, played_at = NULL,
                       skip_reason = NULL, failure_code = NULL, skip_code = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE session_id = ?
                     AND status = 'skipped'
                     AND skip_code = 'ARTIST_REPETITION'
                     AND source IN ('MANUAL', 'PLAYLIST')""",
                (session_id,),
            )
        return max(0, cursor.rowcount)

    def reset_queue_entry_to_waiting(self, queue_id: int) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """UPDATE party_queue
                   SET status = 'waiting', loaded_deck = NULL, played_at = NULL
                       , failure_code = NULL, skip_code = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status IN ('preparing', 'ready', 'playing')""",
                (queue_id,),
            )

    def reset_played_queue_entry_to_waiting(self, queue_id: int) -> None:
        """Return one completed entry to the editable waiting queue."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE party_queue
                   SET status = 'waiting', loaded_deck = NULL, played_at = NULL,
                       skip_reason = NULL, failure_code = NULL, skip_code = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'played'""",
                (queue_id,),
            )
        if cursor.rowcount != 1:
            raise ValueError("Nur gespielte Queue-Einträge können zurückgesetzt werden")

    def override_repetition_skip(self, queue_id: int) -> None:
        """Return one explicit repetition skip to the waiting queue."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE party_queue
                   SET status = 'waiting', loaded_deck = NULL, played_at = NULL,
                       skip_reason = NULL, failure_code = NULL, skip_code = NULL,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'skipped'
                     AND skip_code IN ('TRACK_REPETITION', 'ARTIST_REPETITION')""",
                (queue_id,),
            )
        if cursor.rowcount != 1:
            raise ValueError(
                "Nur wegen Wiederholungsschutz übersprungene Titel können so abgespielt werden"
            )

    def add_history(
        self,
        session_id: int,
        track_id: int,
        deck_id: str,
        started_at: datetime,
        completion_status: CompletionStatus,
        play_duration: float,
        queue_id: int | None = None,
        skip_reason: str | None = None,
        error_message: str | None = None,
        completed_at: datetime | None = None,
        effective_duration: float | None = None,
        playback_ratio: float | None = None,
        skip_code: str | None = None,
        effective_cue_in: float | None = None,
        effective_cue_out: float | None = None,
    ) -> None:
        # Kept as an API compatibility parameter; new history rows persist only
        # stable codes. Existing free text remains readable as migration data.
        skip_reason = None
        with self._database.connect() as connection:
            queue_snapshot = None
            if queue_id is not None:
                queue_snapshot = connection.execute(
                    """SELECT source, cue_in_override, cue_out_override,
                              fade_duration_override, cue_override_source
                       FROM party_queue WHERE id = ?""",
                    (queue_id,),
                ).fetchone()
            cue_in = queue_snapshot["cue_in_override"] if queue_snapshot else None
            cue_out = queue_snapshot["cue_out_override"] if queue_snapshot else None
            fade_duration = queue_snapshot["fade_duration_override"] if queue_snapshot else None
            cue_source = str(queue_snapshot["cue_override_source"]) if queue_snapshot else None
            override_applied = int(
                cue_source in {"queue", "snapshot"}
                and any(value is not None for value in (cue_in, cue_out, fade_duration))
            )
            connection.execute(
                """INSERT INTO play_history
                   (session_id, track_id, deck_id, started_at, finished_at, play_duration,
                    completion_status, queue_id, skip_reason, error_message,
                    effective_duration, playback_ratio, queue_source, result_code, skip_code,
                    cue_in_override, cue_out_override, fade_duration_override,
                    cue_override_source, override_applied, effective_cue_in,
                    effective_cue_out)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?)""",
                (
                    session_id,
                    track_id,
                    deck_id,
                    started_at.isoformat(),
                    (completed_at or datetime.now()).isoformat(),
                    max(0.0, play_duration),
                    completion_status.value,
                    queue_id,
                    skip_reason,
                    error_message,
                    effective_duration,
                    playback_ratio,
                    str(queue_snapshot["source"]) if queue_snapshot else None,
                    completion_status.value,
                    skip_code,
                    cue_in,
                    cue_out,
                    fade_duration,
                    cue_source,
                    override_applied,
                    effective_cue_in,
                    effective_cue_out,
                ),
            )

    def get_setting(self, key: str) -> str | None:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM party_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO party_settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> PartySession:
        return PartySession(
            session_id=int(row["id"]),
            name=str(row["name"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            status=SessionStatus(row["status"]),
            selected_playlist=row["selected_playlist"],
            settings_snapshot=str(row["settings_snapshot"]),
        )

    @staticmethod
    def _queue_from_row(row: sqlite3.Row) -> QueueEntry:
        return QueueEntry(
            queue_id=int(row["id"]),
            track_id=int(row["track_id"]),
            position=int(row["position"]),
            status=QueueStatus(row["status"]),
            source=QueueSource.normalize(str(row["source"])),
            requested_by=str(row["requested_by"]),
            added_at=datetime.fromisoformat(row["added_at"]),
            loaded_deck=row["loaded_deck"],
            played_at=datetime.fromisoformat(row["played_at"]) if row["played_at"] else None,
            skip_reason=row["skip_reason"],
            cue_in_override=row["cue_in_override"],
            cue_out_override=row["cue_out_override"],
            fade_duration_override=row["fade_duration_override"],
            cue_override_source=str(row["cue_override_source"]),
            priority=int(row["priority"]),
            locked=bool(row["locked"]),
            request_count=int(row["request_count"]),
            lock_source=str(row["lock_source"]),
            unique_requester_count=int(row["unique_requester_count"]),
            last_requested_at=(
                datetime.fromisoformat(row["last_requested_at"])
                if row["last_requested_at"]
                else None
            ),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            preparation_attempts=int(row["preparation_attempts"]),
            failure_code=row["failure_code"],
            skip_code=row["skip_code"],
            source_detail=str(row["source"]),
        )
