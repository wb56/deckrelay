"""Immutable, quality-aware metadata snapshots for automatic selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from party_player.metadata_rules import MetadataReviewStatus, MetadataSource
from party_player.models import Track


class SelectionMetadataDisposition(StrEnum):
    """Machine-readable usability decision for one effective catalog field."""

    USABLE = "USABLE"
    MISSING = "MISSING"
    CONFIDENCE_TOO_LOW = "CONFIDENCE_TOO_LOW"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONFLICTING = "CONFLICTING"
    FAILED = "FAILED"
    OUTDATED = "OUTDATED"
    UNCONFIRMED_LEGACY_VALUE = "UNCONFIRMED_LEGACY_VALUE"
    CONFIRMED_WITHOUT_VALUE = "CONFIRMED_WITHOUT_VALUE"

    @property
    def usable(self) -> bool:
        return self is SelectionMetadataDisposition.USABLE


_SAFE_SOURCE_INFORMATION: dict[MetadataSource | None, str] = {
    None: "Keine sichere Herkunft gespeichert",
    MetadataSource.FILE_TAG: "Dateitag",
    MetadataSource.AUDIO_ANALYSIS: "Audioanalyse",
    MetadataSource.EXTERNAL_MUSIC_DATABASE: "Externe Musikdatenbank",
    MetadataSource.FILE_OR_FOLDER_DERIVATION: "Datei- oder Ordnerableitung",
    MetadataSource.MANUAL_INPUT: "Manuelle Eingabe",
    MetadataSource.MANUAL_CONFIRMATION: "Manuelle Bestätigung",
}


ValueT = TypeVar("ValueT")


@dataclass(frozen=True, slots=True)
class SelectionMetadataValue(Generic[ValueT]):
    """One scalar value without private source details or proposal payloads."""

    value: ValueT | None
    source: MetadataSource | None
    source_information: str
    confidence: float | None
    review_status: MetadataReviewStatus | None
    disposition: SelectionMetadataDisposition

    @property
    def usable(self) -> bool:
        return self.disposition.usable


@dataclass(frozen=True, slots=True)
class SelectionMetadataTerms:
    """One normalized, immutable multi-value metadata field."""

    values: tuple[str, ...]
    source: MetadataSource | None
    source_information: str
    confidence: float | None
    review_status: MetadataReviewStatus | None
    disposition: SelectionMetadataDisposition

    @property
    def usable(self) -> bool:
        return self.disposition.usable


@dataclass(frozen=True, slots=True)
class SelectionMetadataSnapshot:
    """Selection-relevant effective metadata for one track."""

    track_id: int
    main_genre: SelectionMetadataValue[str]
    additional_genres: SelectionMetadataTerms
    bpm: SelectionMetadataValue[float]
    alternative_bpm: SelectionMetadataValue[float]
    energy: SelectionMetadataValue[int]
    moods: SelectionMetadataTerms


@dataclass(frozen=True, slots=True)
class SelectionMetadataCatalogSnapshot:
    """One immutable, path-free metadata moment used by a complete selection."""

    metadata: tuple[SelectionMetadataSnapshot, ...]

    def __post_init__(self) -> None:
        track_ids = tuple(item.track_id for item in self.metadata)
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("Auswahlmetadaten dürfen einen Titel nur einmal enthalten")


def selection_metadata_value(
    value: ValueT | None,
    *,
    source: MetadataSource | None,
    confidence: float | None,
    review_status: MetadataReviewStatus | None,
    minimum_analysis_confidence: float | None = None,
) -> SelectionMetadataValue[ValueT]:
    """Apply the central, conservative usability policy to a scalar field."""
    disposition = _disposition(
        missing=value is None or value == "",
        source=source,
        confidence=confidence,
        review_status=review_status,
        minimum_analysis_confidence=minimum_analysis_confidence,
    )
    return SelectionMetadataValue(
        value,
        source,
        _SAFE_SOURCE_INFORMATION[source],
        confidence,
        review_status,
        disposition,
    )


def selection_metadata_terms(
    values: tuple[str, ...],
    *,
    source: MetadataSource | None,
    confidence: float | None,
    review_status: MetadataReviewStatus | None,
) -> SelectionMetadataTerms:
    """Normalize terms and apply the same usability policy as scalar fields."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(value.split())
        identity = text.casefold()
        if text and identity not in seen:
            seen.add(identity)
            normalized.append(text)
    terms = tuple(normalized)
    disposition = _disposition(
        missing=not terms,
        source=source,
        confidence=confidence,
        review_status=review_status,
        minimum_analysis_confidence=None,
    )
    return SelectionMetadataTerms(
        terms,
        source,
        _SAFE_SOURCE_INFORMATION[source],
        confidence,
        review_status,
        disposition,
    )


