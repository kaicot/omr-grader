[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ReleaseRoot,
    [string]$ArchivePath = '',
    [string]$Python = '',
    [ValidateSet('None', 'Writable', 'ReadOnly', 'Both')][string]$Smoke = 'Both',
    [switch]$StrictShutdown
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repository = Split-Path $PSScriptRoot -Parent
if (-not $Python) {
    $Python = Join-Path $repository '.venv\Scripts\python.exe'
}
$release = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$receiptPath = Join-Path $release 'release-receipt.json'
$applicationFolder = Join-Path $release 'OMR Grader'
$executable = Join-Path $applicationFolder 'OMR Grader.exe'
if (-not $ArchivePath) {
    $ArchivePath = "$release.zip"
}

function Fail([string]$Message) {
    throw "PORTABLE_RELEASE_VERIFY_FAILED: $Message"
}

if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { Fail 'release-receipt.json is missing' }
if (-not (Test-Path -LiteralPath $applicationFolder -PathType Container)) { Fail 'OMR Grader folder is missing' }
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { Fail 'OMR Grader.exe is missing' }

$rootEntries = @(Get-ChildItem -LiteralPath $release -Force | ForEach-Object Name | Sort-Object)
$expectedRootEntries = @('OMR Grader', 'release-receipt.json')
if ((ConvertTo-Json $rootEntries) -ne (ConvertTo-Json $expectedRootEntries)) {
    Fail "release root payload is not exact: $($rootEntries -join ', ')"
}

$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
if ($receipt.format -ne 1 -or $receipt.product -ne 'OMR Grader' -or $receipt.payload_root -ne 'OMR Grader') {
    Fail 'receipt identity is invalid'
}
$projectMetadata = Get-Content -LiteralPath (Join-Path $repository 'pyproject.toml') -Raw
$versionMatch = [regex]::Match($projectMetadata, '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$')
$gitHead = (& git -C $repository rev-parse HEAD).Trim()
$workingTreeDiff = (& git -C $repository diff --binary HEAD | Out-String)
$diffHasher = [Security.Cryptography.SHA256]::Create()
try {
    $workingTreeDiffSha256 = [Convert]::ToHexString(
        $diffHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($workingTreeDiff))
    ).ToLowerInvariant()
} finally {
    $diffHasher.Dispose()
}
if ($receipt.version -ne $versionMatch.Groups['version'].Value) { Fail 'receipt version differs from pyproject.toml' }
if ($receipt.git_head -ne $gitHead) { Fail 'receipt Git HEAD differs from current checkout' }
if ($receipt.working_tree_diff_sha256 -ne $workingTreeDiffSha256) { Fail 'receipt source diff differs from current checkout' }

$recordedFiles = @($receipt.payload_files)
if ($recordedFiles.Count -eq 0) { Fail 'receipt contains no payload files' }
$oldNames = @($recordedFiles | Where-Object { $_.path -cmatch '(^|/)(OMR_Grader|OMR_Grader\.exe|CURRENT\.json|IDENTITY\.json)(/|$)' })
if ($oldNames.Count -gt 0) { Fail 'receipt contains legacy payload paths' }
foreach ($recorded in $recordedFiles) {
    $target = Join-Path $applicationFolder ($recorded.path.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { Fail "payload file is missing: $($recorded.path)" }
    $item = Get-Item -LiteralPath $target
    if ($item.Length -ne $recorded.size) { Fail "payload size differs: $($recorded.path)" }
    $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $recorded.sha256) { Fail "payload hash differs: $($recorded.path)" }
}
$exeRecord = $receipt.executable
if ($exeRecord.path -ne 'OMR Grader/OMR Grader.exe') { Fail 'receipt executable path is invalid' }

if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) { Fail "archive is missing: $ArchivePath" }
$archiveHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$sidecar = "$ArchivePath.sha256"
if (Test-Path -LiteralPath $sidecar -PathType Leaf) {
    $recordedArchiveHash = (Get-Content -LiteralPath $sidecar -Raw).Trim().Split()[0].ToLowerInvariant()
    if ($recordedArchiveHash -ne $archiveHash) { Fail 'archive sidecar hash differs' }
}
$archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
try {
    $prefix = [IO.Path]::GetFileName($release)
    $archiveEntries = @(
        $archive.Entries |
            Where-Object { -not $_.FullName.EndsWith('/') } |
            ForEach-Object FullName |
            Sort-Object
    )
    $expectedArchiveEntries = @(
        "$prefix/release-receipt.json"
        $recordedFiles | ForEach-Object { "$prefix/OMR Grader/$($_.path)" }
    ) | Sort-Object
    if ((ConvertTo-Json $archiveEntries) -ne (ConvertTo-Json $expectedArchiveEntries)) {
        Fail 'archive payload does not match the receipt'
    }
} finally {
    $archive.Dispose()
}

if ($Smoke -ne 'None') {
    $smokeScript = Join-Path $repository 'tools\smoke-portable-onedir.py'
    if ($Smoke -in @('Writable', 'Both')) {
        $smokeArguments = @('--release', $applicationFolder)
        if ($StrictShutdown) { $smokeArguments += '--require-graceful-close' }
        & $Python $smokeScript @smokeArguments
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    if ($Smoke -in @('ReadOnly', 'Both')) {
        & $Python $smokeScript --release $applicationFolder --read-only
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}

[PSCustomObject]@{
    result = 'PASS'
    release = $release
    archive = (Resolve-Path -LiteralPath $ArchivePath).Path
    version = $receipt.version
    git_head = $receipt.git_head
    payload_files = $recordedFiles.Count
    archive_sha256 = $archiveHash
    smoke = $Smoke
} | ConvertTo-Json -Depth 4
