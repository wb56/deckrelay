"""Read models and atomic change sets for single-track metadata editing."""

from dataclasses import dataclass, field
from enum import StrEnum
import sqlite3

from party_player.database.connection import Database
from party_player.metadata_persistence import (
    MetadataFieldState,
    MetadataRevisionConflict,
    MetadataTermType,
    _SCALAR_COLUMNS,
    _TERM_FIELDS,
    _check_revision,
    _increment_revision,
    _write_state,
    deserialize_metadata_value,
)
from party_player.metadata_rules import (
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
    RecordingClassification,
    RecordingKind,
    RecordingTrait,
    normalize_metadata_value,
    release_decade,
)


SOURCE_LABELS = {
    MetadataSource.FILE_TAG: "Aus Dateitag übernommen",
    MetadataSource.AUDIO_ANALYSIS: "Automatisch analysiert",
    MetadataSource.EXTERNAL_MUSIC_DATABASE: "Aus externer Musikdatenbank",
    MetadataSource.FILE_OR_FOLDER_DERIVATION: "Aus Dateiname abgeleitet",
    MetadataSource.MANUAL_INPUT: "Manuell eingetragen",
    MetadataSource.MANUAL_CONFIRMATION: "Manuell bestätigt",
}

STATUS_LABELS = {
    MetadataReviewStatus.MISSING: "Fehlt / ungeprüft",
    MetadataReviewStatus.IMPORTED: "Importiert",
    MetadataReviewStatus.ANALYSED: "Analysiert",
    MetadataReviewStatus.SUGGESTED: "Neuer Vorschlag vorhanden",
    MetadataReviewStatus.REVIEW_REQUIRED: "Prüfung erforderlich",
    MetadataReviewStatus.CONFIRMED_WITH_VALUE: "Manuell bestätigt",
    MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE: "Bewusst ohne Wert bestätigt",
    MetadataReviewStatus.CONFLICTING: "Widersprüchliche Angaben",
    MetadataReviewStatus.FAILED: "Ermittlung fehlgeschlagen",
    MetadataReviewStatus.OUTDATED: "Veraltet",
}

FIELD_LABELS = {
    MetadataFieldKey.TITLE: "Titel",
    MetadataFieldKey.ARTIST: "Interpret",
    MetadataFieldKey.ALBUM: "Album",
    MetadataFieldKey.YEAR: "Ausgabejahr",
    MetadataFieldKey.ORIGINAL_RELEASE_YEAR: "Ursprüngliches Erscheinungsjahr",
    MetadataFieldKey.RECORDING_CLASSIFICATION: "Aufnahmeart",
    MetadataFieldKey.BPM: "Wirksame BPM",
    MetadataFieldKey.BPM_CONFIDENCE: "BPM-Konfidenz",
    MetadataFieldKey.ALTERNATIVE_BPM: "Alternative BPM",
    MetadataFieldKey.MAIN_GENRE: "Hauptgenre",
    MetadataFieldKey.ENERGY: "Energie",
    MetadataFieldKey.DANCEABILITY: "Tanzbarkeit",
    MetadataFieldKey.LANGUAGE: "Sprache",
    MetadataFieldKey.RATING: "Bewertung",
    MetadataFieldKey.COMMENT: "Kommentar",
    MetadataFieldKey.MUSICAL_DECADES: "Musikalische Dekaden",
    MetadataFieldKey.ADDITIONAL_GENRES: "Zusätzliche Genres/Stile",
    MetadataFieldKey.MOODS: "Stimmungen",
    MetadataFieldKey.TAGS: "Freie Tags",
}


@dataclass(frozen=True, slots=True)
class MetadataFieldViewModel:
    key: MetadataFieldKey
    value: object
    source: MetadataSource | None
    review_status: MetadataReviewStatus
    source_text: str
    status_text: str
    protected: bool
    has_suggestion: bool = False


@dataclass(frozen=True, slots=True)
class MetadataSuggestionViewModel:
    suggestion_id: int
    field_key: MetadataFieldKey
    current_value: object
    suggested_value: object
    source_text: str
    confidence: float
    source_detail: str
    created_at: str
    protected_conflict: bool


