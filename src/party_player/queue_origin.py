"""Derive a truthful display origin from persisted active-queue entries."""

from dataclasses import dataclass
from pathlib import Path

from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry


@dataclass(frozen=True, slots=True)
class QueueOrigin:
    kind: str
    label: str


def derive_queue_origin(entries: list[QueueEntry]) -> QueueOrigin:
    """Summarize entry provenance without consulting playlist UI selection."""

    active_statuses = {
        QueueStatus.WAITING,
        QueueStatus.PREPARING,
        QueueStatus.LOADED,
        QueueStatus.READY,
        QueueStatus.PLAYING,
    }
    active_entries = [entry for entry in entries if entry.status in active_statuses]
    if not active_entries:
        return QueueOrigin("empty", "Queue leer")
    origins = {entry_origin(entry) for entry in active_entries}
    if len(origins) != 1:
        return QueueOrigin("mixed", "gemischte Queue")
    kind, name = origins.pop()
    if kind == "directory":
        return QueueOrigin(kind, f"Verzeichnis · {name}" if name else "Verzeichnis")
    if kind == "playlist":
        return QueueOrigin(kind, f"Playlist · {name}" if name else "Playlist")
    return QueueOrigin(kind, name)


def entry_origin(entry: QueueEntry) -> tuple[str, str]:
    """Return safe origin kind and label without exposing a full directory path."""
    raw = entry.source_detail.strip()
    normalized = raw.casefold()
    if normalized.startswith("directory:"):
        path = raw.split(":", 1)[1].rstrip("/\\")
        return "directory", Path(path).name or path
    if normalized.startswith("saved_queue:"):
        return "playlist", raw.split(":", 1)[1].strip()
    if normalized == "catalog":
        return "catalog", "Katalog"
    if entry.source is QueueSource.PLAYLIST:
        return "playlist", ""
    if entry.source is QueueSource.GUEST_REQUEST:
        return "manual", "manuell zusammengestellt"
    if entry.source is QueueSource.EMERGENCY:
        return "emergency", "Notfallauswahl"
    if entry.source is QueueSource.AUTOMATIC:
        return "automatic", "Automatische Auswahl"
    return "manual", "manuell zusammengestellt"
