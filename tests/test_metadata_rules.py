"""Unit tests for pure catalog metadata vocabulary and decisions."""

from dataclasses import FrozenInstanceError

import pytest

from party_player.metadata_rules import (
    FIELD_DEFINITIONS,
    MAXIMUM_COMMENT_LENGTH,
    EffectiveMetadataValue,
    EmptyValueBehavior,
    MetadataFieldKey,
    MetadataReviewStatus,
    MetadataSource,
    MetadataSuggestion,
    MetadataSuggestionDecision,
    RecordingClassification,
    RecordingKind,
    RecordingTrait,
    SuggestionDecisionKind,
    decide_metadata_suggestion,
    normalize_metadata_value,
    release_decade,
)


def suggestion(value: object, confidence: float = 0.95) -> MetadataSuggestion:
    return MetadataSuggestion(value, MetadataSource.AUDIO_ANALYSIS, confidence, "test-v1")


def test_every_required_field_has_one_immutable_definition() -> None:
    assert set(FIELD_DEFINITIONS) == set(MetadataFieldKey)
    assert len(FIELD_DEFINITIONS) == 19
    assert FIELD_DEFINITIONS[MetadataFieldKey.TAGS].multiple
    assert FIELD_DEFINITIONS[MetadataFieldKey.TAGS].empty_behavior is (
        EmptyValueBehavior.EMPTY_COLLECTION
    )
    with pytest.raises(TypeError):
        FIELD_DEFINITIONS[MetadataFieldKey.BPM] = FIELD_DEFINITIONS[MetadataFieldKey.BPM]  # type: ignore[index]


@pytest.mark.parametrize("year", [1877, 1950, 2026, 2100])
def test_years_and_derived_release_decade(year: int) -> None:
    assert normalize_metadata_value(MetadataFieldKey.YEAR, year) == year
    assert release_decade(year) == year // 10 * 10


