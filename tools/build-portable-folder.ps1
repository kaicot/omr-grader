[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$DistRoot = '',
    [string]$WorkRoot = '',
    [string]$ArchivePath = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repository = Split-Path $PSScriptRoot -Parent
if (-not $Python) {
    $Python = Join-Path $repository '.venv\Scripts\python.exe'
}
if (-not $DistRoot) {
    $DistRoot = Join-Path $repository 'dist'
}
$spec = Join-Path $repository 'packaging\OMR_Grader.spec'
$dateStamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd')

New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
$usedNumbers = @(
    Get-ChildItem -LiteralPath $DistRoot -Force |
        ForEach-Object {
            if ($_.Name -match '^OMR-Grader-fixed(\d+)-\d{8}(?:\.zip)?$') {
                [int]$Matches[1]
            }
        }
)
$nextNumber = if ($usedNumbers.Count -eq 0) {
    1
} else {
    [int](($usedNumbers | Measure-Object -Maximum).Maximum) + 1
}
do {
    $versionName = "OMR-Grader-fixed$nextNumber-$dateStamp"
    $versionFolder = Join-Path $DistRoot $versionName
    $defaultArchivePath = Join-Path $DistRoot "$versionName.zip"
    $nextNumber += 1
} while (
    (Test-Path -LiteralPath $versionFolder) -or
    (Test-Path -LiteralPath $defaultArchivePath)
)

if (-not $WorkRoot) {
    $WorkRoot = Join-Path $repository "build\$versionName"
}
if (-not $ArchivePath) {
    $ArchivePath = $defaultArchivePath
}
$applicationFolder = Join-Path $versionFolder 'OMR Grader'
$executable = Join-Path $applicationFolder 'OMR Grader.exe'
$receiptPath = Join-Path $versionFolder 'release-receipt.json'
$archiveHashPath = "$ArchivePath.sha256"

if (Test-Path -LiteralPath $WorkRoot) {
    Remove-Item -LiteralPath $WorkRoot -Recurse -Force
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $versionFolder `
    --workpath $WorkRoot `
    $spec
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "PyInstaller did not produce $executable"
}

$projectMetadata = Get-Content -LiteralPath (Join-Path $repository 'pyproject.toml') -Raw
$versionMatch = [regex]::Match($projectMetadata, '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$')
if (-not $versionMatch.Success) {
    throw 'Could not read project version from pyproject.toml'
}
$gitHead = (& git -C $repository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $gitHead -notmatch '^[0-9a-f]{40}$') {
    throw 'Could not resolve the source Git HEAD'
}
$workingTreeDiff = (& git -C $repository diff --binary HEAD | Out-String)
$diffHasher = [Security.Cryptography.SHA256]::Create()
try {
    $workingTreeDiffSha256 = [Convert]::ToHexString(
        $diffHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($workingTreeDiff))
    ).ToLowerInvariant()
} finally {
    $diffHasher.Dispose()
}
$payloadFiles = @(
    Get-ChildItem -LiteralPath $applicationFolder -File -Recurse | Sort-Object FullName |
        ForEach-Object {
            [ordered]@{
                path = $_.FullName.Substring($applicationFolder.Length + 1).Replace('\', '/')
                size = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
)
$receipt = [ordered]@{
    format = 1
    product = 'OMR Grader'
    version = $versionMatch.Groups['version'].Value
    git_head = $gitHead
    working_tree_diff_sha256 = $workingTreeDiffSha256
    built_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    payload_root = 'OMR Grader'
    executable = [ordered]@{
        path = 'OMR Grader/OMR Grader.exe'
        size = (Get-Item -LiteralPath $executable).Length
        sha256 = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    payload_files = $payloadFiles
    build = [ordered]@{
        script_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
        spec_sha256 = (Get-FileHash -LiteralPath $spec -Algorithm SHA256).Hash.ToLowerInvariant()
        python = (& $Python --version 2>&1 | Out-String).Trim()
        pyinstaller = (& $Python -m PyInstaller --version | Out-String).Trim()
    }
}
$receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding utf8

Compress-Archive -LiteralPath $versionFolder -DestinationPath $ArchivePath -CompressionLevel Optimal
"$((Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($ArchivePath))" |
    Set-Content -LiteralPath $archiveHashPath -Encoding ascii
Write-Output $versionFolder
Write-Output $applicationFolder
Write-Output $ArchivePath
Write-Output $receiptPath
Write-Output $archiveHashPath
