from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

import pytest


SCRIPT_SOURCE = Path(__file__).parents[1] / "scripts" / "Invoke-DevPr.ps1"


def run(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = ["pwsh", "-NoLogo", "-NoProfile", "-File", str(SCRIPT_SOURCE), *args]
    with tempfile.TemporaryDirectory() as capture_directory:
        stdout_path = Path(capture_directory) / "stdout.txt"
        stderr_path = Path(capture_directory) / "stderr.txt"
        with (
            stdout_path.open("w", encoding="utf-8") as stdout_file,
            stderr_path.open("w", encoding="utf-8") as stderr_file,
        ):
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                check=False,
            )
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            stdout_path.read_text(encoding="utf-8"),
            stderr_path.read_text(encoding="utf-8"),
        )


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, encoding="utf-8", capture_output=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repository with spaces"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked one.txt").write_text("one\n", encoding="utf-8")
    (repo / "tracked two.txt").write_text("two\n", encoding="utf-8")
    (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
    git(repo, "add", ".gitignore", "tracked one.txt", "tracked two.txt")
    git(repo, "commit", "-m", "initial")
    git(repo, "checkout", "-b", "feature/test")
    return repo


def combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_validate_accepts_clean_tree_and_repeated_calls(repository: Path) -> None:
    first = run("-Action", "validate", "-RepositoryRoot", str(repository), cwd=repository)
    second = run("-Action", "validate", "-RepositoryRoot", str(repository), cwd=repository)
    assert first.returncode == second.returncode == 0
    assert "STATUS: PASS" in first.stdout


def test_validate_blocks_dirty_tree(repository: Path) -> None:
    (repository / "tracked one.txt").write_text("dirty\n", encoding="utf-8")
    result = run("-Action", "validate", "-RepositoryRoot", str(repository), cwd=repository)
    assert result.returncode != 0
    assert "STATUS: BLOCKED" in combined(result)


def test_commit_stages_only_explicit_files(repository: Path) -> None:
    (repository / "tracked one.txt").write_text("changed one\n", encoding="utf-8")
    (repository / "tracked two.txt").write_text("changed two\n", encoding="utf-8")
    result = run(
        "-Action",
        "commit",
        "-RepositoryRoot",
        str(repository),
        "-Files",
        "tracked one.txt",
        "-Message",
        "explicit file",
        cwd=repository,
    )
    assert result.returncode == 0
    assert git(repository, "show", "--pretty=", "--name-only", "HEAD") == "tracked one.txt"
    assert git(repository, "status", "--short") == 'M "tracked two.txt"'


def test_commit_is_idempotent_when_nothing_changed(repository: Path) -> None:
    result = run(
        "-Action",
        "commit",
        "-RepositoryRoot",
        str(repository),
        "-Files",
        "tracked one.txt",
        "-Message",
        "nothing",
        cwd=repository,
    )
    assert result.returncode == 0
    assert "NOOP" in result.stdout


def write_fake_gh(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    executable = tmp_path / "fake-gh.ps1"
    executable.write_text(
        r"""$scenario = $env:FAKE_GH_SCENARIO
$sha = $env:FAKE_HEAD_SHA
$joined = $args -join ' '
if ($scenario -eq 'permissions' -and $joined.Contains('repos/acme/project')) { '{"permissions":{"push":false}}'; exit 0 }
if ($args[0] -eq 'repo' -and $args[1] -eq 'view') { '{"nameWithOwner":"acme/project","owner":{"login":"owner"},"viewerPermission":"ADMIN"}'; exit 0 }
if ($args[0] -eq 'pr' -and $args[1] -eq 'view') {
    $mergeable = if ($scenario -eq 'conflict') { 'CONFLICTING' } else { 'MERGEABLE' }
    $labels = if ($scenario -eq 'owner-missing') { '[]' } else { "[{`"name`":`"owner-approved`"},{`"name`":`"head:$sha`"}]" }
    "{`"number`":7,`"state`":`"OPEN`",`"isDraft`":false,`"headRefOid`":`"$sha`",`"headRefName`":`"feature/test`",`"baseRefName`":`"main`",`"author`":{`"login`":`"author`"},`"labels`":$labels,`"mergeable`":`"$mergeable`",`"reviewDecision`":`"APPROVED`"}"
    exit 0
}
if ($args[0] -eq 'pr' -and $args[1] -eq 'checks') {
    if ($scenario -eq 'gates-running') { '[{"name":"quality","state":"IN_PROGRESS","bucket":"pending"}]' }
    elseif ($scenario -eq 'gates-failed') { '[{"name":"quality","state":"FAILURE","bucket":"fail"}]' }
    else { '[{"name":"quality","state":"SUCCESS","bucket":"pass"}]' }
    exit 0
}
if ($joined.Contains('reviews')) {
    if ($scenario -eq 'review-missing') { '[]' }
    else { "[{`"state`":`"APPROVED`",`"commit_id`":`"$sha`",`"user`":{`"login`":`"reviewer`",`"type`":`"User`"}}]" }
    exit 0
}
if ($joined.Contains('events')) {
    if ($scenario -eq 'owner-missing') { '[]' }
    else { "[{`"event`":`"labeled`",`"actor`":{`"login`":`"owner`"},`"label`":{`"name`":`"owner-approved`"}},{`"event`":`"labeled`",`"actor`":{`"login`":`"owner`"},`"label`":{`"name`":`"head:$sha`"}}]" }
    exit 0
}
if ($joined.Contains('branches/main/protection')) {
    if ($scenario -eq 'protection') { '{"required_pull_request_reviews":{"required_approving_review_count":2}}' }
    else { '{}' }
    exit 0
}
if ($joined.Contains('rulesets?includes_parents=true')) {
    if ($scenario -eq 'ruleset') { '[{"id":1,"target":"branch","enforcement":"active"}]' }
    else { '[]' }
    exit 0
}
if ($joined.Contains('rulesets/1')) { '{"conditions":{"ref_name":{"include":["~DEFAULT_BRANCH"]}},"rules":[{"type":"pull_request","parameters":{"required_approving_review_count":2}}]}'; exit 0 }
if ($args[0] -eq 'pr' -and $args[1] -eq 'merge') { 'merged'; exit 0 }
'{"permissions":{"push":true}}'
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["FAKE_HEAD_SHA"] = "a" * 40
    return executable, env


@pytest.mark.parametrize(
    ("scenario", "action", "expected"),
    [
        ("permissions", "publish", "BLOCKED"),
        ("review-missing", "merge", "Review"),
        ("owner-missing", "merge", "Eigentümerfreigabe"),
        ("gates-running", "gates", "BLOCKED"),
        ("gates-failed", "gates", "FAIL"),
        ("conflict", "merge", "Mergefähigkeit"),
        ("protection", "merge", "Branch-Protection"),
        ("ruleset", "merge", "Ruleset"),
    ],
)
def test_github_safety_failures_are_closed(
    repository: Path, tmp_path: Path, scenario: str, action: str, expected: str
) -> None:
    gh, env = write_fake_gh(tmp_path)
    env["FAKE_GH_SCENARIO"] = scenario
    result = run(
        "-Action",
        action,
        "-RepositoryRoot",
        str(repository),
        "-GitHubExecutable",
        str(gh),
        "-Repository",
        "acme/project",
        "-PrNumber",
        "7",
        "-ExpectedHeadSha",
        "a" * 40,
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )
    assert result.returncode != 0
    assert expected.lower() in combined(result).lower()


def test_merge_rejects_mismatched_head_sha(repository: Path, tmp_path: Path) -> None:
    gh, env = write_fake_gh(tmp_path)
    result = run(
        "-Action",
        "merge",
        "-RepositoryRoot",
        str(repository),
        "-GitHubExecutable",
        str(gh),
        "-Repository",
        "acme/project",
        "-PrNumber",
        "7",
        "-ExpectedHeadSha",
        "b" * 40,
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )
    assert result.returncode != 0
    assert "Head-SHA" in combined(result)


def test_cleanup_refuses_main_and_unrelated_branch(repository: Path) -> None:
    for branch in ("main", "other/topic"):
        result = run(
            "-Action",
            "cleanup",
            "-RepositoryRoot",
            str(repository),
            "-Branch",
            branch,
            cwd=repository,
        )
        assert result.returncode != 0
        assert "BLOCKED" in combined(result)


def test_cleanup_deletes_merged_feature_branch(repository: Path) -> None:
    git(repository, "checkout", "main")
    git(repository, "branch", "feature/merged")
    result = run(
        "-Action",
        "cleanup",
        "-RepositoryRoot",
        str(repository),
        "-Branch",
        "feature/merged",
        cwd=repository,
    )
    assert result.returncode == 0
    assert "feature/merged" not in git(repository, "branch", "--list")
