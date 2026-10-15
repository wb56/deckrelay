@{
    audio_playback = @(
        "tests/test_audio_backend_factory.py"
        "tests/test_deck_and_crossfader.py"
        "tests/test_vlc_backend.py"
    )
    automatic_selection = @(
        "tests/test_automatic_selection.py"
        "tests/test_track_selection.py"
        "tests/test_track_suitability.py"
    )
    database_and_backup = @(
        "tests/test_database.py"
        "tests/test_backup_service.py"
        "tests/test_restore_pipeline.py"
    )
}
