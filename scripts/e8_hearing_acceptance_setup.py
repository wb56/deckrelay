"""Create isolated runtime data for the manual E8 hearing acceptance."""

from __future__ import annotations

import argparse
from pathlib import Path

from party_player import __version__
from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import PlayerMode, QueueSource
from party_player.repository import PartyPlayerRepository
from party_player.settings_service import SettingsService
from party_player.track_suitability import (
    TrackSuitabilityRepository,
    TrackSuitabilityStatus,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_root", type=Path)
    parser.add_argument("media_directory", type=Path)
    parser.add_argument("--audio-device", default="")
    args = parser.parse_args()

    root = args.runtime_root.resolve()
    media = args.media_directory.resolve()
    database_path = root / "data" / "party_player.db"
    if database_path.exists():
        raise SystemExit(f"Zieldatenbank existiert bereits: {database_path}")
    files = tuple(media / f"e8-track-{index:02d}.mp3" for index in range(1, 31))
    missing = tuple(path for path in files if not path.is_file())
    if missing:
        raise SystemExit(f"Es fehlen {len(missing)} E8-Audiodateien in {media}")

    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(database_path)
    migrate(database)
    with database.connect() as connection:
        connection.executemany(
            """INSERT INTO tracks
               (id,file_path,title,artist,album,duration_seconds,genre,bpm,energy,rating)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    index,
                    str(path),
                    f"E8 Hörtest {index:02d}",
                    f"E8 Interpret {((index - 1) % 10) + 1:02d}",
                    "E8 reale Hörabnahme",
                    12.0,
                    ("Dance", "Pop", "Rock")[(index - 1) % 3],
                    float(96 + index),
                    20 + ((index * 7) % 80),
                    1 + ((index - 1) % 5),
                )
                for index, path in enumerate(files, start=1)
            ),
        )
    TrackSuitabilityRepository(database).set_many(
        tuple(range(1, 31)), TrackSuitabilityStatus.SUITABLE, "E8 Hörabnahme"
    )
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)
    settings.set_first_run_completed(True)
    settings.set_system_check_completed_version(__version__)
    settings.set_audio_backend("vlc")
    settings.set_audio_output_device(args.audio_device)
    settings.set_player_mode(PlayerMode.AUTOMATIC)
    settings.set_automatic_selection_minimum_stock(5)
    settings.set_fade_duration(2.0)
    settings.set_minimum_fade_duration(0.5)
    settings.set_minimum_playable_duration(1.0)
    settings.set_restore_last_session(True)

    session = repository.create_session("E8 reale Hörabnahme")
    repository.add_queue_entry(session.session_id, 1, QueueSource.MANUAL)
    repository.add_queue_entry(session.session_id, 2, QueueSource.PLAYLIST)
    repository.add_queue_entry(
        session.session_id, 3, QueueSource.GUEST_REQUEST, requested_by="E8 Gast A"
    )
    repository.add_queue_entry(session.session_id, 4, QueueSource.PLAYLIST)
    repository.add_queue_entry(
        session.session_id, 5, QueueSource.GUEST_REQUEST, requested_by="E8 Gast B"
    )
    print(f"runtime_root={root}")
    print(f"database={database_path}")
    print("tracks=30")
    print("queue_entries=5")
    print(f"audio_device={args.audio_device or 'WINDOWS_DEFAULT'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
