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

function Fail([string]$Message) { throw "PORTABLE_RELEASE_VERIFY_FAILED: $Message" }
$repository = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
if (-not $Python) { $Python = Join-Path $repository '.venv\Scripts\python.exe' }
$Python = [IO.Path]::GetFullPath($Python)
$release = (Resolve-Path -LiteralPath $ReleaseRoot -ErrorAction Stop).Path
if (-not $ArchivePath) { $ArchivePath = "$release.zip" }
$ArchivePath = [IO.Path]::GetFullPath($ArchivePath)
$tool = Join-Path $repository 'tools\portable_release.py'
$smokeScript = Join-Path $repository 'tools\smoke-portable-onedir.py'

# The Python verifier validates directory inventory, source provenance, sidecar,
# ZIP inventory, entries and bytes before anything is launched.
$structuralText = & $Python $tool verify --release $release --archive $ArchivePath --repository $repository
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$structural = $structuralText | ConvertFrom-Json
if ($structural.status -eq 'LEGACY_AUDIT_ONLY') {
    [PSCustomObject]@{ result = 'LEGACY_AUDIT_ONLY'; global_approval = $false; structural = $structural } |
        ConvertTo-Json -Depth 8
    exit 0
}

$smokeResults = @()
if ($Smoke -ne 'None') {
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ("omr-verified-zip-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary -ErrorAction Stop | Out-Null
    try {
        Expand-Archive -LiteralPath $ArchivePath -DestinationPath $temporary -ErrorAction Stop
        $extractedRelease = Join-Path $temporary ([IO.Path]::GetFileName($release))
        # Defense in depth: validate the extracted bytes and receipt against the
        # same verified ZIP before smoke starts the EXE.
        $extractVerify = & $Python $tool verify --release $extractedRelease --archive $ArchivePath
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        foreach ($mode in @('Writable', 'ReadOnly')) {
            if ($Smoke -eq $mode -or $Smoke -eq 'Both') {
                $arguments = @('--release', (Join-Path $extractedRelease 'OMR Grader'), '--mode', $mode.ToLowerInvariant())
                if ($StrictShutdown) { $arguments += '--require-graceful-close' }
                $output = & $Python $smokeScript @arguments
                if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
                $smokeResults += ($output | ConvertFrom-Json)
            }
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    }
}

[PSCustomObject]@{
    result = if ($Smoke -eq 'None') { 'STRUCTURE_PASS' } else { 'VERIFIED_ZIP_SMOKE_PASS' }
    global_approval = ($Smoke -ne 'None')
    structural = $structural
    smoke = $Smoke
    strict_shutdown = [bool]$StrictShutdown
    checks = $smokeResults
} | ConvertTo-Json -Depth 12
