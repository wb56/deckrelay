"""Deterministic, history-aware automatic catalog selection."""

import random
import logging

from party_player.database.connection import Database
from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry, Track
from party_player.repositories.track_repository import TrackRepository
from party_player.track_selection import TrackSelectionService
from party_player.emergency_playlist import LocalEmergencyPlaylistService


class AutomaticSelectionHistory:
    def __init__(self, database: Database) -> None:
        self._database = database

    def play_counts(self) -> dict[int, int]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT track_id, COUNT(*) AS total
                   FROM play_history
                   WHERE completion_status = 'PLAYED'
                   GROUP BY track_id"""
            ).fetchall()
        return {int(row["track_id"]): int(row["total"]) for row in rows}

    def recent_track_ids(self, limit: int) -> set[int]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT track_id FROM play_history
                   WHERE completion_status = 'PLAYED'
                   ORDER BY finished_at DESC, id DESC LIMIT ?""",
                (max(0, limit),),
            ).fetchall()
        return {int(row["track_id"]) for row in rows}


class AutomaticSelectionService:
    def __init__(
        self,
        tracks: TrackRepository,
        history: AutomaticSelectionHistory,
        *,
        recent_track_limit: int = 25,
        randomizer: random.Random | None = None,
        emergency_playlist: LocalEmergencyPlaylistService | None = None,
    ) -> None:
        self._tracks = tracks
        self._history = history
        self.recent_track_limit = max(0, recent_track_limit)
        self._random = randomizer or random.Random()
        self._emergency_playlist = emergency_playlist
        self.last_relaxation_stage = "NONE"
        self._logger = logging.getLogger(__name__)

    def select(self, rules: TrackSelectionService) -> Track | None:
        recent = self._history.recent_track_ids(self.recent_track_limit)
        counts = self._history.play_counts()
        stages: tuple[tuple[str, frozenset[str], bool], ...] = (
            ("STRICT", frozenset(), True),
            ("ARTIST_DISTANCE", frozenset({"ARTIST_REPETITION"}), True),
            (
                "TRACK_DISTANCE",
                frozenset({"ARTIST_REPETITION", "TRACK_REPETITION"}),
                False,
            ),
        )
        candidates = self._tracks.automatic_candidates()
        for stage, relaxed_codes, avoid_recent in stages:
            eligible: list[Track] = []
            for track in candidates:
                if avoid_recent and track.id in recent:
                    continue
                synthetic = QueueEntry(
                    -track.id,
                    track.id,
                    0,
                    QueueStatus.WAITING,
                    source=QueueSource.AUTOMATIC,
                )
                if rules.evaluate(
                    synthetic,
                    track,
                    relaxed_codes=relaxed_codes,
                ).accepted:
                    eligible.append(track)
            if not eligible:
                continue
            minimum_count = min(counts.get(track.id, 0) for track in eligible)
            top = [track for track in eligible if counts.get(track.id, 0) == minimum_count]
            selected = self._random.choice(top)
            self.last_relaxation_stage = stage
            if stage != "STRICT":
                self._logger.warning(
                    "Automatische Auswahl verwendet Regelentspannung %s für track_id=%s",
                    stage,
                    selected.id,
                )
            return selected
        self.last_relaxation_stage = "NO_SAFE_CANDIDATE"
        return self.select_emergency(rules)

    def select_emergency(self, rules: TrackSelectionService) -> Track | None:
        if self._emergency_playlist is None:
            self.last_relaxation_stage = "NO_SAFE_CANDIDATE"
            return None
        relaxed = frozenset(
            {
                "ARTIST_REPETITION",
                "TRACK_REPETITION",
            }
        )
        for track in self._emergency_playlist.candidates():
            synthetic = QueueEntry(
                -track.id,
                track.id,
                0,
                QueueStatus.WAITING,
                source=QueueSource.EMERGENCY,
            )
            if rules.evaluate(synthetic, track, relaxed_codes=relaxed).accepted:
                self.last_relaxation_stage = "EMERGENCY_PLAYLIST"
                self._logger.warning(
                    "Automatische Auswahl verwendet lokale Emergency-Playlist: track_id=%s",
                    track.id,
                )
                return track
        self.last_relaxation_stage = "NO_SAFE_CANDIDATE"
        return None
