"""Focused tests for the state-neutral automatic-preview GUI boundary."""

from datetime import datetime
from dataclasses import replace
from threading import Thread, get_ident
from typing import Any, cast

import pytest

from party_player.controllers.main_controller import MainController
from party_player.automatic_selection_plan_execution import (
    AutomaticSelectionPlanExecutionStatus,
)
from party_player.automatic_selection_plan_ui import PreviewAdoptionCode, PreviewAdoptionResult
from party_player.enums import QueueSource, QueueStatus
from party_player.selection_decision import (
    CandidateDecisionCategory,
    CandidateDecisionReason,
    CandidateEvaluation,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    SelectionCandidate,
    SelectionOutcome,
    SelectionRationale,
)
from party_player.selection_preview import (
    SelectionPreview,
    SelectionPreviewCompletion,
    SelectionPreviewPlanningBasis,
    SelectionPreviewStep,
)
from party_player.ui.automatic_selection_preview_dialog import (
    AutomaticSelectionPreviewDialog,
    PreviewRequestState,
    present_preview_step,
    preview_completion_text,
    preview_exclusion_text,
    preview_depth,
    preview_dialog_dimensions,
    selected_preview,
)
from party_player.ui.main_window import MainWindow


def _preview(
    *,
    depth: int = 1,
    completion: SelectionPreviewCompletion = SelectionPreviewCompletion.REQUESTED_DEPTH_REACHED,
) -> SelectionPreview:
    candidate = SelectionCandidate(
        -7, 7, QueueSource.AUTOMATIC, 0, 0, "Ein sehr langer Titel", "Interpret"
    )
    rules = (
        RuleEvaluation(
            "selection.play_count",
            1,
            RuleKind.SOFT_WEIGHT,
            RuleOutcome.SCORE_DELTA,
            "PLAY_COUNT_SCORE",
            "Selten gespielt",
            "STRICT",
            score_delta=10.0,
        ),
        RuleEvaluation(
            "selection.rating",
            1,
            RuleKind.SOFT_WEIGHT,
            RuleOutcome.UNKNOWN_METADATA,
            "RATING_UNKNOWN",
            "Keine gültige Bewertung vorhanden",
            "STRICT",
        ),
    )
    selected = CandidateEvaluation(
        candidate,
        True,
        "SELECTED",
        QueueStatus.WAITING,
        "",
        rules,
        total_score=10.0,
        decision_category=CandidateDecisionCategory.SELECTED,
        decision_reason_code=CandidateDecisionReason.SELECTED_HIGHEST_SCORE.value,
        play_count=0,
        primary_play_count=0,
        in_primary_play_group=True,
        secondary_score=10.0,
    )
    excluded = CandidateEvaluation(
        SelectionCandidate(-8, 8, QueueSource.AUTOMATIC, 0, 0, "Alternative", "Andere"),
        False,
        "BLOCKED",
        QueueStatus.SKIPPED,
        "Titel ist vorübergehend gesperrt",
        (),
        decision_category=CandidateDecisionCategory.EXCLUDED,
    )
    rationale = SelectionRationale(
        "context",
        SelectionOutcome.ACCEPTED,
        candidate,
        (selected, excluded),
        "STRICT",
        "STABLE",
        evaluated_candidate_count=2,
        decision_reason_code=CandidateDecisionReason.SELECTED_HIGHEST_SCORE.value,
        primary_play_count=0,
        primary_candidate_count=1,
        secondary_score=10.0,
        final_tie_candidate_count=1,
    )
    steps = (
        SelectionPreviewStep(
            1,
            7,
            candidate.title,
            candidate.artist,
            "STRICT",
            rationale,
        ),
    )
    return SelectionPreview(
        "preview",
        datetime(2026, 9, 7, 18, 30),
        depth,
        len(steps),
        steps,
        completion,
    )


@pytest.mark.parametrize("value", [1, 5, 10])
def test_preview_depth_accepts_supported_values(value: int) -> None:
    assert preview_depth(str(value)) == value


