"""Persistent, path-free catalog scope for automatic title selection."""

from __future__ import annotations

from dataclasses import dataclass

from party_player.models import Track
from party_player.selection_metadata import SelectionMetadataSnapshot


@dataclass(frozen=True, slots=True)
class SelectionCatalogFilter:
    genres: tuple[str, ...] = ()
    moods: tuple[str, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    bpm_from: float | None = None
    bpm_to: float | None = None
    energy_from: int | None = None
    energy_to: int | None = None
    minimum_rating: int | None = None
    include_missing: bool = False

    @property
    def active(self) -> bool:
        return any(
            (
                self.genres,
                self.moods,
                self.year_from is not None,
                self.year_to is not None,
                self.bpm_from is not None,
                self.bpm_to is not None,
                self.energy_from is not None,
                self.energy_to is not None,
                self.minimum_rating is not None,
            )
        )

    def summary(self) -> str:
        parts: list[str] = []
        if self.genres:
            parts.append("Genre: " + ", ".join(self.genres))
        if self.moods:
            parts.append("Stimmung: " + ", ".join(self.moods))
        self._append_range(parts, "Jahr", self.year_from, self.year_to)
        self._append_range(parts, "BPM", self.bpm_from, self.bpm_to)
        self._append_range(parts, "Energie", self.energy_from, self.energy_to)
        if self.minimum_rating is not None:
            parts.append(f"Bewertung ab {self.minimum_rating}")
        if self.include_missing and self.active:
            parts.append("fehlende Werte erlaubt")
        return " · ".join(parts) if parts else "gesamter Katalog"

    @staticmethod
    def _append_range(
        parts: list[str],
        label: str,
        minimum: int | float | None,
        maximum: int | float | None,
    ) -> None:
        if minimum is not None and maximum is not None:
            parts.append(f"{label}: {minimum:g}–{maximum:g}")
        elif minimum is not None:
            parts.append(f"{label} ab {minimum:g}")
        elif maximum is not None:
            parts.append(f"{label} bis {maximum:g}")

    def matches(self, track: Track, metadata: SelectionMetadataSnapshot | None) -> bool:
        genre_values = {track.genre.strip().casefold()} if track.genre.strip() else set()
        if metadata is not None:
            if metadata.main_genre.usable and metadata.main_genre.value:
                genre_values.add(metadata.main_genre.value.strip().casefold())
            if metadata.additional_genres.usable:
                genre_values.update(
                    value.strip().casefold() for value in metadata.additional_genres.values
                )
        if self.genres and not self._terms_match(self.genres, genre_values):
            return False

        mood_values = (
            {value.strip().casefold() for value in metadata.moods.values}
            if metadata is not None and metadata.moods.usable
            else set()
        )
        if self.moods and not self._terms_match(self.moods, mood_values):
            return False

        year = track.original_release_year or track.year
        if not self._range_match(year, self.year_from, self.year_to):
            return False
        bpm = metadata.bpm.value if metadata is not None and metadata.bpm.usable else track.bpm
        if not self._range_match(bpm, self.bpm_from, self.bpm_to):
            return False
        energy = metadata.energy.value if metadata is not None and metadata.energy.usable else None
        if not self._range_match(energy, self.energy_from, self.energy_to):
            return False
        if self.minimum_rating is not None and (
            track.rating is None
            and not self.include_missing
            or track.rating is not None
            and track.rating < self.minimum_rating
        ):
            return False
        return True

    def _terms_match(self, requested: tuple[str, ...], values: set[str]) -> bool:
        if not values:
            return self.include_missing
        return bool({value.strip().casefold() for value in requested} & values)

    def _range_match(
        self, value: int | float | None, minimum: int | float | None, maximum: int | float | None
    ) -> bool:
        if minimum is None and maximum is None:
            return True
        if value is None:
            return self.include_missing
        return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
