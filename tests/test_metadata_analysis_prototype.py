from array import array
from datetime import datetime, timezone
import math
from pathlib import Path
import subprocess
from time import monotonic, sleep
import wave

import pytest

from party_player.metadata_analysis_contracts import (
    AnalyzedAudioRange,
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisKind,
    MetadataAnalysisOutcome,
    TempoSegmentDiagnostic,
    TempoAnalysisRangeSnapshot,
    TempoAnalysisScope,
)
from party_player.metadata_tempo_backend import (
    _combine_tempos,
    _combine_tempos_detailed,
    _features,
    _tempo,
    select_ranges,
)
from party_player.metadata_analysis_supervisor import MetadataAnalysisProcessSupervisor


FFMPEG_BIN = Path(".tools/ffmpeg/ffmpeg-8.1.2-essentials_build/bin")
FFMPEG = FFMPEG_BIN / "ffmpeg.exe"
FFPROBE = FFMPEG_BIN / "ffprobe.exe"


def click_samples(bpm: float, seconds: float = 45.0, sample_rate: int = 11_025) -> array:
    samples = array("f", [0.0]) * round(seconds * sample_rate)
    interval = sample_rate * 60.0 / bpm
    for beat in range(round(seconds * bpm / 60.0)):
        offset = round(beat * interval)
        for index in range(min(120, len(samples) - offset)):
            samples[offset + index] += 0.8 * math.exp(-index / 25.0)
    return samples


@pytest.mark.parametrize("reference", [60.0, 100.0, 140.0, 200.0])
def test_onset_autocorrelation_detects_synthetic_click_tempo(reference: float) -> None:
    bpm, alternative, confidence, _stability = _tempo(_features(click_samples(reference)).onset)
    assert min(abs(bpm - reference), abs(alternative - reference)) <= 2.0
    assert 20.0 <= bpm <= 300.0
    assert 20.0 <= alternative <= 300.0
    assert confidence > 0.1


def test_silence_has_no_tempo() -> None:
    assert _tempo(_features(array("f", [0.0]) * 20_000).onset)[0] == 0.0


def test_distributed_tempo_change_uses_median_and_reduces_stability() -> None:
    estimates = tuple(
        _tempo(_features(click_samples(reference, 30.0)).onset)
        for reference in (100.0, 120.0, 150.0)
    )
    bpm, alternative, confidence, stability = _combine_tempos(estimates)
    assert min(abs(bpm - 120.0), abs(alternative - 120.0)) <= 2.0
    assert 0.0 < confidence < 0.9
    assert stability < 0.65


@pytest.mark.parametrize(
    ("estimates", "reference"),
    [
        (
            (
                (98.36, 196.72, 0.19, 0.26),
                (49.59, 99.17, 0.11, 0.15),
                (98.36, 196.72, 0.09, 0.12),
            ),
            98.5,
        ),
        (
            (
                (46.88, 93.75, 0.64, 0.75),
                (46.88, 93.75, 0.59, 0.65),
                (46.88, 93.75, 0.45, 0.52),
            ),
            93.75,
        ),
    ],
)
def test_harmonic_segment_pulses_are_combined_as_one_tempo(estimates, reference) -> None:
    bpm, _alternative, confidence, stability = _combine_tempos(estimates)
    assert bpm == pytest.approx(reference, abs=2.0)
    assert confidence >= 0.55
    assert stability >= 0.55


def test_unrelated_84_and_112_bpm_families_are_not_merged_as_harmonics() -> None:
    estimates = (
        (84.0, 168.0, 0.9, 0.9),
        (112.0, 224.0, 0.9, 0.9),
        (84.2, 168.4, 0.9, 0.9),
        (111.8, 223.6, 0.9, 0.9),
        (84.1, 168.2, 0.9, 0.9),
    )

    _bpm, _alternative, confidence, stability = _combine_tempos(estimates)

    assert confidence < 0.8
    assert stability < 0.65


