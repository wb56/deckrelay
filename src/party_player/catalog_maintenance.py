"""Server-side work queues and immutable selection models for catalog maintenance."""

from dataclasses import asdict, dataclass
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import sqlite3
from time import monotonic
from uuid import uuid4

from party_player.metadata_rules import (
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
    RecordingClassification,
    RecordingKind,
    RecordingTrait,
)
from party_player.database.connection import Database
from party_player.metadata_editor import (
    MetadataEditorService,
    MetadataFieldViewModel,
    StagedSuggestionAction,
    SuggestionEditorAction,
    TrackMetadataChanges,
    TrackMetadataEditorViewModel,
    ValueRemovalMode,
)
from party_player.metadata_persistence import (
    MetadataFieldState,
    MetadataRevisionConflict,
    _write_state,
    deserialize_metadata_value,
    serialize_metadata_value,
)
from party_player.metadata_rules import normalize_metadata_value


BATCH_CHUNK_SIZE = 250
PAGE_SIZE = 50


class WorkQueue(StrEnum):
    MISSING_ORIGINAL_YEAR = "MISSING_ORIGINAL_YEAR"
    UNCONFIRMED_ORIGINAL_YEAR = "UNCONFIRMED_ORIGINAL_YEAR"
    CONFIRMED_EMPTY = "CONFIRMED_EMPTY"
    OPEN_SUGGESTIONS = "OPEN_SUGGESTIONS"
    IMPORT_CONFLICTS = "IMPORT_CONFLICTS"
    CONFLICTING = "CONFLICTING"
    MISSING_BPM = "MISSING_BPM"
    UNCERTAIN_BPM = "UNCERTAIN_BPM"
    POSSIBLE_HALF_DOUBLE_TEMPO = "POSSIBLE_HALF_DOUBLE_TEMPO"
    MISSING_MAIN_GENRE = "MISSING_MAIN_GENRE"
    MISSING_MUSICAL_DECADES = "MISSING_MUSICAL_DECADES"
    MISSING_ENERGY = "MISSING_ENERGY"
    MISSING_DANCEABILITY = "MISSING_DANCEABILITY"
    MISSING_RATING = "MISSING_RATING"
    INCOMPLETE = "INCOMPLETE"
    OUTDATED = "OUTDATED"
    FAILED_ANALYSIS = "FAILED_ANALYSIS"
    RECENT_MANUAL = "RECENT_MANUAL"


class BatchAction(StrEnum):
    SET = "SET"
    CONFIRM = "CONFIRM"
    CONFIRM_EMPTY = "CONFIRM_EMPTY"
    REMOVE_MISSING = "REMOVE_MISSING"
    MULTI_ADD = "MULTI_ADD"
    MULTI_REMOVE = "MULTI_REMOVE"
    MULTI_REPLACE = "MULTI_REPLACE"
    SUGGESTION_ACCEPT = "SUGGESTION_ACCEPT"
    SUGGESTION_ACCEPT_CONFIRM = "SUGGESTION_ACCEPT_CONFIRM"
    SUGGESTION_REJECT = "SUGGESTION_REJECT"
    SUGGESTION_DEFER = "SUGGESTION_DEFER"


@dataclass(frozen=True, slots=True)
class MetadataBatchRequest:
    selection: "SelectionDescription"
    field_mask: frozenset[MetadataFieldKey]
    action: BatchAction
    values: tuple[tuple[MetadataFieldKey, object], ...] = ()
    expected_revisions: tuple[tuple[int, int], ...] = ()
    created_at: str = ""
    preview_token: str = ""
    minimum_confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.field_mask:
            raise ValueError("Eine Sammelaktion benötigt eine ausdrückliche Feldmaske")
        if any(key not in self.field_mask for key, _value in self.values):
            raise ValueError("Zielwerte außerhalb der Feldmaske sind nicht zulässig")


class BatchSkipReason(StrEnum):
    UNCHANGED = "UNCHANGED"
    PROTECTED = "PROTECTED"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    INVALID = "INVALID"
    PREVIEW_STALE = "PREVIEW_STALE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class BatchExample:
    track_id: int
    field: MetadataFieldKey
    before: object
    after: object


@dataclass(frozen=True, slots=True)
class BatchPreview:
    token: str
    request: MetadataBatchRequest
    selected: int
    changeable: int
    unchanged: int
    protected: int
    revision_conflicts: int
    invalid: int
    suggestion_conflicts: int
    examples: tuple[BatchExample, ...]
    undo_field_count: int


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch_id: int
    status: str
    selected: int
    checked: int
    changed: int
    unchanged: int
    protected: int
    revision_conflicts: int
    invalid: int
    failed: int
    cancelled: int
    duration_seconds: float
    chunk_count: int
    reasons: tuple[tuple[BatchSkipReason, int], ...]


@dataclass(frozen=True, slots=True)
class UndoPreview:
    batch_id: int
    changeable_tracks: int
    conflict_tracks: int
    changed_fields: int


