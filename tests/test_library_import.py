"""Tests for explicit, non-destructive file import."""

from pathlib import Path
from types import SimpleNamespace
import logging

from pytest import LogCaptureFixture, MonkeyPatch

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.repositories.track_repository import TrackRepository
from party_player.loudness import LoudnessRepository
from party_player.loudness import LoudnessService
from party_player.services.library_service import LibraryService
from party_player.metadata_import import MetadataImportOutcome
from party_player.metadata_persistence import MetadataFieldStateRepository
from party_player.metadata_rules import MetadataFieldKey, MetadataSource


def _synchsafe(size: int) -> bytes:
    return bytes((size >> 21 & 0x7F, size >> 14 & 0x7F, size >> 7 & 0x7F, size & 0x7F))


def _id3_txxx(description: str, value: str) -> bytes:
    payload = b"\x03" + description.encode() + b"\x00" + value.encode()
    return b"TXXX" + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload


def _write_tagged_mp3(path: Path) -> None:
    frames = b"".join(
        (
            _id3_txxx("REPLAYGAIN_TRACK_GAIN", "-7.25 dB"),
            _id3_txxx("REPLAYGAIN_TRACK_PEAK", "0.8125"),
        )
    )
    path.write_bytes(b"ID3\x04\x00\x00" + _synchsafe(len(frames)) + frames)


def _write_tagged_flac(path: Path) -> None:
    comments = (
        b"REPLAYGAIN_ALBUM_GAIN=+2.50 dB",
        b"REPLAYGAIN_ALBUM_PEAK=0.9500",
    )
    payload = (
        (0).to_bytes(4, "little")
        + len(comments).to_bytes(4, "little")
        + b"".join(len(comment).to_bytes(4, "little") + comment for comment in comments)
    )
    path.write_bytes(b"fLaC" + b"\x84" + len(payload).to_bytes(3, "big") + payload)