def test_confidence_and_stability_are_independent_for_natural_family_spread() -> None:
    estimates = tuple(
        (bpm, bpm * 2, confidence, 0.90)
        for bpm, confidence in zip(
            (89.55, 90.91, 92.31, 96.77, 92.31),
            (0.29, 0.33, 0.38, 0.44, 0.36),
            strict=True,
        )
    )

    bpm, _alternative, confidence, stability, components = _combine_tempos_detailed(estimates)

    assert bpm == pytest.approx(92.7, abs=0.5)
    assert confidence >= 0.80
    assert stability >= 0.65
    assert confidence != stability
    assert dict(components)["usable_window_count"] == 5
    assert dict(components)["family_consensus"] == 1.0


def test_constant_electronic_half_time_windows_remain_high_confidence() -> None:
    estimates = tuple((60.606, 121.212, 0.70, 1.0) for _ in range(5))

    bpm, alternative, confidence, stability = _combine_tempos(estimates)

    assert min(abs(bpm - 121.212), abs(alternative - 121.212)) < 0.01
    assert confidence >= 0.80
    assert stability >= 0.99


def test_bohemian_like_half_double_windows_are_temporally_unstable() -> None:
    estimates = tuple(
        (bpm, bpm * 2 if bpm * 2 <= 300 else bpm / 2, 0.70, 0.90)
        for bpm in (300.0, 139.0, 70.6, 68.2, 80.0)
    )

    _bpm, _alternative, confidence, stability = _combine_tempos(estimates)

    assert confidence >= 0.55
    assert stability < 0.65


def test_harmonic_quality_score_is_explicitly_unbounded() -> None:
    diagnostic = TempoSegmentDiagnostic(0, 0.0, 18.0, 121.2, 242.4, 0.9, 1.05128)

    assert diagnostic.harmonic_quality_score > 1.0


def test_half_and_double_pulses_in_normal_bpm_range_remain_one_family() -> None:
    estimates = (
        (80.0, 160.0, 0.8, 0.9),
        (160.0, 80.0, 0.8, 0.9),
        (80.5, 161.0, 0.8, 0.9),
        (159.5, 79.75, 0.8, 0.9),
        (80.2, 160.4, 0.8, 0.9),
    )

    bpm, alternative, confidence, stability = _combine_tempos(estimates)

    assert min(abs(bpm - 80.0), abs(alternative - 80.0)) < 1.0
    assert confidence >= 0.8
    assert stability >= 0.65


def test_weak_formal_consensus_is_not_promoted_to_high_confidence() -> None:
    estimates = tuple((92.0, 184.0, 0.05, 0.08) for _ in range(5))

    _bpm, _alternative, confidence, stability, components = _combine_tempos_detailed(estimates)

    assert stability >= 0.65
    assert confidence < 0.80
    assert dict(components)["usable_window_count"] == 0
    assert dict(components)["quality_contribution"] == 0.0


def test_one_good_window_cannot_dominate_weak_or_conflicting_windows() -> None:
    estimates = (
        (92.0, 184.0, 0.90, 0.90),
        (92.0, 184.0, 0.05, 0.08),
        (92.0, 184.0, 0.05, 0.08),
        (112.0, 224.0, 0.05, 0.08),
        (56.0, 112.0, 0.05, 0.08),
    )

    _bpm, _alternative, confidence, stability, components = _combine_tempos_detailed(estimates)

    assert confidence < 0.80
    assert stability < 0.65
    assert dict(components)["usable_window_count"] == 1


def test_range_strategies_are_bounded() -> None:
    assert select_ranges(20.0, "distributed") == select_ranges(20.0, "full")
    assert len(select_ranges(240.0, "middle")) == 1
    assert len(select_ranges(240.0, "distributed")) == 5
    assert len(select_ranges(240.0, "begin_middle_end")) == 3


def test_identical_canonical_inputs_produce_identical_ordered_windows() -> None:
    first = select_ranges(237.16, "distributed", cue_in=0.0, cue_out=237.16)
    second = select_ranges(237.16, "distributed", cue_in=0.0, cue_out=237.16)

    assert first == second
    assert tuple(item.start_seconds for item in first) == tuple(
        sorted(item.start_seconds for item in first)
    )