@pytest.mark.parametrize("value", ["0", "11", "text"])
def test_preview_depth_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        preview_depth(value)


def test_successful_preview_has_readable_summary_and_bounded_details() -> None:
    presentation = present_preview_step(_preview().steps[0])

    assert "1. Interpret – Ein sehr langer Titel" in presentation.summary
    assert "Höchste Bewertung" in presentation.summary
    assert "Primäre Abspielgruppe: mindestens 0 · Zugehörigkeit: ja" in presentation.detail
    assert "Sekundärer Bewertungsscore: +10 Punkte" in presentation.detail
    assert "Selten gespielt: Abspielhäufigkeit: +10 Punkte" in presentation.detail
    assert "Titelbewertung: Metadaten unbekannt – keine Auswirkung" in presentation.detail
    assert "Gesamtscore" not in presentation.detail
    assert "Titel ist vorübergehend gesperrt" in presentation.detail
    assert "selection." not in presentation.detail
    assert "PLAY_COUNT_SCORE" not in presentation.detail


def test_preview_explains_active_continuity_rules_and_real_tie_only() -> None:
    step = _preview().steps[0]
    selected = next(
        item
        for item in step.rationale.evaluated_candidates
        if item.decision_category is CandidateDecisionCategory.SELECTED
    )
    rules = (
        RuleEvaluation(
            "selection.bpm_continuity",
            1,
            RuleKind.SOFT_WEIGHT,
            RuleOutcome.SCORE_DELTA,
            "BPM_DISTANCE",
            r"G:\private\must-not-appear",
            "STRICT",
            facts=(("distance", 6.0),),
            score_delta=0.9,
        ),
        RuleEvaluation(
            "selection.energy_continuity",
            1,
            RuleKind.SOFT_WEIGHT,
            RuleOutcome.UNKNOWN_METADATA,
            "METADATA_UNKNOWN",
            "untrusted comment",
            "STRICT",
        ),
        RuleEvaluation(
            "selection.mood_continuity",
            1,
            RuleKind.SOFT_WEIGHT,
            RuleOutcome.SCORE_DELTA,
            "MOOD_OVERLAP",
            "untrusted mood detail",
            "STRICT",
            facts=(("common_terms", r"G:\private\mood"),),
            score_delta=0.5,
        ),
    )
    explained = replace(selected, rules=rules, secondary_score=0.9, total_score=0.9)
    rationale = replace(
        step.rationale,
        evaluated_candidates=(explained,),
        secondary_score=0.9,
        final_tie_candidate_count=2,
    )

    detail = present_preview_step(replace(step, rationale=rationale)).detail

    assert "BPM-Abstand 6 BPM: +0.9 Punkte" in detail
    assert "Energie-Kontinuität: Metadaten unbekannt – keine Auswirkung" in detail
    assert "Gemeinsame Stimmung: bestätigte Stimmung: +0.5 Punkte" in detail
    assert "Gleichstandsentscheidung zwischen 2 gleichrangigen Finalisten" in detail
    assert detail.count("BPM-Kontinuität") == 1
    assert "G:\\private" not in detail
    assert "untrusted comment" not in detail


def test_long_list_text_is_shortened_while_details_keep_the_full_title() -> None:
    preview = _preview()
    step = preview.steps[0]
    long_title = "Sehr langer Titel " * 10
    long_step = SelectionPreviewStep(
        step.position,
        step.track_id,
        long_title,
        "Sehr langer Interpret " * 5,
        step.relaxation_stage,
        step.rationale,
    )

    presentation = present_preview_step(long_step)

    assert "…" in presentation.summary
    assert long_title in presentation.detail


