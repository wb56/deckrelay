"""Track database access."""

from party_player.database.connection import Database
from party_player.models import Track


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
                       genre, year, original_release_year, bpm
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
                       genre, year, original_release_year, bpm
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
                          genre, year, original_release_year, bpm
                   FROM tracks WHERE id = ?""",
                (track_id,),
            ).fetchone()
        return Track(**dict(row)) if row else None

    def get_active(self, track_id: int) -> Track | None:
        """Return a track only while it remains an active catalog entry."""
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT id, file_path, title, artist, album, duration_seconds,
                          genre, year, original_release_year, bpm
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
                               genre, year, original_release_year, bpm
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
                               genre, year, original_release_year, bpm
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
                              genre, year, original_release_year, bpm,
                              ROW_NUMBER() OVER (
                                  PARTITION BY lower(trim(title)), lower(trim(artist))
                                  ORDER BY id
                              ) AS duplicate_rank
                       FROM tracks WHERE catalog_visible = 1
                   )
                   SELECT id, file_path, title, artist, album, duration_seconds,
                          genre, year, original_release_year, bpm
                   FROM ranked WHERE duplicate_rank = 1
                   ORDER BY id"""
            ).fetchall()
        return [Track(**dict(row)) for row in rows]

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
                          genre, year, original_release_year, bpm
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
