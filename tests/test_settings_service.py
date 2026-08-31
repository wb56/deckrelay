"""Persistent settings tests."""

from pathlib import Path

import pytest

from party_player.database.connection import Database
from party_player.database.migrations import migrate
from party_player.enums import PlayerMode
from party_player.repository import PartyPlayerRepository
from party_player.settings_service import SettingsService
from party_player.presentation import PresentationPreference, Workspace
from party_player.system_dependencies import DependencySelectionMode


def test_automatic_loading_setting_survives_service_recreation(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    settings.set_automatic_deck_loading(False)

    assert not SettingsService(PartyPlayerRepository(database)).automatic_deck_loading()


def test_invalid_automatic_loading_setting_uses_safe_default(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    repository.set_setting("automatic_deck_loading", "kaputt")

    assert SettingsService(repository).automatic_deck_loading(default=True)


def test_queue_artist_repetition_switch_is_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "artist-switch.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    assert settings.queue_artist_repetition_enabled()
    settings.set_queue_artist_repetition_enabled(False)

    assert not SettingsService(repository).queue_artist_repetition_enabled()


def test_manual_and_semi_automatic_modes_are_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    settings.set_player_mode(PlayerMode.MANUAL)
    assert SettingsService(repository).player_mode() == PlayerMode.MANUAL
    settings.set_player_mode(PlayerMode.SEMI_AUTOMATIC)
    assert SettingsService(repository).player_mode() == PlayerMode.SEMI_AUTOMATIC


def test_automatic_mode_is_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    settings = SettingsService(PartyPlayerRepository(database))

    settings.set_player_mode(PlayerMode.AUTOMATIC)

    assert SettingsService(PartyPlayerRepository(database)).player_mode() == PlayerMode.AUTOMATIC


def test_queue_duplicate_policy_is_validated_and_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    settings.set_queue_duplicate_policy("prevent")
    assert SettingsService(repository).queue_duplicate_policy() == "prevent"
    repository.set_setting("queue_duplicate_policy", "unknown")
    assert SettingsService(repository).queue_duplicate_policy() == "allow"


def test_workspace_split_ratio_is_persistent_and_safely_bounded(tmp_path: Path) -> None:
    database = Database(tmp_path / "workspace-split.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    assert settings.workspace_catalog_ratio() == 0.5
    settings.set_workspace_catalog_ratio(0.72)
    assert SettingsService(repository).workspace_catalog_ratio() == 0.72
    settings.set_workspace_catalog_ratio(2.0)
    assert SettingsService(repository).workspace_catalog_ratio() == 0.8


def test_main_window_geometry_is_persisted_without_interpreting_display_values(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "window-geometry.db")
    migrate(database)
    settings = SettingsService(PartyPlayerRepository(database))

    assert settings.main_window_geometry() is None
    value = '{"dpi_scale":1.25,"height":700,"width":1100,"x":20,"y":30}'
    settings.set_main_window_geometry(value)

    assert SettingsService(PartyPlayerRepository(database)).main_window_geometry() == value


def test_emergency_track_ids_are_normalized_and_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "emergency-settings.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    settings.set_emergency_track_ids([3, -1, 2, 3, 0])

    assert SettingsService(repository).emergency_track_ids() == [3, 2]

    settings.set_emergency_media_track_ids("BREAK_MUSIC", [8, 8, -1, 9])
    settings.set_emergency_media_track_ids("JINGLE", [10])
    settings.set_emergency_media_track_ids("ANNOUNCEMENT", [11])
    restored = SettingsService(repository)
    assert restored.emergency_media_track_ids("PRIMARY") == [3, 2]
    assert restored.emergency_media_track_ids("BREAK_MUSIC") == [8, 9]
    assert restored.emergency_media_track_ids("JINGLE") == [10]
    assert restored.emergency_media_track_ids("ANNOUNCEMENT") == [11]
    with pytest.raises(ValueError):
        settings.set_emergency_media_track_ids("UNKNOWN", [1])


def test_emergency_storage_roots_are_normalized_and_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "emergency-storage-settings.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    settings.set_emergency_local_ssd_roots([" D:\\Notfall ", "D:\\Notfall", ""])
    settings.set_emergency_approved_removable_roots([" E:\\Freigegeben "])
    restored = SettingsService(repository)

    assert restored.emergency_local_ssd_roots() == ["D:\\Notfall"]
    assert restored.emergency_approved_removable_roots() == ["E:\\Freigegeben"]


def test_emergency_primary_preload_is_optional_and_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "emergency-preload-settings.db")
    migrate(database)
    settings = SettingsService(PartyPlayerRepository(database))

    assert not settings.emergency_preload_primary()
    settings.set_emergency_preload_primary(True)

    assert SettingsService(PartyPlayerRepository(database)).emergency_preload_primary()


def test_effective_queue_duration_setting_is_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    assert not settings.queue_stats_use_effective_cues()
    settings.set_queue_stats_use_effective_cues(True)

    assert SettingsService(repository).queue_stats_use_effective_cues()


def test_cue_analysis_settings_are_validated_and_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)
    settings.set_cue_analysis_edge_window_seconds(55.0)
    settings.set_cue_analysis_level_window_seconds(0.2)
    settings.set_cue_analysis_thresholds(-38.0, -48.0)
    settings.set_cue_analysis_minimum_signal_seconds(0.7)
    settings.set_cue_analysis_minimum_silence_seconds(0.4)

    restored = SettingsService(repository)
    assert restored.cue_analysis_edge_window_seconds() == 55.0
    assert restored.cue_analysis_level_window_seconds() == 0.2
    assert restored.cue_analysis_signal_on_dbfs() == -38.0
    assert restored.cue_analysis_signal_off_dbfs() == -48.0
    assert restored.cue_analysis_minimum_signal_seconds() == 0.7
    assert restored.cue_analysis_minimum_silence_seconds() == 0.4


def test_invalid_cue_analysis_settings_use_safe_defaults(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    for key, value in {
        "cue_analysis_edge_window_seconds": "90",
        "cue_analysis_level_window_seconds": "0",
        "cue_analysis_signal_on_dbfs": "nan",
        "cue_analysis_signal_off_dbfs": "laut",
        "cue_analysis_minimum_signal_seconds": "-1",
        "cue_analysis_minimum_silence_seconds": "999",
    }.items():
        repository.set_setting(key, value)

    settings = SettingsService(repository)
    assert settings.cue_analysis_edge_window_seconds() == 45.0
    assert settings.cue_analysis_level_window_seconds() == 0.1
    assert settings.cue_analysis_signal_on_dbfs() == -45.0
    assert settings.cue_analysis_signal_off_dbfs() == -50.0
    assert settings.cue_analysis_minimum_signal_seconds() == 0.5
    assert settings.cue_analysis_minimum_silence_seconds() == 0.3

    repository.set_setting("cue_analysis_signal_on_dbfs", "-60")
    repository.set_setting("cue_analysis_signal_off_dbfs", "-40")
    assert settings.cue_analysis_thresholds() == (-45.0, -50.0)


def test_audio_and_fade_settings_are_validated_and_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    settings = SettingsService(PartyPlayerRepository(database))

    settings.set_master_volume(0.65)
    settings.set_crossfader_position(0.25)
    settings.set_deck_volume("A", 0.7)
    settings.set_deck_volume("B", 0.8)
    settings.set_fade_duration(12)
    settings.set_minimum_fade_duration(0.75)
    settings.set_minimum_playable_duration(8)
    settings.set_played_ratio_threshold(0.6)
    settings.set_played_seconds_threshold(150)
    settings.set_repetition_partial_ratio_threshold(0.4)
    settings.set_fade_out_stops_deck(True)

    restored = SettingsService(PartyPlayerRepository(database))
    assert restored.master_volume() == 0.65
    assert restored.crossfader_position() == 0.25
    assert restored.deck_volume("A") == 0.7
    assert restored.deck_volume("B") == 0.8
    assert restored.fade_duration() == 12
    assert restored.minimum_fade_duration() == 0.75
    assert restored.minimum_playable_duration() == 8
    assert restored.played_ratio_threshold() == 0.6
    assert restored.played_seconds_threshold() == 150
    assert restored.repetition_partial_ratio_threshold() == 0.4
    assert restored.fade_out_stops_deck()


def test_invalid_audio_and_fade_settings_use_safe_defaults(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    repository.set_setting("master_volume", "laut")
    repository.set_setting("crossfader_position", "2")
    repository.set_setting("deck_a_volume", "nan")
    repository.set_setting("fade_duration", "0")
    repository.set_setting("minimum_fade_duration", "0")
    repository.set_setting("minimum_playable_duration", "nan")
    repository.set_setting("fade_out_stops_deck", "vielleicht")

    settings = SettingsService(repository)
    assert settings.master_volume() == 0.8
    assert settings.crossfader_position() == 0.5
    assert settings.deck_volume("A") == 1.0
    assert settings.fade_duration() == 5.0
    assert settings.minimum_fade_duration() == 0.5
    assert settings.minimum_playable_duration() == 5.0
    assert not settings.fade_out_stops_deck()


def test_startup_settings_are_validated_and_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)
    settings.set_restore_last_session(False)
    settings.set_fullscreen_on_start(True)
    settings.set_performance_diagnostics_enabled(False)
    settings.set_background_analysis_enabled(False)

    restored = SettingsService(PartyPlayerRepository(database))
    assert not restored.restore_last_session()
    assert restored.fullscreen_on_start()
    assert not restored.performance_diagnostics_enabled()
    assert not restored.background_analysis_enabled()

    repository.set_setting("restore_last_session", "defekt")
    repository.set_setting("fullscreen_on_start", "defekt")
    assert restored.restore_last_session(default=True)
    assert not restored.fullscreen_on_start(default=False)


def test_safety_backup_retention_limit_is_validated_and_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    settings = SettingsService(PartyPlayerRepository(database))
    assert settings.safety_backup_retention_limit() == 10

    settings.set_safety_backup_retention_limit(25)

    assert SettingsService(PartyPlayerRepository(database)).safety_backup_retention_limit() == 25
    with pytest.raises(ValueError, match="Retention"):
        settings.set_safety_backup_retention_limit(0)


def test_last_manual_backup_is_stored_as_one_validated_record(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    settings.set_last_manual_backup("2026-08-10T12:00:00+00:00", r"C:\Backups\backup.zip")

    restored = SettingsService(PartyPlayerRepository(database))
    assert restored.last_manual_backup() == (
        "2026-08-10T12:00:00+00:00",
        r"C:\Backups\backup.zip",
    )
    repository.set_setting("last_manual_backup", '{"path": 42}')
    assert restored.last_manual_backup() is None


def test_file_browser_visibility_is_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    settings = SettingsService(PartyPlayerRepository(database))

    settings.set_file_browser_enabled(False)

    assert not SettingsService(PartyPlayerRepository(database)).file_browser_enabled()


def test_audio_settings_are_validated_and_persisted(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    assert settings.audio_backend() == "vlc"
    assert settings.audio_output_device() == ""
    settings.set_audio_backend("vlc")
    settings.set_audio_output_device("  device-42  ")
    settings.set_emergency_action_profile("SAFE_RESET")

    restored = SettingsService(repository)
    assert restored.audio_backend() == "vlc"
    assert restored.audio_output_device() == "device-42"
    assert restored.emergency_action_profile() == "SAFE_RESET"
    repository.set_setting("audio_backend", "unbekannt")
    assert restored.audio_backend() == "vlc"
    with pytest.raises(ValueError):
        settings.set_audio_backend("unbekannt")
    with pytest.raises(ValueError):
        settings.set_emergency_action_profile("unbekannt")


def test_loudness_runtime_settings_are_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "loudness-settings.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    settings.set_normalization_enabled(False)
    settings.set_clip_protection_enabled(False)
    settings.set_normalization_mode("ALBUM")
    settings.set_target_loudness(-15.0)
    settings.set_gain_smoothing_seconds(1.25)
    settings.set_maximum_positive_gain(5.0)
    settings.set_maximum_negative_gain(-10.0)
    settings.set_maximum_output_peak(-2.0)
    settings.set_headroom(1.5)
    settings.set_fallback_positive_gain(2.0)

    restored = SettingsService(repository)
    assert not restored.normalization_enabled()
    assert not restored.clip_protection_enabled()
    assert restored.normalization_mode() == "ALBUM"
    assert restored.target_loudness() == -15.0
    assert restored.gain_smoothing_seconds() == 1.25
    assert restored.maximum_positive_gain() == 5.0
    assert restored.maximum_negative_gain() == -10.0
    assert restored.maximum_output_peak() == -2.0
    assert restored.headroom() == 1.5
    assert restored.fallback_positive_gain() == 2.0


def test_dependency_settings_are_persistent_and_resettable(tmp_path: Path) -> None:
    database = Database(tmp_path / "dependency-settings.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    assert settings.vlc_selection_mode() == DependencySelectionMode.AUTO
    assert settings.ffmpeg_selection_mode() == DependencySelectionMode.AUTO
    assert settings.vlc_installation_path() is None
    assert settings.ffmpeg_bin_path() is None
    assert not settings.first_run_completed()
    assert settings.system_check_completed_version() is None

    settings.set_vlc_installation_path(r"  C:\Tools\VLC  ")
    settings.set_ffmpeg_bin_path(r"  C:\Tools\FFmpeg\bin  ")
    settings.set_first_run_completed(True)
    settings.set_system_check_completed_version(" 1.0.0 ")

    restored = SettingsService(PartyPlayerRepository(database))
    snapshot = restored.dependency_settings()
    assert snapshot.vlc_installation_path == r"C:\Tools\VLC"
    assert snapshot.vlc_selection_mode == DependencySelectionMode.USER
    assert snapshot.ffmpeg_bin_path == r"C:\Tools\FFmpeg\bin"
    assert snapshot.ffmpeg_selection_mode == DependencySelectionMode.USER
    assert snapshot.first_run_completed
    assert snapshot.system_check_completed_version == "1.0.0"

    restored.reset_vlc_installation_path()
    restored.reset_ffmpeg_bin_path()
    assert restored.vlc_installation_path() is None
    assert restored.ffmpeg_bin_path() is None
    assert restored.vlc_selection_mode() == DependencySelectionMode.AUTO
    assert restored.ffmpeg_selection_mode() == DependencySelectionMode.AUTO


def test_invalid_dependency_modes_fall_back_to_auto(tmp_path: Path) -> None:
    database = Database(tmp_path / "invalid-dependency-settings.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    repository.set_setting("vlc_selection_mode", "broken")
    repository.set_setting("ffmpeg_selection_mode", "broken")

    settings = SettingsService(repository)
    assert settings.vlc_selection_mode() == DependencySelectionMode.AUTO
    assert settings.ffmpeg_selection_mode() == DependencySelectionMode.AUTO


def test_presentation_settings_are_safe_and_persistent(tmp_path: Path) -> None:
    database = Database(tmp_path / "presentation-settings.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)

    assert settings.presentation_preference() is PresentationPreference.AUTO
    assert settings.presentation_workspace() is Workspace.LIVE

    settings.set_presentation_preference(PresentationPreference.LARGE)
    settings.set_presentation_workspace(Workspace.PREPARATION)

    restored = SettingsService(repository)
    assert restored.presentation_preference() is PresentationPreference.LARGE
    assert restored.presentation_workspace() is Workspace.PREPARATION


def test_invalid_presentation_settings_use_safe_defaults(tmp_path: Path) -> None:
    database = Database(tmp_path / "invalid-presentation-settings.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    repository.set_setting("presentation_preference", "damaged")
    repository.set_setting("presentation_workspace", "damaged")

    settings = SettingsService(repository)
    assert settings.presentation_preference() is PresentationPreference.AUTO
    assert settings.presentation_workspace() is Workspace.LIVE


def test_temporary_resolution_does_not_overwrite_large_preference(tmp_path: Path) -> None:
    database = Database(tmp_path / "temporary-presentation.db")
    migrate(database)
    repository = PartyPlayerRepository(database)
    settings = SettingsService(repository)
    settings.set_presentation_preference(PresentationPreference.LARGE)

    # ResolvedPresentation is deliberately never persisted by SettingsService.
    assert SettingsService(repository).presentation_preference() is PresentationPreference.LARGE
