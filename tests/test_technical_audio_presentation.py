"""Tests for formatting technical audio information for presentation."""

import pytest

from party_player.analysis import AudioFileInfo
from party_player.ui.technical_audio_presentation import (
    _channel_description,
    _channel_layout_text,
    _format_audio_duration,
    _technical_audio_error_text,
    _technical_audio_field_names,
    _technical_audio_fields,
    _technical_audio_text,
    _technical_audio_unavailable_text,
)


def test_technical_audio_field_names_have_stable_order() -> None:
    assert _technical_audio_field_names() == (
        "Audioformat/Codec",
        "Container",
        "Bitratenmodus",
        "Bitrate",
        "Abtastrate",
        "Bittiefe",
        "Kanäle",
        "Kanallayout",
        "Technische Dauer",
        "Codec-Profil",
        "Encoder",
    )


def test_unavailable_audio_text_has_stable_labels_and_reason() -> None:
    assert _technical_audio_unavailable_text("FFprobe ist nicht verfügbar.") == (
        "Status: Nicht verfügbar\n"
        "Grund: FFprobe ist nicht verfügbar.\n\n"
        "Audioformat/Codec: Nicht verfügbar\n"
        "Container: Nicht verfügbar\n"
        "Bitratenmodus: Nicht verfügbar\n"
        "Bitrate: Nicht verfügbar\n"
        "Abtastrate: Nicht verfügbar\n"
        "Bittiefe: Nicht verfügbar\n"
        "Kanäle: Nicht verfügbar\n"
        "Kanallayout: Nicht verfügbar\n"
        "Technische Dauer: Nicht verfügbar\n"
        "Codec-Profil: Nicht verfügbar\n"
        "Encoder: Nicht verfügbar"
    )


def test_flac_fields_keep_existing_names_order_and_formatting() -> None:
    info = AudioFileInfo(236.466, 44_100, 2, "flac", "flac", 987_000, None, 16, "stereo")

    assert _technical_audio_fields(info) == (
        ("Audioformat/Codec", "FLAC"),
        ("Container", "FLAC"),
        ("Bitratenmodus", "Nicht als MP3-CBR/VBR klassifiziert"),
        ("Bitrate", "durchschnittlich 987 kbit/s"),
        ("Abtastrate", "44.1 kHz"),
        ("Bittiefe", "16 Bit"),
        ("Kanäle", "2"),
        ("Kanallayout", "Stereo"),
        ("Technische Dauer", "3:56,466"),
        ("Codec-Profil", "Nicht verfügbar"),
        ("Encoder", "Nicht verfügbar"),
    )
    assert _technical_audio_text(info) == (
        "Status: Verfügbar\n"
        "Audioformat/Codec: FLAC\n"
        "Container: FLAC\n"
        "Bitratenmodus: Nicht als MP3-CBR/VBR klassifiziert\n"
        "Bitrate: durchschnittlich 987 kbit/s\n"
        "Abtastrate: 44.1 kHz\n"
        "Bittiefe: 16 Bit\n"
        "Kanäle: 2\n"
        "Kanallayout: Stereo\n"
        "Technische Dauer: 3:56,466\n"
        "Codec-Profil: Nicht verfügbar\n"
        "Encoder: Nicht verfügbar"
    )


def test_unknown_codec_container_and_layout_are_preserved() -> None:
    info = AudioFileInfo(
        3661.25,
        96_000,
        6,
        "alac",
        "mov,mp4",
        1_413_000,
        "CBR",
        24,
        "5.1(side)",
        "ALAC",
        "Lavf",
        "Apple Lossless",
        "QuickTime / MOV",
        2,
        4,
    )

    assert _technical_audio_fields(info) == (
        ("Audioformat/Codec", "ALAC – Apple Lossless"),
        ("Container", "QuickTime / MOV"),
        ("Bitratenmodus", "CBR"),
        ("Bitrate", "1413 kbit/s"),
        ("Abtastrate", "96 kHz"),
        ("Bittiefe", "24 Bit"),
        ("Kanäle", "6"),
        ("Kanallayout", "5.1(side)"),
        ("Technische Dauer", "1:01:01,250"),
        ("Codec-Profil", "ALAC"),
        ("Encoder", "Lavf · Audiostreams: 2; verwendet wird Stream 4"),
    )


