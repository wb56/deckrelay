"""Versioned productive profiles and conservative tempo-result thresholds."""

from dataclasses import dataclass
from enum import StrEnum

from party_player.metadata_analysis_contracts import MetadataAnalysisKind


ALGORITHM_VERSION = "ffmpeg-onset-acf-v0.5"
PROFILE_VERSION = "tempo-profile-v3"
HIGH_CONFIDENCE = 0.80
MINIMUM_SUGGESTION_CONFIDENCE = 0.55
TEMPO_CHANGE_STABILITY = 0.65


class MetadataAnalysisProfile(StrEnum):
    TEMPO = "TEMPO"
    TEMPO_AND_ENERGY_EXPERIMENTAL = "TEMPO_AND_ENERGY_EXPERIMENTAL"


class ConfidenceBand(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class ProfileConfiguration:
    profile: MetadataAnalysisProfile
    requested_kinds: tuple[MetadataAnalysisKind, ...]
    segment_strategy: str
    maximum_analyzed_seconds: float
    timeout_seconds: float


PROFILE_CONFIGURATIONS = {
    MetadataAnalysisProfile.TEMPO: ProfileConfiguration(
        MetadataAnalysisProfile.TEMPO,
        (MetadataAnalysisKind.BPM,),
        "distributed",
        90.0,
        180.0,
    ),
    MetadataAnalysisProfile.TEMPO_AND_ENERGY_EXPERIMENTAL: ProfileConfiguration(
        MetadataAnalysisProfile.TEMPO_AND_ENERGY_EXPERIMENTAL,
        (MetadataAnalysisKind.BPM, MetadataAnalysisKind.ENERGY),
        "distributed",
        90.0,
        180.0,
    ),
}


def confidence_band(confidence: float) -> ConfidenceBand:
    if confidence >= HIGH_CONFIDENCE:
        return ConfidenceBand.HIGH
    if confidence >= MINIMUM_SUGGESTION_CONFIDENCE:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW
