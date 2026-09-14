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
    GENRE_DIVERSITY_RULE_ID,
    MOOD_CONTINUITY_RULE_ID,
    BpmContinuityRule,
    ContinuityRuleSetting,
    EnergyContinuityRule,
    GenreDiversityRule,
    MoodContinuityRule,
    SelectionContinuitySettings,
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
    selection_metadata_terms,
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
    main_genre: str | None = None,
    additional_genres: tuple[str, ...] = (),
    moods: tuple[str, ...] = (),
    term_status: MetadataReviewStatus = MetadataReviewStatus.CONFIRMED_WITH_VALUE,
) -> SelectionMetadataSnapshot:
    return replace(
        missing_metadata_snapshot(track_id),
        bpm=_value(bpm),
        alternative_bpm=_value(alternative_bpm),
        energy=_value(energy),
        main_genre=selection_metadata_value(
            main_genre,
            source=MetadataSource.MANUAL_CONFIRMATION,
            confidence=None,
            review_status=term_status,
        ),
        additional_genres=selection_metadata_terms(
            additional_genres,
            source=MetadataSource.MANUAL_CONFIRMATION,
            confidence=None,
            review_status=term_status,
        ),
        moods=selection_metadata_terms(
            moods,
            source=MetadataSource.MANUAL_CONFIRMATION,
            confidence=None,
            review_status=term_status,
        ),
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


def test_genre_repeated_normalized_main_genre_has_priority() -> None:
    result = _evaluate(
        GenreDiversityRule,
        candidate=_metadata(
            2,
            main_genre="  CLASSIC   rock ",
            additional_genres=("rock", "dance"),
        ),
        previous=_metadata(
            1,
            main_genre="Classic Rock",
            additional_genres=("rock", "pop"),
        ),
    )

    assert result.rule_id == GENRE_DIVERSITY_RULE_ID
    assert result.rule_version == 1
    assert result.rule_kind is RuleKind.SOFT_WEIGHT
    assert result.result_code is RuleOutcome.SCORE_DELTA
    assert result.reason_code == "MAIN_GENRE_REPEATED"
    assert result.score_delta == -1.0
    assert dict(result.facts)["previous_main_genre"] == "classic rock"
    assert dict(result.facts)["candidate_main_genre"] == "classic rock"


@pytest.mark.parametrize(
    ("previous_terms", "candidate_terms", "expected", "reason_code"),
    [
        (("rock",), ("ROCK",), -0.5, "ADDITIONAL_GENRE_OVERLAP"),
        (("rock", "pop"), ("rock", "dance"), -1.0 / 6.0, "ADDITIONAL_GENRE_OVERLAP"),
        (("rock",), ("dance",), 0.0, "NO_GENRE_OVERLAP"),
        (("Rock",), ("Rock ’n’ Roll",), 0.0, "NO_GENRE_OVERLAP"),
    ],
)
def test_additional_genre_jaccard(
    previous_terms: tuple[str, ...],
    candidate_terms: tuple[str, ...],
    expected: float,
    reason_code: str,
) -> None:
    result = _evaluate(
        GenreDiversityRule,
        candidate=_metadata(2, main_genre="house", additional_genres=candidate_terms),
        previous=_metadata(1, main_genre="rock", additional_genres=previous_terms),
    )

    assert result.result_code is RuleOutcome.SCORE_DELTA
    assert result.reason_code == reason_code
    assert result.score_delta == pytest.approx(expected)


def test_empty_or_unusable_genres_are_unknown_metadata() -> None:
    empty = _evaluate(
        GenreDiversityRule,
        candidate=_metadata(2, additional_genres=("rock",)),
        previous=_metadata(1),
    )
    suggested = _evaluate(
        GenreDiversityRule,
        candidate=_metadata(2, additional_genres=("rock",)),
        previous=_metadata(
            1,
            additional_genres=("rock",),
            term_status=MetadataReviewStatus.SUGGESTED,
        ),
    )

    assert empty.result_code is RuleOutcome.UNKNOWN_METADATA
    assert empty.reason_code == "METADATA_UNKNOWN"
    assert suggested.result_code is RuleOutcome.UNKNOWN_METADATA


def test_unconfirmed_legacy_main_genre_is_not_used() -> None:
    previous = replace(
        _metadata(1),
        main_genre=selection_metadata_value(
            "rock", source=None, confidence=None, review_status=None
        ),
    )
    result = _evaluate(
        GenreDiversityRule,
        candidate=_metadata(2, main_genre="rock"),
        previous=previous,
    )

    assert result.result_code is RuleOutcome.UNKNOWN_METADATA
    assert result.reason_code == "METADATA_UNKNOWN"


@pytest.mark.parametrize(
    ("previous_terms", "candidate_terms", "expected", "reason_code"),
    [
        (("ruhig",), ("RUHIG",), 1.0, "MOOD_OVERLAP"),
        (("ruhig", "warm"), ("ruhig", "dunkel"), 1.0 / 3.0, "MOOD_OVERLAP"),
        (("ruhig",), ("energetisch",), 0.0, "NO_MOOD_OVERLAP"),
        (("warm",), ("kalt",), 0.0, "NO_MOOD_OVERLAP"),
    ],
)
def test_mood_jaccard_without_semantic_inference(
    previous_terms: tuple[str, ...],
    candidate_terms: tuple[str, ...],
    expected: float,
    reason_code: str,
) -> None:
    result = _evaluate(
        MoodContinuityRule,
        candidate=_metadata(2, moods=candidate_terms),
        previous=_metadata(1, moods=previous_terms),
    )

    assert result.rule_id == MOOD_CONTINUITY_RULE_ID
    assert result.rule_version == 1
    assert result.rule_kind is RuleKind.SOFT_WEIGHT
    assert result.result_code is RuleOutcome.SCORE_DELTA
    assert result.reason_code == reason_code
    assert result.score_delta == pytest.approx(expected)


def test_mood_normalization_deduplicates_terms() -> None:
    result = _evaluate(
        MoodContinuityRule,
        candidate=_metadata(2, moods=(" ruhig ", "RUHIG", "warm")),
        previous=_metadata(1, moods=("  RUHIG", "ruhig  ")),
    )

    facts = dict(result.facts)
    assert result.score_delta == 0.5
    assert facts["previous_mood_count"] == 1
    assert facts["candidate_mood_count"] == 2
    assert facts["common_terms"] == "ruhig"


@pytest.mark.parametrize(
    "status",
    [
        MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE,
        MetadataReviewStatus.MISSING,
        MetadataReviewStatus.REVIEW_REQUIRED,
        MetadataReviewStatus.SUGGESTED,
        MetadataReviewStatus.CONFLICTING,
        MetadataReviewStatus.FAILED,
        MetadataReviewStatus.OUTDATED,
    ],
)
def test_unusable_moods_are_unknown_metadata(status: MetadataReviewStatus) -> None:
    result = _evaluate(
        MoodContinuityRule,
        candidate=_metadata(2, moods=("ruhig",)),
        previous=_metadata(1, moods=("ruhig",), term_status=status),
    )

    assert result.result_code is RuleOutcome.UNKNOWN_METADATA
    assert result.reason_code == "METADATA_UNKNOWN"
    assert result.score_delta == 0.0


@pytest.mark.parametrize("rule_type", [GenreDiversityRule, MoodContinuityRule])
def test_term_rules_report_missing_predecessor(rule_type) -> None:
    result = _evaluate(rule_type, candidate=_metadata(2), previous=None)

    assert result.result_code is RuleOutcome.NOT_APPLICABLE
    assert result.reason_code == "PREDECESSOR_MISSING"


@pytest.mark.parametrize("rule_type", [GenreDiversityRule, MoodContinuityRule])
def test_term_rules_report_zero_weight_before_missing_metadata(rule_type) -> None:
    result = _evaluate(rule_type, candidate=_metadata(2), previous=None, weight=0.0)

    assert result.result_code is RuleOutcome.SCORE_DELTA
    assert result.reason_code == "ZERO_WEIGHT"
    assert result.score_delta == 0.0


@pytest.mark.parametrize("weight", [0.5, 1.0, 2.0])
def test_term_rule_weights_scale_with_full_precision(weight: float) -> None:
    result = _evaluate(
        MoodContinuityRule,
        candidate=_metadata(2, moods=("ruhig", "dunkel")),
        previous=_metadata(1, moods=("ruhig", "warm")),
        weight=weight,
    )

    assert result.score_delta == pytest.approx(weight / 3.0)


def test_genre_and_mood_are_disabled_by_default() -> None:
    settings = SelectionContinuitySettings()

    assert not settings.genre.enabled
    assert not settings.mood.enabled
