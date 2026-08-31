"""Composition layer for the transactional track editor."""

from dataclasses import dataclass
from collections.abc import Callable
from math import isfinite
from typing import cast

from party_player.controllers.cue_point_controller import (
    CuePointController,
    CuePointEditorState,
)
from party_player.controllers.loudness_controller import LoudnessController, LoudnessEditorState
from party_player.models import Track
from party_player.metadata_editor import (
    MetadataEditorService,
    MetadataSaveResult,
    TrackMetadataChanges,
    TrackMetadataEditorViewModel,
    StagedSuggestionAction,
    ValueRemovalMode,
)
from party_player.metadata_rules import (
    MetadataFieldKey,
    RecordingClassification,
    RecordingKind,
    RecordingTrait,
    normalize_metadata_value,
)
from party_player.metadata_analysis_profiles import MetadataAnalysisProfile
from party_player.metadata_analysis_service import MetadataAnalysisService, TempoAnalysisView
from party_player.metadata_analysis_contracts import TempoAnalysisScope
from party_player.performance_monitor import PerformanceMonitor
from party_player.analysis import AudioFileInfo


@dataclass(frozen=True, slots=True)
class TrackEditorViewModel:
    """Read-only data needed by the phase-A editor."""

    track_id: int
    title: str
    artist: str
    album: str
    original_release_year: int | None
    file_path: str
    duration_seconds: float | None
    cue: CuePointEditorState
    loudness: LoudnessEditorState | None = None
    equalizer_preset_key: str | None = None
    equalizer_preset_name: str | None = None
    equalizer_source: str | None = None
    metadata: TrackMetadataEditorViewModel | None = None
    catalog_bpm: float | None = None

    @property
    def heading(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title

    @property
    def analysis_state(self) -> str:
        """Classify the persisted automatic cue suggestion for presentation."""
        cue = self.cue
        automatic = (
            cue.automatic_cue_in,
            cue.automatic_cue_out,
            cue.automatic_fade_duration,
        )
        if all(value is None for value in automatic):
            return "NONE"
        if any(value is None for value in automatic):
            return "INCOMPLETE"
        manual = (
            cue.manual_cue_in,
            cue.manual_cue_out,
            cue.manual_fade_duration,
        )
        return "ADOPTED" if manual == automatic else "SUGGESTED"

    @property
    def effective_play_duration(self) -> float:
        return max(0.0, self.cue.resolved.cue_out - self.cue.resolved.cue_in)


@dataclass(frozen=True, slots=True)
class TrackEditorChanges:
    """Cue changes collected by the dialog until the user saves."""

    cue_in: float | None
    cue_out: float | None
    fade_duration: float | None
    discard_automatic: bool = False


class TrackEditorController:
    """Compose existing editor services without duplicating their domain rules."""

    def __init__(
        self,
        cue_controller: CuePointController,
        loudness_controller: LoudnessController | None = None,
        equalizer_state: Callable[[Track], tuple[str | None, str | None, str]] | None = None,
        performance_monitor: PerformanceMonitor | None = None,
        metadata_service: MetadataEditorService | None = None,
        background_submit: (
            Callable[
                [Callable[[], object], Callable[[object], None], Callable[[Exception], None]],
                bool,
            ]
            | None
        ) = None,
        metadata_analysis: MetadataAnalysisService | None = None,
    ) -> None:
        self._cue = cue_controller
        self._loudness = loudness_controller
        self._equalizer_state = equalizer_state
        self._performance = performance_monitor or PerformanceMonitor()
        self._metadata = metadata_service
        self._background_submit = background_submit
        self._metadata_analysis = metadata_analysis

    def load_tempo_analysis_async(
        self,
        track_id: int,
        completed: Callable[[TempoAnalysisView], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        if self._metadata_analysis is None or self._background_submit is None:
            failed(RuntimeError("Tempoanalyse ist nicht verfügbar"))
            return False
        analysis = self._metadata_analysis

        def publish(value: object) -> None:
            completed(cast(TempoAnalysisView, value))

        return self._background_submit(lambda: analysis.latest_for_track(track_id), publish, failed)

    def load_technical_audio_info_async(
        self,
        track_id: int,
        completed: Callable[[AudioFileInfo], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        if self._metadata_analysis is None or self._background_submit is None:
            failed(RuntimeError("Technische Audiodaten sind nicht verfügbar"))
            return False
        analysis = self._metadata_analysis

        def publish(value: object) -> None:
            completed(cast(AudioFileInfo, value))

        return self._background_submit(
            lambda: analysis.technical_audio_info(track_id), publish, failed
        )

    def load_tempo_scope_async(
        self,
        track_id: int,
        completed: Callable[[tuple[TempoAnalysisView, TempoAnalysisView]], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        if self._metadata_analysis is None or self._background_submit is None:
            failed(RuntimeError("Tempoanalyse ist nicht verfügbar"))
            return False
        analysis = self._metadata_analysis

        def publish(value: object) -> None:
            completed(cast(tuple[TempoAnalysisView, TempoAnalysisView], value))

        return self._background_submit(
            lambda: (
                analysis.latest_for_track(track_id, TempoAnalysisScope.TRACK_DEFAULT_CUES),
                analysis.latest_for_track(track_id, TempoAnalysisScope.TRACK_FULL),
            ),
            publish,
            failed,
        )

    def load_tempo_diagnostics_async(
        self,
        track_id: int,
        completed: Callable[[str], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        if self._metadata_analysis is None or self._background_submit is None:
            failed(RuntimeError("Tempoanalyse ist nicht verfügbar"))
            return False
        analysis = self._metadata_analysis
        return self._background_submit(
            lambda: analysis.tempo_diagnostics_text(track_id),
            lambda value: completed(str(value)),
            failed,
        )

    def start_tempo_analysis_async(
        self,
        track_id: int,
        profile: MetadataAnalysisProfile,
        completed: Callable[[object], None],
        failed: Callable[[Exception], None],
        *,
        scope: TempoAnalysisScope = TempoAnalysisScope.TRACK_FULL,
    ) -> bool:
        if self._metadata_analysis is None or self._background_submit is None:
            failed(RuntimeError("Tempoanalyse ist nicht verfügbar"))
            return False
        analysis = self._metadata_analysis
        if analysis.active_job_count:
            failed(RuntimeError("Es läuft bereits eine Tempoanalyse."))
            return False
        reason = analysis.block_reason(batch=False)
        if reason:
            failed(RuntimeError(reason))
            return False
        return self._background_submit(
            lambda: analysis.analyze_track(track_id, profile, batch=False, scope=scope),
            completed,
            failed,
        )

    def cancel_tempo_analysis(self) -> None:
        if self._metadata_analysis is not None:
            self._metadata_analysis.cancel_current()

    def loudness_analysis_availability(self) -> tuple[bool, str]:
        """Describe whether this editor can analyze the current title."""
        if self._loudness is None:
            return False, "Lautheitsanalyse ist für diese Sitzung nicht verfügbar."
        return self._loudness.analysis_availability()

    def analyze_loudness(
        self,
        track_id: int,
        completed: Callable[[LoudnessEditorState], None],
        failed: Callable[[Exception], None],
    ) -> None:
        """Analyze one title and return refreshed editor state on the GUI thread."""
        if self._loudness is None:
            failed(RuntimeError("Lautheitsanalyse ist für diese Sitzung nicht verfügbar."))
            return
        loudness = self._loudness

        def analysis_completed(_result: object | None, error: str | None) -> None:
            if error is not None:
                failed(RuntimeError(error))
                return
            completed(loudness.state(track_id))

        loudness.analyze_track(track_id, analysis_completed)

    def build_view_model(self, track: Track) -> TrackEditorViewModel:
        with self._performance.measure(
            "track_editor.build_view_model",
            warning_threshold_ms=100.0,
            context={"track_id": track.id},
        ):
            return self._build_view_model(track)

    def _build_view_model(self, track: Track) -> TrackEditorViewModel:
        equalizer_key: str | None = None
        equalizer_name: str | None = None
        equalizer_source: str | None = None
        if self._equalizer_state is not None:
            with self._performance.measure(
                "track_editor.equalizer_resolve",
                warning_threshold_ms=25.0,
                context={"track_id": track.id},
            ):
                equalizer_key, equalizer_name, equalizer_source = self._equalizer_state(track)
        return TrackEditorViewModel(
            track_id=track.id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            original_release_year=track.original_release_year or track.year,
            file_path=track.file_path,
            duration_seconds=track.duration_seconds,
            cue=self._cue.state(track.id),
            loudness=self._loudness.state(track.id) if self._loudness is not None else None,
            equalizer_preset_key=equalizer_key,
            equalizer_preset_name=equalizer_name,
            equalizer_source=equalizer_source,
            catalog_bpm=track.bpm,
        )

    def load_metadata_async(
        self,
        track_id: int,
        completed: Callable[[TrackMetadataEditorViewModel], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        if self._metadata is None or self._background_submit is None:
            failed(RuntimeError("Metadatenpflege ist nicht verfügbar"))
            return False
        metadata = self._metadata

        def publish(value: object) -> None:
            completed(cast(TrackMetadataEditorViewModel, value))

        return self._background_submit(lambda: metadata.load(track_id), publish, failed)

    def save_metadata_async(
        self,
        track_id: int,
        changes: TrackMetadataChanges,
        completed: Callable[[MetadataSaveResult], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        if self._metadata is None or self._background_submit is None:
            failed(RuntimeError("Metadatenpflege ist nicht verfügbar"))
            return False
        metadata = self._metadata

        def publish(value: object) -> None:
            completed(cast(MetadataSaveResult, value))

        return self._background_submit(lambda: metadata.save(track_id, changes), publish, failed)

    @staticmethod
    def with_metadata(
        view_model: TrackEditorViewModel,
        metadata: TrackMetadataEditorViewModel,
    ) -> TrackEditorViewModel:
        title = metadata.field(MetadataFieldKey.TITLE).value
        artist = metadata.field(MetadataFieldKey.ARTIST).value
        album = metadata.field(MetadataFieldKey.ALBUM).value
        original_year = metadata.field(MetadataFieldKey.ORIGINAL_RELEASE_YEAR).value
        return TrackEditorViewModel(
            view_model.track_id,
            str(title or ""),
            str(artist or ""),
            str(album or ""),
            original_year if isinstance(original_year, int) else None,
            view_model.file_path,
            view_model.duration_seconds,
            view_model.cue,
            view_model.loudness,
            view_model.equalizer_preset_key,
            view_model.equalizer_preset_name,
            view_model.equalizer_source,
            metadata,
        )

    @staticmethod
    def build_metadata_changes(
        model: TrackMetadataEditorViewModel,
        scalar_inputs: dict[MetadataFieldKey, str],
        recording_kind: RecordingKind,
        remastered: bool,
        multivalue_inputs: dict[MetadataFieldKey, str],
        confirmations: frozenset[MetadataFieldKey],
        removals: dict[MetadataFieldKey, ValueRemovalMode],
        suggestion_actions: tuple[StagedSuggestionAction, ...],
    ) -> TrackMetadataChanges:
        scalar_values: dict[MetadataFieldKey, object] = {}
        for key, raw in scalar_inputs.items():
            original = model.field(key).value
            parsed = TrackEditorController._parse_metadata_text(key, raw)
            normalized = normalize_metadata_value(key, parsed)
            original_normalized = normalize_metadata_value(key, original)
            if normalized != original_normalized:
                if normalized is None:
                    if key not in removals:
                        raise ValueError(
                            f"Für „{key.value}“ muss Löschen oder „Ohne Wert bestätigen“ gewählt werden."
                        )
                else:
                    scalar_values[key] = normalized
        recording = RecordingClassification(
            recording_kind,
            frozenset({RecordingTrait.REMASTERED}) if remastered else frozenset(),
        )
        if recording != model.field(MetadataFieldKey.RECORDING_CLASSIFICATION).value:
            scalar_values[MetadataFieldKey.RECORDING_CLASSIFICATION] = recording
        multivalues: dict[MetadataFieldKey, tuple[object, ...]] = {}
        for key, raw in multivalue_inputs.items():
            values = tuple(
                part.strip()
                for line in raw.splitlines()
                for part in line.split(",")
                if part.strip()
            )
            if key is MetadataFieldKey.MUSICAL_DECADES:
                parsed_values: tuple[object, ...] = tuple(int(value) for value in values)
            else:
                parsed_values = values
            normalized_values = normalize_metadata_value(key, parsed_values)
            assert isinstance(normalized_values, tuple)
            if normalized_values != model.field(key).value:
                if not normalized_values and key not in removals:
                    raise ValueError(
                        f"Für „{key.value}“ muss eine Löschentscheidung getroffen werden."
                    )
                if normalized_values:
                    multivalues[key] = normalized_values
        actual_confirmations = frozenset(
            key
            for key in confirmations
            if key not in scalar_values and key not in multivalues and key not in removals
        )
        return TrackMetadataChanges(
            model.revision,
            scalar_values,
            multivalues,
            actual_confirmations,
            removals,
            suggestion_actions,
        )

    @staticmethod
    def _parse_metadata_text(key: MetadataFieldKey, raw: str) -> object:
        text = raw.strip()
        if not text:
            return None
        if key in {
            MetadataFieldKey.YEAR,
            MetadataFieldKey.ORIGINAL_RELEASE_YEAR,
            MetadataFieldKey.ENERGY,
            MetadataFieldKey.DANCEABILITY,
            MetadataFieldKey.RATING,
        }:
            try:
                return int(text)
            except ValueError as exc:
                raise ValueError(f"„{key.value}“ benötigt eine ganze Zahl.") from exc
        if key in {MetadataFieldKey.BPM, MetadataFieldKey.ALTERNATIVE_BPM}:
            try:
                return float(text.replace(",", "."))
            except ValueError as exc:
                raise ValueError(f"„{key.value}“ ist keine gültige Zahl.") from exc
        return text

    def validate_changes(
        self,
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
    ) -> TrackEditorChanges:
        cue_in = self._finite_or_none(changes.cue_in, "Cue In")
        cue_out = self._finite_or_none(changes.cue_out, "Cue Out")
        fade = self._finite_or_none(changes.fade_duration, "Überblenddauer")
        if cue_in is not None and cue_in < 0:
            raise ValueError("Cue In darf nicht negativ sein.")
        if cue_out is not None and cue_out < 0:
            raise ValueError("Cue Out darf nicht negativ sein.")
        duration = view_model.duration_seconds
        if duration is not None and cue_out is not None and cue_out > duration:
            raise ValueError("Cue Out darf nicht hinter dem Dateiende liegen.")
        effective_in = cue_in if cue_in is not None else view_model.cue.resolved.cue_in
        effective_out = cue_out if cue_out is not None else view_model.cue.resolved.cue_out
        if effective_out <= effective_in:
            raise ValueError("Cue Out muss hinter Cue In liegen.")
        if fade is not None:
            if fade < 0:
                raise ValueError("Die Überblenddauer darf nicht negativ sein.")
            if fade > effective_out - effective_in:
                raise ValueError(
                    "Die Überblenddauer darf nicht länger als der hörbare Titelbereich sein."
                )
        return TrackEditorChanges(cue_in, cue_out, fade, changes.discard_automatic)

    def save(
        self,
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
    ) -> TrackEditorViewModel:
        validated = self.validate_changes(view_model, changes)
        if not self.has_cue_changes(view_model, validated):
            return view_model
        cue = self._cue.save(
            view_model.track_id,
            validated.cue_in,
            validated.cue_out,
            validated.fade_duration,
        )
        return TrackEditorViewModel(
            track_id=view_model.track_id,
            title=view_model.title,
            artist=view_model.artist,
            album=view_model.album,
            original_release_year=view_model.original_release_year,
            file_path=view_model.file_path,
            duration_seconds=view_model.duration_seconds,
            cue=cue,
            loudness=view_model.loudness,
            equalizer_preset_key=view_model.equalizer_preset_key,
            equalizer_preset_name=view_model.equalizer_preset_name,
            equalizer_source=view_model.equalizer_source,
            metadata=view_model.metadata,
        )

    def save_async(
        self,
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
        completed: Callable[[TrackEditorViewModel], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        """Validate immediately and persist changed cue values outside the GUI thread."""
        with self._performance.measure(
            "track_editor.save",
            warning_threshold_ms=25.0,
            context={"track_id": view_model.track_id},
        ):
            with self._performance.measure(
                "track_editor.save.validate",
                warning_threshold_ms=10.0,
                context={"track_id": view_model.track_id},
            ):
                validated = self.validate_changes(view_model, changes)
            with self._performance.measure(
                "track_editor.save.submit",
                warning_threshold_ms=10.0,
                context={"track_id": view_model.track_id},
            ):
                return self._submit_save(view_model, validated, completed, failed)

    def _submit_save(
        self,
        view_model: TrackEditorViewModel,
        validated: TrackEditorChanges,
        completed: Callable[[TrackEditorViewModel], None],
        failed: Callable[[Exception], None],
    ) -> bool:
        if not self.has_cue_changes(view_model, validated):
            completed(view_model)
            return False

        def cue_saved(cue: CuePointEditorState) -> None:
            completed(
                TrackEditorViewModel(
                    track_id=view_model.track_id,
                    title=view_model.title,
                    artist=view_model.artist,
                    album=view_model.album,
                    original_release_year=view_model.original_release_year,
                    file_path=view_model.file_path,
                    duration_seconds=view_model.duration_seconds,
                    cue=cue,
                    loudness=view_model.loudness,
                    equalizer_preset_key=view_model.equalizer_preset_key,
                    equalizer_preset_name=view_model.equalizer_preset_name,
                    equalizer_source=view_model.equalizer_source,
                    metadata=view_model.metadata,
                )
            )

        self._cue.save_async(
            view_model.track_id,
            validated.cue_in,
            validated.cue_out,
            validated.fade_duration,
            cue_saved,
            failed,
            discard_automatic=validated.discard_automatic,
            changed_fields=self.changed_cue_fields(view_model, validated),
        )
        return True

    def record_event(self, operation: str) -> None:
        """Record a path-free editor lifecycle counter."""
        self._performance.record(operation, 1.0, float("inf"))

    def record_duration(
        self,
        operation: str,
        elapsed_ms: float,
        *,
        warning_threshold_ms: float = 50.0,
    ) -> None:
        """Record GUI work that is deliberately split across event-loop turns."""
        self._performance.record(operation, elapsed_ms, warning_threshold_ms)

    @staticmethod
    def has_cue_changes(
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
    ) -> bool:
        """Return whether the editable cue values differ from their loaded baseline."""
        cue = view_model.cue
        return (
            changes.cue_in != cue.manual_cue_in
            or changes.cue_out != cue.manual_cue_out
            or changes.fade_duration != cue.manual_fade_duration
            or changes.discard_automatic
            and view_model.analysis_state != "NONE"
        )

    @staticmethod
    def changed_cue_fields(
        view_model: TrackEditorViewModel,
        changes: TrackEditorChanges,
    ) -> frozenset[str]:
        """Return only manual columns whose values differ from the baseline."""
        cue = view_model.cue
        return frozenset(
            field
            for field, changed, original in (
                ("cue_in", changes.cue_in, cue.manual_cue_in),
                ("cue_out", changes.cue_out, cue.manual_cue_out),
                ("fade_duration", changes.fade_duration, cue.manual_fade_duration),
            )
            if changed != original
        )

    def automatic_suggestion(
        self,
        view_model: TrackEditorViewModel,
    ) -> TrackEditorChanges:
        """Return a stored automatic suggestion without persisting it as manual data."""
        cue = view_model.cue
        if (
            cue.automatic_cue_in is None
            or cue.automatic_cue_out is None
            or cue.automatic_fade_duration is None
        ):
            raise ValueError("Für diesen Titel liegt kein vollständiger Vorschlag vor.")
        return self.validate_changes(
            view_model,
            TrackEditorChanges(
                cue.automatic_cue_in,
                cue.automatic_cue_out,
                cue.automatic_fade_duration,
            ),
        )

    @staticmethod
    def parse_optional_seconds(raw: str, label: str) -> float | None:
        """Parse an optional German UI seconds value without inventing zero."""
        normalized = raw.strip().replace("−", "-")
        if not normalized:
            return None
        try:
            return float(normalized.replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{label} ist keine gültige Zahl. Beispiele: 12,5 oder 12.5.") from exc

    @staticmethod
    def _finite_or_none(value: float | None, label: str) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError(f"{label} muss eine endliche Zahl sein.")
        return value
