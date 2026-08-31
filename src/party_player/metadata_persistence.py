"""Transactional persistence for catalog metadata and reviewed suggestions."""

from dataclasses import dataclass
from enum import StrEnum
import json
import sqlite3

from party_player.database.connection import Database
from party_player.metadata_rules import (
    EffectiveMetadataValue,
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
    MetadataSuggestion,
    RecordingClassification,
    RecordingKind,
    RecordingTrait,
    SuggestionDecisionKind,
    decide_metadata_suggestion,
    normalize_metadata_value,
)


class MetadataRevisionConflict(RuntimeError):
    """The catalog row changed after the caller read it."""


class MetadataTermType(StrEnum):
    MUSICAL_DECADE = "MUSICAL_DECADE"
    ADDITIONAL_GENRE = "ADDITIONAL_GENRE"
    MOOD = "MOOD"
    FREE_TAG = "FREE_TAG"


class AnalysisRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SuggestionStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True, slots=True)
class MetadataFieldState:
    track_id: int
    field_key: MetadataFieldKey
    source: MetadataSource
    source_detail: str
    confidence: float | None
    review_status: MetadataReviewStatus
    analysis_version: str | None = None
    confirmed_at: str | None = None
    updated_at: str | None = None

    def effective(self, value: object) -> EffectiveMetadataValue:
        return EffectiveMetadataValue(value, self.source, self.review_status)


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    run_id: int
    track_id: int
    analysis_profile: str
    analysis_version: str
    status: AnalysisRunStatus
    priority: int
    file_path_snapshot: str
    file_size: int
    file_modified_ns: int
    fingerprint: str | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class StoredMetadataSuggestion:
    suggestion_id: int
    track_id: int
    analysis_run_id: int
    field_key: MetadataFieldKey
    value: object
    source: MetadataSource
    source_detail: str
    confidence: float
    review_status: MetadataReviewStatus
    status: SuggestionStatus
    analysis_version: str