@dataclass(frozen=True, slots=True)
class MaintenanceFilter:
    work_queue: WorkQueue | None = None
    field: MetadataFieldKey | None = None
    source: MetadataSource | None = None
    review_status: MetadataReviewStatus | None = None
    suggestion_status: str | None = None
    minimum_confidence: float | None = None
    has_value: bool | None = None
    confirmed: bool | None = None
    conflict: bool | None = None
    text: str = ""
    changed_from: str | None = None
    changed_to: str | None = None
    minimum_bpm: float | None = None
    maximum_bpm: float | None = None

    def canonical_json(self) -> str:
        values = {
            key: (value.value if isinstance(value, StrEnum) else value)
            for key, value in asdict(self).items()
        }
        return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SelectionDescription:
    filter: MaintenanceFilter
    all_matches: bool = False
    included_ids: frozenset[int] = frozenset()
    excluded_ids: frozenset[int] = frozenset()
    query_snapshot: str = ""

    @classmethod
    def for_filter(cls, filter_: MaintenanceFilter) -> "SelectionDescription":
        canonical = filter_.canonical_json()
        return cls(filter_, query_snapshot=sha256(canonical.encode()).hexdigest())

    def select(self, track_id: int) -> "SelectionDescription":
        if self.all_matches:
            return SelectionDescription(
                self.filter,
                True,
                self.included_ids,
                self.excluded_ids - {track_id},
                self.query_snapshot,
            )
        return SelectionDescription(
            self.filter,
            False,
            self.included_ids | {track_id},
            self.excluded_ids,
            self.query_snapshot,
        )

    def deselect(self, track_id: int) -> "SelectionDescription":
        if self.all_matches:
            return SelectionDescription(
                self.filter,
                True,
                self.included_ids,
                self.excluded_ids | {track_id},
                self.query_snapshot,
            )
        return SelectionDescription(
            self.filter,
            False,
            self.included_ids - {track_id},
            self.excluded_ids,
            self.query_snapshot,
        )

    def select_all_matches(self) -> "SelectionDescription":
        return SelectionDescription(
            self.filter, True, frozenset(), frozenset(), self.query_snapshot
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class WorkQueueCount:
    queue: WorkQueue
    count: int


@dataclass(frozen=True, slots=True)
class MaintenanceRow:
    track_id: int
    revision: int
    artist: str
    title: str
    field: str
    current_value: str
    current_value_full: str
    suggestion: str
    suggestion_full: str
    source: str
    review_status: str
    confidence: float | None
    changed_at: str
    warning: str


_RECORDING_KIND_LABELS = {
    RecordingKind.ORIGINAL: "Originalaufnahme",
    RecordingKind.RE_RECORDING: "Neuaufnahme",
    RecordingKind.LIVE: "Liveaufnahme",
    RecordingKind.REMIX: "Remix",
    RecordingKind.RADIO_EDIT: "Radio Edit",
    RecordingKind.UNKNOWN: "Aufnahmeart unbekannt",
}


def format_metadata_value(
    key: MetadataFieldKey,
    value: object,
    review_status: MetadataReviewStatus = MetadataReviewStatus.MISSING,
) -> str:
    """Return one German display value without leaking canonical storage forms."""
    if value is None or value == "" or value == ():
        return (
            "Bewusst ohne Wert bestätigt"
            if review_status is MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE
            else "Fehlt / ungeprüft"
        )
    if isinstance(value, RecordingClassification):
        text = _RECORDING_KIND_LABELS[value.kind]
        if RecordingTrait.REMASTERED in value.traits:
            text += " · remastert"
        return text
    if isinstance(value, tuple):
        if key is MetadataFieldKey.MUSICAL_DECADES:
            return ", ".join(f"{item}er" for item in value)
        return ", ".join(str(item) for item in value)
    if key is MetadataFieldKey.BPM_CONFIDENCE:
        return f"{float(str(value)) * 100:.0f} %"
    if key in {MetadataFieldKey.ENERGY, MetadataFieldKey.DANCEABILITY}:
        return f"{int(str(value))} %"
    if key is MetadataFieldKey.RATING:
        rating = int(str(value))
        return f"{'★' * rating}{'☆' * (5 - rating)} ({rating}/5)"
    if key in {MetadataFieldKey.BPM, MetadataFieldKey.ALTERNATIVE_BPM}:
        return f"{float(str(value)):g} BPM"
    return str(value)


def shorten_display_value(value: str, limit: int = 96) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1].rstrip()}…"


@dataclass(frozen=True, slots=True)
class MaintenancePage:
    rows: tuple[MaintenanceRow, ...]
    total: int
    page: int
    page_size: int
    query_snapshot: str


@dataclass(frozen=True, slots=True)
class MaintenanceDiagnostics:
    query_duration_ms: float = 0.0
    result_count: int = 0
    preview_duration_ms: float = 0.0
    batch_duration_ms: float = 0.0
    chunk_count: int = 0
    changed_tracks: int = 0
    skipped_tracks: int = 0
    revision_conflicts: int = 0
    render_duration_ms: float = 0.0
    maximum_visible_rows: int = 0
    maximum_tooltips: int = 0


_QUEUE_PREDICATES = {
    WorkQueue.MISSING_ORIGINAL_YEAR: "t.original_release_year IS NULL",
    WorkQueue.UNCONFIRMED_ORIGINAL_YEAR: "t.original_release_year IS NOT NULL AND COALESCE(s.review_status, 'MISSING') NOT IN ('CONFIRMED_WITH_VALUE','CONFIRMED_WITHOUT_VALUE')",
    WorkQueue.CONFIRMED_EMPTY: "s.review_status = 'CONFIRMED_WITHOUT_VALUE'",
    WorkQueue.OPEN_SUGGESTIONS: "p.id IS NOT NULL",
    WorkQueue.IMPORT_CONFLICTS: "p.id IS NOT NULL AND p.source_type = 'FILE_TAG' AND s.review_status IN ('CONFIRMED_WITH_VALUE','CONFIRMED_WITHOUT_VALUE','CONFLICTING')",
    WorkQueue.CONFLICTING: "s.review_status = 'CONFLICTING'",
    WorkQueue.MISSING_BPM: "t.bpm IS NULL",
    WorkQueue.UNCERTAIN_BPM: "t.bpm IS NOT NULL AND t.bpm_confidence IS NOT NULL AND t.bpm_confidence < 0.8",
    WorkQueue.POSSIBLE_HALF_DOUBLE_TEMPO: "t.bpm IS NOT NULL AND t.alternative_bpm IS NOT NULL AND (ABS(t.alternative_bpm - t.bpm * 2) < 0.5 OR ABS(t.bpm - t.alternative_bpm * 2) < 0.5)",
    WorkQueue.MISSING_MAIN_GENRE: "TRIM(t.genre) = ''",
    WorkQueue.MISSING_MUSICAL_DECADES: "NOT EXISTS (SELECT 1 FROM track_metadata_terms x JOIN metadata_terms mt ON mt.id=x.term_id WHERE x.track_id=t.id AND mt.term_type='MUSICAL_DECADE')",
    WorkQueue.MISSING_ENERGY: "t.energy IS NULL",
    WorkQueue.MISSING_DANCEABILITY: "t.danceability IS NULL",
    WorkQueue.MISSING_RATING: "t.rating IS NULL",
    WorkQueue.INCOMPLETE: "t.original_release_year IS NULL OR TRIM(t.genre)='' OR t.bpm IS NULL OR t.energy IS NULL OR t.danceability IS NULL OR t.rating IS NULL",
    WorkQueue.OUTDATED: "s.review_status = 'OUTDATED'",
    WorkQueue.FAILED_ANALYSIS: "a.status = 'FAILED'",
    WorkQueue.RECENT_MANUAL: "s.source_type IN ('MANUAL_INPUT','MANUAL_CONFIRMATION')",
}

