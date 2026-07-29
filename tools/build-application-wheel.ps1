[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Output,
    [Parameter(Mandatory = $true)][long]$SourceDateEpoch,
    [string]$Source = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$Bundle
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$pythonPath = (Resolve-Path $Python).Path
$sourcePath = (Resolve-Path $Source).Path
$outputPath = [System.IO.Path]::GetFullPath($Output)
$arguments = @(
    (Join-Path $PSScriptRoot 'build_application_wheel.py'),
    '--python', $pythonPath,
    '--source', $sourcePath,
    '--output', $outputPath,
    '--source-date-epoch', $SourceDateEpoch
)
if ($Bundle) {
    $arguments += @('--bundle', (Resolve-Path $Bundle).Path)
}
& $pythonPath @arguments
if ($LASTEXITCODE -ne 0) {
    throw "First-party wheel build failed with exit code $LASTEXITCODE."
}
