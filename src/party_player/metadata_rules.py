"""Pure catalog-metadata vocabulary, validation, and suggestion decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Final


MINIMUM_YEAR: Final = 1877
MAXIMUM_YEAR: Final = 2100
MINIMUM_BPM: Final = 20.0
MAXIMUM_BPM: Final = 300.0
MAXIMUM_COMMENT_LENGTH: Final = 2_000


class MetadataFieldKey(StrEnum):
    TITLE = "title"
    ARTIST = "artist"
    ALBUM = "album"
    YEAR = "year"
    ORIGINAL_RELEASE_YEAR = "original_release_year"
    RECORDING_CLASSIFICATION = "recording_classification"
    BPM = "bpm"
    BPM_CONFIDENCE = "bpm_confidence"
    ALTERNATIVE_BPM = "alternative_bpm"
    MAIN_GENRE = "main_genre"
    ENERGY = "energy"
    DANCEABILITY = "danceability"
    LANGUAGE = "language"
    RATING = "rating"
    COMMENT = "comment"
    MUSICAL_DECADES = "musical_decades"
    ADDITIONAL_GENRES = "additional_genres"
    MOODS = "moods"
    TAGS = "tags"


class MetadataValueType(StrEnum):
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    TEXT = "TEXT"
    RECORDING_CLASSIFICATION = "RECORDING_CLASSIFICATION"
    TEXT_SET = "TEXT_SET"
    INTEGER_SET = "INTEGER_SET"


class EmptyValueBehavior(StrEnum):
    CLEAR = "CLEAR"
    EMPTY_COLLECTION = "EMPTY_COLLECTION"


class MetadataSource(StrEnum):
    FILE_TAG = "FILE_TAG"
    AUDIO_ANALYSIS = "AUDIO_ANALYSIS"
    EXTERNAL_MUSIC_DATABASE = "EXTERNAL_MUSIC_DATABASE"
    FILE_OR_FOLDER_DERIVATION = "FILE_OR_FOLDER_DERIVATION"
    MANUAL_INPUT = "MANUAL_INPUT"
    MANUAL_CONFIRMATION = "MANUAL_CONFIRMATION"


class MetadataReviewStatus(StrEnum):
    MISSING = "MISSING"
    IMPORTED = "IMPORTED"
    ANALYSED = "ANALYSED"
    SUGGESTED = "SUGGESTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONFIRMED_WITH_VALUE = "CONFIRMED_WITH_VALUE"
    CONFIRMED_WITHOUT_VALUE = "CONFIRMED_WITHOUT_VALUE"
    CONFLICTING = "CONFLICTING"
    FAILED = "FAILED"
    OUTDATED = "OUTDATED"

    @property
    def protects_value(self) -> bool:
        return self in {
            MetadataReviewStatus.CONFIRMED_WITH_VALUE,
            MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE,
        }


class RecordingKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    RE_RECORDING = "RE_RECORDING"
    LIVE = "LIVE"
    REMIX = "REMIX"
    RADIO_EDIT = "RADIO_EDIT"
    UNKNOWN = "UNKNOWN"


class RecordingTrait(StrEnum):
    REMASTERED = "REMASTERED"


@dataclass(frozen=True, slots=True)
class RecordingClassification:
    """One primary recording kind plus independent production traits."""

    kind: RecordingKind
    traits: frozenset[RecordingTrait] = frozenset()


@dataclass(frozen=True, slots=True)
class MetadataFieldDefinition:
    key: MetadataFieldKey
    value_type: MetadataValueType
    multiple: bool
    minimum: float | None = None
    maximum: float | None = None
    automatic_suggestion_allowed: bool = True
    automatic_application_allowed: bool = False
    manual_confirmation_protects: bool = True
    minimum_confidence: float = 0.8
    empty_behavior: EmptyValueBehavior = EmptyValueBehavior.CLEAR


def _field(
    key: MetadataFieldKey,
    value_type: MetadataValueType,
    *,
    multiple: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
    suggest: bool = True,
    apply: bool = False,
    confidence: float = 0.8,
) -> MetadataFieldDefinition:
    return MetadataFieldDefinition(
        key,
        value_type,
        multiple,
        minimum,
        maximum,
        suggest,
        apply,
        True,
        confidence,
        EmptyValueBehavior.EMPTY_COLLECTION if multiple else EmptyValueBehavior.CLEAR,
    )


FIELD_DEFINITIONS = MappingProxyType(
    {
        MetadataFieldKey.TITLE: _field(MetadataFieldKey.TITLE, MetadataValueType.TEXT),
        MetadataFieldKey.ARTIST: _field(MetadataFieldKey.ARTIST, MetadataValueType.TEXT),
        MetadataFieldKey.ALBUM: _field(MetadataFieldKey.ALBUM, MetadataValueType.TEXT),
        MetadataFieldKey.YEAR: _field(
            MetadataFieldKey.YEAR,
            MetadataValueType.INTEGER,
            minimum=MINIMUM_YEAR,
            maximum=MAXIMUM_YEAR,
            apply=True,
        ),
        MetadataFieldKey.ORIGINAL_RELEASE_YEAR: _field(
            MetadataFieldKey.ORIGINAL_RELEASE_YEAR,
            MetadataValueType.INTEGER,
            minimum=MINIMUM_YEAR,
            maximum=MAXIMUM_YEAR,
            apply=True,
        ),
        MetadataFieldKey.RECORDING_CLASSIFICATION: _field(
            MetadataFieldKey.RECORDING_CLASSIFICATION,
            MetadataValueType.RECORDING_CLASSIFICATION,
        ),
        MetadataFieldKey.BPM: _field(
            MetadataFieldKey.BPM,
            MetadataValueType.NUMBER,
            minimum=MINIMUM_BPM,
            maximum=MAXIMUM_BPM,
            apply=True,
            confidence=0.9,
        ),
        MetadataFieldKey.BPM_CONFIDENCE: _field(
            MetadataFieldKey.BPM_CONFIDENCE,
            MetadataValueType.NUMBER,
            minimum=0.0,
            maximum=1.0,
            apply=True,
        ),
        MetadataFieldKey.ALTERNATIVE_BPM: _field(
            MetadataFieldKey.ALTERNATIVE_BPM,
            MetadataValueType.NUMBER,
            minimum=MINIMUM_BPM,
            maximum=MAXIMUM_BPM,
        ),
        MetadataFieldKey.MAIN_GENRE: _field(MetadataFieldKey.MAIN_GENRE, MetadataValueType.TEXT),
        MetadataFieldKey.ENERGY: _field(
            MetadataFieldKey.ENERGY,
            MetadataValueType.INTEGER,
            minimum=0,
            maximum=100,
            apply=True,
        ),
        MetadataFieldKey.DANCEABILITY: _field(
            MetadataFieldKey.DANCEABILITY,
            MetadataValueType.INTEGER,
            minimum=0,
            maximum=100,
            apply=True,
        ),
        MetadataFieldKey.LANGUAGE: _field(MetadataFieldKey.LANGUAGE, MetadataValueType.TEXT),
        MetadataFieldKey.RATING: _field(
            MetadataFieldKey.RATING,
            MetadataValueType.INTEGER,
            minimum=1,
            maximum=5,
            suggest=False,
        ),
        MetadataFieldKey.COMMENT: _field(
            MetadataFieldKey.COMMENT, MetadataValueType.TEXT, suggest=False
        ),
        MetadataFieldKey.MUSICAL_DECADES: _field(
            MetadataFieldKey.MUSICAL_DECADES,
            MetadataValueType.INTEGER_SET,
            multiple=True,
            minimum=MINIMUM_YEAR // 10 * 10,
            maximum=MAXIMUM_YEAR // 10 * 10,
        ),
        MetadataFieldKey.ADDITIONAL_GENRES: _field(
            MetadataFieldKey.ADDITIONAL_GENRES,
            MetadataValueType.TEXT_SET,
            multiple=True,
        ),
        MetadataFieldKey.MOODS: _field(
            MetadataFieldKey.MOODS, MetadataValueType.TEXT_SET, multiple=True
        ),
        MetadataFieldKey.TAGS: _field(
            MetadataFieldKey.TAGS,
            MetadataValueType.TEXT_SET,
            multiple=True,
            suggest=False,
        ),
    }
)


class FileTagImportDecisionKind(StrEnum):
    APPLY = "APPLY"
    UNCHANGED = "UNCHANGED"
    PROPOSE = "PROPOSE"
    IGNORE_MISSING = "IGNORE_MISSING"


@dataclass(frozen=True, slots=True)
class FileTagImportDecision:
    kind: FileTagImportDecisionKind
    normalized_value: object
    reason: str


def decide_file_tag_import(
    key: MetadataFieldKey,
    imported_value: object,
    current: EffectiveMetadataValue | None,
    *,
    new_track: bool,
) -> FileTagImportDecision:
    """Decide one file-tag field without persistence or import orchestration."""
    normalized = normalize_metadata_value(key, imported_value)
    missing = normalized is None or normalized == ()
    if missing:
        return FileTagImportDecision(
            FileTagImportDecisionKind.IGNORE_MISSING,
            normalized,
            "Leerer Dateitag verändert keinen Katalogwert",
        )
    if new_track:
        return FileTagImportDecision(
            FileTagImportDecisionKind.APPLY,
            normalized,
            "Gültiger Ausgangswert einer neuen Datei",
        )
    if current is None:
        return FileTagImportDecision(
            FileTagImportDecisionKind.PROPOSE,
            normalized,
            "Bestandswert ohne Feldstatus wird konservativ erhalten",
        )
    current_value = normalize_metadata_value(key, current.value)
    if current_value == normalized:
        return FileTagImportDecision(
            FileTagImportDecisionKind.UNCHANGED,
            normalized,
            "Normalisierter Dateitag ist unverändert",
        )
    if current.protected:
        return FileTagImportDecision(
            FileTagImportDecisionKind.PROPOSE,
            normalized,
            "Manuell bestätigter Wert oder Leerwert bleibt geschützt",
        )
    if (
        current.source is MetadataSource.FILE_TAG
        and current.review_status is MetadataReviewStatus.IMPORTED
    ):
        return FileTagImportDecision(
            FileTagImportDecisionKind.APPLY,
            normalized,
            "Geänderter ungeschützter Dateitag wird aktualisiert",
        )
    return FileTagImportDecision(
        FileTagImportDecisionKind.PROPOSE,
        normalized,
        "Abweichender Bestandswert benötigt eine Prüfung",
    )


def release_decade(original_release_year: int | None) -> int | None:
    """Derive the searchable release decade; it is never an edited field."""
    if original_release_year is None:
        return None
    _validate_number(original_release_year, MINIMUM_YEAR, MAXIMUM_YEAR, integer=True)
    return original_release_year // 10 * 10


def normalize_metadata_value(key: MetadataFieldKey, value: object) -> object:
    """Validate and return one canonical value for a declared catalog field."""
    definition = FIELD_DEFINITIONS[key]
    if value is None:
        return () if definition.multiple else None
    if definition.value_type is MetadataValueType.INTEGER:
        return _validate_number(value, definition.minimum, definition.maximum, integer=True)
    if definition.value_type is MetadataValueType.NUMBER:
        return _validate_number(value, definition.minimum, definition.maximum, integer=False)
    if definition.value_type is MetadataValueType.TEXT:
        text = _normalize_text(value)
        if not text:
            return None
        if key is MetadataFieldKey.COMMENT and len(text) > MAXIMUM_COMMENT_LENGTH:
            raise ValueError(f"Kommentar darf höchstens {MAXIMUM_COMMENT_LENGTH} Zeichen enthalten")
        return text
    if definition.value_type is MetadataValueType.RECORDING_CLASSIFICATION:
        if not isinstance(value, RecordingClassification):
            raise TypeError("Aufnahmeart benötigt eine RecordingClassification")
        return value
    if definition.value_type is MetadataValueType.TEXT_SET:
        return _normalize_text_values(value)
    if definition.value_type is MetadataValueType.INTEGER_SET:
        return _normalize_decades(value, definition)
    raise AssertionError(f"Unbekannter Metadatentyp: {definition.value_type}")


def _validate_number(
    value: object,
    minimum: float | None,
    maximum: float | None,
    *,
    integer: bool,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Numerischer Metadatenwert erwartet")
    numeric = float(value)
    if not isfinite(numeric):
        raise ValueError("Metadatenwert muss endlich sein")
    if integer and not numeric.is_integer():
        raise ValueError("Ganzzahliger Metadatenwert erwartet")
    if minimum is not None and numeric < minimum or maximum is not None and numeric > maximum:
        raise ValueError(f"Metadatenwert liegt außerhalb des Bereichs {minimum} bis {maximum}")
    return int(numeric) if integer else numeric


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Textwert erwartet")
    return " ".join(value.split())


def _collection(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError("Mehrwertiges Feld benötigt eine Sammlung")
    return tuple(value)


def _normalize_text_values(value: object) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _collection(value):
        text = _normalize_text(item)
        if not text:
            raise ValueError("Mehrwertige Felder dürfen keine leeren Elemente enthalten")
        identity = text.casefold()
        if identity not in seen:
            seen.add(identity)
            result.append(text)
    return tuple(result)


def _normalize_decades(value: object, definition: MetadataFieldDefinition) -> tuple[int, ...]:
    result: set[int] = set()
    for item in _collection(value):
        decade = _validate_number(item, definition.minimum, definition.maximum, integer=True)
        assert isinstance(decade, int)
        if decade % 10:
            raise ValueError("Musikalische Dekaden müssen auf 0 enden")
        result.add(decade)
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class EffectiveMetadataValue:
    value: object
    source: MetadataSource
    review_status: MetadataReviewStatus

    @property
    def protected(self) -> bool:
        return self.review_status.protects_value


@dataclass(frozen=True, slots=True)
class MetadataSuggestion:
    value: object
    source: MetadataSource
    confidence: float
    analysis_version: str


class SuggestionDecisionKind(StrEnum):
    APPLIED = "APPLIED"
    PROPOSED = "PROPOSED"
    REJECTED = "REJECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class MetadataSuggestionDecision:
    kind: SuggestionDecisionKind
    normalized_value: object
    reason: str


def decide_metadata_suggestion(
    key: MetadataFieldKey,
    current: EffectiveMetadataValue | None,
    suggestion: MetadataSuggestion,
) -> MetadataSuggestionDecision:
    """Decide without persistence whether an automatic suggestion may become effective."""
    definition = FIELD_DEFINITIONS[key]
    try:
        normalized = normalize_metadata_value(key, suggestion.value)
    except (TypeError, ValueError) as exc:
        return MetadataSuggestionDecision(SuggestionDecisionKind.REJECTED, None, str(exc))
    if not 0.0 <= suggestion.confidence <= 1.0:
        return MetadataSuggestionDecision(
            SuggestionDecisionKind.REJECTED,
            normalized,
            "Konfidenz muss zwischen 0 und 1 liegen",
        )
    if not suggestion.analysis_version.strip():
        return MetadataSuggestionDecision(
            SuggestionDecisionKind.REJECTED,
            normalized,
            "Analyseversion fehlt",
        )
    if not definition.automatic_suggestion_allowed:
        return MetadataSuggestionDecision(
            SuggestionDecisionKind.REJECTED,
            normalized,
            "Automatische Vorschläge sind für dieses Feld nicht zulässig",
        )
    if current is not None and current.protected:
        return MetadataSuggestionDecision(
            SuggestionDecisionKind.PROPOSED,
            normalized,
            "Manuell bestätigter Wert oder Leerwert bleibt geschützt",
        )
    if current is not None and current.review_status in {
        MetadataReviewStatus.CONFLICTING,
        MetadataReviewStatus.REVIEW_REQUIRED,
    }:
        return MetadataSuggestionDecision(
            SuggestionDecisionKind.REVIEW_REQUIRED,
            normalized,
            "Bestehender Konflikt muss fachlich geprüft werden",
        )
    if current is not None and current.value is not None:
        current_value = normalize_metadata_value(key, current.value)
        if current_value != normalized:
            return MetadataSuggestionDecision(
                SuggestionDecisionKind.REVIEW_REQUIRED,
                normalized,
                "Vorschlag widerspricht dem wirksamen Wert",
            )
        return MetadataSuggestionDecision(
            SuggestionDecisionKind.PROPOSED,
            normalized,
            "Wirksamer Wert ist bereits vorhanden",
        )
    if not definition.automatic_application_allowed:
        return MetadataSuggestionDecision(
            SuggestionDecisionKind.PROPOSED,
            normalized,
            "Feld erlaubt keine automatische Übernahme",
        )
    if suggestion.confidence < definition.minimum_confidence:
        return MetadataSuggestionDecision(
            SuggestionDecisionKind.PROPOSED,
            normalized,
            "Mindestkonfidenz ist nicht erreicht",
        )
    return MetadataSuggestionDecision(
        SuggestionDecisionKind.APPLIED,
        normalized,
        "Vorschlag darf automatisch wirksam werden",
    )
