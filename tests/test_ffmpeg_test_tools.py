from pathlib import Path
import subprocess
import sys

from ffmpeg_test_tools import resolve_ffmpeg_test_tool


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_local_bundle_is_used_when_present(tmp_path: Path) -> None:
    bundled = executable(tmp_path / "bundle" / "ffmpeg.exe")

    result = resolve_ffmpeg_test_tool(
        "ffmpeg", environment={}, bundled_bin=bundled.parent, which=lambda _name: "from-path"
    )

    assert result == str(bundled.resolve())


def test_path_is_used_when_local_bundle_is_missing(tmp_path: Path) -> None:
    installed = executable(tmp_path / "system tools" / "ffmpeg.exe")

    result = resolve_ffmpeg_test_tool(
        "ffmpeg",
        environment={},
        bundled_bin=tmp_path / "missing",
        which=lambda _name: str(installed),
    )

    assert result == str(installed.resolve())


def test_missing_bundle_and_path_returns_none(tmp_path: Path) -> None:
    assert (
        resolve_ffmpeg_test_tool(
            "ffmpeg", environment={}, bundled_bin=tmp_path / "missing", which=lambda _name: None
        )
        is None
    )


def test_ffmpeg_and_ffprobe_are_resolved_independently(tmp_path: Path) -> None:
    ffmpeg = executable(tmp_path / "ffmpeg.exe")

    def which(name: str) -> str | None:
        return str(ffmpeg) if name == "ffmpeg" else None

    assert resolve_ffmpeg_test_tool("ffmpeg", environment={}, bundled_bin=tmp_path, which=which)
    assert (
        resolve_ffmpeg_test_tool("ffprobe", environment={}, bundled_bin=tmp_path, which=which)
        is None
    )


def test_explicit_path_with_spaces_has_highest_priority(tmp_path: Path) -> None:
    explicit = executable(tmp_path / "explicit tools" / "ffmpeg custom.exe")
    bundled = executable(tmp_path / "bundle" / "ffmpeg.exe")

    result = resolve_ffmpeg_test_tool(
        "ffmpeg",
        environment={"DECKRELAY_TEST_FFMPEG": str(explicit)},
        bundled_bin=bundled.parent,
        which=lambda _name: "from-path",
    )

    assert result == str(explicit.resolve())


def test_resolved_executable_path_is_passed_to_spawned_process(tmp_path: Path) -> None:
    result_file = tmp_path / "spawn result.txt"
    resolved = resolve_ffmpeg_test_tool(
        "ffmpeg",
        environment={"DECKRELAY_TEST_FFMPEG": sys.executable},
        bundled_bin=tmp_path / "missing",
        which=lambda _name: None,
    )
    assert resolved is not None

    subprocess.run(
        [
            resolved,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.executable)",
            str(result_file),
        ],
        check=True,
        timeout=30,
    )

    assert Path(result_file.read_text()).resolve() == Path(sys.executable).resolve()
