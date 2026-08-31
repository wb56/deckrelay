"""Controller for manual cue-point editing, independent from party playback flow."""

from dataclasses import dataclass
import logging
from concurrent.futures import CancelledError, Executor, Future
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

from party_player.audio.base import AudioBackend
from party_player.analysis import (
    CueAnalysisResult,
    CueAnalysisService,
    CueAnalysisServiceJob,
)
from party_player.bounded_executor import BoundedThreadPoolExecutor
from party_player.persistence_participant import single_worker_participant
from party_player.restore_lifecycle import PersistenceParticipant
from party_player.cue_points import CuePointService, ResolvedTrackBoundaries
from party_player.deck_controller import DeckController
from party_player.gui_event_dispatcher import GuiEvent, GuiEventDispatcher, GuiEventType
from party_player.performance_monitor import PerformanceMonitor
from party_player.worker_diagnostics import WorkerInfo, WorkerRegistry
from party_player.models import Track
from party_player.services.library_service import LibraryService


SOURCE_TEXT = {
    "MANUAL": "Manuell festgelegt",
    "AUTOMATIC": "Automatisch erkannt",
    "FILE_BOUNDARY": "Dateigrenze",
    "GLOBAL": "Globale Einstellung",
    "QUEUE_OVERRIDE": "Queue-Wert",
    "QUEUE_SNAPSHOT": "Veranstaltungs-Snapshot",
}


@dataclass(frozen=True, slots=True)
class CuePointEditorState:
    track_id: int
    title: str
    manual_cue_in: float | None
    manual_cue_out: float | None
    manual_fade_duration: float | None
    resolved: ResolvedTrackBoundaries
    automatic_cue_in: float | None = None
    automatic_cue_out: float | None = None
    automatic_fade_duration: float | None = None
    minimum_level_dbfs: float | None = None
    maximum_level_dbfs: float | None = None
    peak: float | None = None
    confidence: float | None = None
    analysis_version: str | None = None
    analysed_at: str | None = None
    analysis_backend: str | None = None

    @property
    def cue_in_source_text(self) -> str:
        return SOURCE_TEXT.get(self.resolved.cue_in_source, self.resolved.cue_in_source)

    @property
    def cue_out_source_text(self) -> str:
        return SOURCE_TEXT.get(self.resolved.cue_out_source, self.resolved.cue_out_source)

    @property
    def fade_source_text(self) -> str:
        return SOURCE_TEXT.get(self.resolved.fade_source, self.resolved.fade_source)


