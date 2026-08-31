"""Safe DeckRelay settings persistence."""

import logging
import math
import json
from dataclasses import dataclass

from party_player.enums import PlayerMode
from party_player.presentation import (
    PresentationPreference,
    Workspace,
    presentation_preference,
    workspace,
)
from party_player.repository import PartyPlayerRepository
from party_player.system_dependencies import DependencySelectionMode


@dataclass(frozen=True, slots=True)
class DependencySettings:
    vlc_installation_path: str | None
    vlc_selection_mode: DependencySelectionMode
    ffmpeg_bin_path: str | None
    ffmpeg_selection_mode: DependencySelectionMode
    first_run_completed: bool
    system_check_completed_version: str | None


class SettingsService:
    def __init__(self, repository: PartyPlayerRepository) -> None:
        self._repository = repository
        self._logger = logging.getLogger(__name__)

    def automatic_deck_loading(self, default: bool = True) -> bool:
        value = self._repository.get_setting("automatic_deck_loading")
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        self._logger.warning(
            "Ungültige Einstellung automatic_deck_loading=%r; Standardwert wird verwendet",
            value,
        )
        return default

    def set_automatic_deck_loading(self, enabled: bool) -> None:
        self._repository.set_setting("automatic_deck_loading", "true" if enabled else "false")

    def player_mode(self, default: PlayerMode = PlayerMode.SEMI_AUTOMATIC) -> PlayerMode:
        value = self._repository.get_setting("player_mode")
        if value is None:
            return (
                PlayerMode.SEMI_AUTOMATIC
                if self.automatic_deck_loading(default == PlayerMode.SEMI_AUTOMATIC)
                else PlayerMode.MANUAL
            )
        try:
            mode = PlayerMode(value)
        except ValueError:
            self._logger.warning("Ungültiger Player-Modus %r; Standardwert wird verwendet", value)
            return default
        return mode

    def set_player_mode(self, mode: PlayerMode) -> None:
        self._repository.set_setting("player_mode", mode.value)
        self.set_automatic_deck_loading(mode != PlayerMode.MANUAL)

    def queue_duplicate_policy(self, default: str = "allow") -> str:
        value = self._repository.get_setting("queue_duplicate_policy")
        if value in {"allow", "prevent"}:
            return value
        if value is not None:
            self._logger.warning(
                "Ungültige Queue-Duplikatregel %r; Standardwert wird verwendet", value
            )
        return default

    def set_queue_duplicate_policy(self, policy: str) -> None:
        if policy not in {"allow", "prevent"}:
            raise ValueError("Unbekannte Queue-Duplikatregel")
        self._repository.set_setting("queue_duplicate_policy", policy)

    def emergency_track_ids(self) -> list[int]:
        value = self._repository.get_setting("emergency_track_ids") or ""
        result: list[int] = []
        for part in value.split(","):
            try:
                track_id = int(part.strip())
            except ValueError:
                continue
            if track_id > 0 and track_id not in result:
                result.append(track_id)
        return result

    def set_emergency_track_ids(self, track_ids: list[int]) -> None:
        normalized = list(dict.fromkeys(track_id for track_id in track_ids if track_id > 0))
        self._repository.set_setting(
            "emergency_track_ids",
            ",".join(str(track_id) for track_id in normalized),
        )

    def emergency_preload_primary(self, default: bool = False) -> bool:
        return self._bool_setting("emergency_preload_primary", default)

    def set_emergency_preload_primary(self, enabled: bool) -> None:
        self._repository.set_setting("emergency_preload_primary", "true" if enabled else "false")

    def emergency_local_ssd_roots(self) -> list[str]:
        value = self._repository.get_setting("emergency_local_ssd_roots") or "[]"
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        if not isinstance(decoded, list):
            return []
        return list(
            dict.fromkeys(
                item.strip() for item in decoded if isinstance(item, str) and item.strip()
            )
        )

    def set_emergency_local_ssd_roots(self, roots: list[str]) -> None:
        normalized = list(dict.fromkeys(root.strip() for root in roots if root.strip()))
        self._repository.set_setting("emergency_local_ssd_roots", json.dumps(normalized))

    def emergency_approved_removable_roots(self) -> list[str]:
        value = self._repository.get_setting("emergency_approved_removable_roots") or "[]"
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        if not isinstance(decoded, list):
            return []
        return list(
            dict.fromkeys(
                item.strip() for item in decoded if isinstance(item, str) and item.strip()
            )
        )

    def set_emergency_approved_removable_roots(self, roots: list[str]) -> None:
        normalized = list(dict.fromkeys(root.strip() for root in roots if root.strip()))
        self._repository.set_setting("emergency_approved_removable_roots", json.dumps(normalized))

    def emergency_media_track_ids(self, media_type: str) -> list[int]:
        normalized = media_type.strip().upper()
        if normalized == "PRIMARY":
            return self.emergency_track_ids()
        if normalized not in {"BREAK_MUSIC", "JINGLE", "ANNOUNCEMENT"}:
            raise ValueError("Unbekannter Notfallmedientyp")
        value = self._repository.get_setting(f"emergency_{normalized.casefold()}_track_ids") or ""
        result: list[int] = []
        for part in value.split(","):
            try:
                track_id = int(part.strip())
            except ValueError:
                continue
            if track_id > 0 and track_id not in result:
                result.append(track_id)
        return result

    def set_emergency_media_track_ids(self, media_type: str, track_ids: list[int]) -> None:
        normalized = media_type.strip().upper()
        if normalized == "PRIMARY":
            self.set_emergency_track_ids(track_ids)
            return
        if normalized not in {"BREAK_MUSIC", "JINGLE", "ANNOUNCEMENT"}:
            raise ValueError("Unbekannter Notfallmedientyp")
        unique_ids = list(dict.fromkeys(track_id for track_id in track_ids if track_id > 0))
        self._repository.set_setting(
            f"emergency_{normalized.casefold()}_track_ids",
            ",".join(str(track_id) for track_id in unique_ids),
        )

    def queue_stats_use_effective_cues(self, default: bool = False) -> bool:
        return self._bool_setting("queue_stats_use_effective_cues", default)

    def set_queue_stats_use_effective_cues(self, enabled: bool) -> None:
        self._repository.set_setting(
            "queue_stats_use_effective_cues", "true" if enabled else "false"
        )

    def workspace_catalog_ratio(self, default: float = 0.5) -> float:
        """Return the persisted catalog share of the resizable list workspace."""
        value = self._repository.get_setting("workspace_catalog_ratio")
        try:
            ratio = float(value) if value is not None else float(default)
        except ValueError:
            ratio = float(default)
        return min(0.8, max(0.2, ratio))

    def set_workspace_catalog_ratio(self, ratio: float) -> None:
        normalized = min(0.8, max(0.2, float(ratio)))
        self._repository.set_setting("workspace_catalog_ratio", f"{normalized:.4f}")

    def main_window_geometry(self) -> str | None:
        """Return the raw geometry record; the display model validates its contents."""
        return self._optional_text_setting("main_window_geometry")

    def set_main_window_geometry(self, geometry: str) -> None:
        self._repository.set_setting("main_window_geometry", geometry.strip())

    def presentation_preference(self) -> PresentationPreference:
        """Return the global preference, never a temporarily resolved mode."""
        return presentation_preference(self._repository.get_setting("presentation_preference"))

    def set_presentation_preference(self, preference: PresentationPreference) -> None:
        self._repository.set_setting("presentation_preference", preference.value)

    def presentation_workspace(self) -> Workspace:
        return workspace(self._repository.get_setting("presentation_workspace"))

    def set_presentation_workspace(self, selected: Workspace) -> None:
        self._repository.set_setting("presentation_workspace", selected.value)

    def queue_artist_repetition_enabled(self, default: bool = True) -> bool:
        return self._bool_setting("queue_artist_repetition_enabled", default)

    def set_queue_artist_repetition_enabled(self, enabled: bool) -> None:
        self._repository.set_setting(
            "queue_artist_repetition_enabled", "true" if enabled else "false"
        )

    def master_volume(self, default: float = 0.8) -> float:
        return self._float_setting("master_volume", default, 0.0, 1.0)

    def set_master_volume(self, value: float) -> None:
        self._set_float_setting("master_volume", value, 0.0, 1.0)

    def crossfader_position(self, default: float = 0.5) -> float:
        return self._float_setting("crossfader_position", default, 0.0, 1.0)

    def set_crossfader_position(self, value: float) -> None:
        self._set_float_setting("crossfader_position", value, 0.0, 1.0)

    def deck_volume(self, deck_id: str, default: float = 1.0) -> float:
        return self._float_setting(self._deck_volume_key(deck_id), default, 0.0, 1.0)

    def set_deck_volume(self, deck_id: str, value: float) -> None:
        self._set_float_setting(self._deck_volume_key(deck_id), value, 0.0, 1.0)

    def fade_duration(self, default: float = 5.0) -> float:
        return self._float_setting("fade_duration", default, 1.0, 30.0)

    def set_fade_duration(self, value: float) -> None:
        self._set_float_setting("fade_duration", value, 1.0, 30.0)

    def minimum_fade_duration(self, default: float = 0.5) -> float:
        return self._float_setting("minimum_fade_duration", default, 0.25, 5.0)

    def set_minimum_fade_duration(self, value: float) -> None:
        self._set_float_setting("minimum_fade_duration", value, 0.25, 5.0)

    def minimum_playable_duration(self, default: float = 5.0) -> float:
        return self._float_setting("minimum_playable_duration", default, 1.0, 30.0)

    def set_minimum_playable_duration(self, value: float) -> None:
        self._set_float_setting("minimum_playable_duration", value, 1.0, 30.0)

    def played_ratio_threshold(self, default: float = 0.5) -> float:
        return self._float_setting("played_ratio_threshold", default, 0.0, 1.0)

    def set_played_ratio_threshold(self, value: float) -> None:
        self._set_float_setting("played_ratio_threshold", value, 0.0, 1.0)

    def played_seconds_threshold(self, default: float = 120.0) -> float:
        return self._float_setting("played_seconds_threshold", default, 0.0, 3600.0)

    def set_played_seconds_threshold(self, value: float) -> None:
        self._set_float_setting("played_seconds_threshold", value, 0.0, 3600.0)

    def repetition_partial_ratio_threshold(self, default: float = 0.5) -> float:
        return self._float_setting("repetition_partial_ratio_threshold", default, 0.0, 1.0)

    def set_repetition_partial_ratio_threshold(self, value: float) -> None:
        self._set_float_setting("repetition_partial_ratio_threshold", value, 0.0, 1.0)

    def fade_out_stops_deck(self, default: bool = False) -> bool:
        return self._bool_setting("fade_out_stops_deck", default)

    def set_fade_out_stops_deck(self, enabled: bool) -> None:
        self._repository.set_setting("fade_out_stops_deck", "true" if enabled else "false")

    def restore_last_session(self, default: bool = True) -> bool:
        return self._bool_setting("restore_last_session", default)

    def set_restore_last_session(self, enabled: bool) -> None:
        self._repository.set_setting("restore_last_session", "true" if enabled else "false")

    def fullscreen_on_start(self, default: bool = False) -> bool:
        return self._bool_setting("fullscreen_on_start", default)

    def set_fullscreen_on_start(self, enabled: bool) -> None:
        self._repository.set_setting("fullscreen_on_start", "true" if enabled else "false")

    def file_browser_enabled(self, default: bool = True) -> bool:
        return self._bool_setting("file_browser_enabled", default)

    def set_file_browser_enabled(self, enabled: bool) -> None:
        self._repository.set_setting("file_browser_enabled", "true" if enabled else "false")

    def audio_backend(self, default: str = "vlc") -> str:
        value = self._repository.get_setting("audio_backend")
        if value == "vlc":
            return value
        if value is not None:
            self._logger.warning("Unbekanntes Audio-Backend %r; VLC wird verwendet", value)
        return default

    def set_audio_backend(self, backend: str) -> None:
        if backend != "vlc":
            raise ValueError("Derzeit wird ausschließlich das Audio-Backend VLC unterstützt")
        self._repository.set_setting("audio_backend", backend)

    def audio_output_device(self, default: str = "") -> str:
        value = self._repository.get_setting("audio_output_device")
        return value.strip() if value is not None else default

    def set_audio_output_device(self, device_id: str) -> None:
        self._repository.set_setting("audio_output_device", device_id.strip())

    def dependency_settings(self) -> DependencySettings:
        return DependencySettings(
            self.vlc_installation_path(),
            self.vlc_selection_mode(),
            self.ffmpeg_bin_path(),
            self.ffmpeg_selection_mode(),
            self.first_run_completed(),
            self.system_check_completed_version(),
        )

    def vlc_installation_path(self) -> str | None:
        return self._optional_text_setting("vlc_installation_path")

    def set_vlc_installation_path(self, directory: str) -> None:
        normalized = directory.strip()
        if not normalized:
            raise ValueError("Das VLC-Installationsverzeichnis darf nicht leer sein")
        with self._repository.transaction():
            self._repository.set_setting("vlc_installation_path", normalized)
            self.set_vlc_selection_mode(DependencySelectionMode.USER)

    def vlc_selection_mode(self) -> DependencySelectionMode:
        return self._dependency_selection_mode("vlc_selection_mode")

    def set_vlc_selection_mode(self, mode: DependencySelectionMode) -> None:
        self._repository.set_setting("vlc_selection_mode", mode.value)

    def reset_vlc_installation_path(self) -> None:
        with self._repository.transaction():
            self._repository.set_setting("vlc_installation_path", "")
            self.set_vlc_selection_mode(DependencySelectionMode.AUTO)

    def ffmpeg_bin_path(self) -> str | None:
        return self._optional_text_setting("ffmpeg_bin_path")

    def set_ffmpeg_bin_path(self, directory: str) -> None:
        normalized = directory.strip()
        if not normalized:
            raise ValueError("Das FFmpeg-bin-Verzeichnis darf nicht leer sein")
        with self._repository.transaction():
            self._repository.set_setting("ffmpeg_bin_path", normalized)
            self.set_ffmpeg_selection_mode(DependencySelectionMode.USER)

    def ffmpeg_selection_mode(self) -> DependencySelectionMode:
        return self._dependency_selection_mode("ffmpeg_selection_mode")

    def set_ffmpeg_selection_mode(self, mode: DependencySelectionMode) -> None:
        self._repository.set_setting("ffmpeg_selection_mode", mode.value)

    def reset_ffmpeg_bin_path(self) -> None:
        with self._repository.transaction():
            self._repository.set_setting("ffmpeg_bin_path", "")
            self.set_ffmpeg_selection_mode(DependencySelectionMode.AUTO)

    def first_run_completed(self, default: bool = False) -> bool:
        return self._bool_setting("first_run_completed", default)

    def set_first_run_completed(self, completed: bool) -> None:
        self._repository.set_setting("first_run_completed", "true" if completed else "false")

    def system_check_completed_version(self) -> str | None:
        return self._optional_text_setting("system_check_completed_version")

    def set_system_check_completed_version(self, version: str | None) -> None:
        self._repository.set_setting(
            "system_check_completed_version", version.strip() if version else ""
        )

    def emergency_action_profile(self, default: str = "PLAY_EMERGENCY") -> str:
        value = self._repository.get_setting("emergency_action_profile")
        allowed = {"MUTE_ALL", "STOP_ALL", "PLAY_EMERGENCY", "SAFE_RESET"}
        return value if value in allowed else default

    def set_emergency_action_profile(self, profile: str) -> None:
        normalized = profile.strip().upper()
        if normalized not in {"MUTE_ALL", "STOP_ALL", "PLAY_EMERGENCY", "SAFE_RESET"}:
            raise ValueError("Unbekanntes Notfallprofil")
        self._repository.set_setting("emergency_action_profile", normalized)

    def normalization_enabled(self, default: bool = True) -> bool:
        return self._bool_setting("normalization_enabled", default)

    def set_normalization_enabled(self, enabled: bool) -> None:
        self._repository.set_setting("normalization_enabled", "true" if enabled else "false")

    def clip_protection_enabled(self, default: bool = True) -> bool:
        return self._bool_setting("clip_protection_enabled", default)

    def set_clip_protection_enabled(self, enabled: bool) -> None:
        self._repository.set_setting("clip_protection_enabled", "true" if enabled else "false")

    def performance_diagnostics_enabled(self, default: bool = True) -> bool:
        return self._bool_setting("performance_diagnostics_enabled", default)

    def set_performance_diagnostics_enabled(self, enabled: bool) -> None:
        self._repository.set_setting(
            "performance_diagnostics_enabled", "true" if enabled else "false"
        )

    def safety_backup_retention_limit(self, default: int = 10) -> int:
        value = self._repository.get_setting("safety_backup_retention_limit")
        if value is None:
            return default
        try:
            parsed = int(value)
        except ValueError:
            parsed = default
        if not 1 <= parsed <= 1000:
            self._logger.warning(
                "Ungültige Safety-Backup-Retention %r; Standardwert wird verwendet",
                value,
            )
            return default
        return parsed

    def set_safety_backup_retention_limit(self, limit: int) -> None:
        if not 1 <= limit <= 1000:
            raise ValueError("Safety-Backup-Retention muss zwischen 1 und 1000 liegen.")
        self._repository.set_setting("safety_backup_retention_limit", str(limit))

    def last_manual_backup(self) -> tuple[str, str] | None:
        value = self._repository.get_setting("last_manual_backup")
        if not value:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        if not isinstance(decoded, dict) or set(decoded) != {"created_at", "path"}:
            return None
        created_at, path = decoded["created_at"], decoded["path"]
        if not isinstance(created_at, str) or not isinstance(path, str) or not path:
            return None
        return created_at, path

    def set_last_manual_backup(self, created_at: str, path: str) -> None:
        if not created_at.strip() or not path.strip():
            raise ValueError("Backupzeit und -pfad dürfen nicht leer sein.")
        self._repository.set_setting(
            "last_manual_backup",
            json.dumps({"created_at": created_at, "path": path}, ensure_ascii=False),
        )

    def background_analysis_enabled(self, default: bool = True) -> bool:
        return self._bool_setting("background_analysis_enabled", default)

    def set_background_analysis_enabled(self, enabled: bool) -> None:
        self._repository.set_setting("background_analysis_enabled", "true" if enabled else "false")

    def global_equalizer_preset(self, default: str | None = None) -> str | None:
        value = self._repository.get_setting("global_equalizer_preset")
        normalized = value.strip() if value is not None else ""
        return normalized or default

    def set_global_equalizer_preset(self, preset_key: str | None) -> None:
        self._repository.set_setting(
            "global_equalizer_preset", preset_key.strip() if preset_key else ""
        )

    def cue_analysis_edge_window_seconds(self, default: float = 45.0) -> float:
        return self._float_setting("cue_analysis_edge_window_seconds", default, 1.0, 60.0)

    def set_cue_analysis_edge_window_seconds(self, value: float) -> None:
        self._set_float_setting("cue_analysis_edge_window_seconds", value, 1.0, 60.0)

    def cue_analysis_level_window_seconds(self, default: float = 0.1) -> float:
        return self._float_setting("cue_analysis_level_window_seconds", default, 0.01, 1.0)

    def set_cue_analysis_level_window_seconds(self, value: float) -> None:
        self._set_float_setting("cue_analysis_level_window_seconds", value, 0.01, 1.0)

    def cue_analysis_signal_on_dbfs(self, default: float = -45.0) -> float:
        return self._float_setting("cue_analysis_signal_on_dbfs", default, -100.0, -1.0)

    def set_cue_analysis_signal_on_dbfs(self, value: float) -> None:
        self._set_float_setting("cue_analysis_signal_on_dbfs", value, -100.0, -1.0)

    def cue_analysis_signal_off_dbfs(self, default: float = -50.0) -> float:
        return self._float_setting("cue_analysis_signal_off_dbfs", default, -120.0, -2.0)

    def set_cue_analysis_signal_off_dbfs(self, value: float) -> None:
        self._set_float_setting("cue_analysis_signal_off_dbfs", value, -120.0, -2.0)

    def cue_analysis_thresholds(
        self, default_on: float = -45.0, default_off: float = -50.0
    ) -> tuple[float, float]:
        signal_on = self.cue_analysis_signal_on_dbfs(default_on)
        signal_off = self.cue_analysis_signal_off_dbfs(default_off)
        if signal_on > signal_off:
            return signal_on, signal_off
        self._logger.warning(
            "Ungültige Cue-Analyse-Hysterese on=%s, off=%s; Standardwerte werden verwendet",
            signal_on,
            signal_off,
        )
        return default_on, default_off

    def set_cue_analysis_thresholds(self, signal_on: float, signal_off: float) -> None:
        if not signal_on > signal_off:
            raise ValueError("Die Einschaltschwelle muss über der Ausschaltschwelle liegen")
        self.set_cue_analysis_signal_on_dbfs(signal_on)
        self.set_cue_analysis_signal_off_dbfs(signal_off)

    def cue_analysis_minimum_signal_seconds(self, default: float = 0.5) -> float:
        return self._float_setting("cue_analysis_minimum_signal_seconds", default, 0.01, 30.0)

    def set_cue_analysis_minimum_signal_seconds(self, value: float) -> None:
        self._set_float_setting("cue_analysis_minimum_signal_seconds", value, 0.01, 30.0)

    def cue_analysis_minimum_silence_seconds(self, default: float = 0.3) -> float:
        return self._float_setting("cue_analysis_minimum_silence_seconds", default, 0.01, 30.0)

    def set_cue_analysis_minimum_silence_seconds(self, value: float) -> None:
        self._set_float_setting("cue_analysis_minimum_silence_seconds", value, 0.01, 30.0)

    def normalization_mode(self, default: str = "TRACK") -> str:
        value = self._repository.get_setting("normalization_mode")
        if value in {"OFF", "TRACK", "ALBUM"}:
            return value
        if value is not None:
            self._logger.warning("Ungültiger Normalisierungsmodus %r", value)
        return default

    def set_normalization_mode(self, mode: str) -> None:
        if mode not in {"OFF", "TRACK", "ALBUM"}:
            raise ValueError("Unbekannter Normalisierungsmodus")
        self._repository.set_setting("normalization_mode", mode)

    def gain_smoothing_seconds(self, default: float = 0.5) -> float:
        return self._float_setting("gain_smoothing_seconds", default, 0.05, 10.0)

    def set_gain_smoothing_seconds(self, value: float) -> None:
        self._set_float_setting("gain_smoothing_seconds", value, 0.05, 10.0)

    def target_loudness(self, default: float = -14.0) -> float:
        return self._float_setting("target_loudness_lufs", default, -23.0, -10.0)

    def set_target_loudness(self, value: float) -> None:
        self._set_float_setting("target_loudness_lufs", value, -23.0, -10.0)

    def maximum_positive_gain(self, default: float = 8.0) -> float:
        return self._float_setting("maximum_positive_gain_db", default, 0.0, 12.0)

    def set_maximum_positive_gain(self, value: float) -> None:
        self._set_float_setting("maximum_positive_gain_db", value, 0.0, 12.0)

    def maximum_negative_gain(self, default: float = -12.0) -> float:
        return self._float_setting("maximum_negative_gain_db", default, -24.0, 0.0)

    def set_maximum_negative_gain(self, value: float) -> None:
        self._set_float_setting("maximum_negative_gain_db", value, -24.0, 0.0)

    def maximum_output_peak(self, default: float = 0.0) -> float:
        return self._float_setting("maximum_output_peak_db", default, -6.0, 0.0)

    def set_maximum_output_peak(self, value: float) -> None:
        self._set_float_setting("maximum_output_peak_db", value, -6.0, 0.0)

    def headroom(self, default: float = 1.0) -> float:
        return self._float_setting("headroom_db", default, 0.0, 6.0)

    def set_headroom(self, value: float) -> None:
        self._set_float_setting("headroom_db", value, 0.0, 6.0)

    def fallback_positive_gain(self, default: float = 3.0) -> float:
        return self._float_setting("fallback_positive_gain_db", default, 0.0, 6.0)

    def set_fallback_positive_gain(self, value: float) -> None:
        self._set_float_setting("fallback_positive_gain_db", value, 0.0, 6.0)

    def _float_setting(self, key: str, default: float, minimum: float, maximum: float) -> float:
        value = self._repository.get_setting(key)
        try:
            parsed = float(value) if value is not None else default
        except (TypeError, ValueError):
            parsed = math.nan
        if math.isfinite(parsed) and minimum <= parsed <= maximum:
            return parsed
        self._logger.warning("Ungültige Einstellung %s=%r; Standardwert wird verwendet", key, value)
        return default

    def _set_float_setting(self, key: str, value: float, minimum: float, maximum: float) -> None:
        normalized = max(minimum, min(float(value), maximum))
        if not math.isfinite(normalized):
            raise ValueError(f"Ungültiger Wert für {key}")
        self._repository.set_setting(key, format(normalized, ".6g"))

    def _bool_setting(self, key: str, default: bool) -> bool:
        value = self._repository.get_setting(key)
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        self._logger.warning("Ungültige Einstellung %s=%r; Standardwert wird verwendet", key, value)
        return default

    def _optional_text_setting(self, key: str) -> str | None:
        value = self._repository.get_setting(key)
        normalized = value.strip() if value is not None else ""
        return normalized or None

    def _dependency_selection_mode(self, key: str) -> DependencySelectionMode:
        value = self._repository.get_setting(key)
        try:
            return DependencySelectionMode(value or DependencySelectionMode.AUTO.value)
        except ValueError:
            self._logger.warning("Ungültige Einstellung %s=%r; AUTO wird verwendet", key, value)
            return DependencySelectionMode.AUTO

    @staticmethod
    def _deck_volume_key(deck_id: str) -> str:
        if deck_id not in {"A", "B"}:
            raise ValueError("deck_id muss A oder B sein")
        return f"deck_{deck_id.lower()}_volume"