@dataclass(frozen=True, slots=True)
class TrackMetadataEditorViewModel:
    track_id: int
    revision: int
    fields: tuple[MetadataFieldViewModel, ...]
    suggestions: tuple[MetadataSuggestionViewModel, ...]

    def field(self, key: MetadataFieldKey) -> MetadataFieldViewModel:
        return next(field for field in self.fields if field.key is key)

    @property
    def release_decade(self) -> int | None:
        value = self.field(MetadataFieldKey.ORIGINAL_RELEASE_YEAR).value
        return release_decade(value if isinstance(value, int) else None)


class ValueRemovalMode(StrEnum):
    MISSING = "MISSING"
    CONFIRMED_EMPTY = "CONFIRMED_EMPTY"


class SuggestionEditorAction(StrEnum):
    ACCEPT = "ACCEPT"
    ACCEPT_AND_CONFIRM = "ACCEPT_AND_CONFIRM"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class StagedSuggestionAction:
    suggestion_id: int
    action: SuggestionEditorAction
    allow_protected_override: bool = False


@dataclass(frozen=True, slots=True)
class TrackMetadataChanges:
    original_revision: int
    scalar_values: dict[MetadataFieldKey, object] = field(default_factory=dict)
    multivalue_values: dict[MetadataFieldKey, tuple[object, ...]] = field(default_factory=dict)
    confirmations: frozenset[MetadataFieldKey] = frozenset()
    removals: dict[MetadataFieldKey, ValueRemovalMode] = field(default_factory=dict)
    suggestion_actions: tuple[StagedSuggestionAction, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.scalar_values
            or self.multivalue_values
            or self.confirmations
            or self.removals
            or self.suggestion_actions
        )


@dataclass(frozen=True, slots=True)
class MetadataSaveResult:
    view_model: TrackMetadataEditorViewModel
    changed_fields: frozenset[MetadataFieldKey]
    revision_changed: bool


