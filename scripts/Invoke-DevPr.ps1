[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("status", "validate", "commit", "publish", "gates", "merge", "cleanup")]
    [string]$Action,
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Repository,
    [string]$Remote = "DeckRelay",
    [string[]]$Files,
    [string]$Message,
    [int]$PrNumber,
    [string]$ExpectedHeadSha,
    [ValidateSet("REVIEW-REQUIRED", "OWNER-APPROVED")]
    [string]$Mode = "REVIEW-REQUIRED",
    [switch]$ConfirmMerge,
    [string]$Branch,
    [string]$GitExecutable = "git",
    [string]$GitHubExecutable = "gh"
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
$ExitBlocked = 3
$ExitFailed = 2
$script:LogLines = [System.Collections.Generic.List[string]]::new()
$script:LogPath = $null

function Write-Result {
    param([string]$Status, [string]$Detail)
    Write-Output "STATUS: $Status"
    Write-Output $Detail
}

function Stop-Blocked {
    param([string]$Cause)
    Write-Result "BLOCKED" "CAUSE: $Cause"
    Save-Log
    exit $ExitBlocked
}

function Stop-Failed {
    param([string]$Cause)
    Write-Result "FAIL" "CAUSE: $Cause"
    Save-Log
    exit $ExitFailed
}

function Save-Log {
    if ($null -eq $script:LogPath) { return }
    [System.IO.File]::WriteAllLines($script:LogPath, $script:LogLines, $OutputEncoding)
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )
    $displayArguments = $Arguments | ForEach-Object {
        if ($_ -match '(?i)(token|secret|password|authorization)') { "<redacted>" } else { $_ }
    }
    $script:LogLines.Add("COMMAND: $Executable " + ($displayArguments -join " "))
    $global:LASTEXITCODE = 0
    $output = & $Executable @Arguments 2>&1
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    foreach ($line in @($output)) { $script:LogLines.Add([string]$line) }
    $script:LogLines.Add("EXIT: $code")
    if ($code -ne 0 -and -not $AllowFailure) {
        Stop-Failed "Werkzeugaufruf fehlgeschlagen ($Executable, Exit-Code $code)."
    }
    return [pscustomobject]@{ Code = $code; Text = (@($output) -join "`n") }
}

function Invoke-Git {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $safeRoot = $RepositoryRoot.Replace("\", "/")
    Invoke-External $GitExecutable (@("-c", "safe.directory=$safeRoot", "-C", $RepositoryRoot) + $Arguments) -AllowFailure:$AllowFailure
}

function Invoke-Gh {
    param([string[]]$Arguments, [switch]$AllowFailure)
    Invoke-External $GitHubExecutable $Arguments -AllowFailure:$AllowFailure
}

function ConvertFrom-JsonSafe {
    param([string]$Text, [string]$Context)
    try { return $Text | ConvertFrom-Json }
    catch { Stop-Blocked "Unbekannte oder ungültige GitHub-Antwort für $Context." }
}

function Get-CurrentBranch {
    (Invoke-Git @("branch", "--show-current")).Text.Trim()
}

function Assert-FeatureBranch {
    $current = Get-CurrentBranch
    if ([string]::IsNullOrWhiteSpace($current) -or $current -eq "main" -or
        -not ($current.StartsWith("feature/") -or $current.StartsWith("fix/") -or
            $current.StartsWith("ci/") -or $current.StartsWith("docs/"))) {
        Stop-Blocked "Aktion ist nur auf einem eindeutig benannten Featurebranch zulässig."
    }
    return $current
}

function Assert-CleanTree {
    $status = (Invoke-Git @(
            "status", "--porcelain=v1", "--untracked-files=all", "--", ".",
            ":(exclude)logs/dev-pr/**"
        )).Text
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        Stop-Blocked "Working Tree enthält uncommittete Änderungen."
    }
}

function Resolve-Repository {
    if (-not [string]::IsNullOrWhiteSpace($Repository)) { return $Repository }
    $view = Invoke-Gh @("repo", "view", "--json", "nameWithOwner")
    (ConvertFrom-JsonSafe $view.Text "Repository").nameWithOwner
}

function Assert-PushPermission {
    param([string]$Name)
    $response = Invoke-Gh @("api", "repos/$Name")
    $data = ConvertFrom-JsonSafe $response.Text "Berechtigungen"
    if ($null -eq $data.permissions -or $data.permissions.push -ne $true) {
        Stop-Blocked "Fehlende GitHub-Push-Berechtigung."
    }
}

