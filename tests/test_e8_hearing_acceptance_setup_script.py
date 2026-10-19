"""Regression tests for the isolated E8 hearing-acceptance setup."""

from pathlib import Path
import sqlite3
import sys
import wave

import pytest

from scripts import e8_hearing_acceptance_setup as setup


def test_audio_duration_matches_real_wave_file_within_one_sample(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "duration.wav"
    sample_rate = 8_000
    frame_count = 2_000
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frame_count)

    reference_duration = frame_count / sample_rate

    assert setup._audio_duration(audio) == pytest.approx(reference_duration, abs=1 / sample_rate)


@pytest.mark.parametrize("duration", [None, 0.0, -1.0])
def test_audio_duration_rejects_missing_or_invalid_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, duration: float | None
) -> None:
    audio = tmp_path / "broken.mp3"
    audio.write_bytes(b"not audio")
    monkeypatch.setattr(
        setup.TinyTag, "get", lambda _path: type("Tag", (), {"duration": duration})()
    )

    with pytest.raises(ValueError, match="gültige Audiodauer"):
        setup._audio_duration(audio)


def test_audio_duration_uses_measured_file_duration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "measured.mp3"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(
        setup.TinyTag,
        "get",
        lambda _path: type("Tag", (), {"duration": 213.456})(),
    )

    assert setup._audio_duration(audio) == pytest.approx(213.456)
    assert setup._audio_duration(audio) != 12.0


def test_audio_duration_reports_unreadable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "unreadable.mp3"
    audio.write_bytes(b"audio")

    def fail(_path: Path) -> object:
        raise OSError("private diagnostic")

    monkeypatch.setattr(setup.TinyTag, "get", fail)

    with pytest.raises(ValueError, match="Audiodauer konnte nicht gelesen werden"):
        setup._audio_duration(audio)


def test_setup_persists_measured_durations_and_refuses_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    media = tmp_path / "media"
    media.mkdir()
    expected: dict[str, float] = {}
    for index in range(1, 31):
        path = media / f"e8-track-{index:02d}.mp3"
        path.write_bytes(b"audio")
        expected[path.name] = 180.0 + index / 10
    monkeypatch.setattr(setup, "_audio_duration", lambda path: expected[path.name])
    monkeypatch.setattr(sys, "argv", ["e8-setup", str(runtime), str(media)])

    assert setup.main() == 0

    database = runtime / "data" / "party_player.db"
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT file_path, duration_seconds FROM tracks ORDER BY id"
        ).fetchall()
    assert [duration for _path, duration in stored] == pytest.approx(
        [expected[Path(path).name] for path, _duration in stored]
    )
    assert all(duration != 12.0 for _path, duration in stored)

    with pytest.raises(SystemExit, match="Zieldatenbank existiert bereits"):
        setup.main()


def test_invalid_duration_leaves_no_test_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    media = tmp_path / "media"
    media.mkdir()
    for index in range(1, 31):
        (media / f"e8-track-{index:02d}.mp3").write_bytes(b"audio")

    monkeypatch.setattr(
        setup,
        "_audio_duration",
        lambda _path: (_ for _ in ()).throw(ValueError("Keine gültige Audiodauer ermittelt")),
    )
    monkeypatch.setattr(sys, "argv", ["e8-setup", str(runtime), str(media)])

    with pytest.raises(SystemExit, match="Keine gültige Audiodauer"):
        setup.main()

    assert not (runtime / "data" / "party_player.db").exists()
