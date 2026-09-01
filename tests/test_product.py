from pathlib import Path

from party_player.product import (
    PRODUCT_DESCRIPTION,
    PRODUCT_NAME,
    PRODUCT_SLUG,
    PRODUCT_VERSION,
)


def test_public_product_identity() -> None:
    assert PRODUCT_NAME == "DeckRelay"
    assert PRODUCT_SLUG == "deckrelay"
    assert PRODUCT_VERSION == "2.0.0-beta.1"
    assert PRODUCT_DESCRIPTION == "Automatische Zwei-Deck-Musikwiedergabe für Veranstaltungen"


def test_main_window_has_no_legacy_visible_product_name() -> None:
    source = (Path(__file__).parents[1] / "src/party_player/ui/main_window.py").read_text(
        encoding="utf-8"
    )
    assert 'text="PARTYPLAYER"' not in source
    assert "text=PRODUCT_NAME" in source
