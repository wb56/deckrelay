"""Tests for central user-facing help content."""

from party_player.ui.help_content import tempo_analysis_help_text


def test_tempo_analysis_help_explains_interpretation_and_safety() -> None:
    text = tempo_analysis_help_text()

    assert "BPM-Vorschlag" in text
    assert "Halbtempo-/Doppeltempo" in text
    assert "Konfidenz" in text
    assert "Rhythmusstabilität" in text
    assert "Energie ist nicht dasselbe wie Lautheit" in text
    assert "manuell bestätigter Katalogwert" in text
    assert "verändert weder die Musikdatei noch deren Tags" in text
    assert "EMPFOHLENER ABLAUF" in text