def test_relaxation_and_no_safe_candidate_are_explained() -> None:
    source = _preview(depth=5, completion=SelectionPreviewCompletion.NO_SAFE_CANDIDATE)
    step = source.steps[0]
    relaxed = SelectionPreviewStep(
        step.position,
        step.track_id,
        step.title,
        step.artist,
        "TRACK_DISTANCE",
        step.rationale,
    )

    presentation = present_preview_step(relaxed)
    assert "Regeln gelockert" in presentation.summary
    assert "Titel- und Interpretabstand erweitert" in presentation.detail
    assert "Kein sicher geeigneter" in preview_completion_text(source)
    assert "1 von 5" in preview_completion_text(source)


def test_large_and_compact_geometries_are_explicit() -> None:
    assert preview_dialog_dimensions(False) == ((980, 720), (720, 500))
    assert preview_dialog_dimensions(True) == ((760, 650), (500, 430))


def test_request_state_blocks_duplicates_stale_results_and_close_callbacks() -> None:
    state = PreviewRequestState()
    first = state.begin()

    assert first == 1
    assert state.begin() is None
    assert not state.finish(0)
    assert state.finish(1)
    second = state.begin()
    assert second == 2
    assert not state.accepts(1)
    state.close()
    assert not state.accepts(2)
    assert state.begin() is None


class _Widget:
    def __init__(self, value: str = "") -> None:
        self.value = value
        self.values: dict[str, object] = {}

    def get(self) -> str:
        return self.value

    def configure(self, **values: object) -> None:
        self.values.update(values)


class _Requester:
    def __init__(self) -> None:
        self.calls = 0
        self.completed: Any = None
        self.failed: Any = None

    def request_automatic_selection_preview(self, count: int, completed: Any, failed: Any) -> bool:
        self.calls += 1
        self.count = count
        self.completed = completed
        self.failed = failed
        return True


class _DialogDouble:
    def __init__(self) -> None:
        self._requester = _Requester()
        self._request_state = PreviewRequestState()
        self._depth = _Widget("5")
        self._calculate = _Widget()
        self._status = _Widget()
        self._created = _Widget()
        self._detail_text = _Widget()
        self.cleared = 0

    def _clear_result(self) -> None:
        self.cleared += 1

    def _accept_preview(self, generation: int, preview: SelectionPreview) -> None:
        AutomaticSelectionPreviewDialog._accept_preview(cast(Any, self), generation, preview)

    def _accept_error(self, generation: int, message: str) -> None:
        AutomaticSelectionPreviewDialog._accept_error(cast(Any, self), generation, message)


def test_dialog_starts_once_and_error_clears_previous_success() -> None:
    dialog = _DialogDouble()

    AutomaticSelectionPreviewDialog._start(cast(Any, dialog))
    AutomaticSelectionPreviewDialog._start(cast(Any, dialog))

    assert dialog._requester.calls == 1
    assert dialog._requester.count == 5
    assert dialog.cleared == 1
    dialog._requester.failed("Datenbank nicht erreichbar")
    assert dialog.cleared == 2
    assert dialog._created.values["text"] == "Keine aktuelle Vorschau"
    assert "Datenbank nicht erreichbar" in str(dialog._status.values["text"])


def test_no_safe_candidate_replaces_initial_empty_preview_text() -> None:
    source = _preview(depth=5, completion=SelectionPreviewCompletion.NO_SAFE_CANDIDATE)
    empty = SelectionPreview(
        source.preview_id,
        source.created_at,
        source.requested_depth,
        0,
        (),
        source.completion_reason,
    )
    dialog = _DialogDouble()
    generation = dialog._request_state.begin()

    AutomaticSelectionPreviewDialog._accept_preview(cast(Any, dialog), generation, empty)

    assert dialog._detail_text.values["text"] == "Keine geeigneten Titel gefunden."


