"""Deterministic, history-aware automatic catalog selection."""

from collections.abc import Callable
import random
import logging
from threading import Lock
import uuid

from party_player.database.connection import Database
from party_player.enums import QueueSource, QueueStatus
from party_player.models import QueueEntry, Track
from party_player.repositories.track_repository import TrackRepository
from party_player.track_selection import TrackSelectionService
from party_player.emergency_playlist import LocalEmergencyPlaylistService
from party_player.selection_decision import (
    CandidateEvaluation,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    SelectionCandidate,
    SelectionContext,
    SelectionOutcome,
    SelectionRationale,
    SelectionRuleInput,
    hard_rule_evaluation,
)


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

    def __init__(
        self,
        tracks: TrackRepository,
        history: AutomaticSelectionHistory,
        *,
        recent_track_limit: int = 25,
        randomizer: random.Random | None = None,
        emergency_playlist: LocalEmergencyPlaylistService | None = None,
    ) -> None:
        self._tracks = tracks
        self._history = history
        self.recent_track_limit = max(0, recent_track_limit)
        self._random = randomizer or random.Random()
        self._emergency_playlist = emergency_playlist
        self.last_relaxation_stage = "NONE"
        self.last_rationale: SelectionRationale | None = None
        self._selection_lock = Lock()
        self._logger = logging.getLogger(__name__)

    def select(self, rules: TrackSelectionService) -> Track | None:
        with self._selection_lock:
            return self._run_isolated(lambda: self._select(rules))

    def _select(self, rules: TrackSelectionService) -> Track | None:
        context_id = uuid.uuid4().hex
        summaries: list[CandidateEvaluation] = []
        evaluated_count = 0
        recent = self._history.recent_track_ids(self.recent_track_limit)
        recent_rule = AutomaticRecentTrackRule(recent)
        counts = self._history.play_counts()
        stages: tuple[tuple[str, frozenset[str]], ...] = (
            ("STRICT", frozenset()),
            ("ARTIST_DISTANCE", frozenset({"ARTIST_REPETITION"})),
            (
                "TRACK_DISTANCE",
                frozenset({"ARTIST_REPETITION", "TRACK_REPETITION", "RECENT_TRACK"}),
            ),
        )
        candidates = self._tracks.automatic_candidates()
        for stage, relaxed_codes in stages:
            minimum_count: int | None = None
            top: list[tuple[Track, CandidateEvaluation]] = []
            for track in candidates:
                synthetic = QueueEntry(
                    -track.id,
                    track.id,
                    0,
                    QueueStatus.WAITING,
                    source=QueueSource.AUTOMATIC,
                )
                context = SelectionContext(context_id, stage, relaxed_codes)
                rule_input = SelectionRuleInput.from_values(synthetic, track)
                recent_evaluation = recent_rule.evaluate_rule(rule_input, context)
                if recent_evaluation.result_code is RuleOutcome.EXCLUDE:
                    evaluated_count += 1
                    self._append_summary(
                        summaries,
                        CandidateEvaluation(
                            candidate=rule_input.candidate,
                            accepted=False,
                            code=recent_evaluation.reason_code,
                            terminal_status=recent_evaluation.terminal_status,
                            reason=recent_evaluation.reason,
                            rules=(recent_evaluation,),
                        ),
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
                self._append_summary(summaries, candidate_evaluation)
                if decision.accepted:
                    play_count = counts.get(track.id, 0)
                    if minimum_count is None or play_count < minimum_count:
                        minimum_count = play_count
                        top = [(track, candidate_evaluation)]
                    elif play_count == minimum_count:
                        top.append((track, candidate_evaluation))
            if not top:
                continue
            selected, selected_evaluation = self._random.choice(top)
            self.last_relaxation_stage = stage
            self.last_rationale = self._rationale(
                context_id,
                SelectionOutcome.ACCEPTED,
                selected,
                summaries,
                evaluated_count,
                stage,
                "LOWEST_PLAY_COUNT_THEN_INJECTED_RNG",
                selected_evaluation=selected_evaluation,
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
        )

    def select_emergency(self, rules: TrackSelectionService) -> Track | None:
        with self._selection_lock:
            return self._run_isolated(
                lambda: self._select_emergency(
                    rules,
                    context_id=uuid.uuid4().hex,
                    summaries=[],
                    evaluated_count=0,
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
        omitted = max(0, evaluated_count - len(summaries))
        warnings = (
            (f"{omitted} Kandidatenauswertungen wurden nicht gespeichert",) if omitted else ()
        )
        return SelectionRationale(
            context_id=context_id,
            outcome=outcome,
            selected_candidate=selected_candidate,
            evaluated_candidates=tuple(summaries),
            relaxation_stage=stage,
            tie_break_method=tie_break_method,
            warnings=warnings,
            evaluated_candidate_count=evaluated_count,
            omitted_candidate_count=omitted,
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
