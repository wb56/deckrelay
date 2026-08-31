"""Productive dependency-free FFmpeg tempo and technical-energy backend."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
from time import monotonic
from typing import Any

from party_player.metadata_analysis_contracts import (
    AnalyzedAudioRange,
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisResult,
    MetadataAnalysisSource,
    MetadataFieldSuggestion,
    TempoSegmentDiagnostic,
    TechnicalAudioMetric,
)
from party_player.metadata_analysis_profiles import (
    ALGORITHM_VERSION,
    ConfidenceBand,
    PROFILE_VERSION,
    confidence_band,
)


SAMPLE_RATE = 11_025
ENVELOPE_HZ = 100
FAMILY_MATCH_RELATIVE_TOLERANCE = 0.08
STABILITY_RANGE_SCALE = 0.25
STABILITY_MAD_SCALE = 0.08
USABLE_WINDOW_CONFIDENCE = 0.20
WINDOW_CONFIDENCE_FLOOR = 0.15
WINDOW_CONFIDENCE_FULL_SCALE = 0.45
FAMILY_CONSENSUS_WEIGHT = 0.65
WINDOW_QUALITY_WEIGHT = 0.35


@dataclass(frozen=True, slots=True)
class _SegmentFeatures:
    onset: tuple[float, ...]
    rms_mean: float
    rms_std: float
    peak: float
    transient_density: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_options() -> dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _option(job: MetadataAnalysisJob, name: str, default: str) -> str:
    value = dict(job.technical_options).get(name, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Technische Option {name} ist ungültig")
    return value


def _probe_duration(job: MetadataAnalysisJob, cancellation: object) -> float:
    command = [
        _option(job, "ffprobe", "ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        job.input_snapshot.normalized_path,
    ]
    completed = _communicate(command, cancellation, 30.0)
    payload = json.loads(completed.decode("utf-8"))
    duration = float(payload["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("FFprobe lieferte keine gültige Dauer")
    return duration


def _communicate(command: list[str], cancellation: object, timeout: float) -> bytes:
    process: subprocess.Popen[bytes] = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_process_options()
    )
    deadline = monotonic() + timeout
    try:
        while True:
            if cancellation.is_set():  # type: ignore[attr-defined]
                process.terminate()
                raise InterruptedError("Analyse abgebrochen")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                if process.returncode != 0:
                    raise RuntimeError(stderr.decode(errors="replace")[:500])
                return stdout
            except subprocess.TimeoutExpired:
                if monotonic() >= deadline:
                    process.terminate()
                    raise TimeoutError("FFmpeg-Teiloperation überschritt das Zeitlimit")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def select_ranges(
    duration: float,
    strategy: str,
    *,
    cue_in: float = 0.0,
    cue_out: float | None = None,
    fade_duration: float = 0.0,
) -> tuple[AnalyzedAudioRange, ...]:
    """Select bounded excerpts, always inside the already resolved cue range."""
    if duration <= 0:
        return ()
    end = duration if cue_out is None else min(duration, cue_out)
    start = max(0.0, cue_in)
    usable = end - start
    if usable <= 0:
        return ()
    if usable < 6.0:
        return (AnalyzedAudioRange(start, usable),)
    # A fade is documented in the job. It does not change BPM, but the least
    # stable tail receives less weight by keeping distributed excerpts away
    # from the final part of a long fade where possible.
    stable_end = max(start + 6.0, end - min(max(0.0, fade_duration), usable * 0.25))
    bounded_duration = stable_end - start
    if strategy == "full":
        return (AnalyzedAudioRange(start, usable),)
    if strategy == "middle":
        length = min(90.0, bounded_duration)
        return (AnalyzedAudioRange(start + max(0.0, (bounded_duration - length) / 2), length),)
    if bounded_duration <= 30.0:
        return (AnalyzedAudioRange(start, bounded_duration),)
    long_form = bounded_duration >= 150.0
    sample_count = 5 if long_form else 3
    length = min(18.0 if long_form else 30.0, bounded_duration / sample_count)
    fractions: tuple[float, ...]
    if strategy == "begin_middle_end":
        fractions = (0.03, 0.5, 0.97)
    elif long_form:
        fractions = (0.05, 0.275, 0.5, 0.725, 0.95)
    else:
        fractions = (0.15, 0.5, 0.85)
    starts = tuple(
        start
        + min(
            max(0.0, bounded_duration * fraction - length / 2),
            bounded_duration - length,
        )
        for fraction in fractions
    )
    return tuple(AnalyzedAudioRange(start, length) for start in dict.fromkeys(starts))


def _decode(
    job: MetadataAnalysisJob, region: AnalyzedAudioRange, cancellation: object
) -> array[float]:
    command = [
        _option(job, "ffmpeg", "ffmpeg"),
        "-v",
        "error",
        "-ss",
        format(region.start_seconds, ".6f"),
        "-i",
        job.input_snapshot.normalized_path,
        "-t",
        format(region.duration_seconds, ".6f"),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    samples = array("f")
    samples.frombytes(_communicate(command, cancellation, region.duration_seconds + 30.0))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _features(samples: array[float]) -> _SegmentFeatures:
    window = max(1, SAMPLE_RATE // ENVELOPE_HZ)
    rms = []
    peak = 0.0
    for offset in range(0, len(samples), window):
        block = samples[offset : offset + window]
        if not block:
            continue
        squares = sum(float(value) * float(value) for value in block)
        rms.append(math.sqrt(squares / len(block)))
        peak = max(peak, max(abs(float(value)) for value in block))
    if len(rms) < 2:
        return _SegmentFeatures((), 0.0, 0.0, peak, 0.0)
    onset = [max(0.0, rms[index] - rms[index - 1]) for index in range(1, len(rms))]
    baseline = statistics.median(onset)
    spread = statistics.median(abs(value - baseline) for value in onset) or 1e-9
    threshold = baseline + 3.0 * spread
    threshold = max(threshold, max(onset, default=0.0) * 0.1)
    refractory = max(1, round(ENVELOPE_HZ * 0.12))
    peak_count = 0
    next_allowed = 0
    for index in range(1, len(onset) - 1):
        if (
            index >= next_allowed
            and onset[index] > threshold
            and onset[index] >= onset[index - 1]
            and onset[index] > onset[index + 1]
        ):
            peak_count += 1
            next_allowed = index + refractory
    density = peak_count / (len(onset) / ENVELOPE_HZ)
    mean = statistics.fmean(onset)
    centered = tuple(value - mean for value in onset)
    return _SegmentFeatures(
        centered,
        statistics.fmean(rms),
        statistics.pstdev(rms),
        peak,
        density,
    )


def _tempo(onset: tuple[float, ...]) -> tuple[float, float, float, float]:
    if not onset or max(onset, default=0.0) <= 1e-8:
        return 0.0, 0.0, 0.0, 0.0
    minimum_lag = round(ENVELOPE_HZ * 60 / 300)
    maximum_lag = min(len(onset) // 2, round(ENVELOPE_HZ * 60 / 20))
    energy = sum(value * value for value in onset)
    scores: list[tuple[float, int]] = []
    for lag in range(minimum_lag, maximum_lag + 1):
        numerator = sum(onset[index] * onset[index - lag] for index in range(lag, len(onset)))
        score = max(0.0, numerator / max(energy, 1e-12))
        bpm = 60.0 * ENVELOPE_HZ / lag
        harmonic = score
        double_lag = lag * 2
        if double_lag <= maximum_lag:
            harmonic += 0.35 * max(
                0.0,
                sum(
                    onset[index] * onset[index - double_lag]
                    for index in range(double_lag, len(onset))
                )
                / max(energy, 1e-12),
            )
        if 55.0 <= bpm <= 190.0:
            harmonic *= 1.05
        scores.append((harmonic, lag))
    scores.sort(reverse=True)
    harmonic_quality_score, best_lag = scores[0]
    bpm = 60.0 * ENVELOPE_HZ / best_lag
    alternative = bpm * 2 if bpm * 2 <= 300 else bpm / 2
    runner_up = next((score for score, lag in scores[1:] if abs(lag - best_lag) > 2), 0.0)
    separation = max(0.0, harmonic_quality_score - runner_up) / max(harmonic_quality_score, 1e-9)
    confidence = min(1.0, max(0.0, 0.65 * harmonic_quality_score + 0.35 * separation))
    return bpm, alternative, confidence, harmonic_quality_score


def _combine_tempos(
    estimates: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float]:
    bpm, alternative, confidence, stability, _components = _combine_tempos_detailed(estimates)
    return bpm, alternative, confidence, stability


def _calibrated_window_confidence(value: float) -> float:
    """Map conservative local ACF confidence onto aggregate-quality evidence."""
    scale = WINDOW_CONFIDENCE_FULL_SCALE - WINDOW_CONFIDENCE_FLOOR
    return min(1.0, max(0.0, (value - WINDOW_CONFIDENCE_FLOOR) / scale))


def _combine_tempos_detailed(
    estimates: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float, tuple[tuple[str, float | int], ...]]:
    valid = tuple(item for item in estimates if item[0] > 0.0)
    if not valid:
        return (
            0.0,
            0.0,
            0.0,
            0.0,
            (
                ("valid_window_count", 0),
                ("usable_window_count", 0),
                ("family_consensus", 0.0),
                ("robust_window_confidence", 0.0),
                ("calibrated_window_quality", 0.0),
                ("family_contribution", 0.0),
                ("quality_contribution", 0.0),
            ),
        )
    # Autocorrelation commonly locks onto half- or double-tempo pulses.
    # Compare only those harmonically related interpretations across all
    # excerpts before deciding that the excerpts disagree.
    # Only half/double interpretations form one tempo family. Third-pulse
    # transforms can incorrectly merge unrelated families such as 84 and
    # 112 BPM and therefore deliberately remain separate evidence.
    transforms = ((0.5, 0.90), (1.0, 1.0), (2.0, 0.90))
    candidates = tuple(
        (item[0] * factor, max(item[2], 0.05) * quality, index)
        for index, item in enumerate(valid)
        for factor, quality in transforms
        if 70.0 <= item[0] * factor <= 180.0
    )
    if not candidates:
        bpm = statistics.median(item[0] for item in valid)
        weighted_agreement = 0.0
        coverage = 0.0
        stability = 0.0
    else:
        clusters: list[tuple[float, float, tuple[tuple[float, float, int], ...]]] = []
        for anchor, _weight, _index in candidates:
            members = []
            for index in range(len(valid)):
                compatible = tuple(
                    (tempo, weight, source_index)
                    for tempo, weight, source_index in candidates
                    if source_index == index
                    and abs(tempo - anchor) / anchor <= FAMILY_MATCH_RELATIVE_TOLERANCE
                )
                if compatible:
                    members.append(max(compatible, key=lambda item: item[1]))
            score = sum(weight for _tempo, weight, _index in members)
            center = sum(tempo * weight for tempo, weight, _index in members) / max(score, 1e-9)
            # Prefer the usual dance/pop pulse range only as a tie-breaker;
            # excerpt agreement remains the primary evidence.
            range_quality = 1.0 if 75.0 <= center <= 140.0 else 0.92
            clusters.append((score * range_quality, center, tuple(members)))
        _score, bpm, selected_members = max(clusters, key=lambda item: item[0])
        matched_weight = sum(weight for _tempo, weight, _index in selected_members)
        total_weight = sum(max(item[2], 0.05) for item in valid)
        weighted_agreement = min(1.0, matched_weight / max(total_weight, 1e-9))
        normalized = tuple(tempo for tempo, _weight, _index in selected_members)
        coverage = len({index for _tempo, _weight, index in selected_members}) / len(valid)
        center = statistics.median(normalized)
        relative_range = (
            (max(normalized) - min(normalized)) / max(center, 1e-9) if len(normalized) > 1 else 0.0
        )
        relative_mad = statistics.median(abs(value - center) for value in normalized) / max(
            center, 1e-9
        )
        range_quality = max(0.0, 1.0 - relative_range / STABILITY_RANGE_SCALE)
        deviation_quality = max(0.0, 1.0 - relative_mad / STABILITY_MAD_SCALE)
        stability = coverage * (0.6 * range_quality + 0.4 * deviation_quality)
    alternative = bpm * 2 if bpm * 2 <= 300 else bpm / 2
    raw_window_confidences = tuple(item[2] for item in valid)
    robust_window_confidence = statistics.median(raw_window_confidences)
    calibrated_window_quality = statistics.median(
        _calibrated_window_confidence(value) for value in raw_window_confidences
    )
    usable_window_count = sum(value >= USABLE_WINDOW_CONFIDENCE for value in raw_window_confidences)
    family_consensus = 0.5 * weighted_agreement + 0.5 * coverage
    family_contribution = FAMILY_CONSENSUS_WEIGHT * family_consensus
    quality_contribution = WINDOW_QUALITY_WEIGHT * calibrated_window_quality
    confidence = min(1.0, family_contribution + quality_contribution)
    components: tuple[tuple[str, float | int], ...] = (
        ("valid_window_count", len(valid)),
        ("usable_window_count", usable_window_count),
        ("family_coverage", coverage),
        ("weighted_family_agreement", weighted_agreement),
        ("family_consensus", family_consensus),
        ("robust_window_confidence", robust_window_confidence),
        ("calibrated_window_quality", calibrated_window_quality),
        ("family_contribution", family_contribution),
        ("quality_contribution", quality_contribution),
    )
    return bpm, alternative, confidence, stability, components


class FfmpegTempoAnalysisBackend:
    """Analyze bounded mono PCM with onset-envelope autocorrelation."""

    def analyze(self, job: MetadataAnalysisJob, cancellation: object) -> MetadataAnalysisResult:
        started_at = _now()
        try:
            if Path(job.input_snapshot.normalized_path).suffix.lower() not in {
                ".mp3",
                ".flac",
            }:
                return self._failure(
                    job,
                    started_at,
                    MetadataAnalysisOutcome.UNSUPPORTED_FORMAT,
                    "UNSUPPORTED_FORMAT",
                    "Produktive Tempoanalyse unterstützt MP3 und FLAC.",
                )
            duration = _probe_duration(job, cancellation)
            strategy = _option(job, "segment_strategy", "distributed")
            resolved = job.analysis_range
            ranges = select_ranges(
                duration,
                strategy,
                cue_in=resolved.cue_in if resolved is not None else 0.0,
                cue_out=resolved.cue_out if resolved is not None else None,
                fade_duration=resolved.fade_duration if resolved is not None else 0.0,
            )
            if not ranges:
                raise ValueError("Der aufgelöste Cue-Bereich ist ungültig")
            if resolved is not None and resolved.cue_out - resolved.cue_in < 6.0:
                return MetadataAnalysisResult(
                    job.job_id,
                    job.run_id,
                    job.track_id,
                    job.input_snapshot,
                    job.analysis_profile,
                    job.analysis_version,
                    started_at,
                    _now(),
                    MetadataAnalysisOutcome.SUCCESS,
                    analyzed_ranges=ranges,
                    warnings=("Cue-Bereich ist für eine zuverlässige Tempoanalyse zu kurz.",),
                    backend_name="ffmpeg-onset-autocorrelation",
                    backend_version=ALGORITHM_VERSION,
                    scope=job.scope,
                    analysis_range=job.analysis_range,
                    range_signature=job.range_signature,
                )
            features = tuple(_features(_decode(job, region, cancellation)) for region in ranges)
            estimates = tuple(_tempo(feature.onset) for feature in features)
            bpm, alternative, confidence, stability, confidence_components = (
                _combine_tempos_detailed(estimates)
            )
            segment_diagnostics = tuple(
                TempoSegmentDiagnostic(
                    index,
                    region.start_seconds,
                    region.start_seconds + region.duration_seconds,
                    estimate[0],
                    estimate[1],
                    estimate[2],
                    estimate[3],
                )
                for index, (region, estimate) in enumerate(zip(ranges, estimates, strict=True))
            )
            parameters = (
                ("segment_strategy", strategy),
                ("profile_version", PROFILE_VERSION),
                ("sample_rate", SAMPLE_RATE),
                ("envelope_hz", ENVELOPE_HZ),
                ("minimum_confidence", 0.55),
                ("high_confidence", 0.80),
                ("tempo_change_stability", 0.65),
                ("family_match_relative_tolerance", FAMILY_MATCH_RELATIVE_TOLERANCE),
                ("stability_range_scale", STABILITY_RANGE_SCALE),
                ("stability_mad_scale", STABILITY_MAD_SCALE),
                ("usable_window_confidence", USABLE_WINDOW_CONFIDENCE),
                ("window_confidence_floor", WINDOW_CONFIDENCE_FLOOR),
                ("window_confidence_full_scale", WINDOW_CONFIDENCE_FULL_SCALE),
                ("family_consensus_weight", FAMILY_CONSENSUS_WEIGHT),
                ("window_quality_weight", WINDOW_QUALITY_WEIGHT),
                ("duration_source", "ffprobe_clamped_to_canonical_range"),
            )
            if bpm == 0.0:
                return MetadataAnalysisResult(
                    job.job_id,
                    job.run_id,
                    job.track_id,
                    job.input_snapshot,
                    job.analysis_profile,
                    job.analysis_version,
                    started_at,
                    _now(),
                    MetadataAnalysisOutcome.SUCCESS,
                    analyzed_ranges=ranges,
                    warnings=("Kein belastbarer Rhythmus erkannt; kein BPM-Vorschlag.",),
                    backend_name="ffmpeg-onset-autocorrelation",
                    backend_version=ALGORITHM_VERSION,
                    scope=job.scope,
                    analysis_range=job.analysis_range,
                    range_signature=job.range_signature,
                    probed_duration_seconds=duration,
                    segment_diagnostics=segment_diagnostics,
                    decision_reasons=("NO_RHYTHM",),
                    effective_parameters=parameters,
                    aggregated_bpm=None,
                    aggregated_alternative_bpm=None,
                    aggregated_confidence=0.0,
                    confidence_components=confidence_components,
                )
            rms_mean = statistics.fmean(feature.rms_mean for feature in features)
            rms_std = statistics.fmean(feature.rms_std for feature in features)
            peak = max(feature.peak for feature in features)
            transient_density = statistics.fmean(feature.transient_density for feature in features)
            crest = peak / max(rms_mean, 1e-9)
            experimental_energy = min(
                1.0,
                max(
                    0.0,
                    0.45 * min(rms_mean / 0.25, 1.0)
                    + 0.35 * min(transient_density / 5.0, 1.0)
                    + 0.2 * min(rms_std / 0.15, 1.0),
                ),
            )
            warnings = ["Halb-/Doppeltempo-Alternative wird separat ausgewiesen."]
            if stability < 0.65:
                warnings.append(
                    "Die verteilten Ausschnitte zeigen ein wechselndes oder instabiles Tempo."
                )
            band = confidence_band(confidence)
            suggestions = (
                ()
                if band is ConfidenceBand.LOW
                else (
                    MetadataFieldSuggestion(
                        "bpm",
                        round(bpm, 2),
                        MetadataAnalysisSource.AUDIO_ANALYSIS,
                        confidence,
                    ),
                    MetadataFieldSuggestion(
                        "alternative_bpm",
                        round(alternative, 2),
                        MetadataAnalysisSource.AUDIO_ANALYSIS,
                        confidence * 0.8,
                    ),
                    *(
                        (
                            MetadataFieldSuggestion(
                                "energy_experimental",
                                round(experimental_energy * 100),
                                MetadataAnalysisSource.AUDIO_ANALYSIS,
                                confidence * 0.7,
                            ),
                        )
                        if "ENERGY" in {kind.value for kind in job.requested_kinds}
                        else ()
                    ),
                )
            )
            if band is ConfidenceBand.LOW:
                warnings.append("Konfidenz unter 0,55; kein regulärer BPM-Vorschlag erzeugt.")
            elif band is ConfidenceBand.MEDIUM:
                warnings.append("Mittlere Konfidenz; fachliche Prüfung erforderlich.")
            return MetadataAnalysisResult(
                job.job_id,
                job.run_id,
                job.track_id,
                job.input_snapshot,
                job.analysis_profile,
                job.analysis_version,
                started_at,
                _now(),
                MetadataAnalysisOutcome.SUCCESS,
                suggestions=suggestions,
                analyzed_ranges=ranges,
                technical_metrics=(
                    TechnicalAudioMetric("rms_mean", rms_mean, "linear"),
                    TechnicalAudioMetric("rms_variability", rms_std, "linear"),
                    TechnicalAudioMetric("peak", peak, "linear"),
                    TechnicalAudioMetric("crest_factor", crest, "ratio"),
                    TechnicalAudioMetric("transient_density", transient_density, "events/s"),
                    TechnicalAudioMetric("bpm", bpm, "BPM"),
                    TechnicalAudioMetric(
                        "energy_experimental", experimental_energy * 100, "percent"
                    ),
                ),
                rhythm_stability=min(1.0, stability),
                warnings=tuple(warnings),
                backend_name="ffmpeg-onset-autocorrelation",
                backend_version=ALGORITHM_VERSION,
                scope=job.scope,
                analysis_range=job.analysis_range,
                range_signature=job.range_signature,
                probed_duration_seconds=duration,
                segment_diagnostics=segment_diagnostics,
                decision_reasons=tuple(
                    reason
                    for condition, reason in (
                        (band is ConfidenceBand.HIGH, "HIGH_CONFIDENCE"),
                        (band is ConfidenceBand.MEDIUM, "REVIEW_REQUIRED"),
                        (band is ConfidenceBand.LOW, "BELOW_MINIMUM_CONFIDENCE"),
                        (stability < 0.65, "DIFFERENT_TEMPO_FAMILIES"),
                    )
                    if condition
                ),
                effective_parameters=parameters,
                aggregated_bpm=bpm,
                aggregated_alternative_bpm=alternative,
                aggregated_confidence=confidence,
                confidence_components=confidence_components,
            )
        except InterruptedError:
            return self._failure(
                job,
                started_at,
                MetadataAnalysisOutcome.CANCELLED,
                "CANCELLED",
                "Analyse wurde abgebrochen.",
            )
        except FileNotFoundError:
            return self._failure(
                job,
                started_at,
                MetadataAnalysisOutcome.BACKEND_UNAVAILABLE,
                "BACKEND_NOT_FOUND",
                "FFmpeg oder FFprobe ist nicht verfügbar.",
            )
        except TimeoutError as exc:
            return self._failure(
                job, started_at, MetadataAnalysisOutcome.TIMEOUT, "TIMEOUT", str(exc)
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            safe_text = str(exc).replace(job.input_snapshot.normalized_path, "<Eingabedatei>")
            return self._failure(
                job,
                started_at,
                MetadataAnalysisOutcome.ANALYSIS_ERROR,
                "ANALYSIS_ERROR",
                safe_text[:500],
            )

    @staticmethod
    def _failure(
        job: MetadataAnalysisJob,
        started_at: str,
        outcome: MetadataAnalysisOutcome,
        code: str,
        text: str,
        ranges: tuple[AnalyzedAudioRange, ...] = (),
    ) -> MetadataAnalysisResult:
        return MetadataAnalysisResult(
            job.job_id,
            job.run_id,
            job.track_id,
            job.input_snapshot,
            job.analysis_profile,
            job.analysis_version,
            started_at,
            _now(),
            outcome,
            analyzed_ranges=ranges,
            error_code=code,
            error_text=text,
            backend_name="ffmpeg-onset-autocorrelation",
            backend_version=ALGORITHM_VERSION,
            scope=job.scope,
            analysis_range=job.analysis_range,
            range_signature=job.range_signature,
        )
