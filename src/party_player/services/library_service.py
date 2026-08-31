"""Music library business rules."""

from pathlib import Path
import logging
import math
import re
from collections.abc import Iterable

from tinytag import TinyTag, TinyTagException

from party_player.database.connection import Database
from party_player.models import Track
from party_player.loudness import LoudnessRepository
from party_player.metadata_import import (
    FileImportSnapshot,
    ImportedFieldValue,
    ImportedTrackData,
    MetadataImportOperation,
    MetadataImportOutcome,
    MetadataImportResult,
)
from party_player.metadata_rules import MetadataSource
from party_player.repositories.track_repository import TrackRepository


class LibraryService:
    """Provide paginated catalog data to controllers."""

    MAX_PAGE_SIZE = 200
    MINIMUM_REPLAYGAIN_DB = -60.0
    MAXIMUM_REPLAYGAIN_DB = 60.0
    MAXIMUM_REPLAYGAIN_PEAK = 64.0

    def __init__(
        self, repository: TrackRepository, loudness_repository: LoudnessRepository | None = None
    ) -> None:
        self._repository = repository
        self._loudness_repository = loudness_repository
        self._metadata_import = MetadataImportOperation(repository.database)
        self.last_import_result: MetadataImportResult | None = None
        self._logger = logging.getLogger(__name__)

    @property
    def database(self) -> Database:
        """Return the shared database for transaction-level controller services."""
        return self._repository.database

    def catalog_summary(self) -> str:
        """Return a localized catalog status."""
        return f"{self._repository.count():,} Titel im Partykatalog".replace(",", ".")

    def first_page(self, page_size: int = 100) -> list[Track]:
        """Load the first catalog page without reading the whole table."""
        bounded_size = max(1, min(page_size, self.MAX_PAGE_SIZE))
        return self._repository.find_page(bounded_size)

    def page(self, page_size: int = 100, offset: int = 0) -> list[Track]:
        bounded_size = max(1, min(page_size, self.MAX_PAGE_SIZE))
        return self._repository.find_page(bounded_size, max(0, offset))

    def count(self, query: str = "") -> int:
        return self._repository.search_count(query) if query.strip() else self._repository.count()

    def search(self, query: str, page_size: int = 100, offset: int = 0) -> list[Track]:
        bounded_size = max(1, min(page_size, self.MAX_PAGE_SIZE))
        return self._repository.search(query, bounded_size, max(0, offset))

    def get_track(self, track_id: int) -> Track | None:
        return self._repository.get(track_id)

    def get_tracks(self, track_ids: list[int]) -> dict[int, Track]:
        return self._repository.get_many(track_ids)

    def known_file_paths(self, file_paths: list[Path]) -> set[str]:
        """Return normalized paths that already have a catalog record."""
        resolved = [str(path.resolve()) for path in file_paths]
        return set(self._repository.get_by_file_paths(resolved))

    def remove_from_catalog(self, track_id: int) -> None:
        """Hide a catalog entry while retaining its file and relational history."""
        self._repository.hide_from_catalog(track_id)

    def import_file(self, file_path: Path) -> Track:
        """Import one user-selected MP3/FLAC without modifying the audio file."""
        result = self._import_file(file_path)
        if result.track is None:
            raise RuntimeError(result.error or "Katalogimport wurde nicht abgeschlossen")
        return result.track

    def import_file_with_result(self, file_path: Path) -> MetadataImportResult:
        """Import one file atomically and expose structured metadata decisions."""
        try:
            return self._import_file(file_path)
        except Exception as exc:
            result = MetadataImportResult(
                MetadataImportOutcome.FAILED,
                None,
                None,
                error=str(exc)[:500],
            )
            self.last_import_result = result
            return result

    def _import_file(self, file_path: Path) -> MetadataImportResult:
        path = file_path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Datei nicht gefunden: {path}")
        if path.suffix.lower() not in {".mp3", ".flac"}:
            raise ValueError("Es werden nur MP3- und FLAC-Dateien unterstützt.")
        if path.stat().st_size == 0:
            raise ValueError("Die Audiodatei ist leer oder beschädigt.")
        initial_snapshot = FileImportSnapshot.capture(path)
        try:
            tag = TinyTag.get(path)
        except (TinyTagException, OSError) as exc:
            raise ValueError(
                "Die Audiodatei ist beschädigt oder kann nicht gelesen werden."
            ) from exc
        if tag.duration is None or tag.duration <= 0:
            raise ValueError("Die Audiodatei enthält keine lesbare Audiospur.")
        year = self._parse_year(tag.year)
        original_year = self._original_release_year(tag)
        title_from_tag = bool(tag.title and tag.title.strip())
        imported = ImportedTrackData(
            ImportedFieldValue(
                tag.title if title_from_tag else path.stem,
                (
                    MetadataSource.FILE_TAG
                    if title_from_tag
                    else MetadataSource.FILE_OR_FOLDER_DERIVATION
                ),
                "file_tag:title" if title_from_tag else "file_name",
            ),
            ImportedFieldValue(tag.artist, source_detail="file_tag:artist"),
            ImportedFieldValue(tag.album, source_detail="file_tag:album"),
            ImportedFieldValue(tag.genre, source_detail="file_tag:genre"),
            ImportedFieldValue(year, source_detail="file_tag:year"),
            ImportedFieldValue(original_year, source_detail="file_tag:original_release_year"),
            float(tag.duration),
        )
        replaygain: tuple[float | None, float | None, float | None, float | None] | None = None
        if self._loudness_repository is not None:
            replaygain, invalid_fields = self._parse_replaygain_values(tag)
            self._log_invalid_replaygain(None, invalid_fields)
        current_snapshot = FileImportSnapshot.capture(path)
        result = self._metadata_import.apply(
            initial_snapshot,
            imported,
            current_snapshot=current_snapshot,
            persist_related=(
                lambda track_id: (
                    self._loudness_repository.save_replaygain(track_id, *replaygain)
                    if self._loudness_repository is not None and replaygain is not None
                    else None
                )
            ),
        )
        self.last_import_result = result
        if result.outcome is MetadataImportOutcome.FILE_CHANGED:
            return result
        track = result.track
        assert track is not None
        return result

    def cover_data(self, file_path: Path) -> bytes | None:
        """Read embedded cover art without changing the audio file."""
        if not file_path.is_file():
            return None
        tag = TinyTag.get(file_path, tags=True, duration=False, image=True)
        embedded = tag.images.any
        if embedded is not None:
            return embedded.data
        for name in ("cover.jpg", "folder.jpg", "front.jpg", "cover.png", "folder.png"):
            candidate = file_path.parent / name
            if candidate.is_file():
                return candidate.read_bytes()
        return None

    def refresh_replaygain(self, track: Track) -> bool:
        """Read tags for an existing catalog entry without modifying its file."""
        if self._loudness_repository is None:
            return False
        try:
            tag = TinyTag.get(Path(track.file_path), tags=True, duration=False)
            replaygain, invalid_fields = self._parse_replaygain_values(tag)
            self._log_invalid_replaygain(track.id, invalid_fields)
            self._loudness_repository.save_replaygain(track.id, *replaygain)
            return True
        except (TinyTagException, OSError) as exc:
            self._loudness_repository.mark_replaygain_failed(track.id)
            self._logger.warning("ReplayGain für Titel %s nicht lesbar: %s", track.id, exc)
            return False

    @staticmethod
    def directory_audio_files(directory: Path) -> list[Path]:
        """Return supported files recursively in stable playlist order."""
        root = directory.resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"Verzeichnis nicht gefunden: {root}")
        files = [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".mp3", ".flac"}
        ]
        return sorted(files, key=lambda path: str(path.relative_to(root)).casefold())

    @staticmethod
    def _parse_year(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return int(value[:4])
        except ValueError:
            return None

    @classmethod
    def _original_release_year(cls, tag: TinyTag) -> int | None:
        candidates = (
            "originaldate",
            "original_date",
            "originalyear",
            "original_year",
            "originalreleasedate",
            "original_release_date",
        )
        normalized = {
            str(key).casefold().replace(" ", "_"): values for key, values in tag.other.items()
        }
        for key in candidates:
            values = normalized.get(key)
            if values:
                parsed = cls._parse_year(str(values[0]))
                if parsed is not None:
                    return parsed
        return None

    @staticmethod
    def _replaygain_values(
        tag: TinyTag,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        return LibraryService._parse_replaygain_values(tag)[0]

    @staticmethod
    def _parse_replaygain_values(
        tag: TinyTag,
    ) -> tuple[
        tuple[float | None, float | None, float | None, float | None],
        tuple[str, ...],
    ]:
        normalized: dict[str, list[str]] = {}
        for key, raw_values in getattr(tag, "other", {}).items():
            normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if isinstance(raw_values, (str, bytes)):
                values = [
                    (
                        raw_values.decode(errors="replace")
                        if isinstance(raw_values, bytes)
                        else raw_values
                    )
                ]
            elif isinstance(raw_values, Iterable):
                values = [str(value) for value in raw_values]
            else:
                values = [str(raw_values)]
            normalized.setdefault(normalized_key, []).extend(values)

        invalid_fields: list[str] = []

        def number(label: str, *keys: str, gain: bool = False) -> float | None:
            for key in keys:
                values = normalized.get(key)
                if not values:
                    continue
                for raw_value in values:
                    raw = raw_value.strip().casefold()
                    if gain:
                        raw = raw.removesuffix("db").strip()
                    try:
                        value = float(raw.replace(",", "."))
                    except ValueError:
                        continue
                    if not math.isfinite(value):
                        continue
                    if gain and not (
                        LibraryService.MINIMUM_REPLAYGAIN_DB
                        <= value
                        <= LibraryService.MAXIMUM_REPLAYGAIN_DB
                    ):
                        continue
                    if not gain and not (0.0 < value <= LibraryService.MAXIMUM_REPLAYGAIN_PEAK):
                        continue
                    return value
                invalid_fields.append(label)
                return None
            return None

        replaygain_values = (
            number(
                "Track Gain",
                "replaygain_track_gain",
                "replaygain_track_gain_db",
                "rg_track_gain",
                gain=True,
            ),
            number("Track Peak", "replaygain_track_peak", "rg_track_peak"),
            number(
                "Album Gain",
                "replaygain_album_gain",
                "replaygain_album_gain_db",
                "rg_album_gain",
                gain=True,
            ),
            number("Album Peak", "replaygain_album_peak", "rg_album_peak"),
        )
        return replaygain_values, tuple(invalid_fields)

    def _log_invalid_replaygain(
        self, track_id: int | None, invalid_fields: tuple[str, ...]
    ) -> None:
        if invalid_fields:
            self._logger.warning(
                "Ungültige ReplayGain-Tags für Titel %s ignoriert: %s",
                track_id,
                ", ".join(invalid_fields),
            )