_SCALAR_COLUMNS = {
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

_TERM_FIELDS = {
    MetadataFieldKey.MUSICAL_DECADES: MetadataTermType.MUSICAL_DECADE,
    MetadataFieldKey.ADDITIONAL_GENRES: MetadataTermType.ADDITIONAL_GENRE,
    MetadataFieldKey.MOODS: MetadataTermType.MOOD,
    MetadataFieldKey.TAGS: MetadataTermType.FREE_TAG,
}


def _check_revision(
    connection: sqlite3.Connection, track_id: int, expected_revision: int | None
) -> int:
    row = connection.execute(
        "SELECT metadata_revision FROM tracks WHERE id = ?", (track_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Titel {track_id} wurde nicht gefunden")
    revision = int(row["metadata_revision"])
    if expected_revision is not None and revision != expected_revision:
        raise MetadataRevisionConflict(
            f"Metadatenrevision {revision} entspricht nicht {expected_revision}"
        )
    return revision


def _increment_revision(connection: sqlite3.Connection, track_id: int) -> int:
    connection.execute(
        "UPDATE tracks SET metadata_revision = metadata_revision + 1 WHERE id = ?",
        (track_id,),
    )
    row = connection.execute(
        "SELECT metadata_revision FROM tracks WHERE id = ?", (track_id,)
    ).fetchone()
    assert row is not None
    return int(row["metadata_revision"])


def _write_state(connection: sqlite3.Connection, state: MetadataFieldState) -> None:
    if state.confidence is not None and not 0.0 <= state.confidence <= 1.0:
        raise ValueError("Konfidenz muss zwischen 0 und 1 liegen")
    confirmed = "CURRENT_TIMESTAMP" if state.review_status.protects_value else "NULL"
    connection.execute(
        f"""INSERT INTO track_metadata_field_state
                (track_id, field_key, source_type, source_detail, confidence,
                 review_status, analysis_version, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, {confirmed})
            ON CONFLICT(track_id, field_key) DO UPDATE SET
                source_type = excluded.source_type,
                source_detail = excluded.source_detail,
                confidence = excluded.confidence,
                review_status = excluded.review_status,
                analysis_version = excluded.analysis_version,
                confirmed_at = excluded.confirmed_at,
                updated_at = CURRENT_TIMESTAMP""",
        (
            state.track_id,
            state.field_key.value,
            state.source.value,
            state.source_detail.strip(),
            state.confidence,
            state.review_status.value,
            state.analysis_version.strip() if state.analysis_version else None,
        ),
    )


class MetadataFieldStateRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, track_id: int, field_key: MetadataFieldKey) -> MetadataFieldState | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT track_id, field_key, source_type, source_detail, confidence,
                          review_status, analysis_version, confirmed_at, updated_at
                   FROM track_metadata_field_state
                   WHERE track_id = ? AND field_key = ?""",
                (track_id, field_key.value),
            ).fetchone()
        if row is None:
            return None
        return MetadataFieldState(
            int(row["track_id"]),
            MetadataFieldKey(str(row["field_key"])),
            MetadataSource(str(row["source_type"])),
            str(row["source_detail"]),
            float(row["confidence"]) if row["confidence"] is not None else None,
            MetadataReviewStatus(str(row["review_status"])),
            str(row["analysis_version"]) if row["analysis_version"] is not None else None,
            str(row["confirmed_at"]) if row["confirmed_at"] is not None else None,
            str(row["updated_at"]),
        )


class EffectiveMetadataRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def revision(self, track_id: int) -> int:
        with self._database.connect() as connection:
            return _check_revision(connection, track_id, None)

    def get(self, track_id: int, field_key: MetadataFieldKey) -> object:
        with self._database.connect() as connection:
            if field_key is MetadataFieldKey.RECORDING_CLASSIFICATION:
                row = connection.execute(
                    "SELECT recording_type, is_remastered FROM tracks WHERE id = ?",
                    (track_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Titel {track_id} wurde nicht gefunden")
                traits = (
                    frozenset({RecordingTrait.REMASTERED})
                    if bool(row["is_remastered"])
                    else frozenset()
                )
                return RecordingClassification(RecordingKind(str(row["recording_type"])), traits)
            column = _SCALAR_COLUMNS.get(field_key)
            if column is None:
                raise ValueError("Mehrwertiges Feld benötigt das Zuordnungsrepository")
            row = connection.execute(
                f"SELECT {column} AS value FROM tracks WHERE id = ?", (track_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Titel {track_id} wurde nicht gefunden")
        return row["value"]

    def save(
        self,
        track_id: int,
        field_key: MetadataFieldKey,
        value: object,
        state: MetadataFieldState,
        *,
        expected_revision: int | None = None,
    ) -> int:
        if state.track_id != track_id or state.field_key is not field_key:
            raise ValueError("Feldstatus gehört nicht zum gespeicherten Feld")
        normalized = normalize_metadata_value(field_key, value)
        with self._database.transaction() as connection:
            _check_revision(connection, track_id, expected_revision)
            self._write_value(connection, track_id, field_key, normalized)
            _write_state(connection, state)
            return _increment_revision(connection, track_id)

    def save_confirmed_empty(
        self,
        track_id: int,
        field_key: MetadataFieldKey,
        *,
        expected_revision: int | None = None,
        source_detail: str = "",
    ) -> int:
        state = MetadataFieldState(
            track_id,
            field_key,
            MetadataSource.MANUAL_CONFIRMATION,
            source_detail,
            None,
            MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE,
        )
        return self.save(track_id, field_key, None, state, expected_revision=expected_revision)

    @staticmethod
    def _write_value(
        connection: sqlite3.Connection,
        track_id: int,
        field_key: MetadataFieldKey,
        value: object,
    ) -> None:
        if field_key is MetadataFieldKey.RECORDING_CLASSIFICATION:
            if value is None:
                value = RecordingClassification(RecordingKind.UNKNOWN)
            assert isinstance(value, RecordingClassification)
            connection.execute(
                "UPDATE tracks SET recording_type = ?, is_remastered = ? WHERE id = ?",
                (value.kind.value, int(RecordingTrait.REMASTERED in value.traits), track_id),
            )
            return
        column = _SCALAR_COLUMNS.get(field_key)
        if column is None:
            raise ValueError("Mehrwertiges Feld benötigt das Zuordnungsrepository")
        if value is None and field_key in {
            MetadataFieldKey.TITLE,
            MetadataFieldKey.ARTIST,
            MetadataFieldKey.ALBUM,
            MetadataFieldKey.MAIN_GENRE,
        }:
            value = ""
        connection.execute(f"UPDATE tracks SET {column} = ? WHERE id = ?", (value, track_id))


class MultiValueMetadataRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, track_id: int, field_key: MetadataFieldKey) -> tuple[object, ...]:
        term_type = self._term_type(field_key)
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT display_name, numeric_value FROM metadata_terms AS term
                   JOIN track_metadata_terms AS assignment ON assignment.term_id = term.id
                   WHERE assignment.track_id = ? AND term.term_type = ?
                   ORDER BY term.normalized_key""",
                (track_id, term_type.value),
            ).fetchall()
        if term_type is MetadataTermType.MUSICAL_DECADE:
            return tuple(int(row["numeric_value"]) for row in rows)
        return tuple(str(row["display_name"]) for row in rows)

    def replace(
        self,
        track_id: int,
        field_key: MetadataFieldKey,
        values: object,
        state: MetadataFieldState,
        *,
        expected_revision: int | None = None,
    ) -> int:
        if state.track_id != track_id or state.field_key is not field_key:
            raise ValueError("Feldstatus gehört nicht zum gespeicherten Feld")
        normalized = normalize_metadata_value(field_key, values)
        assert isinstance(normalized, tuple)
        with self._database.transaction() as connection:
            _check_revision(connection, track_id, expected_revision)
            self._replace(connection, track_id, field_key, normalized)
            _write_state(connection, state)
            return _increment_revision(connection, track_id)

    def add(
        self,
        track_id: int,
        field_key: MetadataFieldKey,
        values: object,
        state: MetadataFieldState,
        *,
        expected_revision: int | None = None,
    ) -> int:
        current = self.get(track_id, field_key)
        incoming = normalize_metadata_value(field_key, values)
        assert isinstance(incoming, tuple)
        return self.replace(
            track_id,
            field_key,
            current + incoming,
            state,
            expected_revision=expected_revision,
        )

    def remove(
        self,
        track_id: int,
        field_key: MetadataFieldKey,
        values: object,
        state: MetadataFieldState,
        *,
        expected_revision: int | None = None,
    ) -> int:
        current = self.get(track_id, field_key)
        removed = normalize_metadata_value(field_key, values)
        assert isinstance(removed, tuple)
        identities = {self._identity(value) for value in removed}
        retained = tuple(value for value in current if self._identity(value) not in identities)
        return self.replace(
            track_id,
            field_key,
            retained,
            state,
            expected_revision=expected_revision,
        )

    @staticmethod
    def _identity(value: object) -> str:
        return str(value).casefold()

    @staticmethod
    def _term_type(field_key: MetadataFieldKey) -> MetadataTermType:
        try:
            return _TERM_FIELDS[field_key]
        except KeyError as exc:
            raise ValueError("Einwertiges Feld benötigt das effektive Repository") from exc

    def _replace(
        self,
        connection: sqlite3.Connection,
        track_id: int,
        field_key: MetadataFieldKey,
        values: tuple[object, ...],
    ) -> None:
        term_type = self._term_type(field_key)
        connection.execute(
            """DELETE FROM track_metadata_terms
               WHERE track_id = ? AND term_id IN (
                   SELECT id FROM metadata_terms WHERE term_type = ?
               )""",
            (track_id, term_type.value),
        )
        for value in values:
            display = str(value)
            normalized_key = display.casefold()
            numeric: int | None = None
            if term_type is MetadataTermType.MUSICAL_DECADE:
                if not isinstance(value, int):
                    raise TypeError("Musikalische Dekade muss ganzzahlig sein")
                numeric = value
            connection.execute(
                """INSERT INTO metadata_terms
                       (term_type, normalized_key, display_name, numeric_value)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(term_type, normalized_key) DO NOTHING""",
                (term_type.value, normalized_key, display, numeric),
            )
            row = connection.execute(
                """SELECT id FROM metadata_terms
                   WHERE term_type = ? AND normalized_key = ?""",
                (term_type.value, normalized_key),
            ).fetchone()
            assert row is not None
            connection.execute(
                "INSERT INTO track_metadata_terms (track_id, term_id) VALUES (?, ?)",
                (track_id, int(row["id"])),
            )


class AnalysisRunRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self,
        track_id: int,
        analysis_profile: str,
        analysis_version: str,
        file_path_snapshot: str,
        file_size: int,
        file_modified_ns: int,
        *,
        priority: int = 0,
        fingerprint: str | None = None,
    ) -> AnalysisRun:
        if not analysis_profile.strip() or not analysis_version.strip():
            raise ValueError("Analyseprofil und Analyseversion dürfen nicht leer sein")
        if file_size < 0 or file_modified_ns < 0:
            raise ValueError("Dateigröße und Änderungszeit dürfen nicht negativ sein")
        with self._database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO metadata_analysis_runs
                       (track_id, analysis_profile, analysis_version, priority,
                        file_path_snapshot, file_size, file_modified_ns, fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    track_id,
                    analysis_profile.strip(),
                    analysis_version.strip(),
                    priority,
                    file_path_snapshot,
                    file_size,
                    file_modified_ns,
                    fingerprint.strip() if fingerprint else None,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Analyselauf konnte nicht angelegt werden")
            run_id = cursor.lastrowid
        return self.get(run_id)

    def get(self, run_id: int) -> AnalysisRun:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT id, track_id, analysis_profile, analysis_version, status,
                          priority, file_path_snapshot, file_size, file_modified_ns,
                          fingerprint, attempt_count
                   FROM metadata_analysis_runs WHERE id = ?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Analyselauf {run_id} wurde nicht gefunden")
        return AnalysisRun(
            int(row["id"]),
            int(row["track_id"]),
            str(row["analysis_profile"]),
            str(row["analysis_version"]),
            AnalysisRunStatus(str(row["status"])),
            int(row["priority"]),
            str(row["file_path_snapshot"]),
            int(row["file_size"]),
            int(row["file_modified_ns"]),
            str(row["fingerprint"]) if row["fingerprint"] is not None else None,
            int(row["attempt_count"]),
        )

    def start(self, run_id: int) -> AnalysisRun:
        with self._database.connect() as connection:
            connection.execute(
                """UPDATE metadata_analysis_runs
                   SET status = 'RUNNING', started_at = CURRENT_TIMESTAMP,
                       attempt_count = attempt_count + 1, error_code = NULL, error_text = NULL
                   WHERE id = ? AND status = 'PENDING'""",
                (run_id,),
            )
        return self.get(run_id)

    def finish(
        self,
        run_id: int,
        status: AnalysisRunStatus,
        *,
        error_code: str | None = None,
        error_text: str | None = None,
    ) -> AnalysisRun:
        if status not in {
            AnalysisRunStatus.COMPLETED,
            AnalysisRunStatus.FAILED,
            AnalysisRunStatus.CANCELLED,
        }:
            raise ValueError("Analyselauf benötigt einen abschließenden Status")
        if error_text is not None and len(error_text) > 500:
            raise ValueError("Analysetext darf höchstens 500 Zeichen enthalten")
        with self._database.connect() as connection:
            connection.execute(
                """UPDATE metadata_analysis_runs
                   SET status = ?, finished_at = CURRENT_TIMESTAMP,
                       error_code = ?, error_text = ? WHERE id = ?""",
                (status.value, error_code, error_text, run_id),
            )
        return self.get(run_id)


def serialize_metadata_value(field_key: MetadataFieldKey, value: object) -> str:
    normalized = normalize_metadata_value(field_key, value)
    if isinstance(normalized, RecordingClassification):
        payload: object = {
            "kind": normalized.kind.value,
            "traits": sorted(trait.value for trait in normalized.traits),
        }
    elif isinstance(normalized, tuple):
        payload = list(normalized)
    else:
        payload = normalized
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def deserialize_metadata_value(field_key: MetadataFieldKey, serialized: str) -> object:
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise ValueError("Vorschlagswert ist nicht kanonisch lesbar") from exc
    if field_key is MetadataFieldKey.RECORDING_CLASSIFICATION:
        if not isinstance(payload, dict) or set(payload) != {"kind", "traits"}:
            raise ValueError("Ungültige serialisierte Aufnahmeart")
        traits = payload["traits"]
        if not isinstance(traits, list):
            raise ValueError("Ungültige Merkmalsliste der Aufnahmeart")
        payload = RecordingClassification(
            RecordingKind(str(payload["kind"])),
            frozenset(RecordingTrait(str(trait)) for trait in traits),
        )
    return normalize_metadata_value(field_key, payload)


class MetadataSuggestionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save(
        self,
        track_id: int,
        analysis_run_id: int,
        field_key: MetadataFieldKey,
        value: object,
        source: MetadataSource,
        confidence: float,
        source_detail: str = "",
    ) -> StoredMetadataSuggestion:
        serialized = serialize_metadata_value(field_key, value)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("Konfidenz muss zwischen 0 und 1 liegen")
        with self._database.transaction() as connection:
            run = connection.execute(
                "SELECT track_id FROM metadata_analysis_runs WHERE id = ?",
                (analysis_run_id,),
            ).fetchone()
            if run is None or int(run["track_id"]) != track_id:
                raise ValueError("Analyselauf gehört nicht zum Titel")
            cursor = connection.execute(
                """INSERT INTO track_metadata_suggestions
                       (track_id, analysis_run_id, field_key, serialized_value,
                        source_type, source_detail, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    track_id,
                    analysis_run_id,
                    field_key.value,
                    serialized,
                    source.value,
                    source_detail.strip()[:200],
                    confidence,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Metadatenvorschlag konnte nicht angelegt werden")
            suggestion_id = cursor.lastrowid
        return self.get(suggestion_id)

    def get(self, suggestion_id: int) -> StoredMetadataSuggestion:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT suggestion.id, suggestion.track_id, suggestion.analysis_run_id,
                          suggestion.field_key, suggestion.serialized_value,
                          suggestion.source_type, suggestion.source_detail,
                          suggestion.confidence,
                          suggestion.review_status, suggestion.status, run.analysis_version
                   FROM track_metadata_suggestions AS suggestion
                   JOIN metadata_analysis_runs AS run ON run.id = suggestion.analysis_run_id
                   WHERE suggestion.id = ?""",
                (suggestion_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Metadatenvorschlag {suggestion_id} wurde nicht gefunden")
        field_key = MetadataFieldKey(str(row["field_key"]))
        return StoredMetadataSuggestion(
            int(row["id"]),
            int(row["track_id"]),
            int(row["analysis_run_id"]),
            field_key,
            deserialize_metadata_value(field_key, str(row["serialized_value"])),
            MetadataSource(str(row["source_type"])),
            str(row["source_detail"]),
            float(row["confidence"]),
            MetadataReviewStatus(str(row["review_status"])),
            SuggestionStatus(str(row["status"])),
            str(row["analysis_version"]),
        )

    def decide(self, suggestion_id: int, status: SuggestionStatus, reason: str = "") -> None:
        if status not in {SuggestionStatus.REJECTED, SuggestionStatus.SUPERSEDED}:
            raise ValueError("Diese Operation unterstützt nur Ablehnen oder Ablösen")
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE track_metadata_suggestions
                   SET status = ?, decided_at = CURRENT_TIMESTAMP, decision_reason = ?
                   WHERE id = ? AND status = 'PENDING'""",
                (status.value, reason.strip()[:500] or None, suggestion_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Nur offene Vorschläge können abgeschlossen werden")


class MetadataPersistenceService:
    """Coordinate metadata repositories in one bounded SQLite transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self.effective = EffectiveMetadataRepository(database)
        self.fields = MetadataFieldStateRepository(database)
        self.multivalues = MultiValueMetadataRepository(database)
        self.suggestions = MetadataSuggestionRepository(database)

    def accept_suggestion(self, suggestion_id: int, *, expected_revision: int | None = None) -> int:
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT suggestion.track_id, suggestion.field_key,
                          suggestion.serialized_value, suggestion.source_type,
                          suggestion.confidence, suggestion.status, run.analysis_version
                   FROM track_metadata_suggestions AS suggestion
                   JOIN metadata_analysis_runs AS run ON run.id = suggestion.analysis_run_id
                   WHERE suggestion.id = ?""",
                (suggestion_id,),
            ).fetchone()
            if row is None or str(row["status"]) != SuggestionStatus.PENDING.value:
                raise ValueError("Nur ein offener Vorschlag kann angenommen werden")
            track_id = int(row["track_id"])
            field_key = MetadataFieldKey(str(row["field_key"]))
            value = deserialize_metadata_value(field_key, str(row["serialized_value"]))
            _check_revision(connection, track_id, expected_revision)
            state_row = connection.execute(
                """SELECT source_type, review_status FROM track_metadata_field_state
                   WHERE track_id = ? AND field_key = ?""",
                (track_id, field_key.value),
            ).fetchone()
            current_value = self._current_value(connection, track_id, field_key)
            current = None
            if state_row is not None:
                current = EffectiveMetadataValue(
                    current_value,
                    MetadataSource(str(state_row["source_type"])),
                    MetadataReviewStatus(str(state_row["review_status"])),
                )
            elif self._has_effective_value(current_value):
                current = EffectiveMetadataValue(
                    current_value,
                    MetadataSource.FILE_TAG,
                    MetadataReviewStatus.IMPORTED,
                )
            proposal = MetadataSuggestion(
                value,
                MetadataSource(str(row["source_type"])),
                float(row["confidence"]),
                str(row["analysis_version"]),
            )
            decision = decide_metadata_suggestion(field_key, current, proposal)
            if current is not None and current.protected:
                raise MetadataRevisionConflict("Manuell bestätigter Metadatenwert ist geschützt")
            if decision.kind in {
                SuggestionDecisionKind.REJECTED,
                SuggestionDecisionKind.REVIEW_REQUIRED,
            }:
                raise ValueError(decision.reason)
            if field_key in _TERM_FIELDS:
                assert isinstance(value, tuple)
                self.multivalues._replace(connection, track_id, field_key, value)
            else:
                self.effective._write_value(connection, track_id, field_key, value)
            _write_state(
                connection,
                MetadataFieldState(
                    track_id,
                    field_key,
                    MetadataSource.MANUAL_CONFIRMATION,
                    "accepted_suggestion",
                    float(row["confidence"]),
                    MetadataReviewStatus.CONFIRMED_WITH_VALUE,
                    str(row["analysis_version"]),
                ),
            )
            connection.execute(
                """UPDATE track_metadata_suggestions
                   SET status = 'ACCEPTED', review_status = 'CONFIRMED_WITH_VALUE',
                       decided_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (suggestion_id,),
            )
            connection.execute(
                """UPDATE track_metadata_suggestions
                   SET status = 'SUPERSEDED', decided_at = CURRENT_TIMESTAMP,
                       decision_reason = 'Durch angenommenen Vorschlag abgelöst'
                   WHERE track_id = ? AND field_key = ? AND status = 'PENDING' AND id <> ?""",
                (track_id, field_key.value, suggestion_id),
            )
            return _increment_revision(connection, track_id)

    @staticmethod
    def _has_effective_value(value: object) -> bool:
        if value is None or value == "" or value == ():
            return False
        if isinstance(value, RecordingClassification):
            return value.kind is not RecordingKind.UNKNOWN or bool(value.traits)
        return True

    @staticmethod
    def _current_value(
        connection: sqlite3.Connection, track_id: int, field_key: MetadataFieldKey
    ) -> object:
        if field_key in _TERM_FIELDS:
            term_type = _TERM_FIELDS[field_key]
            rows = connection.execute(
                """SELECT display_name, numeric_value FROM metadata_terms AS term
                   JOIN track_metadata_terms AS assignment ON assignment.term_id = term.id
                   WHERE assignment.track_id = ? AND term.term_type = ?
                   ORDER BY term.normalized_key""",
                (track_id, term_type.value),
            ).fetchall()
            if term_type is MetadataTermType.MUSICAL_DECADE:
                return tuple(int(row["numeric_value"]) for row in rows)
            return tuple(str(row["display_name"]) for row in rows)
        if field_key is MetadataFieldKey.RECORDING_CLASSIFICATION:
            row = connection.execute(
                "SELECT recording_type, is_remastered FROM tracks WHERE id = ?", (track_id,)
            ).fetchone()
            assert row is not None
            traits = (
                frozenset({RecordingTrait.REMASTERED})
                if bool(row["is_remastered"])
                else frozenset()
            )
            return RecordingClassification(RecordingKind(str(row["recording_type"])), traits)
        column = _SCALAR_COLUMNS[field_key]
        row = connection.execute(
            f"SELECT {column} AS value FROM tracks WHERE id = ?", (track_id,)
        ).fetchone()
        assert row is not None
        return row["value"]