function Get-PrData {
    param([string]$Name)
    if ($PrNumber -le 0) { Stop-Blocked "Eine PR-Nummer ist erforderlich." }
    $response = Invoke-Gh @(
        "pr", "view", [string]$PrNumber, "--repo", $Name, "--json",
        "number,state,isDraft,headRefOid,headRefName,baseRefName,author,labels,mergeable,reviewDecision"
    )
    ConvertFrom-JsonSafe $response.Text "PR-Status"
}

function Assert-ExpectedHead {
    param($Pr)
    if ($ExpectedHeadSha -notmatch '^[0-9a-fA-F]{40}$') {
        Stop-Blocked "Der erwartete vollständige Head-SHA fehlt."
    }
    if ($Pr.headRefOid -ne $ExpectedHeadSha) {
        Stop-Blocked "Head-SHA weicht vom ausdrücklich geprüften SHA ab."
    }
}

function Get-GateState {
    param([string]$Name, $Pr)
    Assert-ExpectedHead $Pr
    $response = Invoke-Gh @(
        "pr", "checks", [string]$PrNumber, "--repo", $Name, "--json", "name,state,bucket"
    )
    $checks = @(ConvertFrom-JsonSafe $response.Text "Quality Gates")
    if ($checks.Count -eq 0) { Stop-Blocked "Keine Quality Gates für den Head-SHA gefunden." }
    $failed = @($checks | Where-Object { $_.bucket -eq "fail" -or $_.state -in @("FAILURE", "ERROR", "CANCELLED") })
    if ($failed.Count -gt 0) { Stop-Failed "Mindestens ein Quality Gate ist fehlgeschlagen." }
    $pending = @($checks | Where-Object { $_.bucket -ne "pass" -and $_.state -ne "SUCCESS" })
    if ($pending.Count -gt 0) { Stop-Blocked "Quality Gates laufen noch oder haben einen unbekannten Zustand." }
}

function Assert-ReviewEvidence {
    param([string]$Name, $Pr)
    $response = Invoke-Gh @("api", "repos/$Name/pulls/$PrNumber/reviews")
    $reviews = @(ConvertFrom-JsonSafe $response.Text "Reviews")
    $valid = @($reviews | Where-Object {
        $_.state -eq "APPROVED" -and $_.commit_id -eq $ExpectedHeadSha -and
        $_.user.login -ne $Pr.author.login -and $_.user.type -eq "User"
    })
    if ($valid.Count -eq 0) {
        Stop-Blocked "Unabhängige technische Review für den exakten Head-SHA fehlt."
    }
    return $valid.Count
}

function Assert-OwnerApproval {
    param([string]$Name, $Pr)
    $repoData = ConvertFrom-JsonSafe (Invoke-Gh @("repo", "view", "--repo", $Name, "--json", "owner")).Text "Eigentümer"
    $owner = $repoData.owner.login
    if ([string]::IsNullOrWhiteSpace($owner)) { Stop-Blocked "Repository-Eigentümer ist unbekannt." }
    $labels = @("owner-approved", "head:$ExpectedHeadSha")
    $activeLabels = @($Pr.labels | ForEach-Object { $_.name })
    if (@($labels | Where-Object { $_ -notin $activeLabels }).Count -gt 0) {
        Stop-Blocked "SHA-gebundene Eigentümerfreigabe ist am aktuellen PR nicht aktiv."
    }
    $events = @(ConvertFrom-JsonSafe (Invoke-Gh @("api", "repos/$Name/issues/$PrNumber/events")).Text "Eigentümerfreigabe")
    foreach ($label in $labels) {
        $valid = @($events | Where-Object {
            $_.event -eq "labeled" -and $_.label.name -ceq $label -and $_.actor.login -eq $owner
        })
        if ($valid.Count -eq 0) {
            Stop-Blocked "Eigentümerfreigabe '$label' durch den Repository-Eigentümer fehlt."
        }
    }
}

