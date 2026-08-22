[CmdletBinding()]
param(
    [switch]$ContractProbe,
    [switch]$CleanupProbe,
    [switch]$VolumeLayoutProbe,
    [string]$CleanupProbeRoot,
    [string]$CleanupProbeParent,
    [string]$PrimaryTempParent,
    [string]$SecondNtfsRoot,
    [string]$CandidateRoot,
    [string]$GatePython,
    [string]$AttemptRoot,
    [string]$RawRoot,
    [string]$SafeEvidenceRoot,
    [string]$ExpectedSnapshotTree
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$orderedGates = @('Gate00', 'Gate01', 'Gate02', 'Gate03')
if ($ContractProbe) {
    [ordered]@{
        schema_version = 'WindowsD3DockerDatabaseDeploymentRunner/v1'
        ordered_gates = $orderedGates
        required_engine = 'DockerDesktopLinuxEngine'
        required_image_platform = 'linux/amd64'
        required_python = 'CPython 3.12 x64'
        required_filesystem = 'fixed NTFS'
        primary_temp_volume = 'C:'
        second_ntfs_volume = 'D:'
        second_ntfs_gates = @('Gate00', 'Gate02', 'Gate03')
        distinct_volume_preflight = $true
        cleanup_failure_overrides_body = $true
        cleanup_owned_unicode_temp = $true
    } | ConvertTo-Json -Compress
    exit 0
}

function Assert-RegularFile {
    param([string]$Value, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Label is required" }
    $item = Get-Item -LiteralPath $Value -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Label must be a non-reparse regular file"
    }
    return $item.FullName
}

function Assert-FixedNtfsDirectory {
    param([string]$Value, [string]$Label)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Label is required" }
    if (-not [IO.Path]::IsPathFullyQualified($Value) -or $Value.StartsWith('\\')) {
        throw "$Label must be an absolute local path"
    }
    $item = Get-Item -LiteralPath $Value -Force
    if (-not $item.PSIsContainer) { throw "$Label must be a directory" }
    $cursor = $item
    while ($null -ne $cursor) {
        if ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "$Label must not contain a reparse ancestor"
        }
        $cursor = $cursor.Parent
    }
    $driveLetter = [IO.Path]::GetPathRoot($item.FullName).Substring(0, 1)
    $volume = Get-Volume -DriveLetter $driveLetter
    if ($volume.DriveType -ne 'Fixed' -or $volume.FileSystemType -ne 'NTFS') {
        throw "$Label must be on fixed NTFS"
    }
    return $item.FullName
}

function Get-FixedNtfsVolumeIdentity {
    param([string]$Directory)
    $driveLetter = [IO.Path]::GetPathRoot($Directory).Substring(0, 1)
    $volume = Get-Volume -DriveLetter $driveLetter
    return $volume.UniqueId
}

function Assert-D3VolumeLayout {
    param(
        [string]$PrimaryTemp,
        [string]$SecondRoot
    )
    $primary = Assert-FixedNtfsDirectory -Value $PrimaryTemp -Label 'TEMP/TMP root'
    $second = Assert-FixedNtfsDirectory -Value $SecondRoot -Label 'Second NTFS root'
    if ([IO.Path]::GetPathRoot($primary) -ne 'C:\') {
        throw 'TEMP/TMP root must be on C:'
    }
    if ([IO.Path]::GetPathRoot($second) -ne 'D:\') {
        throw 'Second NTFS root must be on D:'
    }
    $primaryIdentity = Get-FixedNtfsVolumeIdentity -Directory $primary
    $secondIdentity = Get-FixedNtfsVolumeIdentity -Directory $second
    if ([string]::IsNullOrWhiteSpace($primaryIdentity) -or
        [string]::IsNullOrWhiteSpace($secondIdentity) -or
        $primaryIdentity -eq $secondIdentity) {
        throw 'TEMP/TMP and second NTFS root must be distinct fixed NTFS volumes'
    }
    return [ordered]@{
        primary = $primary
        second = $second
    }
}

