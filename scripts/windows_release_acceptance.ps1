[CmdletBinding()]
param(
    [switch]$ContractProbe,
    [string]$CandidateRoot,
    [string]$GatePython,
    [string]$PrimaryRoot,
    [string]$SecondRoot,
    [string]$RawRoot,
    [string]$FreeCADCmd,
    [string]$FreeCADExe,
    [string]$ExternalMcpCheckout,
    [string]$ExternalMcpPython,
    [string]$AddonRoot,
    [string]$SettingsFile,
    [ValidateSet('W1', 'W2', 'W3', 'W4')]
    [string[]]$Gates = @('W1', 'W2', 'W3', 'W4')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$orderedGates = @('W1', 'W2', 'W3', 'W4')
if ($ContractProbe) {
    [ordered]@{
        schema_version = 'WindowsReleaseAcceptancePlan/v1'
        ordered_gates = $orderedGates
        cleanup_failure_overrides_body = $true
    } | ConvertTo-Json -Compress
    exit 0
}

function Assert-RequiredFile {
    param([string]$Value, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label is required"
    }
    $item = Get-Item -LiteralPath $Value -Force
    if (-not $item.PSIsContainer -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Label must not be a reparse point"
    }
    if ($item.PSIsContainer) {
        throw "$Label must be a regular file"
    }
    return $item.FullName
}

function Assert-FixedNtfsRoot {
    param([string]$Value, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label is required"
    }
    if ([IO.Path]::IsPathFullyQualified($Value) -ne $true -or $Value.StartsWith('\\')) {
        throw "$Label must be an absolute local path"
    }
    $item = Get-Item -LiteralPath $Value -Force
    if (-not $item.PSIsContainer) {
        throw "$Label must be a directory"
    }
    $cursor = $item
    while ($null -ne $cursor) {
        if ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "$Label must not contain a reparse ancestor"
        }
        $cursor = $cursor.Parent
    }
    $drive = [IO.Path]::GetPathRoot($item.FullName).Substring(0, 1)
    $volume = Get-Volume -DriveLetter $drive
    if ($volume.DriveType -ne 'Fixed' -or $volume.FileSystemType -ne 'NTFS') {
        throw "$Label must be on fixed NTFS"
    }
    return [ordered]@{ Path = $item.FullName; Volume = $volume.UniqueId }
}

function Assert-DistinctVolumes {
    param([object]$First, [object]$Second)
    if ($First.Volume -eq $Second.Volume) {
        throw 'PrimaryRoot and SecondRoot must use distinct fixed NTFS volumes'
    }
}

function Assert-Cpython312X64 {
    param([string]$Python)
    $resolved = Assert-RequiredFile -Value $Python -Label 'GatePython'
    $probe = & $resolved -c "import json,platform,struct,sys; print(json.dumps({'implementation':platform.python_implementation(),'version':list(sys.version_info[:3]),'bits':struct.calcsize('P')*8}))"
    if ($LASTEXITCODE -ne 0) { throw 'GatePython probe failed' }
    $identity = $probe | ConvertFrom-Json
    if ($identity.implementation -ne 'CPython' -or $identity.version[0] -ne 3 -or $identity.version[1] -ne 12 -or $identity.bits -ne 64) {
        throw 'GatePython must be CPython 3.12 x64'
    }
    return $resolved
}

function Get-JunitCounts {
    param([string]$Path)
    [xml]$xml = Get-Content -LiteralPath $Path -Raw
    $root = $xml.DocumentElement
    $suites = if ($root.LocalName -eq 'testsuites') {
        @($root.ChildNodes | Where-Object LocalName -eq 'testsuite')
    } else {
        @($root)
    }
    $tests = [int](($suites | ForEach-Object { [int]$_.GetAttribute('tests') } | Measure-Object -Sum).Sum)
    $failures = [int](($suites | ForEach-Object { [int]$_.GetAttribute('failures') } | Measure-Object -Sum).Sum)
    $errors = [int](($suites | ForEach-Object { [int]$_.GetAttribute('errors') } | Measure-Object -Sum).Sum)
    $skipped = [int](($suites | ForEach-Object { [int]$_.GetAttribute('skipped') } | Measure-Object -Sum).Sum)
    return [ordered]@{
        collected = $tests
        passed = $tests - $failures - $errors - $skipped
        failed = $failures + $errors
        skipped = $skipped
        unexpected_skips = $skipped
    }
}

