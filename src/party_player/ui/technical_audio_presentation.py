"""Pure formatting helpers for technical audio information."""

import math

from party_player.analysis import AudioFileInfo


def _technical_audio_field_names() -> tuple[str, ...]:
    return (
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


def _technical_audio_unavailable_text(reason: str) -> str:
    rows = "\n".join(f"{name}: Nicht verfügbar" for name in _technical_audio_field_names())
    return f"Status: Nicht verfügbar\nGrund: {reason}\n\n{rows}"


def _technical_audio_text(info: AudioFileInfo) -> str:
    return "Status: Verfügbar\n" + "\n".join(
        f"{name}: {value}" for name, value in _technical_audio_fields(info)
    )


def _technical_audio_fields(info: AudioFileInfo) -> tuple[tuple[str, str], ...]:
    unavailable = "Nicht verfügbar"
    codec_key = info.codec_name.casefold()
    codec = {
        "flac": "FLAC",
        "mp3": "MP3 – MPEG Audio Layer III",
    }.get(codec_key)
    if codec is None:
        short = info.codec_name.upper() if info.codec_name else unavailable
        codec = (
            f"{short} – {info.codec_long_name}"
            if info.codec_long_name and info.codec_long_name.casefold() != codec_key
            else short
        )
    format_key = info.format_name.split(",", 1)[0].casefold()
    container = {"flac": "FLAC", "mp3": "MP3"}.get(
        format_key, info.format_long_name or info.format_name.upper() or unavailable
    )
    if codec_key == "flac":
        bitrate_mode = "Nicht als MP3-CBR/VBR klassifiziert"
    else:
        bitrate_mode = info.bitrate_mode or "Nicht zuverlässig bestimmbar"
    if info.bitrate_bps is None:
        bitrate = unavailable
    else:
        prefix = "durchschnittlich " if codec_key == "flac" or info.bitrate_mode == "VBR" else ""
        bitrate = f"{prefix}{info.bitrate_bps / 1000:.0f} kbit/s"
    sample_rate = f"{info.sample_rate_hz / 1000:g} kHz" if info.sample_rate_hz > 0 else unavailable
    if codec_key in {"mp3", "mp2", "aac", "vorbis", "opus", "wma"}:
        bit_depth = "Nicht anwendbar"
    else:
        bit_depth = (
            f"{info.bits_per_sample} Bit" if info.bits_per_sample is not None else unavailable
        )
    channels = str(info.channels) if info.channels > 0 else unavailable
    layout = _channel_layout_text(info.channel_layout, info.channels)
    streams = (
        f" · Audiostreams: {info.audio_stream_count}; verwendet wird Stream "
        f"{info.selected_stream_index}"
        if info.audio_stream_count > 1
        else ""
    )
    return (
        ("Audioformat/Codec", codec),
        ("Container", container),
        ("Bitratenmodus", bitrate_mode),
        ("Bitrate", bitrate),
        ("Abtastrate", sample_rate),
        ("Bittiefe", bit_depth),
        ("Kanäle", channels),
        ("Kanallayout", layout),
        ("Technische Dauer", _format_audio_duration(info.duration_seconds)),
        ("Codec-Profil", info.codec_profile or unavailable),
        ("Encoder", f"{info.encoder or unavailable}{streams}"),
    )


def _technical_audio_error_text(error: Exception) -> str:
    text = str(error).casefold()
    if isinstance(error, FileNotFoundError) or "nicht erreichbar" in text:
        return "Datei fehlt oder ist nicht erreichbar."
    if "timeout" in text or "timed out" in text or "zeitlimit" in text:
        return "FFprobe hat das Zeitlimit überschritten."
    if "geändert" in text:
        return "Datei wurde während der Ermittlung geändert."
    if "audiostream" in text or "unvollständige audiodaten" in text:
        return "Datei enthält keinen verwendbaren Audiostream."
    if "nicht verfügbar" in text or "nicht gefunden" in text:
        return "FFprobe ist nicht verfügbar oder nicht ausführbar."
    if "nicht gestartet" in text or "warteschlange" in text:
        return "Hintergrundwarteschlange ist ausgelastet; bitte erneut versuchen."
    return "Datei konnte technisch nicht gelesen werden."


def _channel_description(channels: int) -> str:
    if channels == 1:
        return "Mono (1 Kanal)"
    if channels == 2:
        return "Stereo (2 Kanäle)"
    return f"{channels} Kanäle" if channels > 0 else "Nicht verfügbar"


def _channel_layout_text(layout: str, channels: int) -> str:
    return {
        "mono": "Mono",
        "stereo": "Stereo",
    }.get(layout.casefold(), layout or _channel_description(channels))


def _format_audio_duration(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds <= 0:
        return "Nicht verfügbar"
    whole_seconds = int(seconds)
    milliseconds = round((seconds - whole_seconds) * 1000)
    if milliseconds == 1000:
        whole_seconds += 1
        milliseconds = 0
    minutes, remaining = divmod(whole_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    base = f"{hours:d}:{minutes:02d}:{remaining:02d}" if hours else f"{minutes:d}:{remaining:02d}"
    return f"{base},{milliseconds:03d}"