function Assert-BranchProtection {
    param([string]$Name, [int]$ReviewCount)
    $result = Invoke-Gh @("api", "repos/$Name/branches/main/protection") -AllowFailure
    if ($result.Code -ne 0 -and $result.Text -notmatch '(?i)(404|not protected)') {
        Stop-Blocked "Branch-Protection konnte nicht zuverlässig geprüft werden."
    }
    if ($result.Code -eq 0) {
        $protection = ConvertFrom-JsonSafe $result.Text "Branch-Protection"
        $required = $protection.required_pull_request_reviews.required_approving_review_count
        if ($null -ne $required -and [int]$required -gt $ReviewCount) {
            Stop-Blocked "Branch-Protection verlangt zusätzliche externe Reviews."
        }
    }
    $rulesetResult = Invoke-Gh @("api", "repos/$Name/rulesets?includes_parents=true") -AllowFailure
    if ($rulesetResult.Code -ne 0) {
        Stop-Blocked "GitHub-Rulesets konnten nicht zuverlässig geprüft werden."
    }
    $rulesets = @(ConvertFrom-JsonSafe $rulesetResult.Text "GitHub-Rulesets")
    foreach ($summary in $rulesets) {
        if ($summary.target -ne "branch" -or $summary.enforcement -ne "active") { continue }
        $detail = ConvertFrom-JsonSafe (Invoke-Gh @("api", "repos/$Name/rulesets/$($summary.id)")).Text "GitHub-Ruleset"
        $includes = @($detail.conditions.ref_name.include)
        $appliesToMain = $includes.Count -eq 0 -or "~DEFAULT_BRANCH" -in $includes -or
            "refs/heads/main" -in $includes -or "main" -in $includes
        if (-not $appliesToMain) { continue }
        foreach ($rule in @($detail.rules | Where-Object { $_.type -eq "pull_request" })) {
            $rulesetRequired = $rule.parameters.required_approving_review_count
            if ($null -ne $rulesetRequired -and [int]$rulesetRequired -gt $ReviewCount) {
                Stop-Blocked "GitHub-Ruleset verlangt zusätzliche externe Reviews."
            }
        }
    }
}

if (-not (Test-Path -LiteralPath $RepositoryRoot -PathType Container)) {
    Write-Result "FAIL" "CAUSE: Repository-Pfad fehlt."
    exit $ExitFailed
}
$RepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
$logDirectory = Join-Path $RepositoryRoot "logs\dev-pr"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$script:LogPath = Join-Path $logDirectory ((Get-Date -Format "yyyyMMdd-HHmmss-fff") + "-$Action.log")
$script:LogLines.Add("ACTION: $Action")
$script:LogLines.Add("ROOT: $RepositoryRoot")