_SCALAR_FILTER_COLUMNS = {
    MetadataFieldKey.TITLE: "t.title",
    MetadataFieldKey.ARTIST: "t.artist",
    MetadataFieldKey.ALBUM: "t.album",
    MetadataFieldKey.YEAR: "t.year",
    MetadataFieldKey.ORIGINAL_RELEASE_YEAR: "t.original_release_year",
    MetadataFieldKey.BPM: "t.bpm",
    MetadataFieldKey.BPM_CONFIDENCE: "t.bpm_confidence",
    MetadataFieldKey.ALTERNATIVE_BPM: "t.alternative_bpm",
    MetadataFieldKey.MAIN_GENRE: "t.genre",
    MetadataFieldKey.ENERGY: "t.energy",
    MetadataFieldKey.DANCEABILITY: "t.danceability",
    MetadataFieldKey.LANGUAGE: "t.language",
    MetadataFieldKey.RATING: "t.rating",
    MetadataFieldKey.COMMENT: "t.comment",
}
_TERM_FILTER_TYPES = {
    MetadataFieldKey.MUSICAL_DECADES: "MUSICAL_DECADE",
    MetadataFieldKey.ADDITIONAL_GENRES: "ADDITIONAL_GENRE",
    MetadataFieldKey.MOODS: "MOOD",
    MetadataFieldKey.TAGS: "FREE_TAG",
}
_WORK_QUEUE_DISPLAY_FIELDS = {
    WorkQueue.MISSING_ORIGINAL_YEAR: MetadataFieldKey.ORIGINAL_RELEASE_YEAR,
    WorkQueue.UNCONFIRMED_ORIGINAL_YEAR: MetadataFieldKey.ORIGINAL_RELEASE_YEAR,
    WorkQueue.MISSING_BPM: MetadataFieldKey.BPM,
    WorkQueue.UNCERTAIN_BPM: MetadataFieldKey.BPM,
    WorkQueue.POSSIBLE_HALF_DOUBLE_TEMPO: MetadataFieldKey.BPM,
    WorkQueue.MISSING_MAIN_GENRE: MetadataFieldKey.MAIN_GENRE,
    WorkQueue.MISSING_MUSICAL_DECADES: MetadataFieldKey.MUSICAL_DECADES,
    WorkQueue.MISSING_ENERGY: MetadataFieldKey.ENERGY,
    WorkQueue.MISSING_DANCEABILITY: MetadataFieldKey.DANCEABILITY,
    WorkQueue.MISSING_RATING: MetadataFieldKey.RATING,
}


