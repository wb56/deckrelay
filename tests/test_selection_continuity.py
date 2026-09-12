"""Focused tests for BPM and energy continuity scoring."""

from dataclasses import replace
import math

import pytest

from party_player.enums import QueueSource, QueueStatus
from party_player.metadata_rules import MetadataReviewStatus, MetadataSource
from party_player.models import QueueEntry, Track
from party_player.selection_continuity import (
    BPM_CONTINUITY_RULE_ID,
    ENERGY_CONTINUITY_RULE_ID,
    BpmContinuityRule,
    ContinuityRuleSetting,
    EnergyContinuityRule,
)
from party_player.selection_decision import (
    RuleKind,
    RuleOutcome,
    SelectionContext,
    SelectionRuleInput,
)
from party_player.selection_metadata import (
    SelectionMetadataCatalogSnapshot,
    SelectionMetadataDisposition,
    SelectionMetadataSnapshot,
    missing_metadata_snapshot,
    selection_metadata_value,
)
from party_player.selection_sequence import SelectionSequenceContext


def _value(
    value: float | int | None,
    *,
    source: MetadataSource = MetadataSource.MANUAL_CONFIRMATION,
    status: MetadataReviewStatus = MetadataReviewStatus.CONFIRMED_WITH_VALUE,
    confidence: float | None = None,
    minimum_confidence: float | None = None,
):
    return selection_metadata_value(
        value,
        source=source,
        confidence=confidence,
        review_status=status,
        minimum_analysis_confidence=minimum_confidence,
    )


def _metadata(
    track_id: int,
    *,
    bpm: float | None = 100.0,
    energy: int | float | None = 50,
    alternative_bpm: float | None = None,
) -> SelectionMetadataSnapshot:
    return replace(
        missing_metadata_snapshot(track_id),
        bpm=_value(bpm),
        alternative_bpm=_value(alternative_bpm),
        energy=_value(energy),
    )


def _evaluate(rule_type, *, candidate, previous, weight=1.0):
    catalog = SelectionMetadataCatalogSnapshot((candidate,))
    rule = rule_type(catalog, weight)
    track = Track(candidate.track_id, "private.mp3", "Candidate", "Artist", "", 180.0)
    entry = QueueEntry(
        -candidate.track_id,
        candidate.track_id,
        0,
        QueueStatus.WAITING,
        source=QueueSource.AUTOMATIC,
    )
    context = SelectionContext(
        "continuity-test",
        sequence=SelectionSequenceContext(
            previous.track_id if previous is not None else None,
            previous,
            1,
        ),
    )
    return rule.evaluate_rule(SelectionRuleInput.from_values(entry, track), context)


@pytest.mark.parametrize(
    ("distance", "expected"),
    [(0, 1.0), (5, 1.0), (10, 0.5), (15, 0.0), (22.5, -0.5), (30, -1.0), (45, -1.0)],
)
def test_bpm_continuity_boundaries(distance: float, expected: float) -> None:
    result = _evaluate(
        BpmContinuityRule,
        candidate=_metadata(2, bpm=100.0 + distance),
        previous=_metadata(1, bpm=100.0),
    )

    assert result.rule_id == BPM_CONTINUITY_RULE_ID
    assert result.rule_version == 1
    assert result.rule_kind is RuleKind.SOFT_WEIGHT
    assert result.result_code is RuleOutcome.SCORE_DELTA
    assert result.reason_code == "BPM_DISTANCE"
    assert dict(result.facts) == {
        "raw_score": expected,
        "weight": 1.0,
        "previous_value": 100.0,
        "candidate_value": 100.0 + distance,
        "distance": distance,
    }
    assert result.score_delta == expected


@pytest.mark.parametrize(
    ("distance", "expected"),
    [(0, 1.0), (10, 1.0), (17.5, 0.5), (25, 0.0), (37.5, -0.5), (50, -1.0), (75, -1.0)],
)
def test_energy_continuity_boundaries(distance: float, expected: float) -> None:
    result = _evaluate(
        EnergyContinuityRule,
        candidate=_metadata(2, energy=distance),
        previous=_metadata(1, energy=0),
    )

    assert result.rule_id == ENERGY_CONTINUITY_RULE_ID
    assert result.result_code is RuleOutcome.SCORE_DELTA
    assert result.reason_code == "ENERGY_DISTANCE"
    assert dict(result.facts)["raw_score"] == expected
    assert result.score_delta == expected


def test_alternative_bpm_is_ignored() -> None:
    result = _evaluate(
        BpmContinuityRule,
        candidate=_metadata(2, bpm=105.0, alternative_bpm=20.0),
        previous=_metadata(1, bpm=100.0, alternative_bpm=300.0),
    )

    assert result.score_delta == 1.0
    assert dict(result.facts)["distance"] == 5.0


@pytest.mark.parametrize("weight", [0.5, 1.0, 2.0])
def test_weight_scales_full_precision_score(weight: float) -> None:
    result = _evaluate(
        BpmContinuityRule,
        candidate=_metadata(2, bpm=110.0),
        previous=_metadata(1, bpm=100.0),
        weight=weight,
    )

    assert result.score_delta == 0.5 * weight


@pytest.mark.parametrize("weight", [-0.1, 2.1, math.inf, -math.inf, math.nan])
def test_invalid_runtime_weights_are_rejected(weight: float) -> None:
    with pytest.raises(ValueError, match="zwischen 0 und 2"):
        ContinuityRuleSetting(True, weight)


