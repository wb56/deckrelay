"""Track database access."""

import sqlite3

from party_player.database.connection import Database
from party_player.metadata_rules import MetadataReviewStatus, MetadataSource
from party_player.models import Track
from party_player.selection_metadata import (
    SelectionMetadataCatalogSnapshot,
    SelectionMetadataSnapshot,
    missing_metadata_snapshot,
    selection_metadata_terms,
    selection_metadata_value,
)


_AUTOMATIC_CANDIDATES_CTE = """WITH ranked AS (
    SELECT id, file_path, title, artist, album, duration_seconds,
           genre, year, original_release_year, bpm, bpm_confidence,
           alternative_bpm, energy, rating,
           ROW_NUMBER() OVER (
               PARTITION BY lower(trim(title)), lower(trim(artist))
               ORDER BY id
           ) AS duplicate_rank
    FROM tracks WHERE catalog_visible = 1
), candidates AS (
    SELECT * FROM ranked WHERE duplicate_rank = 1
)"""


class TrackRepository:
    """Read tracks from the SQLite catalog."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        """Expose the shared connection factory to transaction-level catalog services."""
        return self._database

    def count(self) -> int:
        """Return the number of catalog tracks."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM tracks WHERE catalog_visible = 1"
            ).fetchone()
        return int(row["total"])

    def network_roots(self, limit: int = 10) -> tuple[str, ...]:
        """Return bounded configured UNC server/share roots without touching them."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT file_path FROM tracks WHERE file_path LIKE ?
                   ORDER BY lower(file_path), file_path LIMIT ?""",
                (r"\\%", max(1, int(limit) * 20)),
            ).fetchall()
        roots: list[str] = []
        for row in rows:
            parts = str(row["file_path"]).lstrip("\\").split("\\")
            if len(parts) < 2 or not parts[0] or not parts[1]:
                continue
            configured_parts = parts[:3] if len(parts) >= 4 else parts[:2]
            root = "\\\\" + "\\".join(configured_parts)
            if root.casefold() not in {item.casefold() for item in roots}:
                roots.append(root)
            if len(roots) >= limit:
                break
        return tuple(roots)

    def find_page(self, limit: int, offset: int = 0) -> list[Track]:
        """Return one deterministic, bounded catalog page."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, file_path, title, artist, album, duration_seconds,
                       genre, year, original_release_year, bpm, rating
                FROM tracks WHERE catalog_visible = 1
                ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, id
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [Track(**dict(row)) for row in rows]

    def search(self, query: str, limit: int = 100, offset: int = 0) -> list[Track]:
        """Search indexed catalog columns with a bounded result."""
        pattern = f"%{query.strip()}%"
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, file_path, title, artist, album, duration_seconds,
                       genre, year, original_release_year, bpm, rating
                FROM tracks
                WHERE catalog_visible = 1
                  AND (title LIKE ? COLLATE NOCASE
                   OR artist LIKE ? COLLATE NOCASE
                   OR album LIKE ? COLLATE NOCASE
                   OR genre LIKE ? COLLATE NOCASE
                   OR file_path LIKE ? COLLATE NOCASE
                   OR CAST(year AS TEXT) LIKE ?
                   OR CAST(original_release_year AS TEXT) LIKE ?
                   OR EXISTS (
                       SELECT 1
                       FROM track_metadata_terms AS assignment
                       JOIN metadata_terms AS term ON term.id = assignment.term_id
                       WHERE assignment.track_id = tracks.id
                         AND term.display_name LIKE ? COLLATE NOCASE
                   ))
                ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE, id
                LIMIT ? OFFSET ?
                """,
                (
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    limit,
                    offset,
                ),
            ).fetchall()
        return [Track(**dict(row)) for row in rows]

    def search_count(self, query: str) -> int:
        pattern = f"%{query.strip()}%"
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS total FROM tracks
                   WHERE catalog_visible = 1
                     AND (title LIKE ? COLLATE NOCASE
                      OR artist LIKE ? COLLATE NOCASE
                      OR album LIKE ? COLLATE NOCASE
                      OR genre LIKE ? COLLATE NOCASE
                      OR file_path LIKE ? COLLATE NOCASE
                      OR CAST(year AS TEXT) LIKE ?
                      OR CAST(original_release_year AS TEXT) LIKE ?
                      OR EXISTS (
                          SELECT 1
                          FROM track_metadata_terms AS assignment
                          JOIN metadata_terms AS term ON term.id = assignment.term_id
                            WHERE assignment.track_id = tracks.id
                            AND term.display_name LIKE ? COLLATE NOCASE
                      ))""",
                (pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern),
            ).fetchone()
        return int(row["total"])

    def get(self, track_id: int) -> Track | None:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT id, file_path, title, artist, album, duration_seconds,
                          genre, year, original_release_year, bpm, rating
                   FROM tracks WHERE id = ?""",
                (track_id,),
            ).fetchone()
        return Track(**dict(row)) if row else None

    def get_active(self, track_id: int) -> Track | None:
        """Return a track only while it remains an active catalog entry."""
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT id, file_path, title, artist, album, duration_seconds,
                          genre, year, original_release_year, bpm, rating
                   FROM tracks WHERE id = ? AND catalog_visible = 1""",
                (track_id,),
            ).fetchone()
        return Track(**dict(row)) if row else None

    def get_many(self, track_ids: list[int]) -> dict[int, Track]:
        """Load tracks in batches instead of issuing one query per queue row."""
        unique_ids = list(dict.fromkeys(track_ids))
        tracks: dict[int, Track] = {}
        for start in range(0, len(unique_ids), 900):
            batch = unique_ids[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            with self._database.connect() as connection:
                rows = connection.execute(
                    f"""SELECT id, file_path, title, artist, album, duration_seconds,
                               genre, year, original_release_year, bpm, rating
                        FROM tracks WHERE id IN ({placeholders})""",
                    batch,
                ).fetchall()
            for row in rows:
                track = Track(**dict(row))
                tracks[track.id] = track
        return tracks

    def get_by_file_paths(self, file_paths: list[str]) -> dict[str, Track]:
        """Resolve exact catalog paths in bounded batches using case-insensitive keys."""
        unique = list(dict.fromkeys(path.casefold() for path in file_paths))
        tracks: dict[str, Track] = {}
        for start in range(0, len(unique), 900):
            batch = unique[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            with self._database.connect() as connection:
                rows = connection.execute(
                    f"""SELECT id, file_path, title, artist, album, duration_seconds,
                               genre, year, original_release_year, bpm, rating
                        FROM tracks WHERE lower(file_path) IN ({placeholders})""",
                    batch,
                ).fetchall()
            for row in rows:
                track = Track(**dict(row))
                tracks[track.file_path.casefold()] = track
        return tracks

    def automatic_candidates(self) -> list[Track]:
        """Return visible catalog candidates without duplicate title/artist rows."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """WITH ranked AS (
                       SELECT id, file_path, title, artist, album, duration_seconds,
                              genre, year, original_release_year, bpm, rating,
                              ROW_NUMBER() OVER (
                                  PARTITION BY lower(trim(title)), lower(trim(artist))
                                  ORDER BY id
                              ) AS duplicate_rank
                       FROM tracks WHERE catalog_visible = 1
                   )
                   SELECT id, file_path, title, artist, album, duration_seconds,
                          genre, year, original_release_year, bpm, rating
                   FROM ranked WHERE duplicate_rank = 1
                   ORDER BY id"""
            ).fetchall()
        return [Track(**dict(row)) for row in rows]

    def automatic_selection_snapshot(
        self,
    ) -> tuple[tuple[Track, ...], SelectionMetadataCatalogSnapshot]:
        """Load candidates and effective selection metadata in at most two queries."""
        with self._database.connect() as connection:
            rows = connection.execute(
                _AUTOMATIC_CANDIDATES_CTE
                + """
                SELECT c.*,
                       main.source_type main_source, main.confidence main_confidence,
                       main.review_status main_status,
                       extra.source_type extra_source, extra.confidence extra_confidence,
                       extra.review_status extra_status,
                       tempo.source_type tempo_source, tempo.confidence tempo_confidence,
                       tempo.review_status tempo_status,
                       alternative.source_type alternative_source,
                       alternative.confidence alternative_confidence,
                       alternative.review_status alternative_status,
                       energy_state.source_type energy_source,
                       energy_state.confidence energy_confidence,
                       energy_state.review_status energy_status,
                       mood.source_type mood_source, mood.confidence mood_confidence,
                       mood.review_status mood_status
                FROM candidates c
                LEFT JOIN track_metadata_field_state main
                  ON main.track_id=c.id AND main.field_key='main_genre'
                LEFT JOIN track_metadata_field_state extra
                  ON extra.track_id=c.id AND extra.field_key='additional_genres'
                LEFT JOIN track_metadata_field_state tempo
                  ON tempo.track_id=c.id AND tempo.field_key='bpm'
                LEFT JOIN track_metadata_field_state alternative
                  ON alternative.track_id=c.id AND alternative.field_key='alternative_bpm'
                LEFT JOIN track_metadata_field_state energy_state
                  ON energy_state.track_id=c.id AND energy_state.field_key='energy'
                LEFT JOIN track_metadata_field_state mood
                  ON mood.track_id=c.id AND mood.field_key='moods'
                ORDER BY c.id"""
            ).fetchall()
            if not rows:
                return (), SelectionMetadataCatalogSnapshot(())
            term_rows = connection.execute(
                _AUTOMATIC_CANDIDATES_CTE
                + """
                SELECT c.id track_id, term.term_type, term.display_name
                FROM candidates c
                LEFT JOIN track_metadata_terms assignment ON assignment.track_id=c.id
                LEFT JOIN metadata_terms term ON term.id=assignment.term_id
                  AND term.term_type IN ('ADDITIONAL_GENRE','MOOD')
                ORDER BY c.id, term.term_type, term.normalized_key"""
            ).fetchall()

        tracks = tuple(self._track_from_selection_row(row) for row in rows)
        terms: dict[int, dict[str, list[str]]] = {
            track.id: {"ADDITIONAL_GENRE": [], "MOOD": []} for track in tracks
        }
        present_ids: set[int] = set()
        for row in term_rows:
            track_id = int(row["track_id"])
            present_ids.add(track_id)
            term_type = row["term_type"]
            display_name = row["display_name"]
            if term_type in {"ADDITIONAL_GENRE", "MOOD"} and display_name is not None:
                terms.setdefault(track_id, {}).setdefault(str(term_type), []).append(
                    str(display_name)
                )
        metadata = tuple(
            (
                self._selection_metadata_from_row(row, terms[int(row["id"])])
                if int(row["id"]) in present_ids
                else missing_metadata_snapshot(int(row["id"]))
            )
            for row in rows
        )
        return tracks, SelectionMetadataCatalogSnapshot(metadata)

    @staticmethod
    def _track_from_selection_row(row: sqlite3.Row) -> Track:
        return Track(
            int(row["id"]),
            str(row["file_path"]),
            str(row["title"]),
            str(row["artist"]),
            str(row["album"]),
            float(row["duration_seconds"]) if row["duration_seconds"] is not None else None,
            str(row["genre"]),
            int(row["year"]) if row["year"] is not None else None,
            (
                int(row["original_release_year"])
                if row["original_release_year"] is not None
                else None
            ),
            float(row["bpm"]) if row["bpm"] is not None else None,
            int(row["rating"]) if row["rating"] is not None else None,
        )

    @classmethod
    def _selection_metadata_from_row(
        cls, row: sqlite3.Row, terms: dict[str, list[str]]
    ) -> SelectionMetadataSnapshot:
        main_source, main_confidence, main_status = cls._field_state(row, "main")
        extra_source, extra_confidence, extra_status = cls._field_state(row, "extra")
        tempo_source, tempo_state_confidence, tempo_status = cls._field_state(row, "tempo")
        alternative_source, alternative_confidence, alternative_status = cls._field_state(
            row, "alternative"
        )
        energy_source, energy_confidence, energy_status = cls._field_state(row, "energy")
        mood_source, mood_confidence, mood_status = cls._field_state(row, "mood")
        tempo_confidence = (
            float(row["bpm_confidence"])
            if row["bpm_confidence"] is not None
            else tempo_state_confidence
        )
        return SelectionMetadataSnapshot(
            int(row["id"]),
            selection_metadata_value(
                str(row["genre"]) or None,
                source=main_source,
                confidence=main_confidence,
                review_status=main_status,
            ),
            selection_metadata_terms(
                tuple(terms["ADDITIONAL_GENRE"]),
                source=extra_source,
                confidence=extra_confidence,
                review_status=extra_status,
            ),
            selection_metadata_value(
                float(row["bpm"]) if row["bpm"] is not None else None,
                source=tempo_source,
                confidence=tempo_confidence,
                review_status=tempo_status,
                minimum_analysis_confidence=0.9,
            ),
            selection_metadata_value(
                float(row["alternative_bpm"]) if row["alternative_bpm"] is not None else None,
                source=alternative_source,
                confidence=alternative_confidence,
                review_status=alternative_status,
                minimum_analysis_confidence=0.9,
            ),
            selection_metadata_value(
                int(row["energy"]) if row["energy"] is not None else None,
                source=energy_source,
                confidence=energy_confidence,
                review_status=energy_status,
                minimum_analysis_confidence=0.8,
            ),
            selection_metadata_terms(
                tuple(terms["MOOD"]),
                source=mood_source,
                confidence=mood_confidence,
                review_status=mood_status,
            ),
        )

    @staticmethod
    def _field_state(
        row: sqlite3.Row, prefix: str
    ) -> tuple[MetadataSource | None, float | None, MetadataReviewStatus | None]:
        source = row[f"{prefix}_source"]
        confidence = row[f"{prefix}_confidence"]
        status = row[f"{prefix}_status"]
        return (
            MetadataSource(str(source)) if source is not None else None,
            float(confidence) if confidence is not None else None,
            MetadataReviewStatus(str(status)) if status is not None else None,
        )

    def upsert_file(
        self,
        file_path: str,
        title: str,
        artist: str,
        album: str,
        duration_seconds: float | None,
        genre: str = "",
        year: int | None = None,
        original_release_year: int | None = None,
    ) -> Track:
        """Insert or refresh one explicitly selected catalog file."""
        with self._database.connect() as connection:
            existing = connection.execute(
                "SELECT file_path FROM tracks WHERE lower(file_path) = lower(?) LIMIT 1",
                (file_path,),
            ).fetchone()
            canonical_path = str(existing["file_path"]) if existing is not None else file_path
            connection.execute(
                """INSERT INTO tracks
                   (file_path, title, artist, album, duration_seconds, genre, year,
                    original_release_year)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                       title = excluded.title,
                       artist = excluded.artist,
                       album = excluded.album,
                       duration_seconds = excluded.duration_seconds,
                       genre = excluded.genre,
                       year = excluded.year,
                       original_release_year = excluded.original_release_year,
                       catalog_visible = 1""",
                (
                    canonical_path,
                    title,
                    artist,
                    album,
                    duration_seconds,
                    genre,
                    year,
                    original_release_year,
                ),
            )
            row = connection.execute(
                """SELECT id, file_path, title, artist, album, duration_seconds,
                          genre, year, original_release_year, bpm, rating
                   FROM tracks WHERE file_path = ?""",
                (canonical_path,),
            ).fetchone()
        assert row is not None
        return Track(**dict(row))

    def hide_from_catalog(self, track_id: int) -> None:
        """Hide a track without deleting its file or breaking queue/history references."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                "UPDATE tracks SET catalog_visible = 0 WHERE id = ? AND catalog_visible = 1",
                (track_id,),
            )
        if cursor.rowcount != 1:
            raise ValueError("Titel wurde im Katalog nicht gefunden")
