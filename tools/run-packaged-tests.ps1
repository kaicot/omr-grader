[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedManifestSha256,
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$ReleaseRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Remove-Item Env:NO_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:no_proxy -ErrorAction SilentlyContinue
$env:PIP_NO_INDEX = '1'; $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'; $env:PIP_NO_INPUT = '1'; $env:PIP_ONLY_BINARY = ':all:'; $env:PIP_NO_BUILD_ISOLATION = '1'; $env:PYTHONNOUSERSITE = '1'
$env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9'; $env:ALL_PROXY = 'http://127.0.0.1:9'
$env:http_proxy = $env:HTTP_PROXY; $env:https_proxy = $env:HTTPS_PROXY; $env:all_proxy = $env:ALL_PROXY; $env:PIP_INDEX_URL = 'http://127.0.0.1:9/simple'; $env:PIP_EXTRA_INDEX_URL = 'http://127.0.0.1:9/simple'
$bundle = (Resolve-Path -LiteralPath $BundleRoot).Path
& (Join-Path $PSScriptRoot 'verify-release-bundle.ps1') -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$python = (Resolve-Path -LiteralPath $Python).Path; $release = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("omr-grader-wheel-manifest-" + [Guid]::NewGuid().ToString('N'))
$wheelManifest = Join-Path $tempRoot 'release-wheel-manifest.json'
function Write-FailureRecord([string]$Label, [System.Management.Automation.ErrorRecord]$Failure) {
    [Console]::Error.WriteLine("$Label`: $Failure")
    if (-not [string]::IsNullOrWhiteSpace($Failure.ScriptStackTrace)) { [Console]::Error.WriteLine($Failure.ScriptStackTrace) }
}
New-Item -ItemType Directory -Path $tempRoot | Out-Null
$primaryFailure = $null
$cleanupFailures = [System.Collections.Generic.List[System.Management.Automation.ErrorRecord]]::new()
$primaryExitCode = 0
try {
    & (Join-Path $PSScriptRoot 'verify-release-bundle.ps1') -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256 -WheelManifestPath $wheelManifest
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $primaryExitCode = $exitCode
        throw "VERIFY_RELEASE_BUNDLE_FAILED: $exitCode"
    }
    $repo = Split-Path $PSScriptRoot -Parent
    & $python (Join-Path $repo 'packaging\verify_release.py') --release $release --manifest $wheelManifest --wheelhouse (Join-Path $bundle 'wheelhouse')
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $primaryExitCode = $exitCode
        throw "VERIFY_RELEASE_FAILED: $exitCode"
    }
    $env:OMR_GRADER_RELEASE_DIR = $release
    & $python -m pytest -q (Join-Path $repo 'tests\packaged') (Join-Path $repo 'tests\security') (Join-Path $repo 'tests\fault')
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $primaryExitCode = $exitCode
        throw "PACKAGED_TESTS_FAILED: $exitCode"
    }
}
catch {
    $primaryFailure = $_
}
finally {
    try {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction Stop
            if (Test-Path -LiteralPath $tempRoot) { throw "TEMP_ROOT_RESIDUE: $tempRoot" }
        }
    }
    catch {
        $cleanupFailures.Add($_)
    }
}
if ($null -ne $primaryFailure) {
    Write-FailureRecord 'PRIMARY_FAILURE' $primaryFailure
    foreach ($cleanupFailure in $cleanupFailures) { Write-FailureRecord 'CLEANUP_FAILURE' $cleanupFailure }
    if ($primaryExitCode -ne 0) { exit $primaryExitCode }
    throw $primaryFailure
}
if ($cleanupFailures.Count -ne 0) {
    foreach ($cleanupFailure in $cleanupFailures) { Write-FailureRecord 'CLEANUP_FAILURE' $cleanupFailure }
    throw $cleanupFailures[0]
}
