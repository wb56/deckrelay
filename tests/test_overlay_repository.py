from datetime import datetime, timedelta
from pathlib import Path

import pytest

from party_player.database.connection import Database
from party_player.database.migrations import LATEST_SCHEMA_VERSION, migrate
from party_player.overlay import OverlayDefinition, OverlayPlayResult, OverlayRecord
from party_player.overlay_service import OverlayService
from party_player.repositories.overlay_repository import OverlayRepository


def repository(tmp_path: Path) -> OverlayRepository:
    database = Database(tmp_path / "overlays.db")
    migrate(database)
    return OverlayRepository(database)


def record(name: str, *, favorite: int | None = None, shortcut: str | None = None) -> OverlayRecord:
    return OverlayRecord(
        OverlayDefinition(0, name, f"C:/Jingles/{name}.mp3", category=" Ansagen "),
        favorite_position=favorite,
        keyboard_shortcut=shortcut,
    )


def test_schema_32_creates_overlay_tables_and_constraints(tmp_path: Path) -> None:
    database = Database(tmp_path / "schema.db")
    migrate(database)
    with database.connect() as connection:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert version is not None and version["version"] == LATEST_SCHEMA_VERSION == 41
    assert {"audio_overlays", "overlay_play_history"} <= tables


def test_save_list_search_update_and_disable_overlay(tmp_path: Path) -> None:
    overlays = repository(tmp_path)
    saved = overlays.save(record("Begrüßung", favorite=1, shortcut="Ctrl+1"))

    assert saved.definition.category == "Ansagen"
    assert overlays.get(saved.definition.overlay_id) == saved
    assert overlays.search("grüß") == [saved]
    assert overlays.list_all(enabled_only=True) == [saved]

    changed = overlays.save(
        OverlayRecord(
            OverlayDefinition(
                saved.definition.overlay_id,
                "Begrüßung kurz",
                saved.definition.file_path,
                category="Ansagen",
            ),
            favorite_position=2,
            keyboard_shortcut="Ctrl+2",
        )
    )
    disabled = overlays.set_enabled(changed.definition.overlay_id, False)
    assert not disabled.enabled
    assert overlays.list_all(enabled_only=True) == []


def test_name_favorite_and_shortcut_are_unique(tmp_path: Path) -> None:
    overlays = repository(tmp_path)
    overlays.save(record("Tusch", favorite=1, shortcut="Ctrl+1"))

    with pytest.raises(ValueError, match="Name"):
        overlays.save(record("tusch"))
    with pytest.raises(ValueError, match="Favoritenposition"):
        overlays.save(record("Applaus", favorite=1))
    with pytest.raises(ValueError, match="Tastenkürzel"):
        overlays.save(record("Hinweis", shortcut="ctrl+1"))


def test_all_six_favorites_and_shortcuts_survive_database_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.db"
    database = Database(database_path)
    migrate(database)
    overlays = OverlayRepository(database)
    for position in range(1, 7):
        overlays.save(
            record(
                f"Jingle {position}",
                favorite=position,
                shortcut=f"Ctrl+{position}",
            )
        )

    reopened_database = Database(database_path)
    migrate(reopened_database)
    snapshot = OverlayService(OverlayRepository(reopened_database)).snapshot(enabled_only=False)

    assert [item.definition.name if item is not None else "" for item in snapshot.favorites] == [
        "Jingle 1",
        "Jingle 2",
        "Jingle 3",
        "Jingle 4",
        "Jingle 5",
        "Jingle 6",
    ]
    assert [
        item.keyboard_shortcut if item is not None else None for item in snapshot.favorites
    ] == [f"Ctrl+{position}" for position in range(1, 7)]


def test_missing_file_remains_configured_and_can_receive_new_path(tmp_path: Path) -> None:
    overlays = repository(tmp_path)
    saved = overlays.save(record("Fehlt"))
    assert not Path(saved.definition.file_path).exists()

    replacement = tmp_path / "replacement.flac"
    updated = overlays.save(
        OverlayRecord(
            OverlayDefinition(
                saved.definition.overlay_id,
                saved.definition.name,
                str(replacement),
            )
        )
    )
    assert updated.definition.file_path == str(replacement)


def test_history_is_separate_and_survives_overlay_deletion(tmp_path: Path) -> None:
    overlays = repository(tmp_path)
    saved = overlays.save(record("Tusch"))
    started = datetime(2026, 7, 29, 20, 0)
    history_id = overlays.add_history(
        saved,
        started_at=started,
        completed_at=started + timedelta(seconds=3),
        result=OverlayPlayResult.FADED_OUT,
    )
    assert overlays.delete(saved.definition.overlay_id)

    database = overlays._database
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM overlay_play_history WHERE id = ?", (history_id,)
        ).fetchone()
    assert row is not None
    assert row["overlay_id"] is None
    assert row["overlay_name"] == "Tusch"
    assert row["result"] == "FADED_OUT"


def test_unsaved_preview_history_uses_name_without_foreign_key(tmp_path: Path) -> None:
    overlays = repository(tmp_path)
    started = datetime(2026, 7, 29, 20, 0)
    preview = OverlayDefinition(0, "Ungespeicherte Vorschau", "preview.mp3")

    history_id = overlays.add_definition_history(
        preview,
        started_at=started,
        completed_at=started + timedelta(seconds=1),
        result=OverlayPlayResult.STOPPED,
    )

    with overlays._database.connect() as connection:
        row = connection.execute(
            "SELECT overlay_id, overlay_name FROM overlay_play_history WHERE id = ?",
            (history_id,),
        ).fetchone()
    assert row is not None
    assert row["overlay_id"] is None
    assert row["overlay_name"] == "Ungespeicherte Vorschau"
