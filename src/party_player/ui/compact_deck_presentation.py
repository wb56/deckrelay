"""Pure state projection used by the compact deck view."""

from dataclasses import dataclass
from pathlib import Path

from party_player.enums import DeckState
from party_player.models import Deck


_STATE_TEXT = {
    DeckState.EMPTY: "LEER",
    DeckState.LOADED: "GELADEN",
    DeckState.PLAYING: "WIEDERGABE",
    DeckState.PAUSED: "PAUSE",
    DeckState.STOPPED: "GESTOPPT",
    DeckState.FINISHED: "BEENDET",
    DeckState.ERROR: "FEHLER",
}


@dataclass(frozen=True, slots=True)
class CompactDeckPresentation:
    title: str
    source: str
    state: str
    position: float
    duration: float
    remaining: float
    progress: float
    volume: float
    on_air: bool
    error: str
    warning: str
    bpm: float | None


def compact_deck_presentation(deck: Deck) -> CompactDeckPresentation:
    """Project the existing Deck state without mutating or duplicating it."""
    track = deck.loaded_track
    duration = max(0.0, deck.duration)
    position = min(max(0.0, deck.position), duration) if duration else 0.0
    title = "Kein Titel geladen"
    source = "—"
    if track is not None:
        title = f"{track.artist or 'Unbekannt'} – {track.title}"
        source = Path(track.file_path).name
    return CompactDeckPresentation(
        title=title,
        source=source,
        state="● ON AIR" if deck.is_on_air else _STATE_TEXT[deck.state],
        position=position,
        duration=duration,
        remaining=max(0.0, duration - position),
        progress=position / max(1.0, duration),
        volume=deck.volume,
        on_air=deck.is_on_air,
        error=deck.error_message,
        warning=deck.cue_warning,
        bpm=track.bpm if track is not None else None,
    )