class CuePointController:
    """Validate and persist editor actions without using MainController."""

    def __init__(
        self,
        service: CuePointService,
        library: LibraryService,
        deck_a: DeckController,
        deck_b: DeckController,
        global_fade_duration: float = 7.0,
        preview_backend_factory: Callable[[], AudioBackend] | None = None,
        schedule: Callable[[int, Callable[[], None]], object] | None = None,
        gui_dispatcher: GuiEventDispatcher | None = None,
        performance_monitor: PerformanceMonitor | None = None,
        worker_registry: WorkerRegistry | None = None,
        analysis_service: CueAnalysisService | None = None,
        analysis_unavailable_reason: str | None = None,
        persistence_executor: Executor | None = None,
    ) -> None:
        self._service = service
        self._library = library
        self._decks = (deck_a, deck_b)
        self._global_fade_duration = global_fade_duration
        self._preview_backend_factory = preview_backend_factory
        self._schedule = schedule
        self._gui_dispatcher = gui_dispatcher
        self._performance = performance_monitor or PerformanceMonitor()
        self._worker_registry = worker_registry or WorkerRegistry()
        self._analysis_service = analysis_service
        self._analysis_unavailable_reason = analysis_unavailable_reason
        self._persistence_executor = persistence_executor or BoundedThreadPoolExecutor(
            max_workers=1,
            maximum_pending=1,
            thread_name_prefix="cue-persist",
        )
        self._owns_persistence_executor = persistence_executor is None
        self._analysis_job: CueAnalysisServiceJob | None = None
        self._batch_analysis_job: CueAnalysisServiceJob | None = None
        self._batch_analysis_cancelled = False
        self._batch_analysis_running = False
        self._preview_generation = 0
        self._preview_stop = Event()
        self._preview_lock = Lock()
        self._preview_backend: AudioBackend | None = None
        self._preview_executor = BoundedThreadPoolExecutor(
            max_workers=1, maximum_pending=2, thread_name_prefix="cue-preview"
        )
        self._preview_future: Future[object] | None = None
        self._logger = logging.getLogger(__name__)

    def warm_persistence_worker(self) -> None:
        """Start the persistence thread before the first interactive save."""
        started = monotonic()
        self._persistence_executor.submit(lambda: None).result()
        self._performance.record(
            "track_editor.persist_worker_warmup",
            (monotonic() - started) * 1000.0,
            250.0,
        )

    def restore_participant(self) -> PersistenceParticipant | None:
        if not isinstance(self._persistence_executor, BoundedThreadPoolExecutor):
            return None
        return single_worker_participant("cue-persistence", self._persistence_executor)

    @property
    def active_analysis_job_count(self) -> int:
        return self._analysis_service.active_job_count if self._analysis_service else 0

    def state(self, track_id: int) -> CuePointEditorState:
        track = self._track(track_id)
        stored = self._service.get(track_id)
        return CuePointEditorState(
            track.id,
            f"{track.artist} — {track.title}" if track.artist else track.title,
            stored.manual_cue_in,
            stored.manual_cue_out,
            stored.manual_fade_duration,
            self._service.resolve(track, self._global_fade_duration),
            stored.automatic_cue_in,
            stored.automatic_cue_out,
            stored.automatic_fade_duration,
            stored.minimum_level_dbfs,
            stored.maximum_level_dbfs,
            stored.peak,
            stored.confidence,
            stored.analysis_version,
            stored.analysed_at,
            stored.analysis_backend,
        )

    def manual_track_ids(self, track_ids: list[int]) -> set[int]:
        return self._service.manual_track_ids(track_ids)

    def current_position(self, track_id: int) -> float:
        for deck in self._decks:
            if deck.model.loaded_track is not None and deck.model.loaded_track.id == track_id:
                return deck.model.position
        raise ValueError("Der Titel ist derzeit in keinem Deck geladen.")

    def save(
        self,
        track_id: int,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
    ) -> CuePointEditorState:
        self._service.save_manual(self._track(track_id), cue_in, cue_out, fade_duration)
        state = self.state(track_id)
        self._apply_state_to_decks(state)
        return state

    def save_async(
        self,
        track_id: int,
        cue_in: float | None,
        cue_out: float | None,
        fade_duration: float | None,
        completed: Callable[[CuePointEditorState], None],
        failed: Callable[[Exception], None],
        *,
        discard_automatic: bool = False,
        changed_fields: frozenset[str] | None = None,
    ) -> Future[CuePointEditorState]:
        """Persist cue values serially and apply the result through the GUI dispatcher."""
        start_gate = Event() if self._owns_persistence_executor else None

        def persist() -> CuePointEditorState:
            if start_gate is not None:
                start_gate.wait()
            with self._performance.measure(
                "track_editor.persist",
                warning_threshold_ms=250.0,
                context={"track_id": track_id},
            ):
                self._service.save_editor(
                    self._track(track_id),
                    cue_in,
                    cue_out,
                    fade_duration,
                    discard_automatic=discard_automatic,
                    changed_fields=changed_fields,
                )
                return self.state(track_id)

        future: Future[CuePointEditorState] = self._persistence_executor.submit(persist)

        def finished(done: Future[CuePointEditorState]) -> None:
            try:
                state = done.result()
            except Exception as exc:

                def publish_failure(error: Exception = exc) -> None:
                    failed(error)

                self._publish_gui_callback(publish_failure, source="cue_persist")
                return

            def apply() -> None:
                self._apply_state_to_decks(state)
                completed(state)

            self._publish_gui_callback(apply, source="cue_persist")

        future.add_done_callback(finished)
        if start_gate is not None:
            self._publish_gui_callback(start_gate.set, source="cue_persist_start")
        return future

    def _apply_state_to_decks(self, state: CuePointEditorState) -> None:
        for deck in self._decks:
            if deck.model.loaded_track is not None and deck.model.loaded_track.id == state.track_id:
                deck.model.cue_in = state.resolved.cue_in
                deck.model.cue_out = state.resolved.cue_out
                deck.model.cue_fade_duration = state.resolved.fade_duration
                deck.model.cue_in_source = state.resolved.cue_in_source
                deck.model.cue_out_source = state.resolved.cue_out_source
                deck.model.cue_fade_source = state.resolved.fade_source
                deck.model.cue_warning = state.resolved.warning
                deck.model.automatic_crossfade_allowed = state.resolved.automatic_crossfade_allowed
                deck.model.cue_boundaries_ready = True

    def adopt_automatic(self, track_id: int) -> CuePointEditorState:
        """Promote the complete automatic suggestion to editable manual values."""
        stored = self._service.get(track_id)
        if (
            stored.automatic_cue_in is None
            or stored.automatic_cue_out is None
            or stored.automatic_fade_duration is None
        ):
            raise ValueError("Für diesen Titel liegt kein vollständiger Vorschlag vor.")
        return self.save(
            track_id,
            stored.automatic_cue_in,
            stored.automatic_cue_out,
            stored.automatic_fade_duration,
        )

    def discard_automatic(self, track_id: int) -> CuePointEditorState:
        self._service.clear_automatic(track_id)
        return self.state(track_id)

    def preview_cue_in(self, track_id: int, status: Callable[[str], None] | None = None) -> None:
        resolved = self.state(track_id).resolved
        self._start_preview(track_id, resolved.cue_in, 10.0, status)

    def preview_cue_out(self, track_id: int, status: Callable[[str], None] | None = None) -> None:
        resolved = self.state(track_id).resolved
        start = max(resolved.cue_in, resolved.cue_out - 10.0)
        self._start_preview(track_id, start, resolved.cue_out - start, status)

    def stop_preview(self) -> None:
        self._preview_generation += 1
        self._preview_stop.set()

    def analyze(
        self,
        track_id: int,
        completed: Callable[[CueAnalysisResult], None] | None,
        status: Callable[[str], None] | None = None,
        *,
        state_completed: Callable[[CuePointEditorState], None] | None = None,
    ) -> None:
        """Start one offline analysis and publish completion through the GUI dispatcher."""
        self._require_analysis_available()
        assert self._analysis_service is not None
        if self._analysis_job is not None and not self._analysis_job.future.done():
            raise RuntimeError("Für diesen Editor läuft bereits eine Cue-Analyse.")
        track = self._track(track_id)
        self._notify_analysis(status, "Automatische Cue-Analyse läuft …")
        job = self._analysis_service.analyze(track)
        self._analysis_job = job

        def finished(future: Future[CueAnalysisResult]) -> None:
            try:
                result = future.result()
                editor_state = self.state(track_id) if state_completed is not None else None
            except Exception as exc:
                self._notify_analysis(status, f"Cue-Analyse fehlgeschlagen: {exc}")
                return

            def publish_completion() -> None:
                if completed is not None:
                    completed(result)
                if state_completed is not None:
                    assert editor_state is not None
                    state_completed(editor_state)

            self._publish_analysis_callback(
                publish_completion,
                operation_id=job.job_id,
            )
            self._notify_analysis(
                status,
                f"Automatisch erkannt: {result.cue_in:.2f}–{result.cue_out:.2f} s, "
                f"Konfidenz {result.confidence:.0%}",
            )

        job.future.add_done_callback(finished)

    def analysis_availability(self) -> tuple[bool, str]:
        if self._analysis_unavailable_reason:
            return False, self._analysis_unavailable_reason
        if self._analysis_service is None:
            return False, "Automatische Cue-Analyse ist nicht eingerichtet."
        if not self._analysis_service.is_available():
            return (
                False,
                "FFmpeg und FFprobe fehlen. Automatische Analyse ist nicht verfügbar.",
            )
        return True, "FFmpeg und FFprobe sind verfügbar."

    def cancel_analysis(self) -> None:
        if self._analysis_job is not None and not self._analysis_job.future.done():
            self._analysis_job.cancel()

    def analyze_catalog(
        self,
        progress: Callable[[int, int, int, int], None],
        completed: Callable[[int, int], None],
    ) -> None:
        self._require_analysis_available()
        tracks = self._catalog_tracks()
        self.analyze_tracks([track.id for track in tracks], progress, completed)

    def analyze_outdated_catalog(
        self,
        progress: Callable[[int, int, int, int], None],
        completed: Callable[[int, int], None],
    ) -> None:
        self._require_analysis_available()
        assert self._analysis_service is not None
        track_ids = [
            track.id
            for track in self._catalog_tracks()
            if self._analysis_service.needs_analysis(track.id)
        ]
        if not track_ids:
            raise ValueError("Alle Katalogtitel besitzen bereits die aktuelle Analyseversion.")
        self.analyze_tracks(track_ids, progress, completed)

    def analyze_tracks(
        self,
        track_ids: list[int],
        progress: Callable[[int, int, int, int], None],
        completed: Callable[[int, int], None],
    ) -> None:
        """Analyze selected tracks serially without filling the worker queue."""
        self._require_analysis_available()
        assert self._analysis_service is not None
        analysis_service = self._analysis_service
        if self._batch_analysis_running:
            raise RuntimeError("Eine Kataloganalyse läuft bereits.")
        tracks = [
            track
            for track_id in dict.fromkeys(track_ids)
            if (track := self._library.get_track(track_id)) is not None
        ]
        if not tracks:
            raise ValueError("Es wurden keine analysierbaren Katalogtitel ausgewählt.")
        self._batch_analysis_cancelled = False
        self._batch_analysis_running = True
        total = len(tracks)
        processed = succeeded = failed = 0

        def submit_next() -> None:
            nonlocal processed, succeeded, failed
            if self._batch_analysis_cancelled or processed >= total:
                self._batch_analysis_running = False
                completed(succeeded, failed)
                return
            track = tracks[processed]
            job = analysis_service.analyze(track)
            self._batch_analysis_job = job

            def worker_finished(future: Future[CueAnalysisResult]) -> None:
                def apply_result() -> None:
                    nonlocal processed, succeeded, failed
                    try:
                        future.result()
                        succeeded += 1
                    except CancelledError:
                        pass
                    except Exception:
                        failed += 1
                    processed += 1
                    progress(processed, total, succeeded, failed)
                    submit_next()

                self._publish_analysis_callback(apply_result, operation_id=job.job_id)

            job.future.add_done_callback(worker_finished)

        progress(0, total, 0, 0)
        submit_next()

    def cancel_batch_analysis(self) -> None:
        self._batch_analysis_cancelled = True
        if self._batch_analysis_job is not None and not self._batch_analysis_job.future.done():
            self._batch_analysis_job.cancel()

    def close(self, *, wait: bool = True) -> None:
        self.stop_preview()
        self.cancel_analysis()
        self.cancel_batch_analysis()
        self._preview_executor.shutdown(wait=wait, cancel_futures=True)
        if self._owns_persistence_executor:
            self._persistence_executor.shutdown(wait=False, cancel_futures=False)
        self._preview_future = None
        if self._analysis_service is not None:
            self._analysis_service.close(wait=wait)

    @property
    def active_preview_count(self) -> int:
        future = self._preview_future
        return int(future is not None and not future.done())

    def _start_preview(
        self,
        track_id: int,
        start: float,
        duration: float,
        status: Callable[[str], None] | None,
    ) -> None:
        if self._preview_backend_factory is None:
            raise RuntimeError("Vorhör-Audioausgabe ist nicht verfügbar.")
        backend_factory = self._preview_backend_factory
        self.stop_preview()
        self._preview_generation += 1
        generation = self._preview_generation
        self._preview_stop = Event()
        preview_stop = self._preview_stop
        track = self._track(track_id)
        self._notify(status, "Vorhördatei wird geladen …")

        def worker() -> None:
            backend: AudioBackend | None = None
            try:
                backend = backend_factory()
                with self._preview_lock:
                    if generation != self._preview_generation:
                        self._close_backend(backend)
                        return
                    self._preview_backend = backend
                backend.load(Path(track.file_path))
                backend.set_volume(0.5)
                backend.play()
                backend.seek(start)
                deadline = monotonic() + 3.0
                while monotonic() < deadline and not preview_stop.wait(0.05):
                    if backend.is_playing() and abs(backend.get_position() - start) <= 0.25:
                        break
                else:
                    raise RuntimeError("Vorhörposition wurde nicht rechtzeitig erreicht.")
                self._notify(status, f"Vorhören ab {start:.2f} Sekunden")
                preview_stop.wait(max(0.1, duration))
            except Exception as exc:
                self._notify(status, f"Vorhören fehlgeschlagen: {exc}")
            finally:
                with self._preview_lock:
                    if self._preview_backend is backend:
                        self._preview_backend = None
                if backend is not None:
                    self._close_backend(backend)
                if generation == self._preview_generation:
                    self._notify(status, "Vorhören beendet")

        worker_id = str(uuid4())
        operation_id = str(generation)
        self._worker_registry.started(
            WorkerInfo(worker_id, "cue-preview", "cue_preview", monotonic(), True, operation_id)
        )

        def tracked_worker() -> None:
            state = "completed"
            try:
                with self._performance.measure(
                    "worker.cue_preview",
                    warning_threshold_ms=3000.0,
                    context={"operation_id": operation_id},
                ):
                    worker()
            except Exception:
                state = "failed"
                raise
            finally:
                self._worker_registry.finished(worker_id, state)

        try:
            self._preview_future = self._preview_executor.submit(tracked_worker)
        except RuntimeError:
            self._worker_registry.finished(worker_id, "discarded")
            self._notify(status, "Vorhören ist noch mit dem vorherigen Auftrag beschäftigt")

    def _notify(self, callback: Callable[[str], None] | None, message: str) -> None:
        if callback is None:
            return
        if self._gui_dispatcher is not None:
            self._gui_dispatcher.publish(
                GuiEvent(
                    GuiEventType.CUE_PREVIEW_STATUS,
                    "cue_preview",
                    lambda: callback(message),
                    operation_id=str(self._preview_generation),
                    coalesce_key="cue-preview-status",
                )
            )
        else:
            callback(message)

    def _notify_analysis(self, callback: Callable[[str], None] | None, message: str) -> None:
        if callback is None:
            return
        self._publish_analysis_callback(lambda: callback(message))

    def _publish_analysis_callback(
        self,
        callback: Callable[[], None],
        *,
        operation_id: str | None = None,
    ) -> None:
        if self._gui_dispatcher is not None:
            self._gui_dispatcher.publish(
                GuiEvent(
                    GuiEventType.CALLBACK,
                    "cue_analysis",
                    callback,
                    operation_id=operation_id,
                )
            )
        else:
            callback()

    def _publish_gui_callback(self, callback: Callable[[], None], *, source: str) -> None:
        if self._gui_dispatcher is not None:
            self._gui_dispatcher.publish(GuiEvent(GuiEventType.CALLBACK, source, callback))
        elif self._schedule is not None:
            self._schedule(0, callback)
        else:
            callback()

    def _close_backend(self, backend: AudioBackend) -> None:
        try:
            backend.close()
        except (OSError, RuntimeError) as exc:
            self._logger.warning("Vorhör-Backend konnte nicht sauber schließen: %s", exc)

    def _track(self, track_id: int) -> Track:
        track = self._library.get_track(track_id)
        if track is None:
            raise ValueError("Titel wurde nicht gefunden.")
        return track

    def _catalog_tracks(self) -> list[Track]:
        track_count = self._library.count()
        tracks: list[Track] = []
        for offset in range(0, track_count, self._library.MAX_PAGE_SIZE):
            tracks.extend(self._library.page(self._library.MAX_PAGE_SIZE, offset))
        return tracks

    def _require_analysis_available(self) -> None:
        if self._analysis_unavailable_reason:
            raise RuntimeError(self._analysis_unavailable_reason)
        if self._analysis_service is None or not self._analysis_service.is_available():
            raise RuntimeError("FFmpeg/FFprobe ist für die Cue-Analyse nicht verfügbar.")