@pytest.mark.parametrize("year", [1876, 2101, 1999.5, "1999"])
def test_implausible_years_are_rejected(year: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_metadata_value(MetadataFieldKey.ORIGINAL_RELEASE_YEAR, year)


def test_release_decade_is_absent_without_original_year() -> None:
    assert release_decade(None) is None


def test_recording_kind_and_remaster_trait_are_independent() -> None:
    value = RecordingClassification(RecordingKind.LIVE, frozenset({RecordingTrait.REMASTERED}))
    assert normalize_metadata_value(MetadataFieldKey.RECORDING_CLASSIFICATION, value) == value


@pytest.mark.parametrize("rating", [1, 3, 5, None])
def test_rating_accepts_one_to_five_or_unrated(rating: int | None) -> None:
    assert normalize_metadata_value(MetadataFieldKey.RATING, rating) == rating


@pytest.mark.parametrize("rating", [0, 6, 2.5])
def test_invalid_rating_is_rejected(rating: float) -> None:
    with pytest.raises(ValueError):
        normalize_metadata_value(MetadataFieldKey.RATING, rating)


@pytest.mark.parametrize("key", [MetadataFieldKey.BPM, MetadataFieldKey.ALTERNATIVE_BPM])
def test_bpm_and_alternative_bpm_share_plausible_bounds(key: MetadataFieldKey) -> None:
    assert normalize_metadata_value(key, 128) == 128.0
    for invalid in (0, 19.9, 300.1, float("nan")):
        with pytest.raises(ValueError):
            normalize_metadata_value(key, invalid)


@pytest.mark.parametrize("key", [MetadataFieldKey.ENERGY, MetadataFieldKey.DANCEABILITY])
def test_percentage_scores_are_integer_zero_to_one_hundred(key: MetadataFieldKey) -> None:
    assert normalize_metadata_value(key, 0) == 0
    assert normalize_metadata_value(key, 100) == 100
    with pytest.raises(ValueError):
        normalize_metadata_value(key, 50.5)


def test_confidence_value_uses_zero_to_one_range() -> None:
    assert normalize_metadata_value(MetadataFieldKey.BPM_CONFIDENCE, 0.75) == 0.75
    with pytest.raises(ValueError):
        normalize_metadata_value(MetadataFieldKey.BPM_CONFIDENCE, 1.01)


def test_multivalues_are_normalized_and_case_insensitively_deduplicated() -> None:
    assert normalize_metadata_value(
        MetadataFieldKey.MOODS, ["  Gute   Laune ", "gute laune", "Ruhig"]
    ) == ("Gute Laune", "Ruhig")
    assert normalize_metadata_value(MetadataFieldKey.MUSICAL_DECADES, [1990, 1980, 1990]) == (
        1980,
        1990,
    )


def test_empty_multivalue_elements_are_rejected() -> None:
    with pytest.raises(ValueError, match="keine leeren"):
        normalize_metadata_value(MetadataFieldKey.TAGS, ["Party", "  "])


def test_comment_is_normalized_and_bounded() -> None:
    assert normalize_metadata_value(MetadataFieldKey.COMMENT, "  Nur   Katalog  ") == (
        "Nur Katalog"
    )
    with pytest.raises(ValueError, match="höchstens"):
        normalize_metadata_value(MetadataFieldKey.COMMENT, "x" * (MAXIMUM_COMMENT_LENGTH + 1))


def test_confirmed_value_and_confirmed_absence_are_protected() -> None:
    for status, value in (
        (MetadataReviewStatus.CONFIRMED_WITH_VALUE, 120.0),
        (MetadataReviewStatus.CONFIRMED_WITHOUT_VALUE, None),
    ):
        current = EffectiveMetadataValue(value, MetadataSource.MANUAL_CONFIRMATION, status)
        decision = decide_metadata_suggestion(MetadataFieldKey.BPM, current, suggestion(128.0))
        assert current.protected
        assert decision.kind is SuggestionDecisionKind.PROPOSED


def test_high_confidence_can_apply_and_low_confidence_remains_proposed() -> None:
    assert (
        decide_metadata_suggestion(MetadataFieldKey.BPM, None, suggestion(128.0, 0.95)).kind
        is SuggestionDecisionKind.APPLIED
    )
    assert (
        decide_metadata_suggestion(MetadataFieldKey.BPM, None, suggestion(128.0, 0.89)).kind
        is SuggestionDecisionKind.PROPOSED
    )


def test_conflicting_effective_value_requires_review() -> None:
    current = EffectiveMetadataValue(120.0, MetadataSource.FILE_TAG, MetadataReviewStatus.IMPORTED)
    decision = decide_metadata_suggestion(MetadataFieldKey.BPM, current, suggestion(128.0))
    assert decision.kind is SuggestionDecisionKind.REVIEW_REQUIRED


def test_existing_conflict_without_effective_value_still_requires_review() -> None:
    current = EffectiveMetadataValue(
        None, MetadataSource.EXTERNAL_MUSIC_DATABASE, MetadataReviewStatus.CONFLICTING
    )
    decision = decide_metadata_suggestion(MetadataFieldKey.BPM, current, suggestion(128.0))
    assert decision.kind is SuggestionDecisionKind.REVIEW_REQUIRED


def test_subjective_field_is_never_automatically_applied() -> None:
    decision = decide_metadata_suggestion(MetadataFieldKey.MAIN_GENRE, None, suggestion("Funk"))
    assert decision.kind is SuggestionDecisionKind.PROPOSED


def test_field_that_forbids_automatic_suggestions_rejects_them() -> None:
    decision = decide_metadata_suggestion(MetadataFieldKey.RATING, None, suggestion(5))
    assert decision.kind is SuggestionDecisionKind.REJECTED


def test_invalid_confidence_and_analysis_version_are_rejected() -> None:
    assert (
        decide_metadata_suggestion(MetadataFieldKey.BPM, None, suggestion(120, 1.1)).kind
        is SuggestionDecisionKind.REJECTED
    )
    invalid_version = MetadataSuggestion(120, MetadataSource.AUDIO_ANALYSIS, 0.9, " ")
    assert (
        decide_metadata_suggestion(MetadataFieldKey.BPM, None, invalid_version).kind
        is SuggestionDecisionKind.REJECTED
    )


def test_decision_objects_are_immutable() -> None:
    decision = MetadataSuggestionDecision(SuggestionDecisionKind.PROPOSED, 120.0, "test")
    with pytest.raises(FrozenInstanceError):
        decision.reason = "changed"  # type: ignore[misc]
