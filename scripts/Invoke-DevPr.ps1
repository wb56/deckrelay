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
    [string]$PrTitle,
    [string]$PrBody,
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
        [Parameter(Mandatory = $true)][string]$Operation,
        [switch]$AllowFailure
    )
    # Never persist arguments, stdout or stderr: all three may contain credentials or API data.
    $script:LogLines.Add("CALL: $Operation")
    $process = $null
    try {
        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        if ([System.IO.Path]::GetExtension($Executable) -eq ".ps1") {
            $startInfo.FileName = "pwsh"
            foreach ($prefix in @("-NoLogo", "-NoProfile", "-File", $Executable)) {
                [void]$startInfo.ArgumentList.Add($prefix)
            }
        }
        else {
            $startInfo.FileName = $Executable
        }
        foreach ($argument in $Arguments) { [void]$startInfo.ArgumentList.Add($argument) }
        $startInfo.WorkingDirectory = $RepositoryRoot
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.CreateNoWindow = $true
        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) { throw "start failed" }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $code = $process.ExitCode
        $output = ($stdout + [Environment]::NewLine + $stderr).Trim()
    }
    catch {
        $code = 127
        $output = ""
    }
    finally {
        if ($null -ne $process) { $process.Dispose() }
    }
    $script:LogLines.Add("RESULT: exit=$code")
    if ($code -ne 0 -and -not $AllowFailure) {
        Stop-Failed "Werkzeugaufruf fehlgeschlagen ($Executable, Exit-Code $code)."
    }
    return [pscustomobject]@{ Code = $code; Text = (@($output) -join "`n") }
}

function Invoke-Git {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $safeRoot = $RepositoryRoot.Replace("\", "/")
    $verb = if ($Arguments.Count -gt 0 -and $Arguments[0] -match '^[a-z-]+$') { $Arguments[0] } else { "unknown" }
    Invoke-External $GitExecutable (@("-c", "safe.directory=$safeRoot", "-C", $RepositoryRoot) + $Arguments) -Operation "git:$verb" -AllowFailure:$AllowFailure
}

function Invoke-Gh {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $safeParts = @($Arguments | Select-Object -First 2 | Where-Object { $_ -match '^[a-z-]+$' })
    $operation = if ($safeParts.Count -gt 0) { "gh:" + ($safeParts -join ":") } else { "gh:unknown" }
    Invoke-External $GitHubExecutable $Arguments -Operation $operation -AllowFailure:$AllowFailure
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

function Assert-OwnerAttestation {
    param([string]$Name, $Pr, [string[]]$Labels, [string]$Description)
    $repoData = ConvertFrom-JsonSafe (Invoke-Gh @("repo", "view", "--repo", $Name, "--json", "owner")).Text "Eigentümer"
    $owner = $repoData.owner.login
    if ([string]::IsNullOrWhiteSpace($owner)) { Stop-Blocked "Repository-Eigentümer ist unbekannt." }
    $activeLabels = @($Pr.labels | ForEach-Object { $_.name })
    if (@($Labels | Where-Object { $_ -notin $activeLabels }).Count -gt 0) {
        Stop-Blocked "$Description ist am aktuellen PR nicht aktiv."
    }
    $events = @(ConvertFrom-JsonSafe (Invoke-Gh @("api", "repos/$Name/issues/$PrNumber/events")).Text $Description)
    foreach ($label in $Labels) {
        $valid = @($events | Where-Object {
            $_.event -eq "labeled" -and $_.label.name -ceq $label -and $_.actor.login -eq $owner
        })
        if ($valid.Count -eq 0) {
            Stop-Blocked "$Description '$label' durch den Repository-Eigentümer fehlt."
        }
    }
}

function Assert-BranchProtection {
    param([string]$Name, $Pr)
    $baseBranch = [string]$Pr.baseRefName
    if ([string]::IsNullOrWhiteSpace($baseBranch)) { Stop-Blocked "PR-Basisbranch ist unbekannt." }
    $encodedBranch = [uri]::EscapeDataString($baseBranch)
    $protectionPath = "repos/$Name/branches/$encodedBranch/protection"
    $probe = Invoke-Gh @("api", "--include", "--silent", $protectionPath) -AllowFailure
    $statusMatches = [regex]::Matches($probe.Text, '(?m)^HTTP/\S+\s+(?<status>\d{3})(?:\s|$)')
    if ($statusMatches.Count -ne 1) {
        Stop-Blocked "HTTP-Status der Branch-Protection konnte nicht eindeutig ermittelt werden."
    }
    $httpStatus = [int]$statusMatches[0].Groups["status"].Value
    if ($httpStatus -eq 404 -and $probe.Code -ne 0) {
        $protectionExists = $false
    }
    elseif ($httpStatus -eq 200 -and $probe.Code -eq 0) {
        $protectionExists = $true
    }
    else {
        Stop-Blocked "Branch-Protection konnte nicht zuverlässig geprüft werden."
    }
    if ($protectionExists) {
        $protectionResult = Invoke-Gh @("api", $protectionPath) -AllowFailure
        if ($protectionResult.Code -ne 0) {
            Stop-Blocked "Branch-Protection konnte nicht gelesen werden."
        }
        $protection = ConvertFrom-JsonSafe $protectionResult.Text "Branch-Protection"
        $required = $protection.required_pull_request_reviews.required_approving_review_count
        if ($null -ne $required -and [int]$required -gt 0 -and $Pr.reviewDecision -ne "APPROVED") {
            Stop-Blocked "Branch-Protection verlangt eine formelle GitHub-Review."
        }
    }
    # GitHub resolves include/exclude patterns server-side for this exact base branch.
    $rulesResult = Invoke-Gh @("api", "repos/$Name/rules/branches/$encodedBranch") -AllowFailure
    if ($rulesResult.Code -ne 0) {
        Stop-Blocked "Wirksame GitHub-Regeln konnten nicht zuverlässig geprüft werden."
    }
    $rules = @(ConvertFrom-JsonSafe $rulesResult.Text "wirksame GitHub-Regeln")
    foreach ($rule in @($rules | Where-Object { $_.type -eq "pull_request" })) {
        $rulesetRequired = $rule.parameters.required_approving_review_count
        if ($null -eq $rulesetRequired) {
            Stop-Blocked "Wirksames GitHub-Ruleset enthält unbekannte Review-Anforderungen."
        }
        if ([int]$rulesetRequired -gt 0 -and $Pr.reviewDecision -ne "APPROVED") {
            Stop-Blocked "GitHub-Ruleset verlangt eine formelle GitHub-Review."
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
            if ([string]::IsNullOrWhiteSpace($PrTitle) -or [string]::IsNullOrWhiteSpace($PrBody)) {
                Stop-Blocked "Ein neuer PR erfordert expliziten Titel und Beschreibung."
            }
            $created = Invoke-Gh @(
                "pr", "create", "--repo", $name, "--base", "main", "--head", $branchName,
                "--title", $PrTitle, "--body", $PrBody
            )
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
        Assert-OwnerAttestation $name $pr @("technical-reviewed", "reviewed-head:$ExpectedHeadSha") "Technischer Review-Nachweis"
        Assert-OwnerAttestation $name $pr @("owner-approved", "approved-head:$ExpectedHeadSha") "Eigentümerfreigabe"
        Get-GateState $name $pr
        Assert-BranchProtection $name $pr
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
