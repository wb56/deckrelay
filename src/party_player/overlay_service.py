"""Validated overlay catalog snapshots for controllers and UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from party_player.overlay import (
    SUPPORTED_OVERLAY_FORMATS,
    OverlayRecord,
)
from party_player.repositories.overlay_repository import OverlayRepository


@dataclass(frozen=True, slots=True)
class OverlayCatalogSnapshot:
    """Immutable event payload; no UI callback needs to query SQLite."""

    records: tuple[OverlayRecord, ...]
    categories: tuple[str, ...]
    favorites: tuple[OverlayRecord | None, ...]
    missing_file_ids: frozenset[int]

    def records_for_category(self, category: str) -> tuple[OverlayRecord, ...]:
        if not category or category == "Alle":
            return self.records
        key = category.casefold()
        return tuple(
            record for record in self.records if record.definition.category.casefold() == key
        )


class OverlayService:
    """Apply domain validation and produce complete overlay catalog events."""

    FULL_VOLUME_WITHOUT_DUCKING_WARNING = (
        "100 % Overlay-Lautstärke ohne Musikabsenkung kann den Summenpegel übersteuern."
    )

    def __init__(self, repository: OverlayRepository) -> None:
        self._repository = repository

    def snapshot(self, *, enabled_only: bool = True) -> OverlayCatalogSnapshot:
        all_records = tuple(self._repository.list_all(enabled_only=False))
        records = (
            tuple(record for record in all_records if record.enabled)
            if enabled_only
            else all_records
        )
        categories = tuple(
            sorted(
                {record.definition.category for record in records if record.definition.category},
                key=str.casefold,
            )
        )
        favorites: list[OverlayRecord | None] = [None] * 6
        for record in all_records:
            if record.favorite_position is not None:
                favorites[record.favorite_position - 1] = record
        missing = frozenset(
            record.definition.overlay_id
            for record in all_records
            if not Path(record.definition.file_path).is_file()
        )
        return OverlayCatalogSnapshot(records, categories, tuple(favorites), missing)

    def save(self, record: OverlayRecord) -> OverlayRecord:
        self.validate(record)
        return self._repository.save(record)

    @classmethod
    def safety_warning(cls, record: OverlayRecord) -> str:
        """Return a non-blocking level warning for a valid but risky setting."""

        definition = record.definition
        if definition.volume_percent == 100 and not definition.ducking_enabled:
            return cls.FULL_VOLUME_WITHOUT_DUCKING_WARNING
        return ""

    def set_enabled(self, overlay_id: int, enabled: bool) -> OverlayRecord:
        return self._repository.set_enabled(overlay_id, enabled)

    def delete(self, overlay_id: int, *, active_overlay_id: int | None = None) -> bool:
        if overlay_id == active_overlay_id:
            raise ValueError("Ein laufendes Overlay muss vor dem Entfernen gestoppt werden")
        return self._repository.delete(overlay_id)

    def by_name(self, name: str, *, enabled_only: bool = True) -> OverlayRecord | None:
        key = name.strip().casefold()
        return next(
            (
                record
                for record in self._repository.list_all(enabled_only=enabled_only)
                if record.definition.name.casefold() == key
            ),
            None,
        )

    @staticmethod
    def validate(record: OverlayRecord) -> None:
        definition = record.definition
        if not definition.name.strip():
            raise ValueError("Overlay-Name darf nicht leer sein")
        path = Path(definition.file_path)
        if path.suffix.lower() not in SUPPORTED_OVERLAY_FORMATS:
            raise ValueError("Overlays müssen MP3- oder FLAC-Dateien sein")
        if not 0 <= definition.volume_percent <= 100:
            raise ValueError("Lautstärke muss zwischen 0 und 100 Prozent liegen")
        for name, value in (
            ("Fade-in", definition.fade_in_ms),
            ("Fade-out", definition.fade_out_ms),
            ("Ducking-Attack", definition.ducking_attack_ms),
            ("Ducking-Release", definition.ducking_release_ms),
        ):
            if not 0 <= value <= 60_000:
                raise ValueError(f"{name} muss zwischen 0 und 60000 ms liegen")
        if definition.cue_in_ms < 0:
            raise ValueError("Cue-In darf nicht negativ sein")
        if definition.cue_out_ms is not None and definition.cue_out_ms <= definition.cue_in_ms:
            raise ValueError("Cue-Out muss nach Cue-In liegen")
        if not -60.0 <= definition.ducking_db <= 0.0:
            raise ValueError("Ducking muss zwischen -60 und 0 dB liegen")
        if record.favorite_position is not None and not 1 <= record.favorite_position <= 6:
            raise ValueError("Favoritenposition muss zwischen 1 und 6 liegen")
        if record.keyboard_shortcut is not None:
            expected = (
                f"Ctrl+{record.favorite_position}" if record.favorite_position is not None else None
            )
            if expected is None or record.keyboard_shortcut.casefold() != expected.casefold():
                raise ValueError("Tastenkürzel muss zur Favoritenposition passen")