def test_no_safe_candidate_presents_bounded_reason_summary() -> None:
    from party_player.selection_decision import ExclusionReasonSummary

    source = _preview(depth=5, completion=SelectionPreviewCompletion.NO_SAFE_CANDIDATE)
    rationale = source.steps[0].rationale
    summarized = SelectionPreview(
        source.preview_id,
        source.created_at,
        5,
        0,
        (),
        SelectionPreviewCompletion.NO_SAFE_CANDIDATE,
        completion_rationale=SelectionRationale(
            context_id=rationale.context_id,
            outcome=SelectionOutcome.REJECTED,
            selected_candidate=None,
            evaluated_candidates=(),
            relaxation_stage="NO_SAFE_CANDIDATE",
            tie_break_method="NONE",
            excluded_candidate_count=76,
            exclusion_summary=(ExclusionReasonSummary("SUITABILITY_APPROVAL_REQUIRED", 76),),
        ),
    )

    text = preview_exclusion_text(summarized)

    assert "76 Titel geprüft" in text
    assert "76 Titel sind noch nicht" in text
    assert "selection." not in text


def test_closed_dialog_ignores_late_worker_error_without_widget_access() -> None:
    dialog = _DialogDouble()
    AutomaticSelectionPreviewDialog._start(cast(Any, dialog))
    dialog._request_state.close()
    before = dict(dialog._status.values)

    dialog._requester.failed("zu spät")

    assert dialog._status.values == before


def test_successful_dynamic_adoption_closes_without_plan_overview_detour() -> None:
    opened: list[bool] = []

    class Parent:
        def _show_automatic_plan(self) -> None:
            opened.append(True)

        def after_idle(self, callback: Any) -> None:
            callback()

    class Dialog:
        def __init__(self) -> None:
            self._request_state = PreviewRequestState()
            self._adoption_running = True
            self._adopt = _Widget()
            self._status = _Widget()
            self.master = Parent()
            self.closed = False

        def _close(self) -> None:
            self.closed = True

    dialog = Dialog()

    AutomaticSelectionPreviewDialog._accept_adoption(
        cast(Any, dialog), PreviewAdoptionResult(PreviewAdoptionCode.CREATED)
    )

    assert dialog.closed
    assert opened == []


def test_selected_preview_keeps_display_order_and_renumbers_plan_steps() -> None:
    first = _preview().steps[0]
    source = replace(
        _preview(depth=3),
        achieved_depth=3,
        steps=(
            first,
            replace(first, position=2, track_id=8, title="Zweiter Titel"),
            replace(first, position=3, track_id=9, title="Dritter Titel"),
        ),
    )

    result = selected_preview(source, {7, 9})

    assert [step.track_id for step in result.steps] == [7, 9]
    assert [step.position for step in result.steps] == [1, 2]
    assert result.requested_depth == result.achieved_depth == 2


def test_main_window_opens_one_preview_dialog_from_existing_menu_path(monkeypatch: Any) -> None:
    created: list[tuple[object, object]] = []

    class Window:
        _controller = object()
        _automatic_selection_preview_dialog = None

    window = Window()
    monkeypatch.setattr(
        "party_player.ui.main_window.AutomaticSelectionPreviewDialog",
        lambda parent, controller: created.append((parent, controller)),
    )

    MainWindow._show_automatic_preview(cast(Any, window))

    assert created == [(window, window._controller)]


def test_controller_uses_worker_and_only_state_neutral_preview_boundary() -> None:
    main_thread_id = get_ident()
    preview = _preview()

    class Queue:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def preview_automatic_selection(self, count: int) -> SelectionPreview:
            self.calls.append((count, get_ident()))
            return preview

    class ControllerDouble:
        def __init__(self) -> None:
            self._queue_service = Queue()
            self._closed = False
            self.callbacks: list[Any] = []
            self.worker: Thread | None = None

        def _publish_gui_callback(self, callback: Any, _source: str) -> None:
            self.callbacks.append(callback)

        def _start_worker(self, target: Any, _name: str, _category: str) -> bool:
            self.worker = Thread(target=target)
            self.worker.start()
            return True

    controller = ControllerDouble()
    completed: list[SelectionPreview] = []

    assert MainController.request_automatic_selection_preview(
        cast(Any, controller), 5, completed.append, pytest.fail
    )
    assert controller.worker is not None
    controller.worker.join()
    assert controller._queue_service.calls[0][0] == 5
    assert controller._queue_service.calls[0][1] != main_thread_id
    assert completed == []
    controller.callbacks[0]()
    assert completed == [preview]


