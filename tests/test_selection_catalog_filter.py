from party_player.models import Track
from party_player.selection_catalog_filter import SelectionCatalogFilter


def _track(**changes: object) -> Track:
    values = dict(
        id=1,
        file_path="track.mp3",
        title="Titel",
        artist="Interpret",
        album="Album",
        duration_seconds=180.0,
        genre="Disco",
        year=1979,
        bpm=118.0,
        rating=4,
    )
    values.update(changes)
    return Track(**values)  # type: ignore[arg-type]


def test_catalog_filter_combines_metadata_groups_with_and() -> None:
    selected = SelectionCatalogFilter(
        genres=("Rock", "Disco"),
        year_from=1970,
        year_to=1989,
        bpm_from=110,
        bpm_to=125,
        minimum_rating=4,
    )

    assert selected.matches(_track(), None)
    assert not selected.matches(_track(bpm=130.0), None)
    assert not selected.matches(_track(genre="Soul"), None)


def test_catalog_filter_excludes_missing_values_unless_requested() -> None:
    strict = SelectionCatalogFilter(bpm_from=100)
    permissive = SelectionCatalogFilter(bpm_from=100, include_missing=True)

    assert not strict.matches(_track(bpm=None), None)
    assert permissive.matches(_track(bpm=None), None)


def test_catalog_filter_summary_is_operator_readable() -> None:
    selected = SelectionCatalogFilter(
        genres=("2000s",),
        year_from=2009,
        year_to=2011,
        minimum_rating=4,
    )

    assert selected.summary() == "Genre: 2000s · Jahr: 2009–2011 · Bewertung ab 4"
    assert SelectionCatalogFilter().summary() == "gesamter Katalog"
