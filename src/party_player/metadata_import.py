"""Atomic, provenance-aware import of already-read file metadata."""

from dataclasses import dataclass
from collections.abc import Callable
from enum import StrEnum
import os
from pathlib import Path
import sqlite3

from party_player.database.connection import Database
from party_player.metadata_persistence import (
    MetadataFieldState,
    _increment_revision,
    _write_state,
    serialize_metadata_value,
)
from party_player.metadata_rules import (
    EffectiveMetadataValue,
    FileTagImportDecisionKind,
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
    decide_file_tag_import,
    normalize_metadata_value,
)
from party_player.models import Track
from party_player.repositories.track_repository import TrackRepository


@dataclass(frozen=True, slots=True)
class FileImportSnapshot:
    resolved_path: str
    normalized_path: str
    size: int
    modified_ns: int

    @classmethod
    def capture(cls, path: Path) -> "FileImportSnapshot":
        resolved = path.resolve()
        stat = resolved.stat()
        return cls(
            str(resolved),
            os.path.normcase(str(resolved)),
            stat.st_size,
            stat.st_mtime_ns,
        )

    def matches(self, other: "FileImportSnapshot") -> bool:
        return (
            self.normalized_path == other.normalized_path
            and self.size == other.size
            and self.modified_ns == other.modified_ns
        )


@dataclass(frozen=True, slots=True)
class ImportedFieldValue:
    value: object
    source: MetadataSource = MetadataSource.FILE_TAG
    source_detail: str = "file_tag"


@dataclass(frozen=True, slots=True)
class ImportedTrackData:
    title: ImportedFieldValue
    artist: ImportedFieldValue
    album: ImportedFieldValue
    main_genre: ImportedFieldValue
    year: ImportedFieldValue
    original_release_year: ImportedFieldValue
    duration_seconds: float

    def fields(self) -> tuple[tuple[MetadataFieldKey, ImportedFieldValue], ...]:
        return (
            (MetadataFieldKey.TITLE, self.title),
            (MetadataFieldKey.ARTIST, self.artist),
            (MetadataFieldKey.ALBUM, self.album),
            (MetadataFieldKey.MAIN_GENRE, self.main_genre),
            (MetadataFieldKey.YEAR, self.year),
            (MetadataFieldKey.ORIGINAL_RELEASE_YEAR, self.original_release_year),
        )


class MetadataImportOutcome(StrEnum):
    NEW_TRACK = "NEW_TRACK"
    UNCHANGED = "UNCHANGED"
    UPDATED = "UPDATED"
    PROPOSALS_CREATED = "PROPOSALS_CREATED"
    FILE_CHANGED = "FILE_CHANGED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class MetadataImportResult:
    outcome: MetadataImportOutcome
    track: Track | None
    revision: int | None
    updated_fields: tuple[MetadataFieldKey, ...] = ()
    proposal_fields: tuple[MetadataFieldKey, ...] = ()
    partial_tags: bool = False
    file_changed: bool = False
    error: str = ""