def test_missing_and_nonpositive_values_use_existing_fallbacks() -> None:
    assert _technical_audio_fields(AudioFileInfo(0.0, 0, 0)) == (
        ("Audioformat/Codec", "Nicht verfügbar"),
        ("Container", "Nicht verfügbar"),
        ("Bitratenmodus", "Nicht zuverlässig bestimmbar"),
        ("Bitrate", "Nicht verfügbar"),
        ("Abtastrate", "Nicht verfügbar"),
        ("Bittiefe", "Nicht verfügbar"),
        ("Kanäle", "Nicht verfügbar"),
        ("Kanallayout", "Nicht verfügbar"),
        ("Technische Dauer", "Nicht verfügbar"),
        ("Codec-Profil", "Nicht verfügbar"),
        ("Encoder", "Nicht verfügbar"),
    )


@pytest.mark.parametrize(
    ("layout", "channels", "expected"),
    [
        ("MONO", 1, "Mono"),
        ("Stereo", 2, "Stereo"),
        ("5.1(side)", 6, "5.1(side)"),
        ("", 1, "Mono (1 Kanal)"),
        ("", 2, "Stereo (2 Kanäle)"),
        ("", 6, "6 Kanäle"),
        ("", 0, "Nicht verfügbar"),
    ],
)
def test_channel_layout_text_handles_known_unknown_and_missing_values(
    layout: str, channels: int, expected: str
) -> None:
    assert _channel_layout_text(layout, channels) == expected


@pytest.mark.parametrize(
    ("channels", "expected"),
    [(1, "Mono (1 Kanal)"), (2, "Stereo (2 Kanäle)"), (6, "6 Kanäle"), (0, "Nicht verfügbar")],
)
def test_channel_description_handles_supported_counts(channels: int, expected: str) -> None:
    assert _channel_description(channels) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileNotFoundError(), "Datei fehlt oder ist nicht erreichbar."),
        (RuntimeError("Quelle nicht erreichbar"), "Datei fehlt oder ist nicht erreichbar."),
        (RuntimeError("timed out"), "FFprobe hat das Zeitlimit überschritten."),
        (RuntimeError("Zeitlimit erreicht"), "FFprobe hat das Zeitlimit überschritten."),
        (RuntimeError("Datei wurde geändert"), "Datei wurde während der Ermittlung geändert."),
        (RuntimeError("Kein Audiostream"), "Datei enthält keinen verwendbaren Audiostream."),
        (
            RuntimeError("FFprobe nicht gefunden"),
            "FFprobe ist nicht verfügbar oder nicht ausführbar.",
        ),
        (
            RuntimeError("Warteschlange voll; nicht gestartet"),
            "Hintergrundwarteschlange ist ausgelastet; bitte erneut versuchen.",
        ),
        (RuntimeError("unexpected"), "Datei konnte technisch nicht gelesen werden."),
    ],
)
def test_audio_errors_map_to_existing_user_messages(error: Exception, expected: str) -> None:
    assert _technical_audio_error_text(error) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "Nicht verfügbar"),
        (-1.0, "Nicht verfügbar"),
        (float("nan"), "Nicht verfügbar"),
        (float("inf"), "Nicht verfügbar"),
        (236.466, "3:56,466"),
        (0.9996, "0:01,000"),
        (3599.9996, "1:00:00,000"),
        (3661.25, "1:01:01,250"),
    ],
)
def test_duration_formatting_handles_invalid_and_rounding_boundaries(
    seconds: float, expected: str
) -> None:
    assert _format_audio_duration(seconds) == expected