switch ($Action) {
    "status" {
        $branchName = Get-CurrentBranch
        $head = (Invoke-Git @("rev-parse", "HEAD")).Text.Trim()
        $dirty = -not [string]::IsNullOrWhiteSpace((Invoke-Git @("status", "--porcelain=v1")).Text)
        $remoteUrl = (Invoke-Git @("remote", "get-url", $Remote) -AllowFailure).Text.Trim()
        $worktreeCount = @((Invoke-Git @("worktree", "list", "--porcelain")).Text -split "`r?`n" |
            Where-Object { $_.StartsWith("worktree ") }).Count
        $prStatus = "unbekannt"
        try {
            $name = Resolve-Repository
            $prs = @(ConvertFrom-JsonSafe (Invoke-Gh @(
                        "pr", "list", "--repo", $name, "--head", $branchName, "--state", "all",
                        "--json", "number,state,url,headRefOid"
                    )).Text "PR-Status")
            if ($prs.Count -eq 0) { $prStatus = "keiner" }
            else { $prStatus = "#$($prs[0].number) $($prs[0].state) $($prs[0].headRefOid)" }
        }
        catch { $prStatus = "nicht verfügbar" }
        Write-Result "PASS" "REPOSITORY: $remoteUrl`nBRANCH: $branchName`nHEAD: $head`nDIRTY: $dirty`nWORKTREES: $worktreeCount`nPR: $prStatus`nLOG: $script:LogPath"
    }
    "validate" {
        [void](Assert-FeatureBranch)
        Assert-CleanTree
        Write-Result "PASS" "VALIDATION: sichere Vorbedingungen erfüllt`nLOG: $script:LogPath"
    }
    "commit" {
        [void](Assert-FeatureBranch)
        if (-not $Files -or [string]::IsNullOrWhiteSpace($Message)) {
            Stop-Blocked "Commit erfordert explizite Dateien und eine Commit-Nachricht."
        }
        $preStaged = (Invoke-Git @("diff", "--cached", "--name-only")).Text
        if (-not [string]::IsNullOrWhiteSpace($preStaged)) {
            Stop-Blocked "Bereits gestagte Änderungen verhindern ein deterministisches Commit."
        }
        $normalized = [System.Collections.Generic.List[string]]::new()
        foreach ($file in $Files) {
            if ([System.IO.Path]::IsPathRooted($file)) { Stop-Blocked "Dateipfade müssen relativ sein." }
            $full = [System.IO.Path]::GetFullPath((Join-Path $RepositoryRoot $file))
            if (-not $full.StartsWith($RepositoryRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
                Stop-Blocked "Dateipfad liegt außerhalb des Repositorys."
            }
            $normalized.Add($file.Replace("\", "/"))
        }
        [void](Invoke-Git (@("add", "--") + $normalized))
        $staged = @((Invoke-Git @("diff", "--cached", "--name-only")).Text -split "`r?`n" | Where-Object { $_ })
        if ($staged.Count -eq 0) {
            Write-Result "PASS" "COMMIT: NOOP`nLOG: $script:LogPath"
        }
        else {
            $unexpected = @($staged | Where-Object { $_ -notin $normalized })
            if ($unexpected.Count -gt 0) { Stop-Blocked "Nicht explizit angegebene Dateien wären im Commit enthalten." }
            [void](Invoke-Git @("commit", "-m", $Message, "--") )
            Write-Result "PASS" "COMMIT: erstellt`nFILES: $($staged.Count)`nLOG: $script:LogPath"
        }
    }
    "publish" {
        $branchName = Assert-FeatureBranch
        Assert-CleanTree
        $name = Resolve-Repository
        Assert-PushPermission $name
        [void](Invoke-Git @("push", "--set-upstream", $Remote, "HEAD:refs/heads/$branchName"))
        $existing = Invoke-Gh @("pr", "list", "--repo", $name, "--head", $branchName, "--state", "open", "--json", "number,url")
        $prs = @(ConvertFrom-JsonSafe $existing.Text "PR-Suche")
        if ($prs.Count -eq 0) {
            $created = Invoke-Gh @("pr", "create", "--repo", $name, "--base", "main", "--head", $branchName, "--fill")
            Write-Result "PASS" "PUBLISH: PR erstellt`nPR: $($created.Text.Trim())`nLOG: $script:LogPath"
        }
        else { Write-Result "PASS" "PUBLISH: vorhandener PR #$($prs[0].number)`nPR: $($prs[0].url)`nLOG: $script:LogPath" }
    }
    "gates" {
        $name = Resolve-Repository
        $pr = Get-PrData $name
        Get-GateState $name $pr
        Write-Result "PASS" "GATES: PASS`nHEAD: $ExpectedHeadSha`nLOG: $script:LogPath"
    }
    "merge" {
        if (-not $ConfirmMerge) { Stop-Blocked "Merge erfordert die ausdrückliche Option -ConfirmMerge." }
        if ($Mode -ne "OWNER-APPROVED") { Stop-Blocked "REVIEW-REQUIRED erlaubt keinen automatischen Merge." }
        $name = Resolve-Repository
        Assert-PushPermission $name
        $pr = Get-PrData $name
        Assert-ExpectedHead $pr
        if ($pr.state -ne "OPEN" -or $pr.isDraft -eq $true) { Stop-Blocked "PR ist nicht offen und review-bereit." }
        $reviewCount = Assert-ReviewEvidence $name $pr
        Assert-OwnerApproval $name $pr
        Get-GateState $name $pr
        Assert-BranchProtection $name $reviewCount
        if ($pr.mergeable -ne "MERGEABLE") { Stop-Blocked "Mergefähigkeit ist nicht eindeutig gegeben." }
        # --match-head-commit bindet die serverseitige Mutation an den erneut geprüften SHA.
        [void](Invoke-Gh @("pr", "merge", [string]$PrNumber, "--repo", $name, "--merge", "--match-head-commit", $ExpectedHeadSha))
        Write-Result "PASS" "MERGE: ausgeführt`nHEAD: $ExpectedHeadSha`nLOG: $script:LogPath"
    }
    "cleanup" {
        if ([string]::IsNullOrWhiteSpace($Branch) -or -not $Branch.StartsWith("feature/") -or $Branch -eq (Get-CurrentBranch)) {
            Stop-Blocked "Cleanup akzeptiert nur einen anderen, eindeutig zugehörigen Featurebranch."
        }
        $exists = Invoke-Git @("show-ref", "--verify", "--quiet", "refs/heads/$Branch") -AllowFailure
        if ($exists.Code -ne 0) { Write-Result "PASS" "CLEANUP: NOOP`nLOG: $script:LogPath"; break }
        $merged = Invoke-Git @("merge-base", "--is-ancestor", $Branch, "main") -AllowFailure
        if ($merged.Code -ne 0) { Stop-Blocked "Featurebranch ist nicht eindeutig in main integriert." }
        $worktrees = (Invoke-Git @("worktree", "list", "--porcelain")).Text
        if ($worktrees -match [regex]::Escape("branch refs/heads/$Branch")) {
            Stop-Blocked "Featurebranch ist noch an einen Worktree gebunden."
        }
        [void](Invoke-Git @("branch", "-d", "--", $Branch))
        Write-Result "PASS" "CLEANUP: lokaler Branch gelöscht`nBRANCH: $Branch`nLOG: $script:LogPath"
    }
}

Save-Log
exit 0