class MetadataImportOperation:
    """Apply one normalized tag snapshot in exactly one SQLite transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._tracks = TrackRepository(database)

    def apply(
        self,
        snapshot: FileImportSnapshot,
        imported: ImportedTrackData,
        *,
        current_snapshot: FileImportSnapshot,
        persist_related: Callable[[int], None] | None = None,
    ) -> MetadataImportResult:
        if not snapshot.matches(current_snapshot):
            return MetadataImportResult(
                MetadataImportOutcome.FILE_CHANGED,
                None,
                None,
                file_changed=True,
                error="Datei wurde während des Imports verändert",
            )
        normalized_fields = self._normalize_fields(imported)
        partial = any(field.value is None for field in normalized_fields.values())
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM tracks WHERE lower(file_path) = lower(?) LIMIT 1",
                (snapshot.resolved_path,),
            ).fetchone()
            if existing is None:
                track_id, updated = self._insert_new(
                    connection, snapshot, imported.duration_seconds, normalized_fields
                )
                revision = _increment_revision(connection, track_id)
                outcome = MetadataImportOutcome.NEW_TRACK
                proposals: tuple[MetadataFieldKey, ...] = ()
            else:
                track_id = int(existing["id"])
                updated, proposals = self._update_existing(
                    connection,
                    existing,
                    snapshot,
                    imported.duration_seconds,
                    normalized_fields,
                )
                if updated:
                    revision = _increment_revision(connection, track_id)
                    outcome = MetadataImportOutcome.UPDATED
                else:
                    revision = int(existing["metadata_revision"])
                    outcome = (
                        MetadataImportOutcome.PROPOSALS_CREATED
                        if proposals
                        else MetadataImportOutcome.UNCHANGED
                    )
            if persist_related is not None:
                persist_related(track_id)
        track = self._tracks.get(track_id)
        assert track is not None
        return MetadataImportResult(
            outcome,
            track,
            revision,
            tuple(updated),
            proposals,
            partial,
        )

    @staticmethod
    def _normalize_fields(
        imported: ImportedTrackData,
    ) -> dict[MetadataFieldKey, ImportedFieldValue]:
        normalized: dict[MetadataFieldKey, ImportedFieldValue] = {}
        for key, field in imported.fields():
            normalized[key] = ImportedFieldValue(
                normalize_metadata_value(key, field.value),
                field.source,
                field.source_detail.strip()[:200],
            )
        return normalized

    def _insert_new(
        self,
        connection: sqlite3.Connection,
        snapshot: FileImportSnapshot,
        duration: float,
        fields: dict[MetadataFieldKey, ImportedFieldValue],
    ) -> tuple[int, list[MetadataFieldKey]]:
        title = fields[MetadataFieldKey.TITLE].value
        if not isinstance(title, str) or not title:
            raise ValueError("Eine neue Datei benötigt einen Titel oder Dateinamen")
        cursor = connection.execute(
            """INSERT INTO tracks
                   (file_path, title, artist, album, duration_seconds, genre, year,
                    original_release_year, catalog_visible)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                snapshot.resolved_path,
                title,
                fields[MetadataFieldKey.ARTIST].value or "",
                fields[MetadataFieldKey.ALBUM].value or "",
                duration,
                fields[MetadataFieldKey.MAIN_GENRE].value or "",
                fields[MetadataFieldKey.YEAR].value,
                fields[MetadataFieldKey.ORIGINAL_RELEASE_YEAR].value,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Katalogtitel konnte nicht angelegt werden")
        track_id = cursor.lastrowid
        updated: list[MetadataFieldKey] = []
        for key, field in fields.items():
            if field.value is None:
                continue
            _write_state(
                connection,
                MetadataFieldState(
                    track_id,
                    key,
                    field.source,
                    field.source_detail,
                    None,
                    MetadataReviewStatus.IMPORTED,
                ),
            )
            updated.append(key)
        return track_id, updated

    def _update_existing(
        self,
        connection: sqlite3.Connection,
        existing: sqlite3.Row,
        snapshot: FileImportSnapshot,
        duration: float,
        fields: dict[MetadataFieldKey, ImportedFieldValue],
    ) -> tuple[list[MetadataFieldKey], tuple[MetadataFieldKey, ...]]:
        track_id = int(existing["id"])
        connection.execute(
            "UPDATE tracks SET duration_seconds = ?, catalog_visible = 1 WHERE id = ?",
            (duration, track_id),
        )
        states = {
            MetadataFieldKey(str(row["field_key"])): row
            for row in connection.execute(
                "SELECT * FROM track_metadata_field_state WHERE track_id = ?", (track_id,)
            ).fetchall()
        }
        updated: list[MetadataFieldKey] = []
        proposals: list[MetadataFieldKey] = []
        proposal_values: list[tuple[MetadataFieldKey, ImportedFieldValue]] = []
        for key, imported_field in fields.items():
            current_value = self._row_value(existing, key)
            state_row = states.get(key)
            current = None
            if state_row is not None:
                current = EffectiveMetadataValue(
                    current_value,
                    MetadataSource(str(state_row["source_type"])),
                    MetadataReviewStatus(str(state_row["review_status"])),
                )
            elif current_value not in {None, ""}:
                current = EffectiveMetadataValue(
                    current_value,
                    MetadataSource.MANUAL_INPUT,
                    MetadataReviewStatus.OUTDATED,
                )
            decision = decide_file_tag_import(
                key,
                imported_field.value,
                current,
                new_track=False,
            )
            if decision.kind is FileTagImportDecisionKind.APPLY:
                self._write_scalar(connection, track_id, key, decision.normalized_value)
                _write_state(
                    connection,
                    MetadataFieldState(
                        track_id,
                        key,
                        imported_field.source,
                        imported_field.source_detail,
                        None,
                        MetadataReviewStatus.IMPORTED,
                    ),
                )
                updated.append(key)
            elif decision.kind is FileTagImportDecisionKind.PROPOSE:
                if imported_field.value is not None:
                    proposal_values.append((key, imported_field))
        if proposal_values:
            run_id: int | None = None
            for key, field in proposal_values:
                serialized = serialize_metadata_value(key, field.value)
                if self._same_proposal_exists(connection, track_id, key, serialized, snapshot):
                    continue
                connection.execute(
                    """UPDATE track_metadata_suggestions
                       SET status = 'SUPERSEDED', decided_at = CURRENT_TIMESTAMP,
                           decision_reason = 'Durch neueren Dateitag abgelöst'
                       WHERE track_id = ? AND field_key = ? AND source_type = 'FILE_TAG'
                         AND status = 'PENDING'""",
                    (track_id, key.value),
                )
                if run_id is None:
                    run_id = self._create_import_run(connection, track_id, snapshot)
                connection.execute(
                    """INSERT INTO track_metadata_suggestions
                           (track_id, analysis_run_id, field_key, serialized_value,
                            source_type, source_detail, confidence)
                       VALUES (?, ?, ?, ?, 'FILE_TAG', ?, 1.0)""",
                    (track_id, run_id, key.value, serialized, field.source_detail),
                )
                proposals.append(key)
        return updated, tuple(proposals)

    @staticmethod
    def _row_value(row: sqlite3.Row, key: MetadataFieldKey) -> object:
        columns = {
            MetadataFieldKey.TITLE: "title",
            MetadataFieldKey.ARTIST: "artist",
            MetadataFieldKey.ALBUM: "album",
            MetadataFieldKey.MAIN_GENRE: "genre",
            MetadataFieldKey.YEAR: "year",
            MetadataFieldKey.ORIGINAL_RELEASE_YEAR: "original_release_year",
        }
        return row[columns[key]]

    @staticmethod
    def _write_scalar(
        connection: sqlite3.Connection,
        track_id: int,
        key: MetadataFieldKey,
        value: object,
    ) -> None:
        columns = {
            MetadataFieldKey.TITLE: "title",
            MetadataFieldKey.ARTIST: "artist",
            MetadataFieldKey.ALBUM: "album",
            MetadataFieldKey.MAIN_GENRE: "genre",
            MetadataFieldKey.YEAR: "year",
            MetadataFieldKey.ORIGINAL_RELEASE_YEAR: "original_release_year",
        }
        connection.execute(f"UPDATE tracks SET {columns[key]} = ? WHERE id = ?", (value, track_id))

    @staticmethod
    def _create_import_run(
        connection: sqlite3.Connection, track_id: int, snapshot: FileImportSnapshot
    ) -> int:
        cursor = connection.execute(
            """INSERT INTO metadata_analysis_runs
                   (track_id, analysis_profile, analysis_version, status, priority,
                    file_path_snapshot, file_size, file_modified_ns, attempt_count,
                    started_at, finished_at)
               VALUES (?, 'FILE_TAG_IMPORT', 'file-tag-v1', 'COMPLETED', 0,
                       ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (track_id, snapshot.normalized_path, snapshot.size, snapshot.modified_ns),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Importlauf konnte nicht gespeichert werden")
        return cursor.lastrowid

    @staticmethod
    def _same_proposal_exists(
        connection: sqlite3.Connection,
        track_id: int,
        key: MetadataFieldKey,
        serialized: str,
        snapshot: FileImportSnapshot,
    ) -> bool:
        row = connection.execute(
            """SELECT 1 FROM track_metadata_suggestions AS suggestion
               JOIN metadata_analysis_runs AS run ON run.id = suggestion.analysis_run_id
               WHERE suggestion.track_id = ? AND suggestion.field_key = ?
                 AND suggestion.source_type = 'FILE_TAG'
                 AND suggestion.serialized_value = ?
                 AND suggestion.status IN ('PENDING', 'REJECTED')
                 AND lower(run.file_path_snapshot) = lower(?)
                 AND run.file_size = ? AND run.file_modified_ns = ?
               LIMIT 1""",
            (
                track_id,
                key.value,
                serialized,
                snapshot.normalized_path,
                snapshot.size,
                snapshot.modified_ns,
            ),
        ).fetchone()
        return row is not None
