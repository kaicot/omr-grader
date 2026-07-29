[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedManifestSha256,
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$ReleaseRoot,
    [Parameter(Mandatory = $true)][string]$SmokeRoot,
    [Parameter(Mandatory = $true)][string]$OtherDriveRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Remove-Item Env:NO_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:no_proxy -ErrorAction SilentlyContinue
$env:PIP_NO_INDEX = '1'; $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'; $env:PIP_NO_INPUT = '1'; $env:PIP_ONLY_BINARY = ':all:'; $env:PIP_NO_BUILD_ISOLATION = '1'; $env:PYTHONNOUSERSITE = '1'
$env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9'; $env:ALL_PROXY = 'http://127.0.0.1:9'
$env:http_proxy = $env:HTTP_PROXY; $env:https_proxy = $env:HTTPS_PROXY; $env:all_proxy = $env:ALL_PROXY; $env:PIP_INDEX_URL = 'http://127.0.0.1:9/simple'; $env:PIP_EXTRA_INDEX_URL = 'http://127.0.0.1:9/simple'
if (-not ('SmokeVolumeIdentity' -as [type])) {
    Add-Type @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
public static class SmokeVolumeIdentity {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool GetVolumePathName(string fileName, StringBuilder volumePathName, uint bufferLength);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool GetVolumeNameForVolumeMountPoint(string volumeMountPoint, StringBuilder volumeName, uint bufferLength);
    public static string Get(string path) {
        var mountPoint = new StringBuilder(32768);
        if (!GetVolumePathName(Path.GetFullPath(path), mountPoint, (uint)mountPoint.Capacity)) throw new Win32Exception(Marshal.GetLastWin32Error());
        var volumeName = new StringBuilder(32768);
        if (!GetVolumeNameForVolumeMountPoint(mountPoint.ToString(), volumeName, (uint)volumeName.Capacity)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return volumeName.ToString();
    }
}
'@
}
function Assert-NonReparseDirectory([string]$Path, [string]$Code) {
    $info = Get-Item -LiteralPath $Path -Force
    if ($info -isnot [IO.DirectoryInfo] -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw $Code }
}
function Remove-SmokeRelease([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $Path) { throw "SMOKE_RELEASE_RESIDUE: $Path" }
    }
}
function Write-FailureRecord([string]$Label, [System.Management.Automation.ErrorRecord]$Failure) {
    [Console]::Error.WriteLine("$Label`: $Failure")
    if (-not [string]::IsNullOrWhiteSpace($Failure.ScriptStackTrace)) { [Console]::Error.WriteLine($Failure.ScriptStackTrace) }
}
function New-SmokeRelease([string]$Root, [string]$Label, [string]$Source) {
    $parent = [IO.Path]::GetFullPath($Root)
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Assert-NonReparseDirectory $parent 'SMOKE_ROOT_UNSAFE'
    $destination = Join-Path $parent ("$Label-" + [Guid]::NewGuid().ToString('N'))
    $copyFailure = $null
    try {
        New-Item -ItemType Directory -Path $destination -ErrorAction Stop | Out-Null
        Assert-NonReparseDirectory $destination 'SMOKE_ROOT_UNSAFE'
        foreach ($entry in @(Get-ChildItem -LiteralPath $Source -Force)) {
            Copy-Item -LiteralPath $entry.FullName -Destination $destination -Recurse -ErrorAction Stop
        }
        return $destination
    }
    catch {
        $copyFailure = $_
    }
    try {
        Remove-SmokeRelease $destination
    }
    catch {
        throw "$($copyFailure.Exception.Message)`nCLEANUP_FAILED: $($_.Exception.Message)"
    }
    throw $copyFailure
}
$bundle = (Resolve-Path -LiteralPath $BundleRoot).Path
& (Join-Path $PSScriptRoot 'verify-release-bundle.ps1') -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$python = (Resolve-Path -LiteralPath $Python).Path; $release = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$smokeParent = [IO.Path]::GetFullPath($SmokeRoot); $otherDriveParent = [IO.Path]::GetFullPath($OtherDriveRoot)
if ($smokeParent -eq $otherDriveParent) { throw 'SMOKE_ROOTS_MUST_DIFFER' }
try {
    $smokeVolume = [SmokeVolumeIdentity]::Get($smokeParent)
    $otherDriveVolume = [SmokeVolumeIdentity]::Get($otherDriveParent)
}
catch {
    throw "SMOKE_VOLUME_IDENTITY_UNAVAILABLE: $($_.Exception.Message)"
}
if ([String]::Equals($smokeVolume, $otherDriveVolume, [StringComparison]::OrdinalIgnoreCase)) { throw 'SMOKE_ROOTS_MUST_BE_DIFFERENT_VOLUMES' }
& (Join-Path $PSScriptRoot 'run-packaged-tests.ps1') -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256 -Python $python -ReleaseRoot $release
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("omr-grader-wheel-manifest-" + [Guid]::NewGuid().ToString('N'))
$wheelManifest = Join-Path $tempRoot 'release-wheel-manifest.json'
$smokeRelease = $null; $otherDriveRelease = $null
New-Item -ItemType Directory -Path $tempRoot | Out-Null
$primaryFailure = $null
$primaryExitCode = 0
$cleanupFailures = [System.Collections.Generic.List[System.Management.Automation.ErrorRecord]]::new()
try {
    & (Join-Path $PSScriptRoot 'verify-release-bundle.ps1') -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256 -WheelManifestPath $wheelManifest
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $primaryExitCode = $exitCode
        throw "VERIFY_RELEASE_BUNDLE_FAILED: $exitCode"
    }
    $smokeRelease = New-SmokeRelease $smokeParent 'writable-smoke' $release
    $otherDriveRelease = New-SmokeRelease $otherDriveParent 'other-drive-smoke' $smokeRelease
    $repo = Split-Path $PSScriptRoot -Parent
    & $python (Join-Path $repo 'packaging\verify_release.py') --release $otherDriveRelease --manifest $wheelManifest --wheelhouse (Join-Path $bundle 'wheelhouse') --smoke
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $primaryExitCode = $exitCode
        throw "SMOKE_RELEASE_VERIFICATION_FAILED: $exitCode"
    }
}
catch {
    $primaryFailure = $_
}
finally {
    foreach ($path in @($otherDriveRelease, $smokeRelease)) {
        if ($null -ne $path) {
            try {
                Remove-SmokeRelease $path
            }
            catch {
                $cleanupFailures.Add($_)
            }
        }
    }
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