def test_import_reads_metadata_and_upserts_catalog(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    audio_file = tmp_path / "track.mp3"
    audio_file.write_bytes(b"audio")
    metadata = SimpleNamespace(
        title="Titel",
        artist="Interpret",
        album="Album",
        duration=123.5,
        genre="Pop",
        year="1999-01-01",
        other={"originaldate": ["1978-04-12"]},
    )
    monkeypatch.setattr("party_player.services.library_service.TinyTag.get", lambda _path: metadata)
    database = Database(tmp_path / "test.db")
    migrate(database)
    service = LibraryService(TrackRepository(database))

    first = service.import_file(audio_file)
    second = service.import_file(audio_file)

    assert first.id == second.id
    assert first.title == "Titel"
    assert first.year == 1999
    assert first.original_release_year == 1978
    assert TrackRepository(database).count() == 1


def test_import_invalid_year_and_missing_title_are_handled_without_writing_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    audio_file = tmp_path / "fallback-name.mp3"
    original = b"audio"
    audio_file.write_bytes(original)
    metadata = SimpleNamespace(
        title=" ", artist=None, album=None, duration=10, genre=" ", year="invalid", other={}
    )
    monkeypatch.setattr("party_player.services.library_service.TinyTag.get", lambda _path: metadata)
    database = Database(tmp_path / "partial.db")
    migrate(database)
    service = LibraryService(TrackRepository(database))

    result = service.import_file_with_result(audio_file)

    assert result.outcome is MetadataImportOutcome.NEW_TRACK
    assert result.partial_tags
    assert result.track is not None and result.track.title == "fallback-name"
    title_state = MetadataFieldStateRepository(database).get(
        result.track.id, MetadataFieldKey.TITLE
    )
    assert title_state is not None and title_state.source is (
        MetadataSource.FILE_OR_FOLDER_DERIVATION
    )
    assert result.track.year is None
    assert audio_file.read_bytes() == original


def test_structured_import_reports_failure_without_changing_legacy_signature(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "failure.db")
    migrate(database)
    service = LibraryService(TrackRepository(database))
    missing = tmp_path / "missing.mp3"

    result = service.import_file_with_result(missing)

    assert result.outcome is MetadataImportOutcome.FAILED
    assert result.track is None
    try:
        service.import_file(missing)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("Bestehende Importsignatur muss weiterhin Fehler auslösen")


def test_catalog_path_upsert_is_case_insensitive_for_windows_paths(tmp_path: Path) -> None:
    database = Database(tmp_path / "case-path.db")
    migrate(database)
    repository = TrackRepository(database)

    first = repository.upsert_file(r"D:\Musik\Titel.mp3", "Alt", "", "", 10.0)
    second = repository.upsert_file(r"d:\musik\TITEL.mp3", "Neu", "", "", 11.0)

    assert first.id == second.id
    assert second.title == "Neu"
    assert repository.count() == 1


def test_import_reads_replaygain_without_modifying_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    audio_file = tmp_path / "gain.mp3"
    original = b"unchanged audio"
    audio_file.write_bytes(original)
    metadata = SimpleNamespace(
        title="Gain",
        artist="",
        album="",
        duration=20,
        genre="",
        year=None,
        other={
            "replaygain_track_gain": ["+4.50 dB"],
            "replaygain_track_peak": ["0.75"],
        },
    )
    monkeypatch.setattr("party_player.services.library_service.TinyTag.get", lambda _p: metadata)
    database = Database(tmp_path / "test.db")
    migrate(database)
    loudness = LoudnessRepository(database)

    track = LibraryService(TrackRepository(database), loudness).import_file(audio_file)

    stored = loudness.get(track.id)
    assert stored.replaygain_track_gain_db == 4.5
    assert stored.replaygain_track_peak == 0.75
    assert audio_file.read_bytes() == original


def test_replaygain_parser_accepts_common_key_spellings_and_scalar_values() -> None:
    metadata = SimpleNamespace(
        other={
            "ReplayGain Track Gain DB": "-3,50 DB",
            "RG-TRACK-PEAK": "0.875",
            "replaygain.album.gain": ["+1.25 dB"],
            "rg_album_peak": ["0.99"],
        }
    )

    assert LibraryService._replaygain_values(metadata) == (-3.5, 0.875, 1.25, 0.99)


def test_real_tinytag_results_for_id3_and_vorbis_comments(tmp_path: Path) -> None:
    mp3 = tmp_path / "tagged.mp3"
    flac = tmp_path / "tagged.flac"
    _write_tagged_mp3(mp3)
    _write_tagged_flac(flac)

    mp3_tag = __import__("tinytag").TinyTag.get(mp3, tags=True, duration=False)
    flac_tag = __import__("tinytag").TinyTag.get(flac, tags=True, duration=False)

    assert mp3_tag.other == {
        "replaygain_track_gain": ["-7.25 dB"],
        "replaygain_track_peak": ["0.8125"],
    }
    assert LibraryService._replaygain_values(mp3_tag) == (-7.25, 0.8125, None, None)
    assert flac_tag.other == {
        "replaygain_album_gain": ["+2.50 dB"],
        "replaygain_album_peak": ["0.9500"],
    }
    assert LibraryService._replaygain_values(flac_tag) == (None, None, 2.5, 0.95)


def test_invalid_replaygain_is_logged_and_valid_cached_values_survive(
    tmp_path: Path, monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    audio_file = tmp_path / "invalid-gain.mp3"
    audio_file.write_bytes(b"tag")
    database = Database(tmp_path / "invalid-gain.db")
    migrate(database)
    tracks = TrackRepository(database)
    loudness = LoudnessRepository(database)
    track = tracks.upsert_file(str(audio_file), "Gain", "", "", 20.0, "", None, None)
    loudness.save_replaygain(track.id, -5.0, 0.8, None, None)
    metadata = SimpleNamespace(
        other={
            "REPLAYGAIN_TRACK_GAIN": ["not-a-number"],
            "REPLAYGAIN_TRACK_PEAK": ["-0.2"],
            "REPLAYGAIN_ALBUM_GAIN": ["999 dB"],
        }
    )
    monkeypatch.setattr(
        "party_player.services.library_service.TinyTag.get",
        lambda *_args, **_kwargs: metadata,
    )
    service = LibraryService(tracks, loudness)

    with caplog.at_level(logging.WARNING):
        assert service.refresh_replaygain(track)

    stored = loudness.get(track.id)
    assert stored.replaygain_track_gain_db == -5.0
    assert stored.replaygain_track_peak == 0.8
    assert "Track Gain, Track Peak, Album Gain" in caplog.text


def test_tag_read_error_preserves_cached_replaygain(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    audio_file = tmp_path / "unavailable.mp3"
    audio_file.write_bytes(b"tag")
    database = Database(tmp_path / "read-error.db")
    migrate(database)
    tracks = TrackRepository(database)
    loudness = LoudnessRepository(database)
    track = tracks.upsert_file(str(audio_file), "Gain", "", "", 20.0, "", None, None)
    loudness.save_replaygain(track.id, -6.0, 0.7, None, None)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError("network unavailable")

    monkeypatch.setattr("party_player.services.library_service.TinyTag.get", fail)

    assert not LibraryService(tracks, loudness).refresh_replaygain(track)
    stored = loudness.get(track.id)
    assert stored.replaygain_track_gain_db == -6.0
    assert stored.metadata_status == "FAILED"
    assert LoudnessService(loudness).resolve(track.id).source == "REPLAYGAIN_TAG"


def test_import_rejects_unsupported_file(tmp_path: Path) -> None:
    file_path = tmp_path / "track.wav"
    file_path.touch()
    database = Database(tmp_path / "test.db")
    migrate(database)
    service = LibraryService(TrackRepository(database))

    try:
        service.import_file(file_path)
    except ValueError as error:
        assert "MP3" in str(error)
    else:
        raise AssertionError("Nicht unterstützte Datei wurde akzeptiert")


def test_import_rejects_empty_or_unreadable_audio(tmp_path: Path) -> None:
    audio_file = tmp_path / "broken.mp3"
    audio_file.touch()
    database = Database(tmp_path / "test.db")
    migrate(database)
    service = LibraryService(TrackRepository(database))

    try:
        service.import_file(audio_file)
    except ValueError as error:
        assert "leer oder beschädigt" in str(error)
    else:
        raise AssertionError("Leere Audiodatei wurde importiert")


def test_cover_data_falls_back_to_folder_cover(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    audio_file = tmp_path / "track.mp3"
    audio_file.touch()
    cover_file = tmp_path / "folder.jpg"
    cover_file.write_bytes(b"image-data")
    metadata = SimpleNamespace(images=SimpleNamespace(any=None))
    monkeypatch.setattr(
        "party_player.services.library_service.TinyTag.get", lambda *_args, **_kwargs: metadata
    )
    database = Database(tmp_path / "test.db")
    migrate(database)

    cover = LibraryService(TrackRepository(database)).cover_data(audio_file)

    assert cover == b"image-data"


def test_cover_data_returns_embedded_image(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    audio_file = tmp_path / "track.flac"
    audio_file.touch()
    image = SimpleNamespace(data=b"embedded-image")
    metadata = SimpleNamespace(images=SimpleNamespace(any=image))
    monkeypatch.setattr(
        "party_player.services.library_service.TinyTag.get", lambda *_args, **_kwargs: metadata
    )
    database = Database(tmp_path / "test.db")
    migrate(database)

    cover = LibraryService(TrackRepository(database)).cover_data(audio_file)

    assert cover == b"embedded-image"


def test_directory_playlist_is_recursive_filtered_and_stably_sorted(tmp_path: Path) -> None:
    album = tmp_path / "Album"
    album.mkdir()
    (tmp_path / "02 Song.mp3").touch()
    (tmp_path / "01 Song.FLAC").touch()
    (tmp_path / "Notiz.txt").touch()
    (album / "03 Song.mp3").touch()

    files = LibraryService.directory_audio_files(tmp_path)

    assert [path.relative_to(tmp_path).as_posix() for path in files] == [
        "01 Song.FLAC",
        "02 Song.mp3",
        "Album/03 Song.mp3",
    ]


def test_removing_from_catalog_never_deletes_audio_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    audio_file = tmp_path / "track.mp3"
    audio_file.write_bytes(b"audio")
    metadata = SimpleNamespace(
        title="Titel",
        artist="Interpret",
        album="",
        duration=10,
        genre="",
        year=None,
        other={},
    )
    monkeypatch.setattr("party_player.services.library_service.TinyTag.get", lambda _path: metadata)
    database = Database(tmp_path / "test.db")
    migrate(database)
    service = LibraryService(TrackRepository(database))
    track = service.import_file(audio_file)

    service.remove_from_catalog(track.id)

    assert audio_file.read_bytes() == b"audio"
    assert service.first_page() == []
