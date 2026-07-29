[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BundleRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedManifestSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedInstallerSha256,
    [Parameter(Mandatory = $true)][string]$BuildRoot
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Remove-Item Env:NO_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:no_proxy -ErrorAction SilentlyContinue
$env:PIP_NO_INDEX = '1'; $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'; $env:PIP_NO_INPUT = '1'
$env:PIP_ONLY_BINARY = ':all:'; $env:PIP_NO_BUILD_ISOLATION = '1'; $env:PYTHONNOUSERSITE = '1'
$env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9'; $env:ALL_PROXY = 'http://127.0.0.1:9'
$env:http_proxy = $env:HTTP_PROXY; $env:https_proxy = $env:HTTPS_PROXY; $env:all_proxy = $env:ALL_PROXY
$env:PIP_INDEX_URL = 'http://127.0.0.1:9/simple'; $env:PIP_EXTRA_INDEX_URL = 'http://127.0.0.1:9/simple'
function Fail([string]$Code) { throw $Code }
function Assert-NonReparseDirectory([string]$Path, [string]$Code) {
    $info = Get-Item -LiteralPath $Path -Force
    if ($info -isnot [IO.DirectoryInfo] -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { Fail $Code }
    return $info
}
function Assert-NonReparsePath([string]$Path, [string]$Code) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    $current = [IO.Path]::GetPathRoot($fullPath)
    $null = Assert-NonReparseDirectory $current $Code
    foreach ($part in $fullPath.Substring($current.Length).Split([IO.Path]::DirectorySeparatorChar, [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        $null = Assert-NonReparseDirectory $current $Code
    }
}
function Assert-TrustedDirectory([string]$Path, [Security.Principal.SecurityIdentifier]$CurrentUser, [string]$Code) {
    Assert-NonReparsePath $Path $Code
    $acl = Get-Acl -LiteralPath $Path
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $CurrentUser.Value) { Fail $Code }
    $trusted = @($CurrentUser.Value, 'S-1-5-18', 'S-1-5-32-544')
    $unsafeRights = [Security.AccessControl.FileSystemRights]::WriteData -bor [Security.AccessControl.FileSystemRights]::AppendData -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::WriteAttributes -bor [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and $trusted -notcontains $rule.IdentityReference.Value -and (([int]$rule.FileSystemRights -band [int]$unsafeRights) -ne 0)) { Fail $Code }
    }
}
$bundle = (Resolve-Path -LiteralPath $BundleRoot).Path
$root = [IO.Path]::GetFullPath($BuildRoot)
if (Test-Path -LiteralPath $root) { Fail 'BUILD_ROOT_NOT_FRESH' }
$parent = Split-Path -LiteralPath $root -Parent
if (-not $parent -or -not (Test-Path -LiteralPath $parent -PathType Container)) { Fail 'BUILD_ROOT_PARENT_MISSING' }
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
Assert-TrustedDirectory $parent $currentUser 'BUILD_ROOT_PARENT_UNTRUSTED'
New-Item -ItemType Directory -Path $root | Out-Null
$privateAcl = New-Object Security.AccessControl.DirectorySecurity
$privateAcl.SetAccessRuleProtection($true, $false)
$privateAcl.SetOwner($currentUser)
$privateAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($currentUser, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow)))
Set-Acl -LiteralPath $root -AclObject $privateAcl
Assert-TrustedDirectory $root $currentUser 'BUILD_ROOT_UNTRUSTED'
$verifyBundle = Join-Path $PSScriptRoot 'verify-release-bundle.ps1'
& $verifyBundle -BundleRoot $bundle -ExpectedManifestSha256 $ExpectedManifestSha256
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$installer = Join-Path $bundle 'cpython-3.12.10-amd64.exe'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw 'SUPPLY_INSTALLER_MISSING' }
if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedInstallerSha256) { throw 'SUPPLY_INSTALLER_HASH_MISMATCH' }
$pythonHome = Join-Path $root 'cpython-3.12.10'
if (Test-Path -LiteralPath $pythonHome) { throw 'BUILD_ROOT_NOT_DISPOSABLE' }
$arguments = '/quiet InstallAllUsers=0 Include_pip=0 Include_launcher=0 PrependPath=0 Shortcuts=0 Include_test=0 TargetDir="' + $pythonHome + '"'
$process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -NoNewWindow
if ($process.ExitCode -ne 0) { exit $process.ExitCode }
$basePython = Join-Path $pythonHome 'python.exe'
if (-not (Test-Path -LiteralPath $basePython -PathType Leaf)) { throw 'ATTESTED_PYTHON_MISSING' }
& $basePython -c "import platform,sys; assert sys.version_info[:3] == (3,12,10); assert platform.machine().upper() == 'AMD64'"
if ($LASTEXITCODE -ne 0) { throw 'ATTESTED_PYTHON_MISMATCH' }
$venv = Join-Path $root 'venv'
& $basePython -m venv --without-pip $venv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$python = Join-Path $venv 'Scripts\python.exe'; $pipPyz = Join-Path $bundle 'pip-25.1.1.pyz'; $wheelhouse = Join-Path $bundle 'wheelhouse'
$bootstrap = Join-Path $bundle 'bootstrap.lock'; $constraints = Join-Path $bundle 'constraints\windows-py312.lock'; $application = Join-Path $bundle 'application.lock'
foreach ($file in @($pipPyz, $wheelhouse, $bootstrap, $constraints, $application)) { if (-not (Test-Path -LiteralPath $file)) { throw "SUPPLY_REQUIRED_INPUT_MISSING: $file" } }
& $python $pipPyz install --no-index --find-links $wheelhouse --only-binary=:all: --require-hashes --no-deps -r $bootstrap
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pip install --no-index --find-links $wheelhouse --only-binary=:all: --require-hashes --no-deps -r $constraints
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pip install --no-index --find-links $wheelhouse --only-binary=:all: --require-hashes --no-deps -r $application
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$receipt = Join-Path $root 'installed-distributions.json'
& $python (Join-Path $PSScriptRoot 'write_installed_distributions_receipt.py') --bundle $bundle --expected-manifest-sha256 $ExpectedManifestSha256 --bootstrap-lock $bootstrap --constraints-lock $constraints --application-lock $application --receipt $receipt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python (Join-Path $PSScriptRoot 'verify_installed_distributions_receipt.py') --receipt $receipt --expected-manifest-sha256 $ExpectedManifestSha256
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