def test_zero_weight_is_an_explicit_neutral_score() -> None:
    result = _evaluate(
        BpmContinuityRule,
        candidate=_metadata(2, bpm=None),
        previous=None,
        weight=0.0,
    )

    assert result.result_code is RuleOutcome.SCORE_DELTA
    assert result.reason_code == "ZERO_WEIGHT"
    assert result.score_delta == 0.0


def test_missing_predecessor_is_not_applicable() -> None:
    result = _evaluate(
        EnergyContinuityRule,
        candidate=_metadata(2),
        previous=None,
    )

    assert result.result_code is RuleOutcome.NOT_APPLICABLE
    assert result.reason_code == "PREDECESSOR_MISSING"


def test_missing_predecessor_snapshot_is_not_applicable() -> None:
    candidate = _metadata(2)
    rule = BpmContinuityRule(SelectionMetadataCatalogSnapshot((candidate,)), 1.0)
    track = Track(2, "private.mp3", "Candidate", "Artist", "", 180.0)
    entry = QueueEntry(-2, 2, 0, QueueStatus.WAITING, source=QueueSource.AUTOMATIC)
    context = SelectionContext(
        "missing-predecessor-snapshot",
        sequence=SelectionSequenceContext(1, None, 1),
    )

    result = rule.evaluate_rule(SelectionRuleInput.from_values(entry, track), context)

    assert result.result_code is RuleOutcome.NOT_APPLICABLE
    assert result.reason_code == "PREDECESSOR_MISSING"


@pytest.mark.parametrize(
    "field",
    [
        _value(None),
        _value(100, status=MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE),
        _value(100, status=MetadataReviewStatus.MISSING),
        selection_metadata_value(100, source=None, confidence=None, review_status=None),
        _value(100, status=MetadataReviewStatus.REVIEW_REQUIRED),
        _value(100, status=MetadataReviewStatus.CONFLICTING),
        _value(100, status=MetadataReviewStatus.FAILED),
        _value(100, status=MetadataReviewStatus.OUTDATED),
        _value(
            100,
            source=MetadataSource.AUDIO_ANALYSIS,
            status=MetadataReviewStatus.ANALYSED,
            confidence=0.899,
            minimum_confidence=0.9,
        ),
        _value(math.inf),
        _value(19.9),
        _value(300.1),
    ],
)
def test_unusable_or_invalid_bpm_is_neutral(field) -> None:
    candidate = replace(_metadata(2), bpm=field)
    result = _evaluate(BpmContinuityRule, candidate=candidate, previous=_metadata(1))

    assert result.result_code is RuleOutcome.UNKNOWN_METADATA
    assert result.reason_code == "METADATA_UNKNOWN"
    assert result.score_delta == 0.0


@pytest.mark.parametrize("value", [math.inf, -1, 101])
def test_invalid_energy_is_neutral(value: float) -> None:
    result = _evaluate(
        EnergyContinuityRule,
        candidate=_metadata(2, energy=value),
        previous=_metadata(1),
    )

    assert result.result_code is RuleOutcome.UNKNOWN_METADATA
    assert result.reason_code == "METADATA_UNKNOWN"


def test_manual_imported_and_confident_analysis_values_are_usable() -> None:
    previous = replace(
        _metadata(1),
        bpm=_value(
            100.0,
            source=MetadataSource.AUDIO_ANALYSIS,
            status=MetadataReviewStatus.ANALYSED,
            confidence=0.9,
            minimum_confidence=0.9,
        ),
        energy=_value(50, source=MetadataSource.FILE_TAG, status=MetadataReviewStatus.IMPORTED),
    )
    candidate = replace(
        _metadata(2),
        bpm=_value(105.0),
        energy=_value(
            60,
            source=MetadataSource.AUDIO_ANALYSIS,
            status=MetadataReviewStatus.ANALYSED,
            confidence=0.8,
            minimum_confidence=0.8,
        ),
    )

    assert _evaluate(BpmContinuityRule, candidate=candidate, previous=previous).score_delta == 1.0
    assert (
        _evaluate(EnergyContinuityRule, candidate=candidate, previous=previous).score_delta == 1.0
    )


def test_candidate_snapshot_missing_is_unknown_metadata() -> None:
    previous = _metadata(1)
    catalog = SelectionMetadataCatalogSnapshot(())
    rule = BpmContinuityRule(catalog, 1.0)
    track = Track(2, "private.mp3", "Candidate", "Artist", "", 180.0)
    entry = QueueEntry(-2, 2, 0, QueueStatus.WAITING, source=QueueSource.AUTOMATIC)
    context = SelectionContext(
        "missing-candidate",
        sequence=SelectionSequenceContext(1, previous, 1),
    )

    result = rule.evaluate_rule(SelectionRuleInput.from_values(entry, track), context)

    assert result.result_code is RuleOutcome.UNKNOWN_METADATA
    assert result.reason_code == "METADATA_UNKNOWN"


def test_disposition_thresholds_remain_owned_by_snapshot_projection() -> None:
    too_low_bpm = _value(
        100.0,
        source=MetadataSource.AUDIO_ANALYSIS,
        status=MetadataReviewStatus.ANALYSED,
        confidence=0.899,
        minimum_confidence=0.9,
    )
    too_low_energy = _value(
        50,
        source=MetadataSource.AUDIO_ANALYSIS,
        status=MetadataReviewStatus.ANALYSED,
        confidence=0.799,
        minimum_confidence=0.8,
    )

    assert too_low_bpm.disposition is SelectionMetadataDisposition.CONFIDENCE_TOO_LOW
    assert too_low_energy.disposition is SelectionMetadataDisposition.CONFIDENCE_TOO_LOW
