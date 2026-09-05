# Build ds5bridge-setup-<version>.exe -- the Inno Setup installer.
#
#   powershell -File app\packaging\build-installer.ps1              build (app first if dist\ds5bridge is missing)
#   powershell -File app\packaging\build-installer.ps1 -Rebuild     rebuild the PyInstaller app first (build.ps1 -Clean)
#   powershell -File app\packaging\build-installer.ps1 -BundleUsbip C:\dl\USBip-0.9.7.7-x64.exe -BundleHidHide C:\dl\HidHide_1.5.230_x64.exe
#                                                                     carry the two driver installers inside the exe
#                                                                     instead of downloading them at install time
#
# What goes in: dist\ds5bridge\ (the one-dir PyInstaller build, our own code)
# and app\packaging\setup-helper.ps1. What does NOT go in by default: the two
# third-party driver installers -- ds5bridge.iss downloads them from their
# pinned URLs and checks their SHA-256 at install time (NOTICE: no third-party
# binaries are redistributed). -BundleUsbip/-BundleHidHide flip that per
# file; the hashes are checked either way, so a wrong file fails the build's
# own check here before it can fail on a user's machine.
#
# Needs Inno Setup 6.7+ (DownloadTemporaryFile, ExecAndCaptureOutput, the current CreateCustomForm):
#     winget install --id JRSoftware.InnoSetup --exact --scope user
#
# Nothing is code-signed. SmartScreen will warn on first run of the result;
# that is a certificate problem, not a build one (docs/installer.md).

[CmdletBinding()]
param(
    [switch]$Rebuild,
    [string]$BundleUsbip = '',
    [string]$BundleHidHide = ''
)

$ErrorActionPreference = 'Stop'
$repo    = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dist    = Join-Path $repo 'dist'
$appDir  = Join-Path $dist 'ds5bridge'
$iss     = Join-Path $PSScriptRoot 'ds5bridge.iss'

# Pinned facts, duplicated from ds5bridge.iss for the bundle check only.
$UsbipSha256   = '51620FA5F9F8BE5932BC9D786DEEE557CE06D5407A99CAB490DCFAC71F185FEA'
$HidHideSha256 = 'F4BBBCB82E6258641B887C74BC81C4C5F66E4AA811808DFC304347687B7605F6'

# --- the version: one source, app/ds5app/__init__.py ------------------------
$initPy = Join-Path $repo 'app\ds5app\__init__.py'
$version = (Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $version) { throw "could not read __version__ from $initPy" }

# --- the app build ---------------------------------------------------------
if ($Rebuild -or -not (Test-Path (Join-Path $appDir 'ds5bridge-tray.exe'))) {
    Write-Host '--- app build (build.ps1) ---'
    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'build.ps1'))
    if ($Rebuild) { $args += '-Clean' }
    & powershell.exe @args
    if ($LASTEXITCODE -ne 0) { throw "build.ps1 failed ($LASTEXITCODE)" }
}
$built = (& (Join-Path $appDir 'ds5bridge.exe') --version 2>$null | Select-Object -First 1)
if ("$built" -notmatch [regex]::Escape($version)) {
    throw "dist\ds5bridge reports '$built' but the source is $version -- run with -Rebuild"
}

# --- Inno Setup ------------------------------------------------------------
$iscc = @(
    (Get-Command iscc.exe -ErrorAction SilentlyContinue | ForEach-Object Source),
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    throw 'Inno Setup 6 not found. Install it:  winget install --id JRSoftware.InnoSetup --exact --scope user'
}

$defs = @("/DAppVersion=$version", "/DAppBuildDir=$appDir", "/DOutputDir=$dist")
foreach ($pair in @(@($BundleUsbip, $UsbipSha256, 'BundleUsbip'), @($BundleHidHide, $HidHideSha256, 'BundleHidHide'))) {
    $path, $sha, $name = $pair
    if (-not $path) { continue }
    $path = (Resolve-Path $path).Path
    $actual = (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actual -ne $sha) { throw "$name`: $path has SHA-256 $actual, expected $sha -- refusing to bundle the wrong file" }
    $defs += "/D$name=$path"
    Write-Host "bundling $name from $path (SHA-256 verified)"
}

Write-Host "--- compiling $iss (ds5bridge $version) ---"
& $iscc /Qp @defs $iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

$out = Join-Path $dist "ds5bridge-setup-$version.exe"
if (-not (Test-Path $out)) { throw "ISCC reported success but $out is missing" }
Write-Host ''
Write-Host '--- result ---'
'{0,-32} {1,8:N1} MB' -f (Split-Path $out -Leaf), ((Get-Item $out).Length / 1MB)
'SHA-256  ' + (Get-FileHash -Path $out -Algorithm SHA256).Hash
Write-Host ''
Write-Host "silent install:    $out /SILENT /NORESTART /LOG=`"install.log`" [/COMPONENTS=`"app,usbip,hidhide`"] [/TASKS=`"autostart,restorepoint`"]"
Write-Host "silent uninstall:  `"$env:LOCALAPPDATA\ds5bridge\unins000.exe`" /SILENT /NORESTART /LOG=`"uninstall.log`" [/KEEPUSBIP=1] [/KEEPHIDHIDE=1] [/PURGESETTINGS=1]"