class MetadataEditorService:
    def __init__(self, database: Database) -> None:
        self._database = database

    def load(self, track_id: int) -> TrackMetadataEditorViewModel:
        with self._database.connect() as connection:
            return self._load(connection, track_id)

    def save(self, track_id: int, changes: TrackMetadataChanges) -> MetadataSaveResult:
        if changes.empty:
            return MetadataSaveResult(self.load(track_id), frozenset(), False)
        self._validate_changes(changes)
        changed: set[MetadataFieldKey] = set()
        effective_changed = False
        with self._database.transaction() as connection:
            _check_revision(connection, track_id, changes.original_revision)
            for key, value in changes.scalar_values.items():
                self._write_scalar(connection, track_id, key, normalize_metadata_value(key, value))
                self._manual_state(
                    connection,
                    track_id,
                    key,
                    confirmed=True,
                    source=MetadataSource.MANUAL_INPUT,
                )
                changed.add(key)
                effective_changed = True
            for key, values in changes.multivalue_values.items():
                normalized = normalize_metadata_value(key, values)
                assert isinstance(normalized, tuple)
                self._replace_terms(connection, track_id, key, normalized)
                self._manual_state(
                    connection,
                    track_id,
                    key,
                    confirmed=True,
                    source=MetadataSource.MANUAL_INPUT,
                )
                changed.add(key)
                effective_changed = True
            for key in changes.confirmations:
                current = self._effective_value(connection, track_id, key)
                if self._missing(current):
                    raise ValueError("Ein fehlender Wert benötigt „Ohne Wert bestätigen“")
                current_state = connection.execute(
                    """SELECT source_type, review_status
                       FROM track_metadata_field_state
                       WHERE track_id = ? AND field_key = ?""",
                    (track_id, key.value),
                ).fetchone()
                if (
                    current_state is not None
                    and MetadataReviewStatus(str(current_state["review_status"]))
                    is MetadataReviewStatus.CONFIRMED_WITH_VALUE
                ):
                    continue
                self._manual_state(connection, track_id, key, confirmed=True)
                changed.add(key)
                effective_changed = True
            for key, mode in changes.removals.items():
                self._clear_value(connection, track_id, key)
                self._manual_state(
                    connection,
                    track_id,
                    key,
                    confirmed=mode is ValueRemovalMode.CONFIRMED_EMPTY,
                    empty=True,
                )
                changed.add(key)
                effective_changed = True
            for staged in changes.suggestion_actions:
                suggestion_changed = self._apply_suggestion_action(
                    connection, track_id, staged, changed
                )
                effective_changed = effective_changed or suggestion_changed
            if effective_changed:
                _increment_revision(connection, track_id)
            result = self._load(connection, track_id)
        return MetadataSaveResult(result, frozenset(changed), effective_changed)

    @staticmethod
    def _validate_changes(changes: TrackMetadataChanges) -> None:
        touched = set(changes.scalar_values) | set(changes.multivalue_values)
        touched |= set(changes.confirmations) | set(changes.removals)
        expected = (
            len(changes.scalar_values)
            + len(changes.multivalue_values)
            + len(changes.confirmations)
            + len(changes.removals)
        )
        if len(touched) != expected:
            raise ValueError("Ein Metadatenfeld enthält widersprüchliche lokale Aktionen")
        if MetadataFieldKey.BPM_CONFIDENCE in touched:
            raise ValueError("BPM-Konfidenz ist ein schreibgeschützter Analysewert")

    def _load(self, connection: sqlite3.Connection, track_id: int) -> TrackMetadataEditorViewModel:
        row = connection.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            raise KeyError(f"Titel {track_id} wurde nicht gefunden")
        state_rows = {
            MetadataFieldKey(str(item["field_key"])): item
            for item in connection.execute(
                "SELECT * FROM track_metadata_field_state WHERE track_id = ?", (track_id,)
            ).fetchall()
        }
        suggestion_rows = connection.execute(
            """SELECT suggestion.*, run.analysis_version
               FROM track_metadata_suggestions AS suggestion
               JOIN metadata_analysis_runs AS run ON run.id = suggestion.analysis_run_id
               WHERE suggestion.track_id = ? AND suggestion.status = 'PENDING'
               ORDER BY suggestion.created_at, suggestion.id""",
            (track_id,),
        ).fetchall()
        suggestion_keys = {MetadataFieldKey(str(item["field_key"])) for item in suggestion_rows}
        fields: list[MetadataFieldViewModel] = []
        for key in MetadataFieldKey:
            value = self._effective_value(connection, track_id, key, track_row=row)
            state = state_rows.get(key)
            source = MetadataSource(str(state["source_type"])) if state is not None else None
            status = (
                MetadataReviewStatus(str(state["review_status"]))
                if state is not None
                else MetadataReviewStatus.MISSING
            )
            fields.append(
                MetadataFieldViewModel(
                    key,
                    value,
                    source,
                    status,
                    SOURCE_LABELS[source] if source is not None else "Keine Herkunft gespeichert",
                    STATUS_LABELS[status],
                    status.protects_value,
                    key in suggestion_keys,
                )
            )
        by_key = {field.key: field for field in fields}
        suggestions = tuple(
            MetadataSuggestionViewModel(
                int(item["id"]),
                key := MetadataFieldKey(str(item["field_key"])),
                by_key[key].value,
                deserialize_metadata_value(key, str(item["serialized_value"])),
                SOURCE_LABELS[MetadataSource(str(item["source_type"]))],
                float(item["confidence"]),
                str(item["source_detail"] or "")[:160],
                str(item["created_at"]),
                by_key[key].protected,
            )
            for item in suggestion_rows
        )
        return TrackMetadataEditorViewModel(
            track_id,
            int(row["metadata_revision"]),
            tuple(fields),
            suggestions,
        )

    def _effective_value(
        self,
        connection: sqlite3.Connection,
        track_id: int,
        key: MetadataFieldKey,
        *,
        track_row: sqlite3.Row | None = None,
    ) -> object:
        if key in _TERM_FIELDS:
            term_type = _TERM_FIELDS[key]
            rows = connection.execute(
                """SELECT display_name, numeric_value FROM metadata_terms AS term
                   JOIN track_metadata_terms AS assignment ON assignment.term_id = term.id
                   WHERE assignment.track_id = ? AND term.term_type = ?
                   ORDER BY term.normalized_key""",
                (track_id, term_type.value),
            ).fetchall()
            if term_type is MetadataTermType.MUSICAL_DECADE:
                return tuple(int(item["numeric_value"]) for item in rows)
            return tuple(str(item["display_name"]) for item in rows)
        row = (
            track_row
            or connection.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        )
        if row is None:
            raise KeyError(f"Titel {track_id} wurde nicht gefunden")
        if key is MetadataFieldKey.RECORDING_CLASSIFICATION:
            traits = (
                frozenset({RecordingTrait.REMASTERED})
                if bool(row["is_remastered"])
                else frozenset()
            )
            return RecordingClassification(RecordingKind(str(row["recording_type"])), traits)
        return row[_SCALAR_COLUMNS[key]]

    @staticmethod
    def _write_scalar(
        connection: sqlite3.Connection, track_id: int, key: MetadataFieldKey, value: object
    ) -> None:
        if key is MetadataFieldKey.RECORDING_CLASSIFICATION:
            assert isinstance(value, RecordingClassification)
            connection.execute(
                "UPDATE tracks SET recording_type = ?, is_remastered = ? WHERE id = ?",
                (value.kind.value, int(RecordingTrait.REMASTERED in value.traits), track_id),
            )
            return
        if value is None and key in {
            MetadataFieldKey.TITLE,
            MetadataFieldKey.ARTIST,
            MetadataFieldKey.ALBUM,
            MetadataFieldKey.MAIN_GENRE,
        }:
            value = ""
        connection.execute(
            f"UPDATE tracks SET {_SCALAR_COLUMNS[key]} = ? WHERE id = ?", (value, track_id)
        )

    def _clear_value(
        self, connection: sqlite3.Connection, track_id: int, key: MetadataFieldKey
    ) -> None:
        if key in _TERM_FIELDS:
            self._replace_terms(connection, track_id, key, ())
        elif key is MetadataFieldKey.RECORDING_CLASSIFICATION:
            self._write_scalar(
                connection, track_id, key, RecordingClassification(RecordingKind.UNKNOWN)
            )
        else:
            self._write_scalar(connection, track_id, key, None)

    @staticmethod
    def _manual_state(
        connection: sqlite3.Connection,
        track_id: int,
        key: MetadataFieldKey,
        *,
        confirmed: bool,
        empty: bool = False,
        source: MetadataSource | None = None,
    ) -> None:
        status = (
            MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE
            if confirmed and empty
            else (
                MetadataReviewStatus.CONFIRMED_WITH_VALUE
                if confirmed
                else MetadataReviewStatus.MISSING
            )
        )
        _write_state(
            connection,
            MetadataFieldState(
                track_id,
                key,
                source
                or (
                    MetadataSource.MANUAL_CONFIRMATION if confirmed else MetadataSource.MANUAL_INPUT
                ),
                "track_editor",
                None,
                status,
            ),
        )

    @staticmethod
    def _replace_terms(
        connection: sqlite3.Connection,
        track_id: int,
        key: MetadataFieldKey,
        values: tuple[object, ...],
    ) -> None:
        term_type = _TERM_FIELDS[key]
        connection.execute(
            """DELETE FROM track_metadata_terms WHERE track_id = ? AND term_id IN
               (SELECT id FROM metadata_terms WHERE term_type = ?)""",
            (track_id, term_type.value),
        )
        for value in values:
            display = str(value)
            normalized = display.casefold()
            numeric = int(str(value)) if term_type is MetadataTermType.MUSICAL_DECADE else None
            connection.execute(
                """INSERT INTO metadata_terms
                       (term_type, normalized_key, display_name, numeric_value)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(term_type, normalized_key) DO NOTHING""",
                (term_type.value, normalized, display, numeric),
            )
            term = connection.execute(
                "SELECT id FROM metadata_terms WHERE term_type = ? AND normalized_key = ?",
                (term_type.value, normalized),
            ).fetchone()
            assert term is not None
            connection.execute(
                "INSERT INTO track_metadata_terms (track_id, term_id) VALUES (?, ?)",
                (track_id, int(term["id"])),
            )

    def _apply_suggestion_action(
        self,
        connection: sqlite3.Connection,
        track_id: int,
        staged: StagedSuggestionAction,
        changed: set[MetadataFieldKey],
    ) -> bool:
        row = connection.execute(
            """SELECT * FROM track_metadata_suggestions
               WHERE id = ? AND track_id = ? AND status = 'PENDING'""",
            (staged.suggestion_id, track_id),
        ).fetchone()
        if row is None:
            raise ValueError("Vorschlag ist nicht mehr offen")
        if staged.action is SuggestionEditorAction.REJECT:
            connection.execute(
                """UPDATE track_metadata_suggestions
                   SET status = 'REJECTED', decided_at = CURRENT_TIMESTAMP,
                       decision_reason = 'Im Titeleditor abgelehnt' WHERE id = ?""",
                (staged.suggestion_id,),
            )
            return False
        key = MetadataFieldKey(str(row["field_key"]))
        state = connection.execute(
            """SELECT review_status FROM track_metadata_field_state
               WHERE track_id = ? AND field_key = ?""",
            (track_id, key.value),
        ).fetchone()
        protected = (
            state is not None and MetadataReviewStatus(str(state["review_status"])).protects_value
        )
        if protected and not staged.allow_protected_override:
            raise MetadataRevisionConflict(
                "Geschützter Wert benötigt eine ausdrückliche Konfliktbestätigung"
            )
        value = deserialize_metadata_value(key, str(row["serialized_value"]))
        if key in _TERM_FIELDS:
            assert isinstance(value, tuple)
            self._replace_terms(connection, track_id, key, value)
        else:
            self._write_scalar(connection, track_id, key, value)
        confirmed = staged.action is SuggestionEditorAction.ACCEPT_AND_CONFIRM
        if confirmed:
            self._manual_state(
                connection,
                track_id,
                key,
                confirmed=True,
                source=MetadataSource.MANUAL_CONFIRMATION,
            )
        else:
            suggestion_source = MetadataSource(str(row["source_type"]))
            _write_state(
                connection,
                MetadataFieldState(
                    track_id,
                    key,
                    suggestion_source,
                    str(row["source_detail"]),
                    float(row["confidence"]),
                    (
                        MetadataReviewStatus.ANALYSED
                        if suggestion_source is MetadataSource.AUDIO_ANALYSIS
                        else MetadataReviewStatus.IMPORTED
                    ),
                ),
            )
        connection.execute(
            """UPDATE track_metadata_suggestions
               SET status = 'ACCEPTED', decided_at = CURRENT_TIMESTAMP,
                   review_status = ? WHERE id = ?""",
            (
                (
                    MetadataReviewStatus.CONFIRMED_WITH_VALUE.value
                    if confirmed
                    else MetadataReviewStatus.IMPORTED.value
                ),
                staged.suggestion_id,
            ),
        )
        connection.execute(
            """UPDATE track_metadata_suggestions
               SET status = 'SUPERSEDED', decided_at = CURRENT_TIMESTAMP,
                   decision_reason = 'Durch angenommenen Vorschlag abgelöst'
               WHERE track_id = ? AND field_key = ? AND status = 'PENDING' AND id <> ?""",
            (track_id, key.value, staged.suggestion_id),
        )
        changed.add(key)
        return True

    @staticmethod
    def _missing(value: object) -> bool:
        if value is None or value == "" or value == ():
            return True
        return isinstance(value, RecordingClassification) and (
            value.kind is RecordingKind.UNKNOWN and not value.traits
        )
