from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest


SCRIPT_SOURCE = Path(__file__).parents[1] / "scripts" / "Invoke-DevPr.ps1"


def run(
    *args: str, cwd: Path, env: dict[str, str] | None = None, culture: str | None = None
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as capture_directory:
        command = ["pwsh", "-NoLogo", "-NoProfile", "-File", str(SCRIPT_SOURCE), *args]
        if culture is not None:

            def quote(value: str) -> str:
                return "'" + value.replace("'", "''") + "'"

            script_call = " ".join(
                value if value.startswith("-") else quote(value)
                for value in (str(SCRIPT_SOURCE), *args)
            )
            command = [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-Command",
                (
                    f"$culture = [System.Globalization.CultureInfo]::GetCultureInfo('{culture}'); "
                    "[System.Threading.Thread]::CurrentThread.CurrentCulture = $culture; "
                    "[System.Threading.Thread]::CurrentThread.CurrentUICulture = $culture; "
                    f"& {script_call}"
                ),
            ]
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


def run_timestamp_converter(
    tmp_path: Path, value_expression: str, culture: str
) -> subprocess.CompletedProcess[str]:
    escaped_source = str(SCRIPT_SOURCE).replace("'", "''")
    runner = tmp_path / "run-timestamp-converter.ps1"
    runner.write_text(
        "\n".join(
            (
                "$tokens = $null",
                "$errors = $null",
                f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{escaped_source}', [ref]$tokens, [ref]$errors)",
                "$definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'ConvertTo-UtcDateTimeOffset' }, $true)",
                ". ([scriptblock]::Create($definition.Extent.Text))",
                'function Stop-Blocked { param([string]$Cause); [Console]::Error.WriteLine("BLOCKED: $Cause"); exit 3 }',
                f"$culture = [System.Globalization.CultureInfo]::GetCultureInfo('{culture}')",
                "[System.Threading.Thread]::CurrentThread.CurrentCulture = $culture",
                "[System.Threading.Thread]::CurrentThread.CurrentUICulture = $culture",
                f"$value = {value_expression}",
                "$converted = ConvertTo-UtcDateTimeOffset $value 'Invalid timestamp'",
                "[Console]::Out.WriteLine($converted.ToString('o', [System.Globalization.CultureInfo]::InvariantCulture))",
            )
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-File", str(runner)],
        cwd=tmp_path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


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
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feature")
    git(repo, "remote", "add", "DeckRelay", "https://github.com/acme/project.git")
    git(repo, "update-ref", "refs/remotes/DeckRelay/feature/test", "HEAD")
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
if (-not [string]::IsNullOrWhiteSpace($env:FAKE_CALLS_FILE)) {
    [System.IO.File]::AppendAllText($env:FAKE_CALLS_FILE, $joined + [Environment]::NewLine)
}
if ($scenario -eq 'permissions' -and $joined.Contains('repos/acme/project')) { '{"permissions":{"push":false}}'; exit 0 }
if ($scenario -eq 'permissions-unknown' -and $joined.Contains('repos/acme/project')) { '{"permissions":{}}'; exit 0 }
if ($args[0] -eq 'repo' -and $args[1] -eq 'view') {
    [System.IO.File]::WriteAllText($env:FAKE_OWNER_LOOKUP_FILE, ($args | ConvertTo-Json -Compress))
    if ($args.Count -ne 5 -or $args[2] -ne $env:FAKE_EXPECTED_REPOSITORY -or $args -contains '--repo') {
        [Console]::Error.WriteLine('gh repo view requires a positional repository')
        exit 2
    }
    if ($scenario -eq 'owner-lookup-fail') {
        [Console]::Error.WriteLine('owner lookup failed')
        exit 2
    }
    '{"nameWithOwner":"acme/project","owner":{"login":"owner"},"viewerPermission":"ADMIN"}'
    exit 0
}
if ($args[0] -eq 'pr' -and $args[1] -eq 'view') {
    if ($scenario -eq 'pr-api-error') { [Console]::Error.WriteLine('unavailable'); exit 1 }
    $mergeable = if ($scenario -eq 'conflict') { 'CONFLICTING' } elseif ($scenario -eq 'mergeable-unknown') { 'UNKNOWN' } else { 'MERGEABLE' }
    $labels = @()
    if ($scenario -ne 'review-missing') { $labels += @{name='technical-reviewed'} }
    if ($scenario -ne 'owner-missing') { $labels += @{name='owner-approved'} }
    $labels = $labels | ConvertTo-Json -Compress
    $headTime = if ($scenario -eq 'head-invalid') { 'not-a-timestamp' }
        elseif ($scenario -eq 'head-missing') { $null }
        elseif ($scenario -eq 'offset-timestamps') { '2026-10-13T12:00:00+02:00' }
        else { '2026-10-13T10:00:00Z' }
    $commit = @{oid=$sha;committedDate=$headTime}
    $number = if ($scenario -eq 'pr-number-mismatch') { 8 } else { 7 }
    $state = if ($scenario -eq 'pr-closed') { 'CLOSED' } else { 'OPEN' }
    $draft = $scenario -eq 'pr-draft'
    $reviewDecision = if ($scenario -eq 'protected-success') { 'APPROVED' } else { '' }
    $headBranch = if ($scenario -eq 'pr-branch-mismatch') { 'feature/other' } else { 'feature/test' }
    $pr = @{number=$number;state=$state;isDraft=$draft;headRefOid=$sha;headRefName=$headBranch;baseRefName='main';author=@{login='author'};labels=@($labels | ConvertFrom-Json);mergeable=$mergeable;reviewDecision=$reviewDecision;commits=@($commit)}
    ConvertTo-Json -InputObject $pr -Compress -Depth 6
    exit 0
}
if ($args[0] -eq 'pr' -and $args[1] -eq 'checks') {
    if ($scenario -eq 'gates-running') { '[{"name":"quality","state":"IN_PROGRESS","bucket":"pending"}]' }
    elseif ($scenario -eq 'gates-failed') { '[{"name":"quality","state":"FAILURE","bucket":"fail"}]' }
    else { '[{"name":"quality","state":"SUCCESS","bucket":"pass"}]' }
    exit 0
}
if ($joined.Contains("commits/$sha/check-runs")) {
    $quality = @{name='quality';head_sha=$sha;status='completed';conclusion='success';app=@{slug='github-actions'}}
    if ($scenario -eq 'quality-missing') { $runs = @() }
    elseif ($scenario -eq 'quality-duplicate') { $runs = @($quality, $quality) }
    elseif ($scenario -eq 'quality-wrong-source') { $quality.app.slug = 'external-ci'; $runs = @($quality) }
    elseif ($scenario -eq 'quality-wrong-sha') { $quality.head_sha = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'; $runs = @($quality) }
    elseif ($scenario -eq 'quality-running') { $quality.status = 'in_progress'; $quality.conclusion = $null; $runs = @($quality) }
    elseif ($scenario -eq 'quality-skipped') { $quality.conclusion = 'skipped'; $runs = @($quality) }
    elseif ($scenario -eq 'quality-failed') { $quality.conclusion = 'failure'; $runs = @($quality) }
    else { $runs = @($quality) }
    ConvertTo-Json -InputObject (, @{total_count=$runs.Count;check_runs=$runs}) -Compress -Depth 5
    exit 0
}
if ($joined.Contains('reviews')) {
    '[]'
    exit 0
}
if ($joined.Contains('events')) {
    $events = @()
    $technicalLabelTime = if ($scenario -eq 'label-before-comment') { '2026-10-13T10:04:00Z' }
        elseif ($scenario -eq 'label-same-time-as-comment') { '2026-10-13T10:05:00Z' }
        elseif ($scenario -eq 'offset-timestamps') { '2026-10-13T12:06:00+02:00' }
        elseif ($scenario -eq 'event-invalid') { 'not-a-timestamp' }
        elseif ($scenario -eq 'event-missing') { $null }
        else { '2026-10-13T10:06:00Z' }
    if ($scenario -ne 'review-missing') { $events += @{event='labeled';created_at=$technicalLabelTime;actor=@{login='owner'};label=@{name='technical-reviewed'}} }
    if ($scenario -ne 'owner-missing') { $events += @{event='labeled';created_at='2026-10-13T10:08:00Z';actor=@{login='owner'};label=@{name='owner-approved'}} }
    ConvertTo-Json -InputObject (, $events) -Compress -Depth 5
    exit 0
}
if ($joined.Contains('issues/7/comments')) {
    $comments = @()
    $attestationAuthor = if ($scenario -eq 'attestation-wrong-author') { 'author' } else { 'owner' }
    $technicalSha = if ($scenario -eq 'review-stale') { 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' } else { $sha }
    $technicalTime = if ($scenario -eq 'review-too-early') { '2026-10-13T09:59:00Z' }
        elseif ($scenario -eq 'offset-timestamps') { '2026-10-13T12:05:00+02:00' }
        elseif ($scenario -eq 'comment-invalid') { 'not-a-timestamp' }
        elseif ($scenario -eq 'comment-missing') { $null }
        else { '2026-10-13T10:05:00Z' }
    $technicalUpdatedTime = if ($scenario -eq 'comment-edited') { '2026-10-13T10:06:00Z' } else { $technicalTime }
    if ($scenario -ne 'review-missing') {
        $comments += @{id=101;created_at=$technicalTime;updated_at=$technicalUpdatedTime;author_association='OWNER';user=@{login=$attestationAuthor;type='User'};body="DECKRELAY-ATTESTATION-V1 type=technical-review pr=7 head=$technicalSha decision=approved"}
    }
    if ($scenario -ne 'owner-missing') {
        $comments += @{id=102;created_at='2026-10-13T10:07:00Z';updated_at='2026-10-13T10:07:00Z';author_association='OWNER';user=@{login='owner';type='User'};body="DECKRELAY-ATTESTATION-V1 type=owner-approval pr=7 head=$sha decision=approved"}
    }
    if ($scenario -eq 'review-revoked') {
        $comments += @{id=103;created_at='2026-10-13T10:09:00Z';updated_at='2026-10-13T10:09:00Z';author_association='OWNER';user=@{login='owner';type='User'};body="DECKRELAY-ATTESTATION-V1 type=technical-review pr=7 head=$sha decision=revoked"}
    }
    ConvertTo-Json -InputObject (, $comments) -Compress -Depth 5
    exit 0
}
if ($joined.Contains('branches/main/protection')) {
    if ($joined.Contains('--include')) {
        if ($scenario -eq 'protection-403-with-404-text') { 'HTTP/2.0 403 Forbidden'; [Console]::Error.WriteLine('request 404 API_SECRET_PAYLOAD'); exit 1 }
        if ($scenario -eq 'protection-500') { 'HTTP/2.0 500 Internal Server Error'; exit 1 }
        if ($scenario -eq 'protection-network') { [Console]::Error.WriteLine('network API_SECRET_PAYLOAD 404'); exit 1 }
        if ($scenario -eq 'protection' -or $scenario -eq 'protected-success') { 'HTTP/2.0 200 OK'; exit 0 }
        'HTTP/2.0 404 Not Found'; exit 1
    }
    if ($scenario -eq 'protection' -or $scenario -eq 'protected-success') { '{"required_pull_request_reviews":{"required_approving_review_count":1}}'; exit 0 }
    [Console]::Error.WriteLine('unexpected protection body request'); exit 9
}
if ($joined.Contains('rules/branches/main')) {
    if ($scenario -eq 'rules-effective' -or $scenario -eq 'protected-success') { '[{"type":"pull_request","parameters":{"required_approving_review_count":1}}]'; exit 0 }
    if ($scenario -eq 'rules-ambiguous' -or $scenario -eq 'rules-network') { [Console]::Error.WriteLine('effective rules unavailable API_SECRET_PAYLOAD'); exit 1 }
    '[]'
    exit 0
}
if ($joined.Contains('rulesets')) { [Console]::Error.WriteLine('ruleset pattern endpoint must not be used'); exit 99 }
if ($args[0] -eq 'pr' -and $args[1] -eq 'merge') {
    [System.IO.File]::WriteAllText($env:FAKE_MERGE_FILE, $joined)
    'merged'; exit 0
}
'{"permissions":{"push":true},"private_data":"API_SECRET_PAYLOAD"}'
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["FAKE_HEAD_SHA"] = git(tmp_path / "repository with spaces", "rev-parse", "HEAD")
    env["FAKE_MERGE_FILE"] = str(tmp_path / "merge invocation.txt")
    env["FAKE_CALLS_FILE"] = str(tmp_path / "gh calls.txt")
    env["FAKE_OWNER_LOOKUP_FILE"] = str(tmp_path / "owner lookup arguments.json")
    env["FAKE_EXPECTED_REPOSITORY"] = "acme/project"
    return executable, env


def owner_approved_args(repository: Path, gh: Path, action: str) -> tuple[str, ...]:
    return (
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
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
    )


def test_gates_require_unique_successful_quality_check_from_github_actions(
    repository: Path, tmp_path: Path
) -> None:
    gh, env = write_fake_gh(tmp_path)

    result = run(*owner_approved_args(repository, gh, "gates"), cwd=repository, env=env)

    assert result.returncode == 0, combined(result)
    assert "GATES: PASS" in result.stdout


@pytest.mark.parametrize(
    "scenario",
    [
        "quality-missing",
        "quality-duplicate",
        "quality-wrong-source",
        "quality-wrong-sha",
        "quality-running",
        "quality-skipped",
        "quality-failed",
    ],
)
def test_gates_block_without_unambiguous_successful_quality_check(
    repository: Path, tmp_path: Path, scenario: str
) -> None:
    gh, env = write_fake_gh(tmp_path)
    env["FAKE_GH_SCENARIO"] = scenario

    result = run(*owner_approved_args(repository, gh, "gates"), cwd=repository, env=env)

    assert result.returncode != 0
    assert "quality" in combined(result).lower()


@pytest.mark.parametrize("action", ["gates", "merge"])
@pytest.mark.parametrize(
    "scenario",
    ["pr-branch-mismatch", "remote-repository-mismatch", "remote-head-mismatch"],
)
def test_gates_and_merge_reject_inconsistent_local_pr_context(
    repository: Path, tmp_path: Path, action: str, scenario: str
) -> None:
    gh, env = write_fake_gh(tmp_path)
    if scenario == "pr-branch-mismatch":
        env["FAKE_GH_SCENARIO"] = scenario
    elif scenario == "remote-repository-mismatch":
        git(repository, "remote", "set-url", "DeckRelay", "https://github.com/acme/other.git")
    else:
        git(
            repository,
            "update-ref",
            "refs/remotes/DeckRelay/feature/test",
            git(repository, "rev-parse", "HEAD^"),
        )
    extra = ("-ConfirmMerge",) if action == "merge" else ()

    result = run(*owner_approved_args(repository, gh, action), *extra, cwd=repository, env=env)

    assert result.returncode != 0
    assert not Path(env["FAKE_MERGE_FILE"]).exists()


def test_gates_reject_repository_root_that_is_only_a_subdirectory(
    repository: Path, tmp_path: Path
) -> None:
    gh, env = write_fake_gh(tmp_path)
    nested = repository / "nested"
    nested.mkdir()
    args = list(owner_approved_args(repository, gh, "gates"))
    args[args.index(str(repository))] = str(nested)

    result = run(*args, cwd=repository, env=env)

    assert result.returncode != 0


def test_preflight_reports_ready_and_never_mutates(repository: Path, tmp_path: Path) -> None:
    gh, env = write_fake_gh(tmp_path)
    env["FAKE_GH_SCENARIO"] = "protected-success"
    before_head = git(repository, "rev-parse", "HEAD")
    before_status = git(repository, "status", "--porcelain=v1")
    before_worktrees = git(repository, "worktree", "list", "--porcelain")

    result = run(
        *owner_approved_args(repository, gh, "preflight"),
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )

    assert result.returncode == 0, combined(result)
    assert "READY_FOR_EXPLICIT_MERGE_AUTHORIZATION" in result.stdout
    assert not Path(env["FAKE_MERGE_FILE"]).exists()
    calls = Path(env["FAKE_CALLS_FILE"]).read_text(encoding="utf-8")
    assert "pr merge" not in calls
    assert "workflow run" not in calls
    assert "--method POST" not in calls
    assert git(repository, "rev-parse", "HEAD") == before_head
    assert git(repository, "status", "--porcelain=v1") == before_status
    assert git(repository, "worktree", "list", "--porcelain") == before_worktrees


@pytest.mark.parametrize(
    "scenario",
    [
        "pr-number-mismatch",
        "pr-closed",
        "pr-draft",
        "review-missing",
        "owner-missing",
        "review-stale",
        "review-too-early",
        "comment-edited",
        "review-revoked",
        "attestation-wrong-author",
        "label-before-comment",
        "gates-running",
        "gates-failed",
        "permissions",
        "permissions-unknown",
        "protection",
        "rules-effective",
        "rules-ambiguous",
        "protection-403-with-404-text",
        "protection-500",
        "protection-network",
        "conflict",
        "mergeable-unknown",
        "pr-api-error",
    ],
)
def test_preflight_fails_closed(repository: Path, tmp_path: Path, scenario: str) -> None:
    gh, env = write_fake_gh(tmp_path)
    env["FAKE_GH_SCENARIO"] = scenario
    result = run(*owner_approved_args(repository, gh, "preflight"), cwd=repository, env=env)
    assert result.returncode != 0
    assert "READY_FOR_EXPLICIT_MERGE_AUTHORIZATION" not in combined(result)
    assert not Path(env["FAKE_MERGE_FILE"]).exists()


@pytest.mark.parametrize("scenario", ["review-stale", "gates-running", "conflict"])
def test_preflight_and_merge_share_readiness_failures(
    repository: Path, tmp_path: Path, scenario: str
) -> None:
    results = []
    for action in ("preflight", "merge"):
        gh, env = write_fake_gh(tmp_path)
        env["FAKE_GH_SCENARIO"] = scenario
        extra = ("-ConfirmMerge",) if action == "merge" else ()
        results.append(
            run(
                *owner_approved_args(repository, gh, action),
                *extra,
                cwd=repository,
                env=env,
            )
        )
    assert results[0].returncode == results[1].returncode
    preflight_cause = combined(results[0]).split("CAUSE: ", 1)[1].splitlines()[0]
    merge_cause = combined(results[1]).split("CAUSE: ", 1)[1].splitlines()[0]
    assert preflight_cause == merge_cause


@pytest.mark.parametrize(
    ("scenario", "action", "expected"),
    [
        ("permissions", "publish", "BLOCKED"),
        ("review-missing", "merge", "Review"),
        ("owner-missing", "merge", "Eigentümerfreigabe"),
        ("review-stale", "merge", "Review"),
        ("review-too-early", "merge", "Review"),
        ("review-revoked", "merge", "Review"),
        ("attestation-wrong-author", "merge", "Review"),
        ("gates-running", "gates", "BLOCKED"),
        ("gates-failed", "gates", "FAIL"),
        ("conflict", "merge", "Mergefähigkeit"),
        ("protection", "merge", "Branch-Protection"),
        ("rules-effective", "merge", "Ruleset"),
        ("rules-ambiguous", "merge", "wirksame GitHub-Regeln"),
        ("protection-403-with-404-text", "merge", "Branch-Protection"),
        ("protection-500", "merge", "Branch-Protection"),
        ("protection-network", "merge", "Branch-Protection"),
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
        git(repository, "rev-parse", "HEAD"),
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


def test_merge_succeeds_with_distinct_sha_bound_attestations_and_no_formal_approval(
    repository: Path, tmp_path: Path
) -> None:
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
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )
    assert result.returncode == 0, combined(result)
    invocation = Path(env["FAKE_MERGE_FILE"]).read_text(encoding="utf-8")
    assert "--match-head-commit " + git(repository, "rev-parse", "HEAD") in invocation


@pytest.mark.parametrize(
    ("scenario", "lookup_succeeds"),
    [("", True), ("owner-lookup-fail", False)],
)
def test_owner_lookup_uses_positional_repository_and_blocks_merge_on_failure(
    repository: Path, tmp_path: Path, scenario: str, lookup_succeeds: bool
) -> None:
    gh, env = write_fake_gh(tmp_path)
    repository_name = "example/custom-project"
    git(
        repository,
        "remote",
        "set-url",
        "DeckRelay",
        f"https://github.com/{repository_name}.git",
    )
    env["FAKE_EXPECTED_REPOSITORY"] = repository_name
    env["FAKE_GH_SCENARIO"] = scenario
    result = run(
        "-Action",
        "merge",
        "-RepositoryRoot",
        str(repository),
        "-GitHubExecutable",
        str(gh),
        "-Repository",
        repository_name,
        "-PrNumber",
        "7",
        "-ExpectedHeadSha",
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )

    lookup_arguments = json.loads(Path(env["FAKE_OWNER_LOOKUP_FILE"]).read_text(encoding="utf-8"))
    assert lookup_arguments == ["repo", "view", repository_name, "--json", "owner"]
    merge_file = Path(env["FAKE_MERGE_FILE"])
    if lookup_succeeds:
        assert result.returncode == 0, combined(result)
        assert merge_file.exists()
    else:
        assert result.returncode != 0
        assert not merge_file.exists()


def test_attestations_use_short_labels_and_read_only_pr_comments(
    repository: Path, tmp_path: Path
) -> None:
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
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )
    assert result.returncode == 0, combined(result)
    source = SCRIPT_SOURCE.read_text(encoding="utf-8")
    assert "reviewed-head:" not in source
    assert "approved-head:" not in source
    assert "issues/$PrNumber/comments" in source
    assert '"--paginate", "--slurp"' in source
    assert '"--method", "POST"' not in source


def test_no_effective_ruleset_allows_attested_merge(repository: Path, tmp_path: Path) -> None:
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
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )
    assert result.returncode == 0, combined(result)


def test_logs_never_persist_api_payloads_or_error_details(repository: Path, tmp_path: Path) -> None:
    gh, env = write_fake_gh(tmp_path)
    env["FAKE_GH_SCENARIO"] = "protection-403-with-404-text"
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
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )
    assert result.returncode != 0
    assert "API_SECRET_PAYLOAD" not in combined(result)
    logs = list((repository / "logs" / "dev-pr").glob("*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "API_SECRET_PAYLOAD" not in log
    assert "private_data" not in log


@pytest.mark.parametrize("culture", ["de-DE", "en-US"])
def test_attestation_timestamp_decisions_are_culture_independent(
    repository: Path, tmp_path: Path, culture: str
) -> None:
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
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
        culture=culture,
    )

    assert result.returncode == 0, combined(result)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("head-invalid", "Head-Zeit"),
        ("head-missing", "Head-Zeit"),
        ("comment-invalid", "serverseitige Zeitangaben"),
        ("comment-missing", "serverseitige Zeitangaben"),
        ("event-invalid", "Label-Ereigniszeit"),
        ("event-missing", "Label-Ereigniszeit"),
        ("review-too-early", "review"),
        ("label-before-comment", "technical-reviewed"),
        ("label-same-time-as-comment", "technical-reviewed"),
        ("comment-edited", "verändert"),
    ],
)
def test_invalid_and_out_of_order_attestation_timestamps_block(
    repository: Path, tmp_path: Path, scenario: str, expected: str
) -> None:
    gh, env = write_fake_gh(tmp_path)
    env["FAKE_GH_SCENARIO"] = scenario
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
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
    )

    assert result.returncode != 0
    assert expected.lower() in combined(result).lower()
    assert not Path(env["FAKE_MERGE_FILE"]).exists()


@pytest.mark.parametrize("culture", ["de-DE", "en-US"])
def test_utc_and_offset_timestamps_compare_as_the_same_instant(
    repository: Path, tmp_path: Path, culture: str
) -> None:
    gh, env = write_fake_gh(tmp_path)
    env["FAKE_GH_SCENARIO"] = "offset-timestamps"
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
        git(repository, "rev-parse", "HEAD"),
        "-Mode",
        "OWNER-APPROVED",
        "-ConfirmMerge",
        cwd=repository,
        env=env,
        culture=culture,
    )

    assert result.returncode == 0, combined(result)


