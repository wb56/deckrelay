"""Pure BPM and energy continuity rules for automatic selection."""

from dataclasses import dataclass
import math

from party_player.selection_decision import (
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    SelectionContext,
    SelectionRuleInput,
)
from party_player.selection_metadata import (
    SelectionMetadataCatalogSnapshot,
    SelectionMetadataDisposition,
    SelectionMetadataSnapshot,
    SelectionMetadataValue,
)


BPM_CONTINUITY_RULE_ID = "selection.bpm_continuity"
ENERGY_CONTINUITY_RULE_ID = "selection.energy_continuity"


@dataclass(frozen=True, slots=True)
class ContinuityRuleSetting:
    """Validated, non-persistent activation for one B2 rule."""

    enabled: bool = False
    weight: float = 1.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Aktivstatus muss boolesch sein")
        if not math.isfinite(self.weight) or not 0.0 <= self.weight <= 2.0:
            raise ValueError("Gewichtung muss endlich sein und zwischen 0 und 2 liegen")


@dataclass(frozen=True, slots=True)
class SelectionContinuitySettings:
    """Runtime-only settings; persistence and GUI activation follow later."""

    bpm: ContinuityRuleSetting = ContinuityRuleSetting()
    energy: ContinuityRuleSetting = ContinuityRuleSetting()


DEFAULT_SELECTION_CONTINUITY_SETTINGS = SelectionContinuitySettings()


class _ContinuityRule:
    rule_id: str
    rule_version = 1
    rule_kind = RuleKind.SOFT_WEIGHT
    relaxable_reason_codes: frozenset[str] = frozenset()
    value_label: str
    distance_reason_code: str

    def __init__(self, metadata: SelectionMetadataCatalogSnapshot, weight: float) -> None:
        if not math.isfinite(weight) or not 0.0 <= weight <= 2.0:
            raise ValueError("Gewichtung muss endlich sein und zwischen 0 und 2 liegen")
        self._metadata = {item.track_id: item for item in metadata.metadata}
        self._weight = float(weight)

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        if self._weight == 0.0:
            return self._evaluation(
                context,
                RuleOutcome.SCORE_DELTA,
                "ZERO_WEIGHT",
                "Gewichtung ist null; keine Punktänderung",
                raw_score=0.0,
            )
        sequence = context.sequence
        if (
            sequence is None
            or sequence.previous_track_id is None
            or sequence.previous_metadata is None
        ):
            return self._evaluation(
                context,
                RuleOutcome.NOT_APPLICABLE,
                "PREDECESSOR_MISSING",
                "Kein vorheriger Titel: Regel nicht anwendbar",
                raw_score=0.0,
            )
        candidate = self._metadata.get(rule_input.candidate.track_id)
        values = self._usable_values(sequence.previous_metadata, candidate)
        if values is None:
            return self._evaluation(
                context,
                RuleOutcome.UNKNOWN_METADATA,
                "METADATA_UNKNOWN",
                f"{self.value_label} unbekannt: keine Auswirkung",
                raw_score=0.0,
            )
        previous_value, candidate_value = values
        distance = abs(candidate_value - previous_value)
        raw_score = max(-1.0, min(1.0, self._raw_score(distance)))
        score_delta = raw_score * self._weight
        effect = "keine Punktänderung" if score_delta == 0.0 else f"{score_delta:+g}"
        return self._evaluation(
            context,
            RuleOutcome.SCORE_DELTA,
            self.distance_reason_code,
            f"{self.value_label}-Abstand {distance:g}: {effect}",
            raw_score=raw_score,
            score_delta=score_delta,
            previous_value=previous_value,
            candidate_value=candidate_value,
            distance=distance,
        )

    def _usable_values(
        self,
        previous: SelectionMetadataSnapshot,
        candidate: SelectionMetadataSnapshot | None,
    ) -> tuple[float, float] | None:
        if candidate is None:
            return None
        previous_field = self._field(previous)
        candidate_field = self._field(candidate)
        previous_value = self._usable_value(previous_field)
        candidate_value = self._usable_value(candidate_field)
        if previous_value is None or candidate_value is None:
            return None
        return previous_value, candidate_value

    def _usable_value(
        self,
        field: SelectionMetadataValue[float] | SelectionMetadataValue[int],
    ) -> float | None:
        if field.disposition is not SelectionMetadataDisposition.USABLE:
            return None
        value = field.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        if not math.isfinite(numeric) or not self._in_range(numeric):
            return None
        return numeric

    def _evaluation(
        self,
        context: SelectionContext,
        outcome: RuleOutcome,
        reason_code: str,
        reason: str,
        *,
        raw_score: float,
        score_delta: float = 0.0,
        previous_value: float | None = None,
        candidate_value: float | None = None,
        distance: float | None = None,
    ) -> RuleEvaluation:
        return RuleEvaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            rule_kind=self.rule_kind,
            result_code=outcome,
            reason_code=reason_code,
            reason=reason,
            relaxation_stage=context.relaxation_stage,
            facts=(
                ("raw_score", raw_score),
                ("weight", self._weight),
                ("previous_value", previous_value),
                ("candidate_value", candidate_value),
                ("distance", distance),
            ),
            score_delta=score_delta,
        )

    def _field(
        self,
        metadata: SelectionMetadataSnapshot,
    ) -> SelectionMetadataValue[float] | SelectionMetadataValue[int]:
        raise NotImplementedError

    def _in_range(self, value: float) -> bool:
        raise NotImplementedError

    def _raw_score(self, distance: float) -> float:
        raise NotImplementedError


class BpmContinuityRule(_ContinuityRule):
    rule_id = BPM_CONTINUITY_RULE_ID
    value_label = "BPM"
    distance_reason_code = "BPM_DISTANCE"

    def _field(self, metadata: SelectionMetadataSnapshot) -> SelectionMetadataValue[float]:
        return metadata.bpm

    def _in_range(self, value: float) -> bool:
        return 20.0 <= value <= 300.0

    def _raw_score(self, distance: float) -> float:
        if distance <= 5.0:
            return 1.0
        if distance < 15.0:
            return 1.0 - (distance - 5.0) / 10.0
        if distance < 30.0:
            return -(distance - 15.0) / 15.0
        return -1.0


class EnergyContinuityRule(_ContinuityRule):
    rule_id = ENERGY_CONTINUITY_RULE_ID
    value_label = "Energie"
    distance_reason_code = "ENERGY_DISTANCE"

    def _field(self, metadata: SelectionMetadataSnapshot) -> SelectionMetadataValue[int]:
        return metadata.energy

    def _in_range(self, value: float) -> bool:
        return 0.0 <= value <= 100.0

    def _raw_score(self, distance: float) -> float:
        if distance <= 10.0:
            return 1.0
        if distance < 25.0:
            return 1.0 - (distance - 10.0) / 15.0
        if distance < 50.0:
            return -(distance - 25.0) / 25.0
        return -1.0
