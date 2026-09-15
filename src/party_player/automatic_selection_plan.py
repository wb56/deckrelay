"""Immutable persistence models for a future automatic-selection plan."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import math
import re
from uuid import UUID

from party_player.enums import QueueStatus

_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AutomaticSelectionPlanStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    INVALIDATED = "INVALIDATED"
    DISCARDED = "DISCARDED"


class AutomaticSelectionPlanStepStatus(StrEnum):
    PLANNED = "PLANNED"
    QUEUED = "QUEUED"
    INVALIDATED = "INVALIDATED"


def _require_uuid(value: str, field_name: str) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field_name} must be a canonical UUID")


def _require_code(value: str, field_name: str) -> None:
    if not _CODE_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded machine-readable code")


def _require_timestamp(value: datetime, field_name: str) -> None:
    if value.tzinfo is not None and value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must use UTC")


@dataclass(frozen=True, slots=True)
class AutomaticSelectionPlan:
    plan_id: str
    session_id: int
    status: AutomaticSelectionPlanStatus
    created_at: datetime
    updated_at: datetime
    planning_seed: int | None
    rule_configuration_version: int
    rule_configuration_digest: str
    rationale_schema_version: int
    source_context_code: str
    planned_depth: int
    revision: int = 0
    invalid_reason_code: str | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.plan_id, "plan_id")
        if isinstance(self.session_id, bool) or self.session_id <= 0:
            raise ValueError("session_id must be positive")
        if self.planning_seed is not None and (
            isinstance(self.planning_seed, bool) or not isinstance(self.planning_seed, int)
        ):
            raise ValueError("planning_seed must be an integer or None")
        if (
            isinstance(self.rule_configuration_version, bool)
            or isinstance(self.rationale_schema_version, bool)
            or self.rule_configuration_version < 1
            or self.rationale_schema_version < 1
        ):
            raise ValueError("schema and configuration versions must be positive")
        if not _DIGEST_PATTERN.fullmatch(self.rule_configuration_digest):
            raise ValueError("rule_configuration_digest must be lowercase SHA-256")
        _require_code(self.source_context_code, "source_context_code")
        if not 1 <= self.planned_depth <= 10:
            raise ValueError("planned_depth must be between 1 and 10")
        if isinstance(self.revision, bool) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        _require_timestamp(self.created_at, "created_at")
        _require_timestamp(self.updated_at, "updated_at")
        needs_reason = self.status in {
            AutomaticSelectionPlanStatus.INVALIDATED,
            AutomaticSelectionPlanStatus.DISCARDED,
        }
        if needs_reason != (self.invalid_reason_code is not None):
            raise ValueError("invalid_reason_code does not match plan status")
        if self.invalid_reason_code is not None:
            _require_code(self.invalid_reason_code, "invalid_reason_code")


@dataclass(frozen=True, slots=True)
class AutomaticSelectionPlanStep:
    step_id: str
    plan_id: str
    position: int
    track_id: int
    status: AutomaticSelectionPlanStepStatus
    planned_at: datetime
    previous_track_id: int | None
    relaxation_stage_code: str
    primary_play_count: int | None
    secondary_score: float
    tie_candidate_count: int
    tie_break_method_code: str
    selection_reason_code: str
    executed_queue_entry_id: int | None = None
    executed_queue_status: QueueStatus | None = None
    invalid_reason_code: str | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.step_id, "step_id")
        _require_uuid(self.plan_id, "plan_id")
        if self.position < 1 or self.track_id < 1:
            raise ValueError("position and track_id must be positive")
        if self.previous_track_id is not None and self.previous_track_id < 1:
            raise ValueError("previous_track_id must be positive or None")
        if self.primary_play_count is not None and self.primary_play_count < 0:
            raise ValueError("primary_play_count must be non-negative or None")
        if not math.isfinite(self.secondary_score):
            raise ValueError("secondary_score must be finite")
        if self.tie_candidate_count < 1:
            raise ValueError("tie_candidate_count must be positive")
        for value, name in (
            (self.relaxation_stage_code, "relaxation_stage_code"),
            (self.tie_break_method_code, "tie_break_method_code"),
            (self.selection_reason_code, "selection_reason_code"),
        ):
            _require_code(value, name)
        _require_timestamp(self.planned_at, "planned_at")
        queued = self.status is AutomaticSelectionPlanStepStatus.QUEUED
        if queued != (self.executed_queue_entry_id is not None):
            raise ValueError("executed_queue_entry_id does not match step status")
        if self.executed_queue_status is not None and not queued:
            raise ValueError("queue status is only valid for queued steps")
        invalidated = self.status is AutomaticSelectionPlanStepStatus.INVALIDATED
        if invalidated != (self.invalid_reason_code is not None):
            raise ValueError("invalid_reason_code does not match step status")
        if self.invalid_reason_code is not None:
            _require_code(self.invalid_reason_code, "invalid_reason_code")


@dataclass(frozen=True, slots=True)
class AutomaticSelectionPlanBundle:
    plan: AutomaticSelectionPlan
    steps: tuple[AutomaticSelectionPlanStep, ...]

    def __post_init__(self) -> None:
        if len(self.steps) != self.plan.planned_depth:
            raise ValueError("step count must equal planned_depth")
        for position, step in enumerate(self.steps, start=1):
            if step.plan_id != self.plan.plan_id or step.position != position:
                raise ValueError("steps must belong to the plan and be contiguous")


def selection_configuration_digest(configuration: Mapping[str, object]) -> str:
    """Return a stable digest without retaining configuration values."""
    canonical = _canonical_value(configuration)
    encoded = json.dumps(
        {"digest_format_version": 1, "configuration": canonical},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("configuration numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("configuration keys must be strings")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    raise ValueError("configuration contains an unsupported value")