def neutral_catalog_snapshot(tracks: tuple[Track, ...]) -> SelectionMetadataCatalogSnapshot:
    """Build a query-free snapshot for compatible non-repository track sources."""
    snapshots = tuple(
        SelectionMetadataSnapshot(
            track.id,
            selection_metadata_value(
                track.genre or None, source=None, confidence=None, review_status=None
            ),
            selection_metadata_terms((), source=None, confidence=None, review_status=None),
            selection_metadata_value(track.bpm, source=None, confidence=None, review_status=None),
            selection_metadata_value(None, source=None, confidence=None, review_status=None),
            selection_metadata_value(None, source=None, confidence=None, review_status=None),
            selection_metadata_terms((), source=None, confidence=None, review_status=None),
        )
        for track in tracks
    )
    return SelectionMetadataCatalogSnapshot(snapshots)


def missing_metadata_snapshot(track_id: int) -> SelectionMetadataSnapshot:
    """Represent a candidate that disappeared while its snapshot was assembled."""
    return SelectionMetadataSnapshot(
        track_id,
        selection_metadata_value(None, source=None, confidence=None, review_status=None),
        selection_metadata_terms((), source=None, confidence=None, review_status=None),
        selection_metadata_value(None, source=None, confidence=None, review_status=None),
        selection_metadata_value(None, source=None, confidence=None, review_status=None),
        selection_metadata_value(None, source=None, confidence=None, review_status=None),
        selection_metadata_terms((), source=None, confidence=None, review_status=None),
    )


def _disposition(
    *,
    missing: bool,
    source: MetadataSource | None,
    confidence: float | None,
    review_status: MetadataReviewStatus | None,
    minimum_analysis_confidence: float | None,
) -> SelectionMetadataDisposition:
    if review_status is MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE:
        return SelectionMetadataDisposition.CONFIRMED_WITHOUT_VALUE
    if review_status is MetadataReviewStatus.CONFLICTING:
        return SelectionMetadataDisposition.CONFLICTING
    if review_status is MetadataReviewStatus.FAILED:
        return SelectionMetadataDisposition.FAILED
    if review_status is MetadataReviewStatus.OUTDATED:
        return SelectionMetadataDisposition.OUTDATED
    if review_status in {
        MetadataReviewStatus.REVIEW_REQUIRED,
        MetadataReviewStatus.SUGGESTED,
    }:
        return SelectionMetadataDisposition.REVIEW_REQUIRED
    if missing:
        return SelectionMetadataDisposition.MISSING
    if review_status is None:
        return SelectionMetadataDisposition.UNCONFIRMED_LEGACY_VALUE
    if review_status is MetadataReviewStatus.MISSING:
        return SelectionMetadataDisposition.MISSING
    if review_status is MetadataReviewStatus.ANALYSED and minimum_analysis_confidence is not None:
        if confidence is None or confidence < minimum_analysis_confidence:
            return SelectionMetadataDisposition.CONFIDENCE_TOO_LOW
    if review_status in {
        MetadataReviewStatus.IMPORTED,
        MetadataReviewStatus.ANALYSED,
        MetadataReviewStatus.CONFIRMED_WITH_VALUE,
    }:
        return SelectionMetadataDisposition.USABLE
    return SelectionMetadataDisposition.REVIEW_REQUIRED