function Remove-D3OwnedEntryNoFollow {
    param(
        [string]$EntryPath,
        [string]$OwnedRootPrefix
    )
    $entry = Get-Item -LiteralPath $EntryPath -Force
    $entryFull = [IO.Path]::GetFullPath($entry.FullName)
    if (-not $entryFull.StartsWith(
        $OwnedRootPrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Attempt cleanup descendant escaped the owned root'
    }
    if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        Remove-Item -LiteralPath $entryFull -Force
        return
    }
    if ($entry.PSIsContainer) {
        foreach ($child in @(Get-ChildItem -LiteralPath $entryFull -Force)) {
            Remove-D3OwnedEntryNoFollow `
                -EntryPath $child.FullName `
                -OwnedRootPrefix $OwnedRootPrefix
        }
    }
    Remove-Item -LiteralPath $entryFull -Force
}

function Remove-D3AttemptOwnedTree {
    param(
        [string]$AttemptPath,
        [string]$ExpectedParent
    )
    if ([string]::IsNullOrWhiteSpace($AttemptPath) -or
        [string]::IsNullOrWhiteSpace($ExpectedParent)) {
        throw 'Attempt cleanup paths are required'
    }
    if (-not (Test-Path -LiteralPath $AttemptPath)) { return }
    $parentItem = Get-Item -LiteralPath $ExpectedParent -Force
    $attemptItem = Get-Item -LiteralPath $AttemptPath -Force
    if (-not $parentItem.PSIsContainer -or -not $attemptItem.PSIsContainer) {
        throw 'Attempt cleanup requires directories'
    }
    if (($parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        ($attemptItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Attempt cleanup rejects a reparse root'
    }
    $parentFull = [IO.Path]::GetFullPath($parentItem.FullName).TrimEnd('\')
    $attemptFull = [IO.Path]::GetFullPath($attemptItem.FullName).TrimEnd('\')
    if (-not [string]::Equals(
        [IO.Path]::GetDirectoryName($attemptFull),
        $parentFull,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Attempt cleanup root is not directly owned by the expected parent'
    }
    if ($attemptItem.Name -notmatch '^数据库 部署 验收 [0-9a-f]{32}$') {
        throw 'Attempt cleanup root does not match the owned naming contract'
    }
    $prefix = $attemptFull + '\'
    foreach ($child in @(Get-ChildItem -LiteralPath $attemptFull -Force)) {
        Remove-D3OwnedEntryNoFollow `
            -EntryPath $child.FullName `
            -OwnedRootPrefix $prefix
    }
    Remove-Item -LiteralPath $attemptFull -Force
    if (Test-Path -LiteralPath $attemptFull) {
        throw 'Attempt cleanup did not remove the owned root'
    }
}

if ($CleanupProbe) {
    Remove-D3AttemptOwnedTree `
        -AttemptPath $CleanupProbeRoot `
        -ExpectedParent $CleanupProbeParent
    [ordered]@{ cleanup = 'passed' } | ConvertTo-Json -Compress
    exit 0
}

if ($VolumeLayoutProbe) {
    $layout = Assert-D3VolumeLayout `
        -PrimaryTemp $PrimaryTempParent `
        -SecondRoot $SecondNtfsRoot
    $probeAttempt = Join-Path $layout.primary (
        '数据库 部署 验收 ' + [guid]::NewGuid().ToString('N')
    )
    [void](New-Item -ItemType Directory -Path $probeAttempt)
    $cleaned = $false
    try {
        $unicodeSpace = $probeAttempt.Contains('数据库 部署 验收 ')
    } finally {
        Remove-D3AttemptOwnedTree `
            -AttemptPath $probeAttempt `
            -ExpectedParent $layout.primary
        $cleaned = -not (Test-Path -LiteralPath $probeAttempt)
    }
    [ordered]@{
        primary_temp_fixed_ntfs = $true
        second_root_fixed_ntfs = $true
        distinct_volumes = $true
        unicode_space_temp = $unicodeSpace
        cleanup = $cleaned
    } | ConvertTo-Json -Compress
    exit 0
}

function Assert-Cpython312X64 {
    param([string]$Python)
    $resolved = Assert-RegularFile -Value $Python -Label 'GatePython'
    $probe = & $resolved -c "import json,platform,struct,sys; print(json.dumps({'implementation':platform.python_implementation(),'version':list(sys.version_info[:3]),'bits':struct.calcsize('P')*8}))"
    if ($LASTEXITCODE -ne 0) { throw 'GatePython probe failed' }
    $identity = $probe | ConvertFrom-Json
    if ($identity.implementation -ne 'CPython' -or
        $identity.version[0] -ne 3 -or $identity.version[1] -ne 12 -or
        $identity.bits -ne 64) {
        throw 'GatePython must be CPython 3.12 x64'
    }
    return $resolved
}

function Get-JunitCounts {
    param([string]$Path, [int]$ExpectedSkips)
    [xml]$xml = Get-Content -LiteralPath $Path -Raw
    $root = $xml.DocumentElement
    $suites = if ($root.LocalName -eq 'testsuites') {
        @($root.ChildNodes | Where-Object LocalName -eq 'testsuite')
    } else { @($root) }
    $tests = [int](($suites | ForEach-Object { [int]$_.GetAttribute('tests') } | Measure-Object -Sum).Sum)
    $failures = [int](($suites | ForEach-Object { [int]$_.GetAttribute('failures') } | Measure-Object -Sum).Sum)
    $errors = [int](($suites | ForEach-Object { [int]$_.GetAttribute('errors') } | Measure-Object -Sum).Sum)
    $skipped = [int](($suites | ForEach-Object { [int]$_.GetAttribute('skipped') } | Measure-Object -Sum).Sum)
    return [ordered]@{
        collected = $tests
        passed = $tests - $failures - $errors - $skipped
        failed = $failures + $errors
        skipped = $skipped
        unexpected_skips = [Math]::Max(0, $skipped - $ExpectedSkips)
    }
}

function Invoke-Gate {
    param(
        [string]$Name,
        [string[]]$Nodes,
        [int]$ExpectedSkips
    )
    $junit = Join-Path $script:rawDirectory "$Name-junit.xml"
    $stdout = Join-Path $script:rawDirectory "$Name-stdout.txt"
    $stderr = Join-Path $script:rawDirectory "$Name-stderr.txt"
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
    $counts = Get-JunitCounts -Path $junit -ExpectedSkips $ExpectedSkips
    if ($process.ExitCode -ne 0 -or $counts.failed -ne 0 -or
        $counts.unexpected_skips -ne 0 -or $counts.skipped -ne $ExpectedSkips) {
        throw "$Name failed its exact zero-failure/skip contract"
    }
    return $counts
}

function Get-D3DockerProjects {
    $projects = @()
    foreach ($command in @(
        @('ps', '-a', '--format', '{{.Label "com.docker.compose.project"}}'),
        @('network', 'ls', '--format', '{{.Label "com.docker.compose.project"}}'),
        @('volume', 'ls', '--format', '{{.Label "com.docker.compose.project"}}')
    )) {
        $values = & docker @command
        if ($LASTEXITCODE -ne 0) { throw 'Docker project inventory failed' }
        $projects += @($values | Where-Object { $_ -match '^md3dcad-[0-9a-f]{32}$' })
    }
    return @($projects | Sort-Object -Unique)
}

function Get-ResidualD3TestProcesses {
    $matches = @()
    foreach ($process in @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^(python|pythonw|pytest|uv)(\.exe)?$'
    })) {
        $commandLine = [string]$process.CommandLine
        if ($commandLine -match '(?i)(MechanicalDesignD3|md3dcad|database_deployment)') {
            $matches += $process.ProcessId
        }
    }
    return @($matches)
}

function Clear-DeploymentEnvironment {
    foreach ($name in @(
        'PYTHONPATH', 'MECH_DESIGN_WORKSPACE', 'MECH_DESIGN_ENV_FILE',
        'MECH_DESIGN_DATABASE_URL', 'MECH_DESIGN_NEO4J_URI',
        'MECH_DESIGN_NEO4J_USER', 'MECH_DESIGN_NEO4J_PASSWORD',
        'MECH_DESIGN_WINDOWS_POSTGRES_ADMIN_DSN',
        'MECH_DESIGN_WINDOWS_NEO4J_MODE',
        'MECH_DESIGN_WINDOWS_NEO4J_ADMIN_URI',
        'MECH_DESIGN_WINDOWS_NEO4J_ADMIN_USER',
        'MECH_DESIGN_WINDOWS_NEO4J_ADMIN_PASSWORD',
        'MECH_DESIGN_WINDOWS_NEO4J_DISPOSABLE_CONFIRMATION',
        'MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT',
        'MECH_DESIGN_DOCKER_DATABASE_LIVE',
        'MECH_DESIGN_D3_SAFE_EVIDENCE_DIR'
    )) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:PYTHONNOUSERSITE = '1'
}

$candidateDirectory = Assert-FixedNtfsDirectory -Value $CandidateRoot -Label 'CandidateRoot'
if (-not (Test-Path -LiteralPath (Join-Path $candidateDirectory 'pyproject.toml') -PathType Leaf)) {
    throw 'CandidateRoot must be the project clone root'
}
$attemptBase = Assert-FixedNtfsDirectory -Value $AttemptRoot -Label 'AttemptRoot'
$rawDirectory = Assert-FixedNtfsDirectory -Value $RawRoot -Label 'RawRoot'
$safeDirectory = Assert-FixedNtfsDirectory -Value $SafeEvidenceRoot -Label 'SafeEvidenceRoot'
$python = Assert-Cpython312X64 -Python $GatePython

$dockerIdentity = (& docker info --format '{{.OSType}}/{{.Architecture}}').Trim()
if ($LASTEXITCODE -ne 0 -or $dockerIdentity -notin @('linux/x86_64', 'linux/amd64')) {
    throw 'DockerDesktopLinuxEngine must report native linux/amd64'
}
$composeVersion = & docker compose version --short
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($composeVersion)) {
    throw 'Docker Compose v2 is required'
}
if (@(Get-D3DockerProjects).Count -ne 0) {
    throw 'A pre-existing md3dcad UUID Compose project blocks the gate'
}
if (@(Get-ResidualD3TestProcesses).Count -ne 0) {
    throw 'A residual D3 pytest/python/uv process blocks the gate'
}

$sourceTreeBefore = (& git -C $candidateDirectory rev-parse 'HEAD^{tree}').Trim()
$sourceStatusBefore = @(& git -C $candidateDirectory status --porcelain=v1)
if ($sourceStatusBefore.Count -ne 0) { throw 'Candidate must be clean before D3' }
if (-not [string]::IsNullOrWhiteSpace($ExpectedSnapshotTree) -and
    $sourceTreeBefore -ne $ExpectedSnapshotTree) {
    throw 'Candidate tree does not match the approved snapshot'
}

$unicodeAttempt = Join-Path $rawDirectory ("数据库 部署 验收 " + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $unicodeAttempt)
$env:TEMP = $unicodeAttempt
$env:TMP = $unicodeAttempt
$volumeLayout = Assert-D3VolumeLayout `
    -PrimaryTemp $unicodeAttempt `
    -SecondRoot $attemptBase
if ([string]::IsNullOrWhiteSpace($env:DOCKER_CONFIG)) {
    $env:DOCKER_CONFIG = Join-Path $env:USERPROFILE '.docker'
}
Clear-DeploymentEnvironment

$report = [ordered]@{
    schema_version = 'WindowsD3DockerDatabaseDeploymentEvidence/v1'
    overall = 'FAILED'
    gates = [ordered]@{}
    image_platform = 'linux/amd64'
    unicode_space_path = 'passed'
    cleanup = 'not_run'
    source_integrity = 'not_run'
    privacy = 'pending_external_scan'
}
$bodyFailed = $false
try {
    $env:MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT = $volumeLayout.second
    $report.gates.Gate00 = Invoke-Gate -Name 'Gate00-targeted-offline' -ExpectedSkips 1 -Nodes @(
        'tests/test_database_deployment.py',
        'tests/test_database_deployment_live.py'
    )
    $env:MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT = $null

    $env:MECH_DESIGN_DOCKER_DATABASE_LIVE = '1'
    $env:MECH_DESIGN_D3_SAFE_EVIDENCE_DIR = $safeDirectory
    $report.gates.Gate01 = Invoke-Gate -Name 'Gate01-clean-installed-live' -ExpectedSkips 0 -Nodes @(
        'tests/test_database_deployment_live.py::test_clean_installed_wheel_database_deployment'
    )
    Clear-DeploymentEnvironment

    $env:MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT = $volumeLayout.second
    $report.gates.Gate02 = Invoke-Gate -Name 'Gate02-full-offline' -ExpectedSkips 58 -Nodes @('.')
    $env:MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT = $null
    $env:MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT = $volumeLayout.second
    $report.gates.Gate03 = Invoke-Gate -Name 'Gate03-release-boundary' -ExpectedSkips 1 -Nodes @(
        'tests/test_database_bootstrap.py',
        'tests/test_database_deployment.py',
        'tests/test_database_deployment_live.py',
        'tests/test_public_distribution.py',
        'tests/test_public_release_contract.py',
        'tests/test_runtime_hardcode_packaging.py',
        'tests/test_third_party_licensing.py'
    )
    $env:MECH_DESIGN_WINDOWS_SECOND_NTFS_ROOT = $null
} catch {
    $bodyFailed = $true
    throw
} finally {
    Clear-DeploymentEnvironment
    $cleanupFailed = $false
    if (@(Get-D3DockerProjects).Count -ne 0) { $cleanupFailed = $true }
    $sourceTreeAfter = (& git -C $candidateDirectory rev-parse 'HEAD^{tree}').Trim()
    $sourceStatusAfter = @(& git -C $candidateDirectory status --porcelain=v1)
    if ($sourceTreeAfter -ne $sourceTreeBefore -or $sourceStatusAfter.Count -ne 0) {
        $cleanupFailed = $true
    }
    try {
        Remove-D3AttemptOwnedTree `
            -AttemptPath $unicodeAttempt `
            -ExpectedParent $rawDirectory
    } catch {
        $cleanupFailed = $true
    }
    if ($cleanupFailed) {
        $report.cleanup = 'failed'
        throw 'Windows D3 cleanup/source-integrity verification failed'
    }
    $report.cleanup = 'passed'
    $report.source_integrity = 'passed'
}

if ($bodyFailed) { throw 'Windows D3 body failed' }
$report.overall = 'PASSED'
$reportPath = Join-Path $safeDirectory 'WINDOWS-D3-DOCKER-DATABASE-DEPLOYMENT-REPORT.json'
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding utf8NoBOM
$reportHash = (Get-FileHash -LiteralPath $reportPath -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText(
    (Join-Path $safeDirectory 'WINDOWS-D3-DOCKER-DATABASE-DEPLOYMENT-REPORT.json.sha256'),
    "$reportHash`n",
    [Text.UTF8Encoding]::new($false)
)
$report | ConvertTo-Json -Depth 8
