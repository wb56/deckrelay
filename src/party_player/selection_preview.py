"""Immutable result model for state-neutral automatic-selection previews."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from party_player.selection_decision import SelectionRationale


class SelectionPreviewCompletion(StrEnum):
    REQUESTED_DEPTH_REACHED = "REQUESTED_DEPTH_REACHED"
    NO_SAFE_CANDIDATE = "NO_SAFE_CANDIDATE"


@dataclass(frozen=True, slots=True)
class SelectionPreviewStep:
    position: int
    track_id: int
    title: str
    artist: str
    relaxation_stage: str
    rationale: SelectionRationale


@dataclass(frozen=True, slots=True)
class SelectionPreview:
    preview_id: str
    created_at: datetime
    requested_depth: int
    achieved_depth: int
    steps: tuple[SelectionPreviewStep, ...]
    completion_reason: SelectionPreviewCompletion
    warning: str = (
        "Momentaufnahme: Queue-Eingriffe, Wünsche, Metadatenänderungen, Dateifehler "
        "oder zwischenzeitliche Wiedergaben können die spätere reale Folge verändern."
    )
    schema_version: int = 2
    completion_rationale: SelectionRationale | None = None
