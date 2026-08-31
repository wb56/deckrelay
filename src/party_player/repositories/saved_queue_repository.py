"""Persistence for reusable named queue templates."""

from party_player.database.connection import Database
from party_player.models import SavedQueue, SavedQueueEntry


class SavedQueueRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_all(self) -> list[SavedQueue]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT id, name, equalizer_preset_id FROM saved_queues
                   ORDER BY name COLLATE NOCASE, id"""
            ).fetchall()
        return [
            SavedQueue(
                int(row["id"]),
                str(row["name"]),
                equalizer_preset_id=(
                    int(row["equalizer_preset_id"])
                    if row["equalizer_preset_id"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def save(self, name: str, entries: list[SavedQueueEntry]) -> SavedQueue:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Die gespeicherte Queue benötigt einen Namen")
        if not entries:
            raise ValueError("Die aktuelle Queue enthält keine geplanten Titel")
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO saved_queues (name) VALUES (?)
                   ON CONFLICT(name) DO UPDATE SET updated_at = CURRENT_TIMESTAMP""",
                (normalized_name,),
            )
            row = connection.execute(
                "SELECT id FROM saved_queues WHERE name = ? COLLATE NOCASE",
                (normalized_name,),
            ).fetchone()
            assert row is not None
            saved_queue_id = int(row["id"])
            connection.execute(
                "DELETE FROM saved_queue_entries WHERE saved_queue_id = ?",
                (saved_queue_id,),
            )
            connection.executemany(
                """INSERT INTO saved_queue_entries
                   (saved_queue_id, track_id, position, cue_in, cue_out, fade_duration,
                    cue_source) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        saved_queue_id,
                        entry.track_id,
                        position,
                        entry.cue_in,
                        entry.cue_out,
                        entry.fade_duration,
                        entry.cue_source,
                    )
                    for position, entry in enumerate(entries, start=1)
                ],
            )
        with self._database.connect() as connection:
            persisted_ids = {
                int(row["position"]): int(row["id"])
                for row in connection.execute(
                    """SELECT id,position FROM saved_queue_entries
                       WHERE saved_queue_id=?""",
                    (saved_queue_id,),
                ).fetchall()
            }
        normalized_entries = tuple(
            SavedQueueEntry(
                entry.track_id,
                position,
                entry.cue_in,
                entry.cue_out,
                entry.fade_duration,
                entry.cue_source,
                persisted_ids.get(position),
            )
            for position, entry in enumerate(entries, start=1)
        )
        return SavedQueue(saved_queue_id, normalized_name, normalized_entries)

    def get(self, saved_queue_id: int) -> SavedQueue | None:
        with self._database.connect() as connection:
            queue_row = connection.execute(
                """SELECT id, name, equalizer_preset_id FROM saved_queues
                   WHERE id = ?""",
                (saved_queue_id,),
            ).fetchone()
            if queue_row is None:
                return None
            entry_rows = connection.execute(
                """SELECT id, track_id, position, cue_in, cue_out, fade_duration, cue_source
                   FROM saved_queue_entries
                   WHERE saved_queue_id = ? ORDER BY position, id""",
                (saved_queue_id,),
            ).fetchall()
        return SavedQueue(
            int(queue_row["id"]),
            str(queue_row["name"]),
            tuple(
                SavedQueueEntry(
                    int(row["track_id"]),
                    int(row["position"]),
                    row["cue_in"],
                    row["cue_out"],
                    row["fade_duration"],
                    str(row["cue_source"]),
                    int(row["id"]),
                )
                for row in entry_rows
            ),
            (
                int(queue_row["equalizer_preset_id"])
                if queue_row["equalizer_preset_id"] is not None
                else None
            ),
        )
