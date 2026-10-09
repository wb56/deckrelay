from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT_SOURCE = Path(__file__).parents[1] / "scripts" / "Invoke-DevTests.ps1"


@pytest.fixture
def dev_test_project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    project = tmp_path / "project with spaces"
    scripts = project / "scripts"
    python_dir = project / ".venv" / "Scripts"
    fake_modules = project / "fake modules"
    fake_pytest = fake_modules / "pytest"
    scripts.mkdir(parents=True)
    python_dir.mkdir(parents=True)
    fake_pytest.mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "tests" / "test audio.py").write_text("", encoding="utf-8")
    (project / "tests" / "test_backend.py").write_text("", encoding="utf-8")
    (project / "tests" / "test_example.py").write_text("", encoding="utf-8")

    shutil.copy2(SCRIPT_SOURCE, scripts / SCRIPT_SOURCE.name)
    shutil.copy2(sys.executable, python_dir / "python.exe")
    (project / ".venv" / "pyvenv.cfg").write_text(
        f"home = {sys.base_prefix}\n"
        f"executable = {sys.executable}\n"
        "include-system-site-packages = false\n",
        encoding="utf-8",
    )
    (scripts / "test-groups.psd1").write_text(
        "@{ audio = @('tests/test audio.py', 'tests/test_backend.py::test_ok') }\n",
        encoding="utf-8",
    )
    (fake_pytest / "__init__.py").write_text("", encoding="utf-8")
    (fake_pytest / "__main__.py").write_text(
        """import json
import os
from pathlib import Path
import sys

Path(os.environ["FAKE_ARGS_FILE"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
mode = os.environ.get("FAKE_PYTEST_MODE", "pass")
if mode == "fail":
    print("tests/test_example.py::test_failure FAILED")
    print("E   AssertionError: concise failure detail")
    print("1 failed, 2 passed in 0.15s")
    raise SystemExit(1)
if mode == "no-tests":
    print("no tests ran in 0.01s")
    raise SystemExit(5)
print("3 passed in 0.12s")
""",
        encoding="utf-8",
    )

    args_file = project / "captured args.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fake_modules)
    env["FAKE_ARGS_FILE"] = str(args_file)
    return project, env


def run_script(
    project: Path, env: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "pwsh",
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(project / "scripts" / "Invoke-DevTests.ps1"),
            *arguments,
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def captured_args(project: Path) -> list[str]:
    return json.loads((project / "captured args.json").read_text(encoding="utf-8"))


def test_quick_passes_explicit_path_and_keyword_as_separate_arguments(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project

    result = run_script(
        project,
        env,
        "-Profile",
        "quick",
        "-Tests",
        "tests/test audio.py::test_ok",
        "-Keyword",
        "name; Write-Error injected",
    )

    assert result.returncode == 0
    args = captured_args(project)
    assert args[:2] == ["-q", "--tb=short"]
    assert "tests/test audio.py::test_ok" in args
    assert args[args.index("-k") + 1] == "name; Write-Error injected"
    assert {"--tb=short", "-x", "--no-header", "--no-summary"} <= set(args)
    assert "Profil: quick" in result.stdout
    assert "Ergebnis: PASS" in result.stdout
    assert "Tests: 3" in result.stdout
    assert "3 passed" not in result.stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("-Profile", "quick"), "quick erfordert"),
        (("-Profile", "regression"), "regression erfordert"),
        (("-Profile", "full", "-Keyword", "test"), "full akzeptiert"),
    ],
)
def test_invalid_profile_arguments_fail_before_start(
    dev_test_project: tuple[Path, dict[str, str]],
    arguments: tuple[str, ...],
    message: str,
) -> None:
    project, env = dev_test_project

    result = run_script(project, env, *arguments)

    assert result.returncode != 0
    assert message in (result.stdout + result.stderr).lower()
    assert not (project / "captured args.json").exists()


def test_regression_resolves_only_declared_group(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project

    result = run_script(project, env, "-Profile", "regression", "-Group", "audio")

    assert result.returncode == 0
    args = captured_args(project)
    assert "tests/test audio.py" in args
    assert "tests/test_backend.py::test_ok" in args


def test_unknown_regression_group_is_an_error(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project

    result = run_script(project, env, "-Profile", "regression", "-Group", "missing")

    assert result.returncode != 0
    assert "unbekannte regressionstestgruppe" in (result.stdout + result.stderr).lower()
    assert not (project / "captured args.json").exists()


def test_quick_rejects_missing_test_path(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project

    result = run_script(project, env, "-Profile", "quick", "-Tests", "tests/missing.py")

    assert result.returncode != 0
    assert "ungültiger oder fehlender testpfad" in (result.stdout + result.stderr).lower()
    assert not (project / "captured args.json").exists()


def test_full_runs_without_test_selectors_and_writes_success_log(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project

    result = run_script(project, env, "-Profile", "full")

    assert result.returncode == 0
    assert captured_args(project) == [
        "-q",
        "--tb=short",
        "-x",
        "--no-header",
        "--no-summary",
    ]
    logs = list((project / "logs" / "dev-tests").glob("*.log"))
    assert len(logs) == 1
    assert "3 passed in 0.12s" in logs[0].read_text(encoding="utf-8")


def test_failure_preserves_exit_code_writes_full_log_and_prints_short_excerpt(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project
    env["FAKE_PYTEST_MODE"] = "fail"

    result = run_script(project, env, "-Profile", "quick", "-Tests", "tests/test_example.py")

    assert result.returncode == 1
    assert "Ergebnis: FAIL" in result.stdout
    assert "Tests: 3" in result.stdout
    assert "AssertionError: concise failure detail" in result.stdout
    logs = list((project / "logs" / "dev-tests").glob("*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "tests/test_example.py::test_failure FAILED" in log
    assert "1 failed, 2 passed" in log


def test_nonstandard_pytest_exit_code_is_preserved(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project
    env["FAKE_PYTEST_MODE"] = "no-tests"

    result = run_script(project, env, "-Profile", "quick", "-Keyword", "nothing")

    assert result.returncode == 5
    assert "Ergebnis: FAIL" in result.stdout
    assert "Tests: 0" in result.stdout


def test_missing_python_is_reported_as_start_error(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project
    (project / ".venv" / "Scripts" / "python.exe").unlink()

    result = run_script(project, env, "-Profile", "full")

    assert result.returncode != 0
    assert "python-interpreter fehlt" in (result.stdout + result.stderr).lower()
    assert not (project / "captured args.json").exists()


def test_missing_group_configuration_is_an_error(
    dev_test_project: tuple[Path, dict[str, str]],
) -> None:
    project, env = dev_test_project
    (project / "scripts" / "test-groups.psd1").unlink()

    result = run_script(project, env, "-Profile", "regression", "-Group", "audio")

    assert result.returncode != 0
    assert "gruppenkonfiguration fehlt" in (result.stdout + result.stderr).lower()
