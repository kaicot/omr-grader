[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$DistRoot = '',
    [string]$WorkRoot = '',
    [string]$ArchivePath = '',
    [int]$BuildNumber = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Fail([string]$Message) { throw "PORTABLE_RELEASE_BUILD_FAILED: $Message" }
function FullPath([string]$PathValue) {
    if ([string]::IsNullOrWhiteSpace($PathValue)) { Fail 'empty path is not allowed' }
    return [IO.Path]::GetFullPath($PathValue)
}
function Assert-NoReparseAncestor([string]$PathValue, [string]$Label) {
    $current = FullPath $PathValue
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Fail "$Label must not be a symbolic-link or junction path: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
}

$repository = FullPath (Split-Path $PSScriptRoot -Parent)
if (-not $Python) { $Python = Join-Path $repository '.venv\Scripts\python.exe' }
$Python = FullPath $Python
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { Fail "Python does not exist: $Python" }
if (-not $DistRoot) { $DistRoot = Join-Path $repository 'dist' }
if (-not $WorkRoot) { $WorkRoot = Join-Path $repository 'build' }
$DistRoot = FullPath $DistRoot
$WorkRoot = FullPath $WorkRoot
$spec = Join-Path $repository 'packaging\OMR_Grader.spec'
$releaseTool = Join-Path $repository 'tools\portable_release.py'

# All prerequisites are checked before creating any caller-provided directory or
# reservation. A WorkRoot is a parent owned by its caller, never a scratch folder
# that this script may erase.
Assert-NoReparseAncestor $repository 'repository'
Assert-NoReparseAncestor $DistRoot 'DistRoot'
Assert-NoReparseAncestor $WorkRoot 'WorkRoot'
if (-not (Test-Path -LiteralPath $spec -PathType Leaf)) { Fail "Spec file is missing: $spec" }
if (-not (Test-Path -LiteralPath $releaseTool -PathType Leaf)) { Fail "Release tool is missing: $releaseTool" }
& $Python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version; import PyInstaller'
if ($LASTEXITCODE -ne 0) { Fail 'Python 3.12 with PyInstaller is required' }

$dateStamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd')
if ($BuildNumber -lt 0) { Fail 'BuildNumber must be a positive integer when supplied' }
if ($BuildNumber -eq 0) {
    $numbers = @(
        if (Test-Path -LiteralPath $DistRoot) {
            Get-ChildItem -LiteralPath $DistRoot -Force | ForEach-Object {
                if ($_.Name -match '^OMR-Grader-fixed(\d+)-\d{8}(?:\.zip|\.reserve)?$') { [int]$Matches[1] }
            }
        }
    )
    $BuildNumber = if ($numbers.Count) { [int](($numbers | Measure-Object -Maximum).Maximum) + 1 } else { 1 }
}
$releaseName = "OMR-Grader-fixed$BuildNumber-$dateStamp"
$versionFolder = Join-Path $DistRoot $releaseName
$reservation = Join-Path $DistRoot "$releaseName.reserve"
if (-not $ArchivePath) { $ArchivePath = Join-Path $DistRoot "$releaseName.zip" }
$ArchivePath = FullPath $ArchivePath
if ([IO.Path]::GetDirectoryName($ArchivePath) -ne $DistRoot) { Fail 'ArchivePath must be a direct child of DistRoot' }
if ([IO.Path]::GetFileName($ArchivePath) -ne "$releaseName.zip") { Fail 'ArchivePath must use the selected release name' }
foreach ($path in @($versionFolder, $ArchivePath, "$ArchivePath.sha256", $reservation)) {
    if (Test-Path -LiteralPath $path) { Fail "output collision; no existing artifact will be replaced: $path" }
}

# Only after inputs and every destination have been checked do we create a new,
# uniquely owned child. This script never deletes WorkRoot or any existing child.
New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
$workChild = Join-Path $WorkRoot ("portable-build-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $workChild -ErrorAction Stop | Out-Null
$reservationToken = [guid]::NewGuid().ToString('N')
[IO.File]::WriteAllText($reservation, $reservationToken, [Text.UTF8Encoding]::new($false))

$beforeSnapshot = Join-Path $workChild 'build-inputs-before.json'
$stageRelease = Join-Path $workChild $releaseName
$stageArchive = Join-Path $workChild "$releaseName.zip"
try {
    & $Python $releaseTool snapshot-inputs --repository $repository --output $beforeSnapshot
    if ($LASTEXITCODE -ne 0) { Fail 'could not capture pre-build input snapshot' }
    & $Python -m PyInstaller --noconfirm --clean --distpath $stageRelease $spec --workpath (Join-Path $workChild 'pyinstaller-work')
    if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed with exit code $LASTEXITCODE" }
    $applicationFolder = Join-Path $stageRelease 'OMR Grader'
    if (-not (Test-Path -LiteralPath (Join-Path $applicationFolder 'OMR Grader.exe') -PathType Leaf)) {
        Fail 'PyInstaller did not produce OMR Grader.exe in the onedir payload'
    }
    & $Python $releaseTool create-receipt --release $stageRelease --repository $repository --before-snapshot $beforeSnapshot --python $Python
    if ($LASTEXITCODE -ne 0) { Fail 'could not create format-2 release receipt' }
    Compress-Archive -LiteralPath $stageRelease -DestinationPath $stageArchive -CompressionLevel Optimal
    "$((Get-FileHash -LiteralPath $stageArchive -Algorithm SHA256).Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($ArchivePath))" |
        Set-Content -LiteralPath "$stageArchive.sha256" -Encoding ascii
    # Verify staged directory and exact ZIP bytes before publishing either.
    & $Python $releaseTool verify --release $stageRelease --archive $stageArchive --repository $repository
    if ($LASTEXITCODE -ne 0) { Fail 'staged release verification failed' }
    Move-Item -LiteralPath $stageRelease -Destination $versionFolder -ErrorAction Stop
    Move-Item -LiteralPath $stageArchive -Destination $ArchivePath -ErrorAction Stop
    Move-Item -LiteralPath "$stageArchive.sha256" -Destination "$ArchivePath.sha256" -ErrorAction Stop
    Remove-Item -LiteralPath $reservation -Force -ErrorAction Stop
    [PSCustomObject]@{
        result = 'BUILT_NOT_SMOKE_APPROVED'; release = $versionFolder
        application = (Join-Path $versionFolder 'OMR Grader'); archive = $ArchivePath
        archive_sha256_sidecar = "$ArchivePath.sha256"; work_child = $workChild
    } | ConvertTo-Json -Depth 3
} catch {
    # Keep the reservation and uniquely owned child as an auditable failed attempt.
    # Do not touch caller files or a previously published output.
    throw
}
