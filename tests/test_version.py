from pathlib import Path
import tomllib

from party_player import __version__
from party_player.release_version import windows_version


def test_public_and_project_versions_use_their_required_forms() -> None:
    project_file = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))

    assert __version__ == "2.0.0-beta.1"
    assert project["project"]["version"] == "2.0.0b1"


def test_windows_version_maps_beta_and_stable_releases() -> None:
    assert windows_version(__version__) == (2, 0, 0, 1)
    assert windows_version("2.0.0") == (2, 0, 0, 0)