function Invoke-GatePytest {
    param(
        [string]$Name,
        [string[]]$Nodes,
        [int]$ExpectedSkips = 0
    )
    $token = [guid]::NewGuid().ToString('N')
    $junit = Join-Path $script:rawDirectory "$Name-$token.xml"
    $stdout = Join-Path $script:rawDirectory "$Name-$token.stdout.txt"
    $stderr = Join-Path $script:rawDirectory "$Name-$token.stderr.txt"
    $arguments = @('-m', 'pytest') + $Nodes + @('-q', '-rs', '--junitxml', $junit)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $script:python
    $start.WorkingDirectory = $script:candidateDirectory
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in $arguments) { [void]$start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    [void]$process.Start()
    $outText = $process.StandardOutput.ReadToEnd()
    $errText = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    [IO.File]::WriteAllText($stdout, $outText, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($stderr, $errText, [Text.UTF8Encoding]::new($false))
    if (-not (Test-Path -LiteralPath $junit -PathType Leaf)) {
        throw "$Name did not create JUnit evidence"
    }
    $counts = Get-JunitCounts -Path $junit
    $counts.unexpected_skips = [Math]::Max(0, $counts.skipped - $ExpectedSkips)
    if ($process.ExitCode -ne 0 -or $counts.failed -ne 0 -or $counts.unexpected_skips -ne 0) {
        throw "$Name failed its zero-failure/unexpected-skip contract"
    }
    return $counts
}

function Invoke-W2Gate {
    $hadOffline = Test-Path Env:UV_OFFLINE
    $offlineValue = $env:UV_OFFLINE
    try {
        # The accepted W2 harness creates its own empty attempt cache and is the
        # protected orchestrator's bounded dependency-resolution stage. Gate 00
        # separately proves the final W5 build and installs completely offline.
        $env:UV_OFFLINE = $null
        return Invoke-GatePytest -Name 'W2' -Nodes @(
            'tests/test_windows_freecad_discovery.py',
            'tests/test_windows_packaging.py::test_windows_clean_installed_wheel_core_contract'
        )
    } finally {
        if ($hadOffline) { $env:UV_OFFLINE = $offlineValue }
        else { $env:UV_OFFLINE = $null }
    }
}

function Set-ExplicitPathEnvironment {
    $env:MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT = $script:second.Path
    $env:MECH_DESIGN_W2_ROOT = $script:second.Path
    $env:MECH_DESIGN_W3_ROOT = $script:second.Path
    $env:MECH_DESIGN_W4_ROOT = $script:second.Path
    $env:MECH_DESIGN_FREECADCMD = $FreeCADCmd
    $env:MECH_DESIGN_FREECADCMD_EXPECTED_VERSION = '1.1.3'
    $env:MECH_DESIGN_FREECAD_EXE = $FreeCADExe
    $env:MECH_DESIGN_FREECAD_GUI_MCP_CHECKOUT = $ExternalMcpCheckout
    $env:MECH_DESIGN_FREECAD_GUI_MCP_EXECUTABLE = $ExternalMcpPython
    $env:MECH_DESIGN_FREECAD_GUI_MCP_ADDON_PATH = $AddonRoot
    $env:MECH_DESIGN_FREECAD_GUI_MCP_SETTINGS = $SettingsFile
}

function Clear-MutationOptIns {
    $env:MECH_DESIGN_WINDOWS_DB_LIVE_TESTS = $null
    $env:MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_PREFLIGHT_TESTS = $null
    $env:MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_LIVE_TESTS = $null
}

$candidateDirectory = (Get-Item -LiteralPath $CandidateRoot -Force).FullName
if (-not (Test-Path -LiteralPath (Join-Path $candidateDirectory 'pyproject.toml') -PathType Leaf)) {
    throw 'CandidateRoot is not the project clone root'
}
$primary = Assert-FixedNtfsRoot -Value $PrimaryRoot -Label 'PrimaryRoot'
$second = Assert-FixedNtfsRoot -Value $SecondRoot -Label 'SecondRoot'
Assert-DistinctVolumes -First $primary -Second $second
$python = Assert-Cpython312X64 -Python $GatePython
$rawDirectory = (Get-Item -LiteralPath $RawRoot -Force).FullName
if (-not (Get-Item -LiteralPath $rawDirectory).PSIsContainer) { throw 'RawRoot must be a directory' }
foreach ($path in @($FreeCADCmd, $FreeCADExe, $ExternalMcpPython, $SettingsFile)) {
    [void](Assert-RequiredFile -Value $path -Label 'external executable/settings path')
}
foreach ($path in @($ExternalMcpCheckout, $AddonRoot)) {
    $directory = Get-Item -LiteralPath $path -Force
    if (-not $directory.PSIsContainer -or ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'external checkout/addon path must be a non-reparse directory'
    }
}
if ((@($Gates) -join ',') -ne ($orderedGates -join ',')) {
    throw 'W1-W4 gates must run in the approved order without selection'
}

Set-ExplicitPathEnvironment
$report = [ordered]@{
    schema_version = 'WindowsReleaseAcceptanceEvidence/v1'
    ordered_gates = [ordered]@{}
    cleanup_failed = $false
    unexpected_skips = 0
    overall = 'FAILED'
}
$bodyFailed = $false
try {
    Clear-MutationOptIns
    $report.ordered_gates.W1 = Invoke-GatePytest -Name 'W1' -Nodes @(
        'tests/test_windows_portability.py',
        'tests/test_windows_secure_fs.py'
    )

    $report.ordered_gates.W2 = Invoke-W2Gate

    foreach ($required in @(
        'MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN',
        'MECH_DESIGN_WINDOWS_NEO4J_MODE',
        'MECH_DESIGN_WINDOWS_NEO4J_ADMIN_URI',
        'MECH_DESIGN_WINDOWS_NEO4J_ADMIN_USER',
        'MECH_DESIGN_WINDOWS_NEO4J_ADMIN_PASSWORD',
        'MECH_DESIGN_WINDOWS_NEO4J_DISPOSABLE_CONFIRMATION'
    )) {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($required))) {
            throw 'W3 protected environment is incomplete'
        }
    }
    $env:MECH_DESIGN_WINDOWS_DB_LIVE_TESTS = '1'
    $report.ordered_gates.W3 = Invoke-GatePytest -Name 'W3' -Nodes @(
        'tests/test_windows_database_live.py::test_windows_installed_wheel_database_live_gate'
    )
    $env:MECH_DESIGN_WINDOWS_DB_LIVE_TESTS = $null

    $env:MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_PREFLIGHT_TESTS = '1'
    $w4Preflight = Invoke-GatePytest -Name 'W4-preflight' -Nodes @(
        'tests/test_windows_freecad_gui_mcp_live.py::test_real_windows_gate_is_explicit_opt_in'
    )
    $env:MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_PREFLIGHT_TESTS = $null
    $env:MECH_DESIGN_WINDOWS_FREECAD_GUI_MCP_LIVE_TESTS = '1'
    $w4Live = Invoke-GatePytest -Name 'W4-live' -Nodes @(
        'tests/test_windows_freecad_gui_mcp_live.py::test_real_windows_gate_is_explicit_opt_in'
    )
    $report.ordered_gates.W4 = [ordered]@{
        collected = $w4Preflight.collected + $w4Live.collected
        passed = $w4Preflight.passed + $w4Live.passed
        failed = 0
        skipped = 0
        unexpected_skips = 0
    }
    $report.overall = 'PASSED'
} catch {
    $bodyFailed = $true
    throw
} finally {
    try {
        Clear-MutationOptIns
    } catch {
        $report.cleanup_failed = $true
    }
    foreach ($gate in $report.ordered_gates.Values) {
        $report.unexpected_skips += $gate.unexpected_skips
    }
    if ($bodyFailed -or $report.cleanup_failed -or $report.unexpected_skips -ne 0) {
        $report.overall = 'FAILED'
    }
    $safeReport = Join-Path $rawDirectory 'windows-release-acceptance-summary.json'
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $safeReport -Encoding utf8NoBOM
}

if ($report.overall -ne 'PASSED') { exit 1 }
$report | ConvertTo-Json -Depth 8 -Compress
