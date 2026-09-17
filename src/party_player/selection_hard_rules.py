"""Immutable, path-free facts used by hard automatic-selection rules."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TypeVar


@dataclass(frozen=True, slots=True)
class SelectionHardRulePolicyFact:
    status: str
    reason: str = ""
    scope: str | None = None
    session_id: int | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SelectionHardRuleCueFact:
    manual_cue_in: float | None = None
    manual_cue_out: float | None = None
    manual_fade_duration: float | None = None
    automatic_cue_in: float | None = None
    automatic_cue_out: float | None = None
    automatic_fade_duration: float | None = None


@dataclass(frozen=True, slots=True)
class SelectionHardRuleRecentPlay:
    track_id: int
    artist: str
    finished_at: datetime


T = TypeVar("T")


def immutable_mapping(values: Mapping[int | str, T]) -> Mapping[int | str, T]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class SelectionHardRuleCatalogSnapshot:
    track_policies: Mapping[int | str, SelectionHardRulePolicyFact]
    artist_policies: Mapping[int | str, SelectionHardRulePolicyFact]
    suitability: Mapping[int | str, SelectionHardRulePolicyFact]
    cues: Mapping[int | str, SelectionHardRuleCueFact]
    recent_plays: tuple[SelectionHardRuleRecentPlay, ...]

    @classmethod
    def empty(cls) -> "SelectionHardRuleCatalogSnapshot":
        return cls(
            immutable_mapping({}),
            immutable_mapping({}),
            immutable_mapping({}),
            immutable_mapping({}),
            (),
        )