class CatalogMaintenanceRepository:
    """Count and page maintenance rows entirely in SQLite."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self.last_query_duration_ms = 0.0
        self.last_result_count = 0

    @staticmethod
    def _from() -> str:
        return """FROM tracks t
            LEFT JOIN track_metadata_field_state s ON s.track_id=t.id
            LEFT JOIN track_metadata_suggestions p ON p.track_id=t.id AND p.status='PENDING'
            LEFT JOIN metadata_analysis_runs a ON a.track_id=t.id AND a.status='FAILED'"""

    def counts(self) -> tuple[WorkQueueCount, ...]:
        with self._database.connect() as connection:
            return tuple(
                WorkQueueCount(
                    queue,
                    int(
                        connection.execute(
                            f"SELECT COUNT(DISTINCT t.id) {self._from()} WHERE t.catalog_visible=1 AND ({predicate})"
                        ).fetchone()[0]
                    ),
                )
                for queue, predicate in _QUEUE_PREDICATES.items()
            )

    def page(
        self, filter_: MaintenanceFilter, page: int, page_size: int = PAGE_SIZE
    ) -> MaintenancePage:
        started = monotonic()
        where, parameters = self._where(filter_)
        base = self._from()
        display_key = filter_.field or (
            _WORK_QUEUE_DISPLAY_FIELDS.get(filter_.work_queue)
            if filter_.work_queue is not None
            else None
        )
        if display_key is None:
            display_from = base
            field_expression = "COALESCE(s.field_key,p.field_key,'')"
            state_alias, proposal_alias = "s", "p"
        else:
            safe_field = display_key.value
            display_from = f"""{base}
                LEFT JOIN track_metadata_field_state ds
                    ON ds.track_id=t.id AND ds.field_key='{safe_field}'
                LEFT JOIN track_metadata_suggestions dp
                    ON dp.track_id=t.id AND dp.field_key='{safe_field}' AND dp.status='PENDING'"""
            field_expression = f"'{safe_field}'"
            state_alias, proposal_alias = "ds", "dp"
        with self._database.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT t.id) {base} WHERE {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""SELECT t.id, t.metadata_revision, t.artist, t.title,
                    t.album, t.year, t.original_release_year, t.recording_type,
                    t.is_remastered, t.bpm, t.bpm_confidence, t.alternative_bpm,
                    t.genre, t.energy, t.danceability, t.language, t.rating, t.comment,
                    {field_expression} field_key,
                    COALESCE({state_alias}.source_type,{proposal_alias}.source_type,'') source_type,
                    COALESCE({state_alias}.review_status,{proposal_alias}.review_status,'MISSING') review_status,
                    {proposal_alias}.serialized_value,
                    COALESCE({proposal_alias}.confidence,{state_alias}.confidence) confidence,
                    COALESCE({state_alias}.updated_at,{proposal_alias}.created_at,t.created_at) changed_at,
                    (SELECT GROUP_CONCAT(COALESCE(mt.numeric_value, mt.display_name), char(31))
                     FROM track_metadata_terms tm JOIN metadata_terms mt ON mt.id=tm.term_id
                     WHERE tm.track_id=t.id AND mt.term_type='MUSICAL_DECADE') musical_decades,
                    (SELECT GROUP_CONCAT(mt.display_name, char(31))
                     FROM track_metadata_terms tm JOIN metadata_terms mt ON mt.id=tm.term_id
                     WHERE tm.track_id=t.id AND mt.term_type='ADDITIONAL_GENRE') additional_genres,
                    (SELECT GROUP_CONCAT(mt.display_name, char(31))
                     FROM track_metadata_terms tm JOIN metadata_terms mt ON mt.id=tm.term_id
                     WHERE tm.track_id=t.id AND mt.term_type='MOOD') moods,
                    (SELECT GROUP_CONCAT(mt.display_name, char(31))
                     FROM track_metadata_terms tm JOIN metadata_terms mt ON mt.id=tm.term_id
                     WHERE tm.track_id=t.id AND mt.term_type='FREE_TAG') tags
                    {display_from} WHERE {where} GROUP BY t.id ORDER BY t.artist COLLATE NOCASE,t.title COLLATE NOCASE,t.id LIMIT ? OFFSET ?""",
                (*parameters, page_size, max(0, page - 1) * page_size),
            ).fetchall()
        result_items: list[MaintenanceRow] = []
        for row in rows:
            field_text = str(row["field_key"])
            status = MetadataReviewStatus(str(row["review_status"]))
            key = MetadataFieldKey(field_text) if field_text else None
            current = self._row_value(row, key) if key is not None else None
            current_full = format_metadata_value(key, current, status) if key is not None else ""
            suggestion_value = (
                deserialize_metadata_value(key, str(row["serialized_value"]))
                if key is not None and row["serialized_value"] is not None
                else None
            )
            suggestion_full = (
                format_metadata_value(key, suggestion_value)
                if key is not None and row["serialized_value"] is not None
                else ""
            )
            result_items.append(
                MaintenanceRow(
                    int(row["id"]),
                    int(row["metadata_revision"]),
                    str(row["artist"]),
                    str(row["title"]),
                    field_text,
                    shorten_display_value(current_full),
                    current_full,
                    shorten_display_value(suggestion_full),
                    suggestion_full,
                    str(row["source_type"]),
                    status.value,
                    float(row["confidence"]) if row["confidence"] is not None else None,
                    str(row["changed_at"]),
                    "Konflikt" if status is MetadataReviewStatus.CONFLICTING else "",
                )
            )
        result = tuple(result_items)
        self.last_query_duration_ms = (monotonic() - started) * 1000.0
        self.last_result_count = total
        return MaintenancePage(
            result,
            total,
            page,
            page_size,
            SelectionDescription.for_filter(filter_).query_snapshot,
        )

    @staticmethod
    def _row_value(row: sqlite3.Row, key: MetadataFieldKey) -> object:
        columns = {
            MetadataFieldKey.TITLE: "title",
            MetadataFieldKey.ARTIST: "artist",
            MetadataFieldKey.ALBUM: "album",
            MetadataFieldKey.YEAR: "year",
            MetadataFieldKey.ORIGINAL_RELEASE_YEAR: "original_release_year",
            MetadataFieldKey.BPM: "bpm",
            MetadataFieldKey.BPM_CONFIDENCE: "bpm_confidence",
            MetadataFieldKey.ALTERNATIVE_BPM: "alternative_bpm",
            MetadataFieldKey.MAIN_GENRE: "genre",
            MetadataFieldKey.ENERGY: "energy",
            MetadataFieldKey.DANCEABILITY: "danceability",
            MetadataFieldKey.LANGUAGE: "language",
            MetadataFieldKey.RATING: "rating",
            MetadataFieldKey.COMMENT: "comment",
        }
        if key in columns:
            return row[columns[key]]
        if key is MetadataFieldKey.RECORDING_CLASSIFICATION:
            traits = (
                frozenset({RecordingTrait.REMASTERED})
                if bool(row["is_remastered"])
                else frozenset()
            )
            return RecordingClassification(RecordingKind(str(row["recording_type"])), traits)
        raw = row[key.value]
        if raw is None:
            return ()
        parts = tuple(str(raw).split(chr(31)))
        return (
            tuple(int(item) for item in parts) if key is MetadataFieldKey.MUSICAL_DECADES else parts
        )

    def resolve_selection(self, selection: SelectionDescription) -> tuple[tuple[int, int], ...]:
        if (
            selection.query_snapshot
            != SelectionDescription.for_filter(selection.filter).query_snapshot
        ):
            raise ValueError("Auswahlsnapshot passt nicht mehr zum Filter")
        if not selection.all_matches:
            if not selection.included_ids:
                return ()
            placeholders = ",".join("?" for _ in selection.included_ids)
            where, filter_parameters = self._where(selection.filter)
            with self._database.connect() as connection:
                rows = connection.execute(
                    f"""SELECT DISTINCT t.id, t.metadata_revision {self._from()}
                        WHERE {where} AND t.id IN ({placeholders}) ORDER BY t.id""",
                    (*filter_parameters, *sorted(selection.included_ids)),
                ).fetchall()
            return tuple((int(row["id"]), int(row["metadata_revision"])) for row in rows)
        where, parameters = self._where(selection.filter)
        exclusion = ""
        if selection.excluded_ids:
            exclusion = f" AND t.id NOT IN ({','.join('?' for _ in selection.excluded_ids)})"
            parameters = (*parameters, *sorted(selection.excluded_ids))
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT t.id, t.metadata_revision {self._from()}
                    WHERE {where}{exclusion} ORDER BY t.id""",
                parameters,
            ).fetchall()
        return tuple((int(row["id"]), int(row["metadata_revision"])) for row in rows)

    def restrict_selection(
        self, selection: SelectionDescription, filter_: MaintenanceFilter
    ) -> tuple[tuple[int, int], ...]:
        """Intersect an existing cross-page selection with a new SQL filter."""
        selected = self.resolve_selection(selection)
        if not selected:
            return ()
        ids = tuple(track_id for track_id, _revision in selected)
        where, parameters = self._where(filter_)
        placeholders = ",".join("?" for _ in ids)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT t.id,t.metadata_revision {self._from()}
                    WHERE {where} AND t.id IN ({placeholders}) ORDER BY t.id""",
                (*parameters, *ids),
            ).fetchall()
        return tuple((int(row["id"]), int(row["metadata_revision"])) for row in rows)

    def _where(self, filter_: MaintenanceFilter) -> tuple[str, tuple[object, ...]]:
        clauses = ["t.catalog_visible=1"]
        values: list[object] = []
        if filter_.work_queue:
            clauses.append(f"({_QUEUE_PREDICATES[filter_.work_queue]})")
        if filter_.field is not None and (
            filter_.source is not None
            or filter_.review_status is not None
            or filter_.suggestion_status is not None
        ):
            clauses.append("COALESCE(s.field_key,p.field_key)=?")
            values.append(filter_.field.value)
        for column, value in (
            ("COALESCE(s.source_type,p.source_type)", filter_.source),
            ("COALESCE(s.review_status,p.review_status)", filter_.review_status),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value.value)
        if filter_.text.strip():
            clauses.append("(t.title LIKE ? ESCAPE '\\' OR t.artist LIKE ? ESCAPE '\\')")
            escaped = (
                filter_.text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            values.extend((f"%{escaped}%", f"%{escaped}%"))
        if filter_.minimum_confidence is not None:
            clauses.append("COALESCE(p.confidence,s.confidence)>=?")
            values.append(filter_.minimum_confidence)
        if filter_.minimum_bpm is not None:
            clauses.append("t.bpm>=?")
            values.append(filter_.minimum_bpm)
        if filter_.maximum_bpm is not None:
            clauses.append("t.bpm<=?")
            values.append(filter_.maximum_bpm)
        if filter_.suggestion_status:
            clauses.append("p.status=?")
            values.append(filter_.suggestion_status)
        if filter_.confirmed is not None:
            expression = "COALESCE(s.review_status,'MISSING') IN ('CONFIRMED_WITH_VALUE','CONFIRMED_WITHOUT_VALUE')"
            clauses.append(expression if filter_.confirmed else f"NOT ({expression})")
        if filter_.conflict is not None:
            expression = "COALESCE(s.review_status,'')='CONFLICTING'"
            clauses.append(expression if filter_.conflict else f"NOT ({expression})")
        if filter_.has_value is not None:
            if filter_.field in _SCALAR_FILTER_COLUMNS:
                column = _SCALAR_FILTER_COLUMNS[filter_.field]
                expression = f"{column} IS NOT NULL"
                if filter_.field in {
                    MetadataFieldKey.TITLE,
                    MetadataFieldKey.ARTIST,
                    MetadataFieldKey.ALBUM,
                    MetadataFieldKey.MAIN_GENRE,
                }:
                    expression += f" AND TRIM({column})<>''"
            elif filter_.field in _TERM_FILTER_TYPES:
                expression = "EXISTS (SELECT 1 FROM track_metadata_terms hv JOIN metadata_terms hm ON hm.id=hv.term_id WHERE hv.track_id=t.id AND hm.term_type=?)"
                values.append(_TERM_FILTER_TYPES[filter_.field])
            else:
                expression = "COALESCE(s.review_status,'MISSING') NOT IN ('MISSING','CONFIRMED_WITHOUT_VALUE')"
            clauses.append(f"({expression})" if filter_.has_value else f"NOT ({expression})")
        for operator, boundary in (
            (">=", filter_.changed_from),
            ("<=", filter_.changed_to),
        ):
            if boundary:
                clauses.append(f"COALESCE(s.updated_at,p.created_at,t.created_at) {operator} ?")
                values.append(boundary)
        return " AND ".join(clauses), tuple(values)


class CatalogMaintenanceService:
    """Preview and execute bounded serial metadata batches."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self.repository = CatalogMaintenanceRepository(database)
        self._editor = MetadataEditorService(database)
        self._previews: dict[str, MetadataBatchRequest] = {}
        self._diagnostics = MaintenanceDiagnostics()

    @property
    def diagnostics(self) -> MaintenanceDiagnostics:
        return self._diagnostics

    def preview(self, request: MetadataBatchRequest) -> BatchPreview:
        started = monotonic()
        resolved = self.repository.resolve_selection(request.selection)
        expected = tuple(resolved)
        changed = unchanged = protected = invalid = 0
        examples: list[BatchExample] = []
        for track_id, _revision in resolved:
            model = self._editor.load(track_id)
            try:
                if request.action in {
                    BatchAction.SUGGESTION_ACCEPT,
                    BatchAction.SUGGESTION_ACCEPT_CONFIRM,
                    BatchAction.SUGGESTION_REJECT,
                    BatchAction.SUGGESTION_DEFER,
                }:
                    actions = self._suggestion_actions(request, model)
                    if request.action is BatchAction.SUGGESTION_DEFER or not actions:
                        unchanged += 1
                    elif (
                        any(
                            model.field(s.field_key).protected
                            for s in model.suggestions
                            if s.suggestion_id in {a.suggestion_id for a in actions}
                        )
                        and request.action is not BatchAction.SUGGESTION_REJECT
                    ):
                        protected += 1
                    else:
                        changed += 1
                    continue
                track_changed = False
                track_protected = False
                for key in request.field_mask:
                    field = model.field(key)
                    if field.protected and request.action in {
                        BatchAction.SET,
                        BatchAction.CONFIRM_EMPTY,
                        BatchAction.REMOVE_MISSING,
                        BatchAction.MULTI_ADD,
                        BatchAction.MULTI_REMOVE,
                        BatchAction.MULTI_REPLACE,
                    }:
                        track_protected = True
                        break
                    target, field_changed = self._preview_field(request, key, field)
                    if field_changed:
                        track_changed = True
                        if len(examples) < 20:
                            examples.append(BatchExample(track_id, key, field.value, target))
                if track_protected:
                    protected += 1
                else:
                    changed += int(track_changed)
                    unchanged += int(not track_changed)
            except (TypeError, ValueError):
                invalid += 1
        token = uuid4().hex
        checked = MetadataBatchRequest(
            request.selection,
            request.field_mask,
            request.action,
            request.values,
            expected,
            request.created_at or utc_now(),
            token,
            request.minimum_confidence,
        )
        self._previews[token] = checked
        result = BatchPreview(
            token,
            checked,
            len(resolved),
            changed,
            unchanged,
            protected,
            0,
            invalid,
            0,
            tuple(examples),
            len(examples),
        )
        self._diagnostics = MaintenanceDiagnostics(
            self.repository.last_query_duration_ms,
            self.repository.last_result_count,
            (monotonic() - started) * 1000.0,
        )
        return result

    @staticmethod
    def _preview_field(
        request: MetadataBatchRequest,
        key: MetadataFieldKey,
        field: MetadataFieldViewModel,
    ) -> tuple[object, bool]:
        # Kept free of persistence so preview can never mutate catalog data.
        current = field.value
        status = field.review_status
        values = dict(request.values)
        if request.action is BatchAction.SET:
            target = normalize_metadata_value(key, values[key])
            return target, target != current
        if request.action is BatchAction.CONFIRM:
            if current is None or current == "" or current == ():
                raise ValueError("Fehlender Wert kann nicht als vorhanden bestätigt werden")
            return current, status is not MetadataReviewStatus.CONFIRMED_WITH_VALUE
        if request.action in {BatchAction.CONFIRM_EMPTY, BatchAction.REMOVE_MISSING}:
            target = normalize_metadata_value(key, None)
            target_status = (
                MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE
                if request.action is BatchAction.CONFIRM_EMPTY
                else MetadataReviewStatus.MISSING
            )
            return target, target != current or status is not target_status
        if request.action in {
            BatchAction.MULTI_ADD,
            BatchAction.MULTI_REMOVE,
            BatchAction.MULTI_REPLACE,
        }:
            if not isinstance(current, tuple):
                raise ValueError("Mehrfachaktion benötigt ein Mehrfachfeld")
            supplied = normalize_metadata_value(key, values[key])
            if not isinstance(supplied, tuple):
                raise ValueError("Mehrfachwert erwartet")
            if request.action is BatchAction.MULTI_ADD:
                target_input = (*current, *supplied)
            elif request.action is BatchAction.MULTI_REMOVE:
                target_input = tuple(item for item in current if item not in supplied)
            else:
                target_input = supplied
            target = normalize_metadata_value(key, target_input)
            return target, target != current
        raise ValueError("Nicht unterstützte Vorschauaktion")

    def execute(
        self,
        request: MetadataBatchRequest,
        *,
        cancel_requested: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> BatchResult:
        started = monotonic()
        approved = self._previews.pop(request.preview_token, None)
        if approved != request:
            raise ValueError("Vorschau ist veraltet oder wurde bereits verwendet")
        changed = unchanged = protected = conflicts = failed = chunks = 0
        expected = request.expected_revisions
        with self._database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO metadata_batch_actions
                    (action_type,status,selection_json,field_mask_json,preview_token,planned_count)
                    VALUES (?,'RUNNING',?,?,?,?)""",
                (
                    request.action.value,
                    request.selection.filter.canonical_json(),
                    json.dumps(sorted(key.value for key in request.field_mask)),
                    request.preview_token,
                    len(expected),
                ),
            )
            assert cursor.lastrowid is not None
            batch_id = int(cursor.lastrowid)
        cancelled = 0
        for offset in range(0, len(expected), BATCH_CHUNK_SIZE):
            if cancel_requested is not None and cancel_requested():
                cancelled = len(expected) - offset
                break
            chunks += 1
            with self._database.transaction() as transaction:
                for track_id, revision in expected[offset : offset + BATCH_CHUNK_SIZE]:
                    transaction.execute("SAVEPOINT metadata_batch_track")
                    try:
                        before = self._editor.load(track_id)
                        changes = self._changes(track_id, revision, request)
                        suggestion_before = self._suggestion_snapshot(
                            transaction, track_id, changes.suggestion_actions
                        )
                        result = self._editor.save(track_id, changes)
                        applied = result.revision_changed or bool(changes.suggestion_actions)
                        changed += int(applied)
                        unchanged += int(not applied)
                        if result.revision_changed:
                            with self._database.connect() as connection:
                                for key in result.changed_fields:
                                    old = before.field(key)
                                    new = result.view_model.field(key)
                                    connection.execute(
                                        """INSERT INTO metadata_batch_changes
                                            (batch_id,track_id,field_key,previous_value_json,new_value_json,
                                             previous_state_json,new_state_json,revision_before,revision_after,result_status)
                                            VALUES (?,?,?,?,?,?,?,?,?,'CHANGED')""",
                                        (
                                            batch_id,
                                            track_id,
                                            key.value,
                                            serialize_metadata_value(key, old.value),
                                            serialize_metadata_value(key, new.value),
                                            self._json(
                                                (
                                                    (old.source.value if old.source else None),
                                                    old.review_status.value,
                                                )
                                            ),
                                            self._json(
                                                (
                                                    (new.source.value if new.source else None),
                                                    new.review_status.value,
                                                )
                                            ),
                                            revision,
                                            result.view_model.revision,
                                        ),
                                    )
                        self._record_suggestion_changes(
                            transaction,
                            batch_id,
                            track_id,
                            suggestion_before,
                        )
                        transaction.execute("RELEASE SAVEPOINT metadata_batch_track")
                    except PermissionError:
                        transaction.execute("ROLLBACK TO SAVEPOINT metadata_batch_track")
                        transaction.execute("RELEASE SAVEPOINT metadata_batch_track")
                        protected += 1
                    except MetadataRevisionConflict:
                        transaction.execute("ROLLBACK TO SAVEPOINT metadata_batch_track")
                        transaction.execute("RELEASE SAVEPOINT metadata_batch_track")
                        conflicts += 1
                    except Exception:
                        transaction.execute("ROLLBACK TO SAVEPOINT metadata_batch_track")
                        transaction.execute("RELEASE SAVEPOINT metadata_batch_track")
                        failed += 1
            if progress is not None:
                progress(min(offset + BATCH_CHUNK_SIZE, len(expected)), len(expected))
        status = (
            "COMPLETED"
            if failed == 0 and cancelled == 0 and protected == 0 and conflicts == 0
            else "PARTIAL"
        )
        with self._database.connect() as connection:
            connection.execute(
                """UPDATE metadata_batch_actions SET status=?,finished_at=CURRENT_TIMESTAMP,
                changed_count=?,skipped_count=?,failed_count=?,cancelled=?,summary_json=?
                WHERE id=?""",
                (
                    status,
                    changed,
                    unchanged + protected + conflicts + cancelled,
                    failed,
                    int(cancelled > 0),
                    json.dumps(
                        {
                            "chunks": chunks,
                            "revision_conflicts": conflicts,
                            "protected": protected,
                            "cancelled": cancelled,
                        }
                    ),
                    batch_id,
                ),
            )
        checked = len(expected) - cancelled
        duration = monotonic() - started
        self._diagnostics = MaintenanceDiagnostics(
            self.repository.last_query_duration_ms,
            self.repository.last_result_count,
            self._diagnostics.preview_duration_ms,
            duration * 1000.0,
            chunks,
            changed,
            unchanged + protected + conflicts + failed + cancelled,
            conflicts,
            maximum_visible_rows=12,
        )
        return BatchResult(
            batch_id,
            status,
            len(expected),
            checked,
            changed,
            unchanged,
            protected,
            conflicts,
            0,
            failed,
            cancelled,
            duration,
            chunks,
            (
                (BatchSkipReason.UNCHANGED, unchanged),
                (BatchSkipReason.PROTECTED, protected),
                (BatchSkipReason.REVISION_CONFLICT, conflicts),
                (BatchSkipReason.FAILED, failed),
                (BatchSkipReason.CANCELLED, cancelled),
            ),
        )

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))

    @staticmethod
    def _suggestion_snapshot(
        connection: sqlite3.Connection,
        track_id: int,
        actions: tuple[StagedSuggestionAction, ...],
    ) -> dict[int, tuple[str, str | None, str | None, bool]]:
        if not actions:
            return {}
        action_ids = {item.suggestion_id for item in actions}
        placeholders = ",".join("?" for _ in action_ids)
        chosen = connection.execute(
            f"SELECT field_key FROM track_metadata_suggestions WHERE id IN ({placeholders})",
            tuple(sorted(action_ids)),
        ).fetchall()
        fields = {str(row["field_key"]) for row in chosen}
        if not fields:
            return {}
        field_placeholders = ",".join("?" for _ in fields)
        rows = connection.execute(
            f"""SELECT id,status,decided_at,decision_reason
                FROM track_metadata_suggestions
                WHERE track_id=? AND field_key IN ({field_placeholders})
                AND (id IN ({placeholders}) OR status='PENDING')""",
            (track_id, *sorted(fields), *sorted(action_ids)),
        ).fetchall()
        return {
            int(row["id"]): (
                str(row["status"]),
                str(row["decided_at"]) if row["decided_at"] is not None else None,
                (str(row["decision_reason"]) if row["decision_reason"] is not None else None),
                int(row["id"]) not in action_ids,
            )
            for row in rows
        }

    @staticmethod
    def _record_suggestion_changes(
        connection: sqlite3.Connection,
        batch_id: int,
        track_id: int,
        before: dict[int, tuple[str, str | None, str | None, bool]],
    ) -> None:
        for suggestion_id, previous in before.items():
            row = connection.execute(
                """SELECT field_key,status,decided_at,decision_reason
                   FROM track_metadata_suggestions WHERE id=?""",
                (suggestion_id,),
            ).fetchone()
            if row is None:
                raise MetadataRevisionConflict("Vorschlag wurde zwischenzeitlich entfernt")
            current = (
                str(row["status"]),
                str(row["decided_at"]) if row["decided_at"] is not None else None,
                (str(row["decision_reason"]) if row["decision_reason"] is not None else None),
            )
            if current == previous[:3]:
                continue
            connection.execute(
                """INSERT INTO metadata_batch_suggestion_changes
                    (batch_id,track_id,suggestion_id,field_key,previous_status,new_status,
                     previous_decided_at,new_decided_at,previous_decision_reason,
                     new_decision_reason,superseded_by_acceptance)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    track_id,
                    suggestion_id,
                    str(row["field_key"]),
                    previous[0],
                    current[0],
                    previous[1],
                    current[1],
                    previous[2],
                    current[2],
                    int(previous[3] and current[0] == "SUPERSEDED"),
                ),
            )

    def _changes(
        self, track_id: int, revision: int, request: MetadataBatchRequest
    ) -> TrackMetadataChanges:
        values = dict(request.values)
        model = self._editor.load(track_id)
        if request.action in {
            BatchAction.SUGGESTION_ACCEPT,
            BatchAction.SUGGESTION_ACCEPT_CONFIRM,
            BatchAction.SUGGESTION_REJECT,
            BatchAction.SUGGESTION_DEFER,
        }:
            actions = self._suggestion_actions(request, model)
            if request.action is not BatchAction.SUGGESTION_REJECT and any(
                model.field(s.field_key).protected
                for s in model.suggestions
                if s.suggestion_id in {a.suggestion_id for a in actions}
            ):
                raise PermissionError("Geschützter Metadatenwert")
            return TrackMetadataChanges(revision, suggestion_actions=actions)
        if request.action in {
            BatchAction.SET,
            BatchAction.MULTI_ADD,
            BatchAction.MULTI_REMOVE,
            BatchAction.MULTI_REPLACE,
        } and any(model.field(key).protected for key in request.field_mask):
            raise PermissionError("Geschützter Metadatenwert")
        if request.action is BatchAction.SET:
            return TrackMetadataChanges(revision, {key: values[key] for key in request.field_mask})
        if request.action is BatchAction.CONFIRM:
            return TrackMetadataChanges(revision, confirmations=request.field_mask)
        if request.action in {
            BatchAction.MULTI_ADD,
            BatchAction.MULTI_REMOVE,
            BatchAction.MULTI_REPLACE,
        }:
            replacements: dict[MetadataFieldKey, tuple[object, ...]] = {}
            for key in request.field_mask:
                current = model.field(key).value
                if not isinstance(current, tuple):
                    raise ValueError("Mehrfachaktion benötigt ein Mehrfachfeld")
                target = normalize_metadata_value(key, values[key])
                if not isinstance(target, tuple):
                    raise ValueError("Mehrfachwert erwartet")
                if request.action is BatchAction.MULTI_ADD:
                    combined = (*current, *target)
                elif request.action is BatchAction.MULTI_REMOVE:
                    combined = tuple(item for item in current if item not in target)
                else:
                    combined = target
                normalized = normalize_metadata_value(key, combined)
                assert isinstance(normalized, tuple)
                replacements[key] = normalized
            return TrackMetadataChanges(revision, multivalue_values=replacements)
        mode = (
            ValueRemovalMode.CONFIRMED_EMPTY
            if request.action is BatchAction.CONFIRM_EMPTY
            else ValueRemovalMode.MISSING
        )
        return TrackMetadataChanges(revision, removals={key: mode for key in request.field_mask})

    @staticmethod
    def _suggestion_actions(
        request: MetadataBatchRequest,
        model: TrackMetadataEditorViewModel,
    ) -> tuple[StagedSuggestionAction, ...]:
        if request.action is BatchAction.SUGGESTION_DEFER:
            return ()
        action = {
            BatchAction.SUGGESTION_ACCEPT: SuggestionEditorAction.ACCEPT,
            BatchAction.SUGGESTION_ACCEPT_CONFIRM: SuggestionEditorAction.ACCEPT_AND_CONFIRM,
            BatchAction.SUGGESTION_REJECT: SuggestionEditorAction.REJECT,
        }[request.action]
        matching = tuple(
            StagedSuggestionAction(suggestion.suggestion_id, action)
            for suggestion in model.suggestions
            if suggestion.field_key in request.field_mask
            and (
                request.minimum_confidence is None
                or suggestion.confidence >= request.minimum_confidence
            )
        )
        if action is SuggestionEditorAction.REJECT:
            return matching
        # One accepted value per field; accepting it supersedes the other open proposals.
        selected: dict[MetadataFieldKey, tuple[float, StagedSuggestionAction]] = {}
        by_id = {item.suggestion_id: item for item in model.suggestions}
        for staged in matching:
            suggestion = by_id[staged.suggestion_id]
            current = selected.get(suggestion.field_key)
            if current is None or suggestion.confidence > current[0]:
                selected[suggestion.field_key] = (suggestion.confidence, staged)
        return tuple(item[1] for item in selected.values())

    def preview_undo(self) -> UndoPreview | None:
        with self._database.connect() as connection:
            batch = connection.execute(
                """SELECT id FROM metadata_batch_actions
                WHERE status IN ('COMPLETED','PARTIAL') AND undone_by_batch_id IS NULL
                AND action_type <> 'UNDO'
                AND (
                    EXISTS (SELECT 1 FROM metadata_batch_changes c
                            WHERE c.batch_id=metadata_batch_actions.id)
                    OR EXISTS (SELECT 1 FROM metadata_batch_suggestion_changes sc
                               WHERE sc.batch_id=metadata_batch_actions.id)
                )
                ORDER BY finished_at DESC,id DESC LIMIT 1"""
            ).fetchone()
            if batch is None:
                return None
            field_rows = connection.execute(
                "SELECT * FROM metadata_batch_changes WHERE batch_id=? ORDER BY track_id,id",
                (int(batch["id"]),),
            ).fetchall()
            suggestion_rows = connection.execute(
                """SELECT * FROM metadata_batch_suggestion_changes
                   WHERE batch_id=? ORDER BY track_id,id""",
                (int(batch["id"]),),
            ).fetchall()
            suggestion_current = {
                int(row["id"]): row
                for row in connection.execute(
                    "SELECT id,status,decided_at,decision_reason FROM track_metadata_suggestions"
                ).fetchall()
            }
        by_track: dict[int, list[sqlite3.Row]] = {}
        proposal_by_track: dict[int, list[sqlite3.Row]] = {}
        for row in field_rows:
            by_track.setdefault(int(row["track_id"]), []).append(row)
        for row in suggestion_rows:
            proposal_by_track.setdefault(int(row["track_id"]), []).append(row)
        changeable: set[int] = set()
        conflicts: set[int] = set()
        for track_id in set(by_track) | set(proposal_by_track):
            model = self._editor.load(track_id)
            valid = all(
                model.revision == int(row["revision_after"])
                and serialize_metadata_value(
                    MetadataFieldKey(str(row["field_key"])),
                    model.field(MetadataFieldKey(str(row["field_key"]))).value,
                )
                == str(row["new_value_json"])
                for row in by_track.get(track_id, ())
            )
            for row in proposal_by_track.get(track_id, ()):
                current = suggestion_current.get(int(row["suggestion_id"]))
                valid = (
                    valid
                    and current is not None
                    and (
                        str(current["status"]),
                        current["decided_at"],
                        current["decision_reason"],
                    )
                    == (
                        str(row["new_status"]),
                        row["new_decided_at"],
                        row["new_decision_reason"],
                    )
                )
            (changeable if valid else conflicts).add(track_id)
        return UndoPreview(
            int(batch["id"]),
            len(changeable),
            len(conflicts),
            len(field_rows) + len(suggestion_rows),
        )

    def undo(self, preview: UndoPreview) -> BatchResult:
        started = monotonic()
        current = self.preview_undo()
        if current != preview:
            raise ValueError("Rückgängig-Vorschau ist veraltet")
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM metadata_batch_changes WHERE batch_id=? ORDER BY track_id,id",
                (preview.batch_id,),
            ).fetchall()
            suggestion_rows = connection.execute(
                """SELECT * FROM metadata_batch_suggestion_changes
                   WHERE batch_id=? ORDER BY track_id,id""",
                (preview.batch_id,),
            ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = {}
        suggestion_grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(int(row["track_id"]), []).append(row)
        for row in suggestion_rows:
            suggestion_grouped.setdefault(int(row["track_id"]), []).append(row)
        changed = conflicts = 0
        track_ids = set(grouped) | set(suggestion_grouped)
        for track_id in sorted(track_ids):
            track_rows = grouped.get(track_id, [])
            proposal_rows = suggestion_grouped.get(track_id, [])
            model = self._editor.load(track_id)
            if any(model.revision != int(row["revision_after"]) for row in track_rows):
                conflicts += 1
                continue
            with self._database.connect() as connection:
                proposal_conflict = any(
                    not self._suggestion_matches_after(connection, row) for row in proposal_rows
                )
            if proposal_conflict:
                conflicts += 1
                continue
            scalar: dict[MetadataFieldKey, object] = {}
            multiple: dict[MetadataFieldKey, tuple[object, ...]] = {}
            removals: dict[MetadataFieldKey, ValueRemovalMode] = {}
            for row in track_rows:
                key = MetadataFieldKey(str(row["field_key"]))
                if serialize_metadata_value(key, model.field(key).value) != str(
                    row["new_value_json"]
                ):
                    conflicts += 1
                    break
                previous = deserialize_metadata_value(key, str(row["previous_value_json"]))
                if previous is None or previous == ():
                    removals[key] = ValueRemovalMode.MISSING
                elif isinstance(previous, tuple):
                    multiple[key] = previous
                else:
                    scalar[key] = previous
            else:
                try:
                    with self._database.transaction() as connection:
                        result = self._editor.save(
                            track_id,
                            TrackMetadataChanges(
                                model.revision, scalar, multiple, removals=removals
                            ),
                        )
                        for row in proposal_rows:
                            if not self._suggestion_matches_after(connection, row):
                                raise MetadataRevisionConflict(
                                    "Vorschlag wurde zwischenzeitlich entschieden"
                                )
                            connection.execute(
                                """UPDATE track_metadata_suggestions
                                   SET status=?,decided_at=?,decision_reason=? WHERE id=?""",
                                (
                                    str(row["previous_status"]),
                                    row["previous_decided_at"],
                                    row["previous_decision_reason"],
                                    int(row["suggestion_id"]),
                                ),
                            )
                        for row in track_rows:
                            key = MetadataFieldKey(str(row["field_key"]))
                            source, status = json.loads(str(row["previous_state_json"]))
                            if source is None:
                                connection.execute(
                                    "DELETE FROM track_metadata_field_state WHERE track_id=? AND field_key=?",
                                    (track_id, key.value),
                                )
                            else:
                                _write_state(
                                    connection,
                                    MetadataFieldState(
                                        track_id,
                                        key,
                                        MetadataSource(source),
                                        "batch_undo",
                                        None,
                                        MetadataReviewStatus(status),
                                    ),
                                )
                    changed += int(result.revision_changed or bool(proposal_rows))
                except (MetadataRevisionConflict, ValueError):
                    conflicts += 1
                    continue
        status = "COMPLETED" if conflicts == 0 else "PARTIAL"
        with self._database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO metadata_batch_actions
                (action_type,status,selection_json,field_mask_json,preview_token,planned_count,
                 changed_count,skipped_count,finished_at,summary_json)
                VALUES ('UNDO',?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)""",
                (
                    status,
                    json.dumps({"undo_of": preview.batch_id}),
                    "[]",
                    uuid4().hex,
                    len(track_ids),
                    changed,
                    conflicts,
                    json.dumps({"conflicts": conflicts}),
                ),
            )
            assert cursor.lastrowid is not None
            undo_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE metadata_batch_actions SET undone_by_batch_id=? WHERE id=?",
                (undo_id, preview.batch_id),
            )
        return BatchResult(
            undo_id,
            status,
            len(track_ids),
            len(track_ids),
            changed,
            0,
            0,
            conflicts,
            0,
            0,
            0,
            monotonic() - started,
            1,
            ((BatchSkipReason.REVISION_CONFLICT, conflicts),),
        )

    @staticmethod
    def _suggestion_matches_after(connection: sqlite3.Connection, history: sqlite3.Row) -> bool:
        current = connection.execute(
            """SELECT status,decided_at,decision_reason
               FROM track_metadata_suggestions WHERE id=?""",
            (int(history["suggestion_id"]),),
        ).fetchone()
        return current is not None and (
            str(current["status"]),
            current["decided_at"],
            current["decision_reason"],
        ) == (
            str(history["new_status"]),
            history["new_decided_at"],
            history["new_decision_reason"],
        )