def test_controller_appends_preview_to_editable_queue_in_displayed_order() -> None:
    preview = replace(
        _preview(),
        planning_basis=SelectionPreviewPlanningBasis(42, "digest", 3, None, "history"),
    )

    class Queue:
        def __init__(self) -> None:
            self.added_entries: list[Any] = []
            self.source = ""

        def add_many(
            self,
            entries: list[Any],
            *,
            source: str,
            use_saved_cues: bool,
            replace_queue: bool,
        ) -> tuple[int, int]:
            self.added_entries = entries
            self.source = source
            assert not use_saved_cues
            assert replace_queue
            return len(entries), 0

        def entries(self) -> list[Any]:
            return []

    class ControllerDouble:
        def __init__(self) -> None:
            self._session = type("Session", (), {"session_id": 42})()
            self._queue_service = Queue()
            self._automatic_plan_ui = None
            self.refreshed = False
            self.paused = False

        def _pause_automatic_queue(self, _reason: str) -> None:
            self.paused = True

        def _publish_gui_callback(self, callback: Any, _source: str) -> None:
            callback()

        def _start_worker(self, target: Any, _name: str, _category: str) -> bool:
            target()
            return True

        def _refresh_queue(self) -> None:
            self.refreshed = True

    controller = ControllerDouble()
    completed: list[tuple[int, int]] = []

    assert MainController.request_preview_queue_adoption(
        cast(Any, controller),
        preview,
        lambda added, skipped: completed.append((added, skipped)),
        pytest.fail,
    )

    assert [entry.track_id for entry in controller._queue_service.added_entries] == [7]
    assert controller._queue_service.source == QueueSource.AUTOMATIC.value
    assert controller.paused
    assert controller.refreshed
    assert completed == [(1, 0)]


def test_controller_extends_existing_queue_without_pausing_or_replacing_it() -> None:
    preview = replace(
        _preview(),
        planning_basis=SelectionPreviewPlanningBasis(42, "digest", 3, None, "history"),
    )

    class Queue:
        def __init__(self) -> None:
            self.source = ""

        def add_many(
            self,
            _entries: list[Any],
            *,
            source: str,
            use_saved_cues: bool,
            replace_queue: bool,
        ) -> tuple[int, int]:
            self.source = source
            assert not use_saved_cues
            assert not replace_queue
            return 1, 0

    class ControllerDouble:
        def __init__(self) -> None:
            self._session = type("Session", (), {"session_id": 42})()
            self._queue_service = Queue()
            self._automatic_plan_ui = None
            self.paused = False

        def _pause_automatic_queue(self, _reason: str) -> None:
            self.paused = True

        def _publish_gui_callback(self, callback: Any, _source: str) -> None:
            callback()

        def _start_worker(self, target: Any, _name: str, _category: str) -> bool:
            target()
            return True

        def _refresh_queue(self) -> None:
            pass

    controller = ControllerDouble()
    completed: list[tuple[int, int]] = []

    assert MainController.request_preview_queue_adoption(
        cast(Any, controller),
        preview,
        lambda added, skipped: completed.append((added, skipped)),
        pytest.fail,
        replace_queue=False,
    )

    assert not controller.paused
    assert controller._queue_service.source == QueueSource.MANUAL.value
    assert completed == [(1, 0)]


