[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$BundleRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedManifestSha256,
    [ValidateNotNullOrEmpty()][string]$WheelManifestPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not ('BundleFileInfo' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class BundleFileInfo {
    [StructLayout(LayoutKind.Sequential)] public struct BY_HANDLE_FILE_INFORMATION {
        public uint FileAttributes, CreationTimeLow, CreationTimeHigh, LastAccessTimeLow, LastAccessTimeHigh, LastWriteTimeLow, LastWriteTimeHigh, VolumeSerialNumber, FileSizeHigh, FileSizeLow, NumberOfLinks, FileIndexHigh, FileIndexLow;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle handle, out BY_HANDLE_FILE_INFORMATION information);
    public static uint LinkCount(string path) {
        using (var stream = new System.IO.FileStream(path, System.IO.FileMode.Open, System.IO.FileAccess.Read, System.IO.FileShare.ReadWrite | System.IO.FileShare.Delete)) {
            BY_HANDLE_FILE_INFORMATION information;
            if (!GetFileInformationByHandle(stream.SafeFileHandle, out information)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            return information.NumberOfLinks;
        }
    }
}
'@
}
function Fail([string]$Code, [string]$Message) { throw "$Code`: $Message" }
function Assert-RealEntry([System.IO.FileSystemInfo]$Entry, [string]$Relative) {
    if (($Entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { Fail 'SUPPLY_UNSAFE_PATH' "link or reparse point is forbidden: $Relative" }
    if ($Entry -is [System.IO.FileInfo] -and [BundleFileInfo]::LinkCount($Entry.FullName) -ne 1) { Fail 'SUPPLY_UNSAFE_PATH' "alias or non-regular file is forbidden: $Relative" }
}
function Get-Sha256([string]$Path) {
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
    )
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try { $hash = $algorithm.ComputeHash($stream) }
        finally { $algorithm.Dispose() }
        return [BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
    }
    finally { $stream.Dispose() }
}
function Assert-Properties([object]$Value, [string[]]$Expected, [string]$Label) {
    if ($null -eq $Value -or $Value -isnot [pscustomobject]) { Fail 'SUPPLY_MANIFEST_FORMAT' "$Label keys are invalid" }
    $names = @($Value.PSObject.Properties.Name)
    if (($names -join "`0") -cne ($Expected -join "`0")) { Fail 'SUPPLY_MANIFEST_FORMAT' "$Label keys are invalid" }
}
function Assert-RelativePath([string]$Path) {
    if (
        [string]::IsNullOrWhiteSpace($Path) -or
        $Path.Contains('\') -or
        $Path.StartsWith('/') -or
        $Path -match '^[A-Za-z]:' -or
        $Path -ne $Path.Normalize([Text.NormalizationForm]::FormC)
    ) { Fail 'SUPPLY_UNSAFE_PATH' "unsafe path: $Path" }
    foreach ($part in $Path.Split('/')) {
        $hasControl = @(
            $part.ToCharArray() | Where-Object { [int]$_ -lt 32 }
        ).Count -gt 0
        if (
            [string]::IsNullOrEmpty($part) -or
            $part -in '.', '..' -or
            $part.Contains(':') -or
            $hasControl
        ) { Fail 'SUPPLY_UNSAFE_PATH' "unsafe path: $Path" }
    }
}
function Write-FailureRecord([string]$Label, [System.Management.Automation.ErrorRecord]$Failure) {
    [Console]::Error.WriteLine("$Label`: $Failure")
    if (-not [string]::IsNullOrWhiteSpace($Failure.ScriptStackTrace)) { [Console]::Error.WriteLine($Failure.ScriptStackTrace) }
}

try {
    $root = [IO.Path]::GetFullPath($BundleRoot)
    if ((Split-Path (Split-Path $root -Parent) -Leaf) -ne 'windows-py312' -or (Split-Path (Split-Path (Split-Path $root -Parent) -Parent) -Leaf) -ne 'supply' -or (Split-Path $root -Leaf) -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { Fail 'SUPPLY_BUNDLE_LAYOUT' 'bundle root must be supply/windows-py312/<release-id>' }
    $rootInfo = Get-Item -LiteralPath $root -Force
    if ($rootInfo -isnot [IO.DirectoryInfo]) { Fail 'SUPPLY_BUNDLE_LAYOUT' 'bundle root is not a directory' }
    Assert-RealEntry $rootInfo '.'
    $manifestPath = Join-Path $root 'manifest.json'
    $manifestInfo = Get-Item -LiteralPath $manifestPath -Force
    if ($manifestInfo -isnot [IO.FileInfo]) { Fail 'SUPPLY_UNSAFE_PATH' 'manifest is not a regular file' }
    Assert-RealEntry $manifestInfo 'manifest.json'
    $manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
    if ((Get-Sha256 $manifestPath) -ne $ExpectedManifestSha256) { Fail 'SUPPLY_MANIFEST_DIGEST' 'external manifest SHA-256 does not match' }
    $manifestText = [Text.Encoding]::UTF8.GetString($manifestBytes)
    if ($manifestText.Contains("`r") -or -not $manifestText.EndsWith("`n")) { Fail 'SUPPLY_MANIFEST_FORMAT' 'manifest bytes are not canonical JSON with one LF' }
    $manifest = $manifestText | ConvertFrom-Json -ErrorAction Stop
    $canonical = (($manifest | ConvertTo-Json -Compress -Depth 20) + "`n")
    if ([Convert]::ToBase64String($manifestBytes) -cne [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($canonical))) { Fail 'SUPPLY_MANIFEST_FORMAT' 'manifest bytes are not canonical JSON with one LF' }
    Assert-Properties $manifest @('schema_version','release_id','created_at','target','artifacts') 'manifest'
    if ($manifest.schema_version -ne 1 -or $manifest.release_id -ne (Split-Path $root -Leaf) -or $manifest.release_id -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$' -or [string]::IsNullOrWhiteSpace($manifest.created_at)) { Fail 'SUPPLY_MANIFEST_FORMAT' 'invalid manifest identity' }
    Assert-Properties $manifest.target @('platform','architecture','python') 'target'
    if ($manifest.target.platform -cne 'windows' -or $manifest.target.architecture -cne 'x64' -or $manifest.target.python -cne '3.12.10' -or $manifest.artifacts -isnot [array]) { Fail 'SUPPLY_MANIFEST_FORMAT' 'invalid target or artifacts' }
    $actual = @{}; $folded = @{}
    foreach ($entry in @(Get-ChildItem -LiteralPath $root -Force -Recurse)) {
        $relative = $entry.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
        Assert-RelativePath $relative; Assert-RealEntry $entry $relative
        $key = $relative.Normalize([Text.NormalizationForm]::FormC).ToLowerInvariant(); if ($folded.ContainsKey($key)) { Fail 'SUPPLY_UNSAFE_PATH' "casefold collision: $relative" }; $folded[$key] = $true
        if ($entry -is [IO.FileInfo]) { $actual[$relative] = $entry }
    }
    $required = @('manifest.json','cpython-3.12.10-amd64.exe','pip-25.1.1.pyz','bootstrap.lock','constraints/windows-py312.lock','application.lock')
    foreach ($path in $required) { if (-not $actual.ContainsKey($path)) { Fail 'SUPPLY_BUNDLE_LAYOUT' "required bundle file missing: $path" } }
    foreach ($path in $actual.Keys) {
        if ($path -notin $required -and $path -notmatch '^wheelhouse/[^/]+\.whl$' -and $path -notmatch '^licenses/[^/]+$') { Fail 'SUPPLY_BUNDLE_LAYOUT' "unexpected bundle path: $path" }
    }
    $wheelCount = @($actual.Keys | Where-Object { $_ -match '^wheelhouse/[^/]+\.whl$' }).Count; $licenseCount = @($actual.Keys | Where-Object { $_ -match '^licenses/[^/]+$' }).Count
    if ($wheelCount -eq 0 -or $licenseCount -eq 0) { Fail 'SUPPLY_BUNDLE_LAYOUT' 'wheelhouse and licenses must both be non-empty' }
    $listed = @{}
    $previousArtifact = $null
    foreach ($artifact in $manifest.artifacts) {
        Assert-Properties $artifact @('path','size','sha256','role','distribution','version','wheel_tags','upstream_url','license','signing_evidence') 'artifact'
        Assert-RelativePath $artifact.path
        $sizeIsInteger = $artifact.size -is [int] -or $artifact.size -is [long]
        if (
            $listed.ContainsKey($artifact.path) -or
            $artifact.path -eq 'manifest.json' -or
            -not $sizeIsInteger -or
            $artifact.size -lt 0 -or
            $artifact.sha256 -notmatch '^[0-9a-f]{64}$' -or
            ($null -ne $previousArtifact -and
                [StringComparer]::Ordinal.Compare($previousArtifact, $artifact.path) -ge 0)
        ) { Fail 'SUPPLY_MANIFEST_FORMAT' "invalid artifact: $($artifact.path)" }
        $previousArtifact = $artifact.path
        $listed[$artifact.path] = $artifact
    }
    $actualNames = @($actual.Keys | Where-Object { $_ -ne 'manifest.json' } | Sort-Object); $listedNames = @($listed.Keys | Sort-Object)
    if (($actualNames -join "`0") -cne ($listedNames -join "`0")) { Fail 'SUPPLY_BUNDLE_LAYOUT' 'manifest artifact list does not exactly match bundle files' }
    foreach ($path in $listed.Keys) { $file = $actual[$path]; $artifact = $listed[$path]; if ($file.Length -ne $artifact.size -or (Get-Sha256 $file.FullName) -cne $artifact.sha256) { Fail 'SUPPLY_FILE_INTEGRITY' "size or SHA-256 mismatch: $path" } }
    if ($PSBoundParameters.ContainsKey('WheelManifestPath')) {
        $wheelManifest = [IO.Path]::GetFullPath($WheelManifestPath)
        if (Test-Path -LiteralPath $wheelManifest) { Fail 'SUPPLY_WHEEL_MANIFEST_NOT_FRESH' 'wheel manifest output already exists' }
        $wheelDirectory = [IO.Path]::GetDirectoryName($wheelManifest)
        if (-not (Test-Path -LiteralPath $wheelDirectory -PathType Container)) { Fail 'SUPPLY_WHEEL_MANIFEST_PARENT_MISSING' 'wheel manifest parent directory is missing' }
        $wheels = @(
            foreach ($path in @($listed.Keys | Sort-Object)) {
                if ($path -like 'wheelhouse/*.whl') {
                    $artifact = $listed[$path]
                    if ([string]::IsNullOrWhiteSpace($artifact.license) -or [string]::IsNullOrWhiteSpace($artifact.upstream_url)) { Fail 'SUPPLY_WHEEL_METADATA_MISSING' $path }
                    [pscustomobject][ordered]@{ filename = [IO.Path]::GetFileName($path); size = $artifact.size; sha256 = $artifact.sha256; license = $artifact.license; provenance = $artifact.upstream_url; acquisition_record_id = "supply:$($artifact.distribution):$($artifact.version)" }
                }
            }
        )
        if ($wheels.Count -eq 0) { Fail 'SUPPLY_WHEEL_MANIFEST_EMPTY' 'bundle contains no wheels' }
        $wheelManifestContent = [pscustomobject][ordered]@{ version = 1; wheels = $wheels } | ConvertTo-Json -Compress -Depth 6
        $temporaryManifest = Join-Path $wheelDirectory ('.release-wheel-manifest-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        $primaryFailure = $null
        $cleanupFailures = [System.Collections.Generic.List[System.Management.Automation.ErrorRecord]]::new()
        try {
            $stream = [IO.File]::Open($temporaryManifest, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $bytes = [Text.UTF8Encoding]::new($false).GetBytes($wheelManifestContent)
                $stream.Write($bytes, 0, $bytes.Length)
                $stream.Flush($true)
            }
            finally { $stream.Dispose() }
            [IO.File]::Move($temporaryManifest, $wheelManifest)
        }
        catch {
            $primaryFailure = $_
        }
        finally {
            try {
                if (Test-Path -LiteralPath $temporaryManifest) { Remove-Item -LiteralPath $temporaryManifest -Force -ErrorAction Stop }
            }
            catch {
                $cleanupFailures.Add($_)
            }
        }
        if ($null -ne $primaryFailure) {
            Write-FailureRecord 'ATOMIC_WRITE_PRIMARY_FAILURE' $primaryFailure
            foreach ($cleanupFailure in $cleanupFailures) { Write-FailureRecord 'ATOMIC_WRITE_CLEANUP_FAILURE' $cleanupFailure }
            throw $primaryFailure
        }
        if ($cleanupFailures.Count -ne 0) {
            foreach ($cleanupFailure in $cleanupFailures) { Write-FailureRecord 'ATOMIC_WRITE_CLEANUP_FAILURE' $cleanupFailure }
            throw $cleanupFailures[0]
        }
    }
    exit 0
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    [Console]::Error.WriteLine($_.ScriptStackTrace)
    exit 70
}