def test_cue_bounded_ranges_never_include_intro_or_content_after_cue_out() -> None:
    ranges = select_ranges(300.0, "distributed", cue_in=60.0, cue_out=180.0, fade_duration=12.0)
    assert len(ranges) == 3
    assert all(region.start_seconds >= 60.0 for region in ranges)
    assert all(region.start_seconds + region.duration_seconds <= 180.0 for region in ranges)
    assert max(region.start_seconds + region.duration_seconds for region in ranges) <= 168.0


def test_short_cue_range_is_kept_bounded_for_unreliable_result() -> None:
    assert select_ranges(180.0, "distributed", cue_in=50.0, cue_out=54.0) == (
        AnalyzedAudioRange(50.0, 4.0),
    )


def write_wave(path: Path, samples: array, sample_rate: int = 11_025) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(
            b"".join(
                round(max(-1.0, min(1.0, value)) * 32767).to_bytes(2, "little", signed=True)
                for value in samples
            )
        )


def product_job(path: Path, suffix: str = "") -> MetadataAnalysisJob:
    return MetadataAnalysisJob(
        f"product{suffix}",
        1,
        1,
        FileSnapshot.capture(str(path)),
        "tempo",
        "ffmpeg-onset-acf-v0.1",
        (MetadataAnalysisKind.BPM, MetadataAnalysisKind.ENERGY),
        0,
        30.0,
        datetime.now(timezone.utc).isoformat(),
        MetadataAnalysisBackendKind.FFMPEG_TEMPO,
        (
            ("ffmpeg", str(FFMPEG.resolve())),
            ("ffprobe", str(FFPROBE.resolve())),
            ("segment_strategy", "middle"),
        ),
        TempoAnalysisScope.TRACK_DEFAULT_CUES,
        TempoAnalysisRangeSnapshot(
            2.0,
            18.0,
            2.0,
            20.0,
            datetime.now(timezone.utc).isoformat(),
            "test-cue-revision",
        ),
        "a" * 64,
    )


def await_result(supervisor: MetadataAnalysisProcessSupervisor):
    deadline = monotonic() + 30.0
    while monotonic() < deadline:
        result = supervisor.poll()
        if result is not None:
            return result
        sleep(0.01)
    raise AssertionError("Kein Analyseergebnis")


@pytest.mark.skipif(not FFMPEG.is_file() or not FFPROBE.is_file(), reason="lokales FFmpeg fehlt")
@pytest.mark.parametrize(
    ("suffix", "codec"),
    [
        (".mp3", ("-codec:a", "libmp3lame", "-b:a", "128k")),
        (".flac", ("-codec:a", "flac")),
        (".vbr.mp3", ("-codec:a", "libmp3lame", "-q:a", "4")),
    ],
)
def test_real_formats_run_in_spawn_process(
    tmp_path: Path, suffix: str, codec: tuple[str, ...]
) -> None:
    source = tmp_path / "source.wav"
    encoded = tmp_path / f"tempo{suffix}"
    write_wave(source, click_samples(120.0, 20.0))
    subprocess.run(
        [str(FFMPEG), "-v", "error", "-y", "-i", str(source), *codec, str(encoded)],
        check=True,
        timeout=30,
    )
    original_bytes = encoded.read_bytes()
    original_modified_ns = encoded.stat().st_mtime_ns
    supervisor = MetadataAnalysisProcessSupervisor()
    try:
        supervisor.submit(product_job(encoded, suffix))
        result = await_result(supervisor)
        assert result.outcome is MetadataAnalysisOutcome.SUCCESS
        bpm = float(result.suggestions[0].canonical_value)
        alternative = float(result.suggestions[1].canonical_value)
        assert min(abs(bpm - 120.0), abs(alternative - 120.0)) <= 2.0
        assert result.technical_metrics
        assert result.analyzed_ranges
        assert all(region.start_seconds >= 2.0 for region in result.analyzed_ranges)
        assert all(
            region.start_seconds + region.duration_seconds <= 18.0
            for region in result.analyzed_ranges
        )
        assert result.scope is TempoAnalysisScope.TRACK_DEFAULT_CUES
        assert encoded.read_bytes() == original_bytes
        assert encoded.stat().st_mtime_ns == original_modified_ns
        assert not tuple(tmp_path.glob("*.pcm"))
    finally:
        supervisor.close()