def test_controller_can_adopt_and_activate_dynamic_preview_in_one_action() -> None:
    preview = _preview()
    draft = type(
        "Bundle",
        (),
        {
            "plan": type(
                "Plan",
                (),
                {
                    "plan_id": "plan",
                    "revision": 4,
                    "status": type("Status", (), {"value": "DRAFT"})(),
                },
            )()
        },
    )()
    active = object()

    class Plans:
        def adopt_preview(self, _preview: Any, **_kwargs: Any) -> PreviewAdoptionResult:
            return PreviewAdoptionResult(PreviewAdoptionCode.CREATED, draft)

        def activate(self, plan_id: str, revision: int) -> object:
            assert (plan_id, revision) == ("plan", 4)
            return active

    class ControllerDouble:
        def __init__(self) -> None:
            self._automatic_plan_ui = Plans()
            self.mode: Any = None

        def _publish_gui_callback(self, callback: Any, _source: str) -> None:
            callback()

        def _start_worker(self, target: Any, _name: str, _category: str) -> bool:
            target()
            return True

        def set_player_mode(self, mode: Any) -> None:
            self.mode = mode

        _queue_service = type(
            "QueueService",
            (),
            {
                "ensure_automatic_buffer": lambda _self, _count: type(
                    "Result",
                    (),
                    {
                        "status": AutomaticSelectionPlanExecutionStatus.MATERIALIZED,
                        "reason_code": None,
                    },
                )()
            },
        )()

    controller = ControllerDouble()
    completed: list[PreviewAdoptionResult] = []

    assert MainController.request_preview_adoption(
        cast(Any, controller),
        preview,
        completed.append,
        pytest.fail,
        activate=True,
    )

    assert completed == [PreviewAdoptionResult(PreviewAdoptionCode.CREATED, active)]
    assert controller.mode.value == "automatic"


def test_controller_does_not_activate_an_already_active_replacement_twice() -> None:
    active = type(
        "Bundle",
        (),
        {"plan": type("Plan", (), {"status": type("Status", (), {"value": "ACTIVE"})()})()},
    )()

    class Plans:
        def adopt_preview(self, _preview: Any, **_kwargs: Any) -> PreviewAdoptionResult:
            return PreviewAdoptionResult(PreviewAdoptionCode.REPLACED, active)

        def activate(self, _plan_id: str, _revision: int) -> object:
            raise AssertionError("active replacement must not be activated again")

    class ControllerDouble:
        _automatic_plan_ui = Plans()
        _queue_service = type(
            "QueueService",
            (),
            {
                "ensure_automatic_buffer": lambda _self, _count: type(
                    "Result",
                    (),
                    {
                        "status": AutomaticSelectionPlanExecutionStatus.MATERIALIZED,
                        "reason_code": None,
                    },
                )()
            },
        )()

        def _publish_gui_callback(self, callback: Any, _source: str) -> None:
            callback()

        def _start_worker(self, target: Any, _name: str, _category: str) -> bool:
            target()
            return True

        def set_player_mode(self, _mode: Any) -> None:
            pass

    completed: list[PreviewAdoptionResult] = []
    assert MainController.request_preview_adoption(
        cast(Any, ControllerDouble()),
        _preview(),
        completed.append,
        pytest.fail,
        activate=True,
    )
    assert completed == [PreviewAdoptionResult(PreviewAdoptionCode.REPLACED, active)]


def test_controller_does_not_expose_internal_error_details_to_the_gui() -> None:
    class Queue:
        def preview_automatic_selection(self, _count: int) -> SelectionPreview:
            raise RuntimeError(r"G:\private\music\catalog.db")

    class Logger:
        def exception(self, _message: str) -> None:
            pass

    class ControllerDouble:
        _queue_service = Queue()
        _closed = False
        _logger = Logger()

        def __init__(self) -> None:
            self.callback: Any = None

        def _publish_gui_callback(self, callback: Any, _source: str) -> None:
            self.callback = callback

        def _start_worker(self, target: Any, _name: str, _category: str) -> bool:
            target()
            return True

    controller = ControllerDouble()
    errors: list[str] = []

    assert MainController.request_automatic_selection_preview(
        cast(Any, controller), 5, pytest.fail, errors.append
    )
    controller.callback()

    assert errors == ["Die Vorschau konnte nicht berechnet werden. Bitte erneut versuchen."]
    assert "G:" not in errors[0]