def test_merge_rejects_mismatched_pr_head_and_label_after_comment_is_accepted(
    repository: Path, tmp_path: Path
) -> None:
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
    assert not Path(env["FAKE_MERGE_FILE"]).exists()


@pytest.mark.parametrize(
    ("value_expression", "culture", "expected"),
    [
        (
            "[DateTimeOffset]::Parse('2026-10-13T12:05:00+02:00', [CultureInfo]::InvariantCulture)",
            "de-DE",
            "2026-10-13T10:05:00.0000000+00:00",
        ),
        (
            "'2026-10-13T12:05:00+02:00'",
            "de-DE",
            "2026-10-13T10:05:00.0000000+00:00",
        ),
        (
            "'2026-10-13T10:05:00Z'",
            "en-US",
            "2026-10-13T10:05:00.0000000+00:00",
        ),
    ],
)
def test_timestamp_converter_normalizes_zoned_inputs(
    tmp_path: Path, value_expression: str, culture: str, expected: str
) -> None:
    result = run_timestamp_converter(
        tmp_path,
        value_expression,
        culture,
    )

    assert result.returncode == 0, combined(result)
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "value_expression",
    [
        "'2026-10-13T10:05:00'",
        "[DateTime]::SpecifyKind([DateTime]::new(2026,10,13,10,5,0), [DateTimeKind]::Unspecified)",
    ],
)
def test_timestamp_converter_rejects_ambiguous_inputs(
    tmp_path: Path, value_expression: str
) -> None:
    result = run_timestamp_converter(tmp_path, value_expression, "en-US")

    assert result.returncode == 3
    assert "BLOCKED: Invalid timestamp" in result.stderr


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
