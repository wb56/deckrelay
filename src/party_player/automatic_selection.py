"""Deterministic, history-aware automatic catalog selection."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
import random
import logging
import inspect
from threading import Lock
from typing import Protocol
import uuid

from party_player.database.connection import Database
from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry, Track
from party_player.repositories.track_repository import TrackRepository
from party_player.track_selection import TrackSelectionService
from party_player.emergency_playlist import LocalEmergencyPlaylistService
from party_player.selection_decision import (
    CandidateEvaluation,
    CandidateDecisionCategory,
    CandidateDecisionReason,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    ExecutableSelectionRule,
    ExclusionReasonSummary,
    SelectionCandidate,
    SelectionContext,
    SelectionOutcome,
    SelectionRationale,
    SelectionRuleInput,
    hard_rule_evaluation,
)
from party_player.selection_scoring import (
    CandidateScorer,
    PlayCountScoringRule,
    RatingScoringRule,
)
from party_player.selection_preview import (
    SelectionPreview,
    SelectionPreviewCompletion,
    SelectionPreviewStep,
)
from party_player.selection_decision import attach_source_resolution
from party_player.selection_source import SelectionSourceResolver, SourceResolutionReason
from party_player.selection_rule_settings import (
    DEFAULT_SELECTION_SCORING_SETTINGS,
    SelectionScoringSettings,
    SelectionRuleSettingsRepository,
)
from party_player.selection_metadata import (
    SelectionMetadataCatalogSnapshot,
    neutral_catalog_snapshot,
)
from party_player.selection_sequence import SelectionSequenceContext
from party_player.selection_continuity import (
    BpmContinuityRule,
    ContinuityRuleSetting,
    EnergyContinuityRule,
    GenreDiversityRule,
    MoodContinuityRule,
    SelectionContinuitySettings,
)
from party_player.enums import EmptyQueuePolicy


@dataclass(slots=True)
class _PreviewHistory:
    counts: dict[int, int]
    recent_ids: list[int]

    def play_counts(self) -> dict[int, int]:
        return dict(self.counts)

    def recent_track_ids(self, limit: int) -> set[int]:
        return set(self.recent_ids[: max(0, limit)])

    def record_played(self, track: Track) -> None:
        self.counts[track.id] = self.counts.get(track.id, 0) + 1
        self.recent_ids.insert(0, track.id)

    def preview_snapshot(self) -> "_PreviewHistory":
        return _PreviewHistory(dict(self.counts), list(self.recent_ids))


@dataclass(frozen=True, slots=True)
class _PreviewTracks:
    candidates: tuple[Track, ...]

    def automatic_candidates(self) -> list[Track]:
        return list(self.candidates)


@dataclass(frozen=True, slots=True)
class SelectionSimulationStep:
    position: int
    previous_track_id: int | None
    track: Track
    rationale: SelectionRationale


@dataclass(frozen=True, slots=True)
class SelectionSimulation:
    requested_depth: int
    steps: tuple[SelectionSimulationStep, ...]
    settings: SelectionScoringSettings
    context_code: str
    completion_rationale: SelectionRationale | None = None


@dataclass(slots=True)
class _PreparedSimulation:
    rules: TrackSelectionService
    settings: SelectionScoringSettings
    history: _PreviewHistory
    previous_track_id: int | None
    candidates: tuple[Track, ...]
    metadata: SelectionMetadataCatalogSnapshot


class _AutomaticTrackSource(Protocol):
    def automatic_candidates(self) -> list[Track]: ...


class _AutomaticHistorySource(Protocol):
    def play_counts(self) -> dict[int, int]: ...

    def recent_track_ids(self, limit: int) -> set[int]: ...

    def preview_snapshot(self) -> _PreviewHistory: ...


class AutomaticSelectionHistory:
    def __init__(self, database: Database) -> None:
        self._database = database

    def play_counts(self) -> dict[int, int]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT track_id, COUNT(*) AS total
                   FROM play_history
                   WHERE completion_status = 'PLAYED'
                   GROUP BY track_id"""
            ).fetchall()
        return {int(row["track_id"]): int(row["total"]) for row in rows}

    def recent_track_ids(self, limit: int) -> set[int]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT track_id FROM play_history
                   WHERE completion_status = 'PLAYED'
                   ORDER BY finished_at DESC, id DESC LIMIT ?""",
                (max(0, limit),),
            ).fetchall()
        return {int(row["track_id"]) for row in rows}

    def preview_snapshot(self) -> _PreviewHistory:
        """Load ordered history once for an isolated multi-step simulation."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT track_id FROM play_history
                   WHERE completion_status = 'PLAYED'
                   ORDER BY finished_at DESC, id DESC"""
            ).fetchall()
        recent_ids = [int(row["track_id"]) for row in rows]
        counts: dict[int, int] = {}
        for track_id in recent_ids:
            counts[track_id] = counts.get(track_id, 0) + 1
        return _PreviewHistory(counts, recent_ids)


class AutomaticRecentTrackRule:
    rule_id = "selection.automatic_recent_track"
    rule_version = 1
    rule_kind = RuleKind.HARD_EXCLUSION
    relaxable_reason_codes = frozenset({"RECENT_TRACK"})

    def __init__(self, recent_track_ids: set[int]) -> None:
        self._recent_track_ids = recent_track_ids

    def evaluate_rule(
        self,
        rule_input: SelectionRuleInput,
        context: SelectionContext,
    ) -> RuleEvaluation:
        recent = rule_input.candidate.track_id in self._recent_track_ids
        return hard_rule_evaluation(
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            context=context,
            reason_code="RECENT_TRACK" if recent else "RECENT_TRACK_ALLOWED",
            reason=(
                "Titel gehört zu den zuletzt gespielten Titeln"
                if recent
                else "Titel gehört nicht zu den zuletzt gespielten Titeln"
            ),
            excluded=recent,
            relaxable_reason_codes=self.relaxable_reason_codes,
        )


class AutomaticSelectionService:
    _RATIONALE_CANDIDATE_LIMIT = 50
    MAX_PREVIEW_DEPTH = 10

    def __init__(
        self,
        tracks: TrackRepository | _AutomaticTrackSource,
        history: AutomaticSelectionHistory | _AutomaticHistorySource,
        *,
        recent_track_limit: int = 25,
        randomizer: random.Random | None = None,
        emergency_playlist: LocalEmergencyPlaylistService | None = None,
        rule_settings: SelectionRuleSettingsRepository | None = None,
        continuity_settings: SelectionContinuitySettings | None = None,
    ) -> None:
        self._tracks = tracks
        self._history = history
        self.recent_track_limit = max(0, recent_track_limit)
        self._random = randomizer or random.Random()
        self._emergency_playlist = emergency_playlist
        self._rule_settings = rule_settings
        self._continuity_settings = continuity_settings
        self.last_relaxation_stage = "NONE"
        self.last_rationale: SelectionRationale | None = None
        self._selection_lock = Lock()
        self._logger = logging.getLogger(__name__)

    def select(self, rules: TrackSelectionService) -> Track | None:
        def select_once() -> Track | None:
            settings = self._load_rule_settings()
            history = self._history.preview_snapshot()
            previous_track_id = self._previous_track_id(history)
            candidates, metadata = self._load_catalog_snapshot(previous_track_id)
            sequence = SelectionSequenceContext(
                previous_track_id,
                metadata.for_track(previous_track_id),
                0,
            )
            return self._select(rules, settings, candidates, metadata, history, sequence)

        with self._selection_lock:
            return self._run_isolated(select_once)

    def preview(self, rules: TrackSelectionService, count: int) -> SelectionPreview:
        """Predict automatic choices without advancing any productive state."""
        if not 1 <= count <= self.MAX_PREVIEW_DEPTH:
            raise ValueError(f"Vorschautiefe muss zwischen 1 und {self.MAX_PREVIEW_DEPTH} liegen")
        with self._selection_lock:
            preview_random = random.Random()
            preview_random.setstate(self._random.getstate())
            prepared = self._prepare_simulation(rules)
        simulation = self._simulate_prepared(
            prepared, count, preview_random, context_code="AUTOMATIC_PREVIEW"
        )
        steps = tuple(
            SelectionPreviewStep(
                step.position,
                step.track.id,
                step.track.title,
                step.track.artist,
                step.rationale.relaxation_stage,
                step.rationale,
            )
            for step in simulation.steps
        )
        completion = (
            SelectionPreviewCompletion.REQUESTED_DEPTH_REACHED
            if len(steps) == count
            else SelectionPreviewCompletion.NO_SAFE_CANDIDATE
        )
        return SelectionPreview(
            preview_id=uuid.uuid4().hex,
            created_at=datetime.now(),
            requested_depth=count,
            achieved_depth=len(steps),
            steps=steps,
            completion_reason=completion,
            completion_rationale=simulation.completion_rationale,
        )

    def simulate_sequence(
        self,
        rules: TrackSelectionService,
        count: int,
        *,
        randomizer: random.Random,
        context_code: str = "AUTOMATIC_PLANNING",
    ) -> SelectionSimulation:
        """Simulate one sequence with an isolated caller-owned RNG."""
        if not 1 <= count <= self.MAX_PREVIEW_DEPTH:
            raise ValueError(f"Vorschautiefe muss zwischen 1 und {self.MAX_PREVIEW_DEPTH} liegen")
        with self._selection_lock:
            prepared = self._prepare_simulation(rules)
        return self._simulate_prepared(prepared, count, randomizer, context_code=context_code)

    def _prepare_simulation(self, rules: TrackSelectionService) -> _PreparedSimulation:
        settings = self._load_rule_settings()
        history = self._history.preview_snapshot()
        previous_track_id = self._previous_track_id(history)
        candidates, metadata = self._load_catalog_snapshot(previous_track_id)
        return _PreparedSimulation(
            rules.copy_for_preview(),
            settings,
            history,
            previous_track_id,
            candidates,
            metadata,
        )

    def _simulate_prepared(
        self,
        prepared: _PreparedSimulation,
        count: int,
        randomizer: random.Random,
        *,
        context_code: str,
    ) -> SelectionSimulation:
        previous_track_id = prepared.previous_track_id
        simulation_selector = AutomaticSelectionService(
            _PreviewTracks(prepared.candidates),
            prepared.history,
            recent_track_limit=self.recent_track_limit,
            randomizer=randomizer,
            emergency_playlist=None,
            continuity_settings=self._continuity_settings,
        )
        steps: list[SelectionSimulationStep] = []
        completion_rationale: SelectionRationale | None = None
        for position in range(1, count + 1):
            step_previous_track_id = previous_track_id
            sequence = SelectionSequenceContext(
                previous_track_id,
                prepared.metadata.for_track(previous_track_id),
                position,
            )
            selected = simulation_selector._run_isolated(
                lambda: simulation_selector._select(
                    prepared.rules,
                    prepared.settings,
                    prepared.candidates,
                    prepared.metadata,
                    prepared.history,
                    sequence,
                )
            )
            rationale = simulation_selector.last_rationale
            if selected is None or rationale is None:
                completion_rationale = rationale
                break
            source_entry = QueueEntry(
                -selected.id,
                selected.id,
                0,
                QueueStatus.WAITING,
                source=QueueSource.AUTOMATIC,
            )
            resolution = SelectionSourceResolver().describe_generated(
                source_entry,
                context_id=rationale.context_id,
                rationale_context_id=rationale.context_id,
                empty_queue_policy=EmptyQueuePolicy.AUTOMATIC_SELECTION,
                reason=SourceResolutionReason.AUTOMATIC_REQUIRED_EMPTY_QUEUE,
            )
            rationale = attach_source_resolution(rationale, resolution)
            steps.append(
                SelectionSimulationStep(
                    position,
                    step_previous_track_id,
                    selected,
                    rationale,
                )
            )
            prepared.history.record_played(selected)
            prepared.rules.record_preview_played(selected)
            previous_track_id = selected.id
        return SelectionSimulation(
            steps=tuple(steps),
            requested_depth=count,
            settings=prepared.settings,
            context_code=context_code,
            completion_rationale=completion_rationale,
        )

    def _load_rule_settings(self) -> SelectionScoringSettings:
        return (
            self._rule_settings.load()
            if self._rule_settings is not None
            else DEFAULT_SELECTION_SCORING_SETTINGS
        )

    def scoring_settings_snapshot(self) -> SelectionScoringSettings:
        """Load one immutable scoring snapshot for plan configuration checks."""
        return self._load_rule_settings()

    def _load_catalog_snapshot(
        self,
        previous_track_id: int | None,
    ) -> tuple[tuple[Track, ...], SelectionMetadataCatalogSnapshot]:
        loader = getattr(self._tracks, "automatic_selection_snapshot", None)
        if callable(loader):
            parameters = inspect.signature(loader).parameters
            loaded = (
                loader(previous_track_id=previous_track_id)
                if "previous_track_id" in parameters
                else loader()
            )
            if (
                not isinstance(loaded, tuple)
                or len(loaded) != 2
                or not isinstance(loaded[1], SelectionMetadataCatalogSnapshot)
            ):
                raise TypeError("Ungültiger Auswahl-Katalogsnapshot")
            candidates, metadata = loaded
            return tuple(candidates), metadata
        candidates = tuple(self._tracks.automatic_candidates())
        return candidates, neutral_catalog_snapshot(candidates)

    @staticmethod
    def _previous_track_id(history: _AutomaticHistorySource | _PreviewHistory) -> int | None:
        recent = history.recent_track_ids(1)
        return next(iter(recent), None)

    def _resolved_continuity_settings(
        self, settings: SelectionScoringSettings
    ) -> SelectionContinuitySettings:
        if self._continuity_settings is not None:
            return self._continuity_settings
        return SelectionContinuitySettings(
            bpm=ContinuityRuleSetting(
                settings.bpm_continuity.enabled, settings.bpm_continuity.weight
            ),
            energy=ContinuityRuleSetting(
                settings.energy_continuity.enabled, settings.energy_continuity.weight
            ),
            genre=ContinuityRuleSetting(
                settings.genre_diversity.enabled, settings.genre_diversity.weight
            ),
            mood=ContinuityRuleSetting(
                settings.mood_continuity.enabled, settings.mood_continuity.weight
            ),
        )

    def _select(
        self,
        rules: TrackSelectionService,
        settings: SelectionScoringSettings,
        candidates: tuple[Track, ...],
        metadata: SelectionMetadataCatalogSnapshot,
        history: _AutomaticHistorySource | _PreviewHistory,
        sequence: SelectionSequenceContext,
    ) -> Track | None:
        context_id = uuid.uuid4().hex
        summaries: list[CandidateEvaluation] = []
        evaluated_count = 0
        recent = history.recent_track_ids(self.recent_track_limit)
        recent_rule = AutomaticRecentTrackRule(recent)
        counts = history.play_counts()
        soft_rules: list[ExecutableSelectionRule] = []
        play_count_rule = (
            PlayCountScoringRule(counts, settings.play_count.weight)
            if settings.play_count.enabled
            else None
        )
        if settings.rating.enabled:
            soft_rules.append(RatingScoringRule(settings.rating.weight))
        scorer = CandidateScorer(tuple(soft_rules))
        continuity_rules: list[ExecutableSelectionRule] = []
        continuity_settings = self._resolved_continuity_settings(settings)
        if continuity_settings.bpm.enabled:
            continuity_rules.append(BpmContinuityRule(metadata, continuity_settings.bpm.weight))
        if continuity_settings.energy.enabled:
            continuity_rules.append(
                EnergyContinuityRule(metadata, continuity_settings.energy.weight)
            )
        if continuity_settings.genre.enabled:
            continuity_rules.append(GenreDiversityRule(metadata, continuity_settings.genre.weight))
        if continuity_settings.mood.enabled:
            continuity_rules.append(MoodContinuityRule(metadata, continuity_settings.mood.weight))
        continuity_scorer = CandidateScorer(tuple(continuity_rules))
        stages: tuple[tuple[str, frozenset[str]], ...] = (
            ("STRICT", frozenset()),
            ("ARTIST_DISTANCE", frozenset({"ARTIST_REPETITION"})),
            (
                "TRACK_DISTANCE",
                frozenset({"ARTIST_REPETITION", "TRACK_REPETITION", "RECENT_TRACK"}),
            ),
        )
        terminal_exclusions: dict[int, CandidateEvaluation] = {}
        for stage, relaxed_codes in stages:
            eligible: list[tuple[Track, CandidateEvaluation]] = []
            for track in candidates:
                synthetic = QueueEntry(
                    -track.id,
                    track.id,
                    0,
                    QueueStatus.WAITING,
                    source=QueueSource.AUTOMATIC,
                )
                context = SelectionContext(context_id, stage, relaxed_codes, sequence)
                rule_input = SelectionRuleInput.from_values(synthetic, track)
                recent_evaluation = recent_rule.evaluate_rule(rule_input, context)
                if recent_evaluation.result_code is RuleOutcome.EXCLUDE:
                    evaluated_count += 1
                    excluded_evaluation = CandidateEvaluation(
                        candidate=rule_input.candidate,
                        accepted=False,
                        code=recent_evaluation.reason_code,
                        terminal_status=recent_evaluation.terminal_status,
                        reason=recent_evaluation.reason,
                        rules=(recent_evaluation,),
                    )
                    terminal_exclusions[track.id] = excluded_evaluation
                    self._append_summary(
                        summaries,
                        excluded_evaluation,
                    )
                    continue
                decision, rationale = rules.evaluate_with_rationale(
                    synthetic,
                    track,
                    context=context,
                    relaxed_codes=relaxed_codes,
                )
                evaluated_count += 1
                evaluated = rationale.evaluated_candidates[0]
                candidate_evaluation = CandidateEvaluation(
                    candidate=evaluated.candidate,
                    accepted=evaluated.accepted,
                    code=evaluated.code,
                    terminal_status=evaluated.terminal_status,
                    reason=evaluated.reason,
                    rules=(recent_evaluation, *evaluated.rules),
                )
                if decision.accepted:
                    if play_count_rule is not None:
                        play_count_evaluation = play_count_rule.evaluate_rule(rule_input, context)
                        candidate_evaluation = replace(
                            candidate_evaluation,
                            rules=(*candidate_evaluation.rules, play_count_evaluation),
                        )
                    candidate_evaluation = scorer.evaluate(
                        rule_input,
                        context,
                        candidate_evaluation,
                    )
                if decision.accepted:
                    terminal_exclusions.pop(track.id, None)
                else:
                    terminal_exclusions[track.id] = candidate_evaluation
                if decision.accepted:
                    eligible.append((track, candidate_evaluation))
                else:
                    self._append_summary(summaries, candidate_evaluation)
            if not eligible:
                continue
            primary_play_count = (
                min(max(0, counts.get(track.id, 0)) for track, _evaluation in eligible)
                if settings.play_count.enabled
                else None
            )
            ranked: list[tuple[Track, CandidateEvaluation]] = []
            for track, evaluation in eligible:
                play_count = max(0, counts.get(track.id, 0))
                in_primary_group = primary_play_count is None or play_count == primary_play_count
                if in_primary_group and continuity_rules:
                    synthetic = QueueEntry(
                        -track.id,
                        track.id,
                        0,
                        QueueStatus.WAITING,
                        source=QueueSource.AUTOMATIC,
                    )
                    ranked_context = SelectionContext(
                        context_id,
                        stage,
                        relaxed_codes,
                        sequence,
                    )
                    evaluation = continuity_scorer.evaluate(
                        SelectionRuleInput.from_values(synthetic, track),
                        ranked_context,
                        evaluation,
                    )
                ranked_evaluation = replace(
                    evaluation,
                    play_count=play_count,
                    primary_play_count=primary_play_count,
                    in_primary_play_group=in_primary_group,
                    secondary_score=evaluation.total_score,
                )
                ranked.append((track, ranked_evaluation))
                self._append_summary(summaries, ranked_evaluation)
            primary = [item for item in ranked if item[1].in_primary_play_group]
            highest_secondary_score = max(item[1].secondary_score for item in primary)
            top = [item for item in primary if item[1].secondary_score == highest_secondary_score]
            stable_top = sorted(top, key=lambda item: item[0].id)
            selected, selected_evaluation = (
                stable_top[0] if len(stable_top) == 1 else self._random.choice(stable_top)
            )
            self.last_relaxation_stage = stage
            self.last_rationale = self._rationale(
                context_id,
                SelectionOutcome.ACCEPTED,
                selected,
                summaries,
                evaluated_count,
                stage,
                "PRIMARY_PLAY_COUNT_THEN_SECONDARY_SCORE_THEN_STABLE_ID_THEN_INJECTED_RNG",
                selected_evaluation=selected_evaluation,
                tie_candidate_count=len(stable_top),
                primary_play_count=primary_play_count,
                primary_candidate_count=len(primary),
                sequence_step_index=sequence.step_index,
            )
            self._log_decision(self.last_rationale, reason_code="SELECTED")
            if stage != "STRICT":
                self._logger.warning(
                    "Automatische Auswahl verwendet Regelentspannung %s für track_id=%s",
                    stage,
                    selected.id,
                )
            return selected
        self.last_relaxation_stage = "NO_SAFE_CANDIDATE"
        return self._select_emergency(
            rules,
            context_id=context_id,
            summaries=summaries,
            evaluated_count=evaluated_count,
            terminal_exclusions=terminal_exclusions,
        )

    def select_emergency(self, rules: TrackSelectionService) -> Track | None:
        with self._selection_lock:
            return self._run_isolated(
                lambda: self._select_emergency(
                    rules,
                    context_id=uuid.uuid4().hex,
                    summaries=[],
                    evaluated_count=0,
                    terminal_exclusions={},
                )
            )

    def _run_isolated(self, operation: Callable[[], Track | None]) -> Track | None:
        self.last_relaxation_stage = "NONE"
        self.last_rationale = None
        try:
            return operation()
        except BaseException:
            self.last_relaxation_stage = "NONE"
            self.last_rationale = None
            raise

    def _select_emergency(
        self,
        rules: TrackSelectionService,
        *,
        context_id: str,
        summaries: list[CandidateEvaluation],
        evaluated_count: int,
        terminal_exclusions: dict[int, CandidateEvaluation],
    ) -> Track | None:
        if self._emergency_playlist is None:
            self.last_relaxation_stage = "NO_SAFE_CANDIDATE"
            self.last_rationale = self._rationale(
                context_id,
                SelectionOutcome.NO_SAFE_CANDIDATE,
                None,
                summaries,
                evaluated_count,
                self.last_relaxation_stage,
                "NONE",
                terminal_exclusions=terminal_exclusions,
            )
            self._log_decision(self.last_rationale, reason_code="NO_SAFE_CANDIDATE")
            return None
        relaxed = frozenset(
            {
                "ARTIST_REPETITION",
                "TRACK_REPETITION",
            }
        )
        for track in self._emergency_playlist.candidates():
            synthetic = QueueEntry(
                -track.id,
                track.id,
                0,
                QueueStatus.WAITING,
                source=QueueSource.EMERGENCY,
            )
            decision, rationale = rules.evaluate_with_rationale(
                synthetic,
                track,
                relaxed_codes=relaxed,
                context=SelectionContext(context_id, "EMERGENCY_PLAYLIST", relaxed),
            )
            evaluated_count += 1
            self._append_summary(summaries, rationale.evaluated_candidates[0])
            evaluated = rationale.evaluated_candidates[0]
            if decision.accepted:
                terminal_exclusions.pop(track.id, None)
            else:
                terminal_exclusions[track.id] = evaluated
            if decision.accepted:
                self.last_relaxation_stage = "EMERGENCY_PLAYLIST"
                self.last_rationale = self._rationale(
                    context_id,
                    SelectionOutcome.ACCEPTED,
                    track,
                    summaries,
                    evaluated_count,
                    self.last_relaxation_stage,
                    "EMERGENCY_PLAYLIST_ORDER",
                    selected_evaluation=rationale.evaluated_candidates[0],
                )
                self._log_decision(self.last_rationale, reason_code="SELECTED")
                self._logger.warning(
                    "Automatische Auswahl verwendet lokale Emergency-Playlist: track_id=%s",
                    track.id,
                )
                return track
        self.last_relaxation_stage = "NO_SAFE_CANDIDATE"
        self.last_rationale = self._rationale(
            context_id,
            SelectionOutcome.NO_SAFE_CANDIDATE,
            None,
            summaries,
            evaluated_count,
            self.last_relaxation_stage,
            "NONE",
            terminal_exclusions=terminal_exclusions,
        )
        self._log_decision(self.last_rationale, reason_code="NO_SAFE_CANDIDATE")
        return None

    def _append_summary(
        self,
        summaries: list[CandidateEvaluation],
        evaluation: CandidateEvaluation,
    ) -> None:
        if len(summaries) < self._RATIONALE_CANDIDATE_LIMIT:
            summaries.append(evaluation)

    @staticmethod
    def _rationale(
        context_id: str,
        outcome: SelectionOutcome,
        selected: Track | None,
        summaries: list[CandidateEvaluation],
        evaluated_count: int,
        stage: str,
        tie_break_method: str,
        selected_evaluation: CandidateEvaluation | None = None,
        tie_candidate_count: int = 0,
        terminal_exclusions: dict[int, CandidateEvaluation] | None = None,
        primary_play_count: int | None = None,
        primary_candidate_count: int = 0,
        sequence_step_index: int = 0,
    ) -> SelectionRationale:
        if selected_evaluation is not None and selected_evaluation not in summaries:
            if len(summaries) >= AutomaticSelectionService._RATIONALE_CANDIDATE_LIMIT:
                summaries[-1] = selected_evaluation
            else:
                summaries.append(selected_evaluation)
        selected_candidate = next(
            (
                item.candidate
                for item in reversed(summaries)
                if selected is not None and item.candidate.track_id == selected.id and item.accepted
            ),
            None,
        )
        if selected is not None and selected_candidate is None:
            source = (
                QueueSource.EMERGENCY if stage == "EMERGENCY_PLAYLIST" else QueueSource.AUTOMATIC
            )
            selected_candidate = SelectionCandidate.from_entry(
                QueueEntry(
                    -selected.id,
                    selected.id,
                    0,
                    QueueStatus.WAITING,
                    source=source,
                ),
                selected,
            )
        selected_score = (
            selected_evaluation.total_score if selected_evaluation is not None else None
        )
        finalized: list[CandidateEvaluation] = []
        for item in summaries:
            if selected_evaluation is not None and item is selected_evaluation:
                if stage == "EMERGENCY_PLAYLIST":
                    reason = CandidateDecisionReason.SELECTED_EMERGENCY_ORDER
                elif "INJECTED_RNG" in tie_break_method:
                    reason = (
                        CandidateDecisionReason.SELECTED_RNG_TIE_BREAK
                        if tie_candidate_count > 1
                        else CandidateDecisionReason.SELECTED_HIGHEST_SECONDARY_SCORE
                    )
                else:
                    reason = CandidateDecisionReason.SELECTED_STABLE_TIE_BREAK
                finalized.append(
                    replace(
                        item,
                        decision_category=CandidateDecisionCategory.SELECTED,
                        decision_reason_code=reason.value,
                        tie_break_method=tie_break_method,
                    )
                )
                continue
            if item.accepted:
                if not item.in_primary_play_group:
                    reason = CandidateDecisionReason.HIGHER_PLAY_COUNT
                elif selected_score is not None and item.secondary_score < selected_score:
                    reason = CandidateDecisionReason.LOWER_SECONDARY_SCORE
                elif "INJECTED_RNG" in tie_break_method:
                    reason = CandidateDecisionReason.RNG_TIE_BREAK_LOSS
                else:
                    reason = CandidateDecisionReason.STABLE_TIE_BREAK_LOSS
                finalized.append(
                    replace(
                        item,
                        decision_category=CandidateDecisionCategory.ELIGIBLE_NOT_SELECTED,
                        decision_reason_code=reason.value,
                        tie_break_method=tie_break_method,
                    )
                )
                continue
            finalized.append(item)
        omitted = max(0, evaluated_count - len(finalized))
        warnings = (
            (f"{omitted} Kandidatenauswertungen wurden nicht gespeichert",) if omitted else ()
        )
        selected_summary = next(
            (
                candidate
                for candidate in finalized
                if candidate.decision_category is CandidateDecisionCategory.SELECTED
            ),
            None,
        )
        reason_counts = Counter(
            evaluation.code
            for evaluation in (terminal_exclusions or {}).values()
            if evaluation.code
        )
        ordered_reasons = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
        if len(ordered_reasons) > 5:
            displayed_reasons = ordered_reasons[:4]
            displayed_reasons.append(
                ("OTHER_EXCLUSIONS", sum(count for _code, count in ordered_reasons[4:]))
            )
        else:
            displayed_reasons = ordered_reasons
        exclusion_summary = tuple(
            ExclusionReasonSummary(reason_code, count) for reason_code, count in displayed_reasons
        )
        return SelectionRationale(
            context_id=context_id,
            outcome=outcome,
            selected_candidate=selected_candidate,
            evaluated_candidates=tuple(finalized),
            relaxation_stage=stage,
            tie_break_method=tie_break_method,
            warnings=warnings,
            evaluated_candidate_count=evaluated_count,
            omitted_candidate_count=omitted,
            decision_reason_code=(
                selected_summary.decision_reason_code
                if selected_summary is not None
                else outcome.value
            ),
            excluded_candidate_count=sum(item.count for item in exclusion_summary),
            exclusion_summary=exclusion_summary,
            primary_play_count=primary_play_count,
            primary_candidate_count=primary_candidate_count,
            secondary_score=(
                selected_evaluation.secondary_score if selected_evaluation is not None else None
            ),
            final_tie_candidate_count=tie_candidate_count,
            sequence_step_index=sequence_step_index,
        )

    def _log_decision(self, rationale: SelectionRationale, *, reason_code: str) -> None:
        self._logger.info(
            "Automatische Auswahlentscheidung",
            extra={
                "selection_context_id": rationale.context_id,
                "selection_outcome": rationale.outcome.value,
                "reason_code": reason_code,
                "relaxation_stage": rationale.relaxation_stage,
            },
        )
