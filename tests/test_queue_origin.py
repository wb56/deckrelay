from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry
from party_player.queue_origin import derive_queue_origin


def entry(queue_id: int, source: QueueSource, detail: str) -> QueueEntry:
    return QueueEntry(
        queue_id, queue_id, queue_id, QueueStatus.WAITING, source, source_detail=detail
    )


def test_directory_origin_uses_persisted_path_not_playlist_selection() -> None:
    origin = derive_queue_origin(
        [
            entry(1, QueueSource.PLAYLIST, r"directory:G:\Musik\Sommerfest"),
            entry(2, QueueSource.PLAYLIST, r"directory:G:\Musik\Sommerfest"),
        ]
    )

    assert origin.kind == "directory"
    assert origin.label == "Verzeichnis · Sommerfest"


def test_saved_playlist_origin_names_only_the_playlist_that_created_entries() -> None:
    origin = derive_queue_origin([entry(1, QueueSource.PLAYLIST, "saved_queue:Sappeure_Test")])

    assert origin.label == "Playlist · Sappeure_Test"


def test_different_entry_origins_are_reported_as_mixed_queue() -> None:
    origin = derive_queue_origin(
        [
            entry(1, QueueSource.MANUAL, "catalog"),
            entry(2, QueueSource.PLAYLIST, "saved_queue:Abend"),
        ]
    )

    assert origin.label == "Gemischte Queue"


def test_manual_and_empty_queue_have_neutral_truthful_labels() -> None:
    assert derive_queue_origin([entry(1, QueueSource.MANUAL, "MANUAL")]).label == (
        "manuell zusammengestellt"
    )
    assert derive_queue_origin([]).label == "Queue leer"


def test_ambiguous_playlist_origin_is_not_guessed() -> None:
    origin = derive_queue_origin([entry(1, QueueSource.PLAYLIST, "")])

    assert origin.kind == "ambiguous"
    assert origin.label == "Playlist/Verzeichnis · Herkunft unbekannt"


def test_guest_requests_are_named_explicitly() -> None:
    origin = derive_queue_origin([entry(1, QueueSource.GUEST_REQUEST, "")])

    assert origin.kind == "guest_request"
    assert origin.label == "Gastwünsche"


def test_automatic_origin_names_catalog_selection() -> None:
    origin = derive_queue_origin([entry(1, QueueSource.AUTOMATIC, "")])

    assert origin.label == "Automatische Katalogauswahl"


def test_unrecognized_source_without_detail_stays_explicitly_unknown() -> None:
    unknown = entry(1, QueueSource.MANUAL, "")
    object.__setattr__(unknown, "source", "legacy-unknown")

    origin = derive_queue_origin([unknown])

    assert origin.kind == "unknown"
    assert origin.label == "Quelle nicht eindeutig"


def test_completed_history_does_not_make_current_directory_queue_mixed() -> None:
    played = QueueEntry(
        1,
        1,
        1,
        QueueStatus.PLAYED,
        QueueSource.MANUAL,
        source_detail="MANUAL",
    )
    waiting = entry(2, QueueSource.PLAYLIST, r"directory:G:\Musik\Sommerfest")

    assert derive_queue_origin([played, waiting]).label == "Verzeichnis · Sommerfest"
