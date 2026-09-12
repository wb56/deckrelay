"""Immutable context for one automatic-selection sequence step."""

from dataclasses import dataclass

from party_player.selection_metadata import SelectionMetadataSnapshot


@dataclass(frozen=True, slots=True)
class SelectionSequenceContext:
    """Path-free predecessor information for current and future soft rules."""

    previous_track_id: int | None
    previous_metadata: SelectionMetadataSnapshot | None
    step_index: int
