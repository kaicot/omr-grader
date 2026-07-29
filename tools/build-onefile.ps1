[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedManifestSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedInstallerSha256,
    [Parameter(Mandatory = $true)][ValidateRange(1, [long]::MaxValue)][long]$SourceDateEpoch,
    [Parameter(Mandatory = $true)][string]$BuildRoot,
    [Parameter(Mandatory = $true)][string]$ReleaseRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Remove-Item Env:NO_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:no_proxy -ErrorAction SilentlyContinue
$env:PIP_NO_INDEX = '1'; $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'; $env:PIP_NO_INPUT = '1'; $env:PIP_ONLY_BINARY = ':all:'; $env:PIP_NO_BUILD_ISOLATION = '1'; $env:PYTHONNOUSERSITE = '1'
$env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9'; $env:ALL_PROXY = 'http://127.0.0.1:9'
$env:http_proxy = $env:HTTP_PROXY; $env:https_proxy = $env:HTTPS_PROXY; $env:all_proxy = $env:ALL_PROXY; $env:PIP_INDEX_URL = 'http://127.0.0.1:9/simple'; $env:PIP_EXTRA_INDEX_URL = 'http://127.0.0.1:9/simple'; $env:SOURCE_DATE_EPOCH = "$SourceDateEpoch"
$bundle = (Resolve-Path -LiteralPath $BundleRoot).Path
& (Join-Path $PSScriptRoot 'verify-release-bundle.ps1') -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$root = [IO.Path]::GetFullPath($BuildRoot); $release = [IO.Path]::GetFullPath($ReleaseRoot)
if (Test-Path -LiteralPath $release) { throw 'RELEASE_ROOT_NOT_DISPOSABLE' }
& (Join-Path $PSScriptRoot 'install-bootstrap.ps1') -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256 -ExpectedInstallerSha256 $ExpectedInstallerSha256 -BuildRoot $root
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$python = Join-Path $root 'venv\Scripts\python.exe'; $receipt = Join-Path $root 'installed-distributions.json'
& $python (Join-Path $PSScriptRoot 'verify_installed_distributions_receipt.py') --receipt $receipt --expected-manifest-sha256 $ExpectedManifestSha256
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$wheelManifest = Join-Path $root 'release-wheel-manifest.json'
& (Join-Path $PSScriptRoot 'verify-release-bundle.ps1') -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256 -WheelManifestPath $wheelManifest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$repo = Split-Path $PSScriptRoot -Parent
& $python (Join-Path $repo 'packaging\build_release.py') --manifest $wheelManifest --wheelhouse (Join-Path $bundle 'wheelhouse') --output $release
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python (Join-Path $repo 'packaging\verify_release.py') --release $release --manifest $wheelManifest --wheelhouse (Join-Path $bundle 'wheelhouse')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
