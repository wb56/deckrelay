[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("quick", "regression", "full")]
    [string]$Profile,
    [string[]]$Tests,
    [string]$Keyword,
    [string]$Group
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$GroupConfiguration = Join-Path $PSScriptRoot "test-groups.psd1"
$LogDirectory = Join-Path $ProjectRoot "logs\dev-tests"

function Stop-DevTestRun {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine("Fehler: $Message")
    exit 2
}

function Assert-TestSelector {
    param([Parameter(Mandatory = $true)][string]$Selector)
    if ([string]::IsNullOrWhiteSpace($Selector) -or $Selector.StartsWith("-")) {
        Stop-DevTestRun "Ungültiger Testpfad oder Node-ID: '$Selector'."
    }
    $PathPart = $Selector.Split("::", 2)[0]
    if ([System.IO.Path]::IsPathRooted($PathPart)) {
        Stop-DevTestRun "Testpfade müssen relativ zum Projekt sein: '$Selector'."
    }
    $Candidate = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $PathPart))
    $TestsRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "tests"))
    if (-not $Candidate.StartsWith(
            $TestsRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        Stop-DevTestRun "Ungültiger oder fehlender Testpfad: '$PathPart'."
    }
}

$Selectors = [System.Collections.Generic.List[string]]::new()
switch ($Profile) {
    "quick" {
        if (-not $Tests -and [string]::IsNullOrWhiteSpace($Keyword)) {
            Stop-DevTestRun "quick erfordert mindestens -Tests oder -Keyword."
        }
        if (-not [string]::IsNullOrWhiteSpace($Group)) {
            Stop-DevTestRun "quick akzeptiert keine Regressionstestgruppe."
        }
        foreach ($Selector in $Tests) {
            Assert-TestSelector $Selector
            $Selectors.Add($Selector)
        }
    }
    "regression" {
        if ([string]::IsNullOrWhiteSpace($Group)) {
            Stop-DevTestRun "regression erfordert -Group."
        }
        if ($Tests -or -not [string]::IsNullOrWhiteSpace($Keyword)) {
            Stop-DevTestRun "regression akzeptiert nur eine deklarierte Gruppe."
        }
        if (-not (Test-Path -LiteralPath $GroupConfiguration -PathType Leaf)) {
            Stop-DevTestRun "Gruppenkonfiguration fehlt: $GroupConfiguration"
        }
        $Groups = Import-PowerShellDataFile -LiteralPath $GroupConfiguration
        if (-not $Groups.ContainsKey($Group)) {
            Stop-DevTestRun "Unbekannte Regressionstestgruppe: '$Group'."
        }
        $ConfiguredSelectors = @($Groups[$Group])
        if ($ConfiguredSelectors.Count -eq 0) {
            Stop-DevTestRun "Regressionstestgruppe '$Group' ist leer."
        }
        foreach ($Selector in $ConfiguredSelectors) {
            if ($Selector -isnot [string]) {
                Stop-DevTestRun "Regressionstestgruppe '$Group' enthält einen ungültigen Eintrag."
            }
            Assert-TestSelector $Selector
            $Selectors.Add($Selector)
        }
    }
    "full" {
        if ($Tests -or -not [string]::IsNullOrWhiteSpace($Keyword) -or
            -not [string]::IsNullOrWhiteSpace($Group)) {
            Stop-DevTestRun "full akzeptiert keine Testauswahlparameter."
        }
    }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Stop-DevTestRun "Python-Interpreter fehlt: $Python"
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$LogPath = Join-Path $LogDirectory "$Timestamp-$Profile.log"
$Arguments = [System.Collections.Generic.List[string]]::new()
foreach ($Argument in @("-m", "pytest", "-q", "--tb=short", "-x", "--no-header", "--no-summary")) {
    $Arguments.Add($Argument)
}
foreach ($Selector in $Selectors) {
    $Arguments.Add($Selector)
}
if (-not [string]::IsNullOrWhiteSpace($Keyword)) {
    $Arguments.Add("-k")
    $Arguments.Add($Keyword)
}

$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$Process = $null
try {
    $StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $Python
    $StartInfo.WorkingDirectory = $ProjectRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true
    foreach ($Argument in $Arguments) {
        [void]$StartInfo.ArgumentList.Add($Argument)
    }
    $Process = [System.Diagnostics.Process]::new()
    $Process.StartInfo = $StartInfo
    if (-not $Process.Start()) {
        throw "Der pytest-Prozess konnte nicht gestartet werden."
    }
    $StandardOutputTask = $Process.StandardOutput.ReadToEndAsync()
    $StandardErrorTask = $Process.StandardError.ReadToEndAsync()
    $Process.WaitForExit()
    $StandardOutput = $StandardOutputTask.GetAwaiter().GetResult()
    $StandardError = $StandardErrorTask.GetAwaiter().GetResult()
    $ExitCode = $Process.ExitCode
}
catch {
    $Stopwatch.Stop()
    $Message = "pytest-Startfehler: $($_.Exception.Message)"
    [System.IO.File]::WriteAllText($LogPath, $Message + [Environment]::NewLine)
    [Console]::Error.WriteLine("Fehler: $Message")
    [Console]::Error.WriteLine("Log: $LogPath")
    exit 2
}
finally {
    if ($null -ne $Process) {
        $Process.Dispose()
    }
}
$Stopwatch.Stop()

$LogContent = @(
    "Profil: $Profile"
    "Argumente: " + ($Arguments | ConvertTo-Json -Compress)
    "Exit-Code: $ExitCode"
    "--- stdout ---"
    $StandardOutput
    "--- stderr ---"
    $StandardError
) -join [Environment]::NewLine
[System.IO.File]::WriteAllText($LogPath, $LogContent, [System.Text.UTF8Encoding]::new($false))

$CombinedOutput = $StandardOutput + [Environment]::NewLine + $StandardError
$ResultLine = ($CombinedOutput -split "`r?`n" |
    Where-Object { $_ -match "\d+ (passed|failed|errors?|skipped|xfailed|xpassed)" } |
    Select-Object -Last 1)
$TestCount = 0
if ($ResultLine) {
    foreach ($Match in [regex]::Matches(
            $ResultLine,
            "(?<count>\d+) (?:passed|failed|errors?|skipped|xfailed|xpassed)")) {
        $TestCount += [int]$Match.Groups["count"].Value
    }
}

$Result = if ($ExitCode -eq 0) { "PASS" } else { "FAIL" }
Write-Output "Profil: $Profile"
Write-Output "Ergebnis: $Result"
Write-Output "Tests: $TestCount"
Write-Output ("Dauer: {0:N2}s" -f $Stopwatch.Elapsed.TotalSeconds)
Write-Output "Log: $LogPath"
if ($ExitCode -ne 0) {
    $FailureLines = @($CombinedOutput -split "`r?`n" |
        Where-Object { $_ -match "^(FAILED|ERROR|E\s+)" } |
        Select-Object -First 4)
    if ($FailureLines.Count -gt 0) {
        Write-Output "Fehlerauszug:"
        foreach ($Line in $FailureLines) {
            if ($Line.Length -gt 300) {
                Write-Output ($Line.Substring(0, 300) + "...")
            }
            else {
                Write-Output $Line
            }
        }
    }
}
exit $ExitCode
