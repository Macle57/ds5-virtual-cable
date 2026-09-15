# Build ds5bridge-setup-<version>.exe -- the Inno Setup installer.
#
#   powershell -File app\packaging\build-installer.ps1              download-mode build (app first if dist\ds5bridge is missing)
#   powershell -File app\packaging\build-installer.ps1 -Bundle      the bundled build: fetches the two pinned vendor
#                                                                     installers into app\packaging\bundle\vendor\ (once),
#                                                                     verifies them, and carries them inside the exe
#   powershell -File app\packaging\build-installer.ps1 -Rebuild     rebuild the PyInstaller app first (build.ps1 -Clean)
#   powershell -File app\packaging\build-installer.ps1 -BundleUsbip C:\dl\USBip-0.9.7.7-x64.exe -BundleHidHide C:\dl\HidHide_1.5.230_x64.exe
#                                                                     the same as -Bundle, from files you already have
#   ... -OutputDir dist\bundle                                        write the exe somewhere other than dist\
#
# What goes in: dist\ds5bridge\ (the one-dir PyInstaller build, our own code)
# and app\packaging\setup-helper.ps1. The two third-party driver installers
# go in only for a bundled build; the download-mode build has ds5bridge.iss
# fetch them from their pinned URLs at install time instead. Their SHA-256 is
# checked either way -- here before bundling, and again on the user's machine
# before either flavour runs one.
#
# The two flavours have different file names, so they can coexist:
#     dist\ds5bridge-setup-<version>.exe            download mode
#     dist\ds5bridge-setup-<version>-bundled.exe    bundled (NOTICE: the
#                                                   vendors' notices ship in
#                                                   THIRD-PARTY-NOTICES.txt)
# Builds are deterministic: the same inputs give a byte-identical exe.
#
# Needs Inno Setup 6.7 (DownloadTemporaryFile, ExecAndCaptureOutput, the
# current CreateCustomForm). 6.7.3 is what the installer was verified with and
# what .github/workflows/release.yml pins; Inno Setup 7 has not been tried.
#     winget install --id JRSoftware.InnoSetup --exact --scope user --version 6.7.3
#
# Nothing is code-signed. SmartScreen will warn on first run of the result;
# that is a certificate problem, not a build one (docs/installer.md).

[CmdletBinding()]
param(
    [switch]$Rebuild,
    [switch]$Bundle,
    [string]$BundleUsbip = '',
    [string]$BundleHidHide = '',
    [string]$OutputDir = '',
    # Passed through to build.ps1 (-Python): the interpreter to build the app
    # with, for a worktree without its own venv.
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$repo    = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dist    = Join-Path $repo 'dist'
$appDir  = Join-Path $dist 'ds5bridge'
$iss     = Join-Path $PSScriptRoot 'ds5bridge.iss'
$vendor  = Join-Path $PSScriptRoot 'bundle\vendor'
if (-not $OutputDir) { $OutputDir = $dist }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path $OutputDir).Path

# Pinned facts, duplicated from ds5bridge.iss (and scripts\install.ps1) for
# the bundle fetch and check. Keep the four in step.
$Vendors = @(
    @{ Define = 'BundleUsbip'
       Name   = 'USBip-0.9.7.7-x64.exe'
       Url    = 'https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.7.7/USBip-0.9.7.7-x64.exe'
       Sha256 = '51620FA5F9F8BE5932BC9D786DEEE557CE06D5407A99CAB490DCFAC71F185FEA'
       Path   = $BundleUsbip },
    @{ Define = 'BundleHidHide'
       Name   = 'HidHide_1.5.230_x64.exe'
       Url    = 'https://github.com/nefarius/HidHide/releases/download/v1.5.230.0/HidHide_1.5.230_x64.exe'
       Sha256 = 'F4BBBCB82E6258641B887C74BC81C4C5F66E4AA811808DFC304347687B7605F6'
       Path   = $BundleHidHide }
)

# --- the version: one source, app/ds5app/__init__.py ------------------------
$initPy = Join-Path $repo 'app\ds5app\__init__.py'
$version = (Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $version) { throw "could not read __version__ from $initPy" }

# --- the app build ---------------------------------------------------------
if ($Rebuild -or -not (Test-Path (Join-Path $appDir 'ds5bridge-tray.exe'))) {
    Write-Host '--- app build (build.ps1) ---'
    $buildArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'build.ps1'))
    if ($Rebuild) { $buildArgs += '-Clean' }
    if ($Python) { $buildArgs += @('-Python', $Python) }
    & powershell.exe @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "build.ps1 failed ($LASTEXITCODE)" }
}
$built = (& (Join-Path $appDir 'ds5bridge.exe') --version 2>$null | Select-Object -First 1)
if ("$built" -notmatch [regex]::Escape($version)) {
    throw "dist\ds5bridge reports '$built' but the source is $version -- run with -Rebuild"
}

# --- the vendor installers, for a bundled build ----------------------------
# -Bundle fetches whichever of the two is not already in bundle\vendor\ with
# the right hash (the directory is git-ignored). A file given explicitly is
# used as is. Either way nothing is embedded until its SHA-256 matches the pin
# and its Authenticode signature is valid: a bundled build redistributes the
# vendor's bytes, so it had better be the vendor's bytes.
if ($Bundle) {
    New-Item -ItemType Directory -Force -Path $vendor | Out-Null
    foreach ($v in $Vendors) {
        if ($v.Path) { continue }
        $dest = Join-Path $vendor $v.Name
        if (-not ((Test-Path $dest) -and ((Get-FileHash $dest -Algorithm SHA256).Hash -eq $v.Sha256))) {
            Write-Host "downloading $($v.Name) ..."
            $old = $global:ProgressPreference
            $global:ProgressPreference = 'SilentlyContinue'
            try { Invoke-WebRequest -Uri $v.Url -OutFile $dest -UseBasicParsing }
            finally { $global:ProgressPreference = $old }
        }
        $v.Path = $dest
    }
}
$defs = @("/DAppVersion=$version", "/DAppBuildDir=$appDir", "/DOutputDir=$OutputDir")
$bundled = $false
foreach ($v in $Vendors) {
    if (-not $v.Path) { continue }
    $path = (Resolve-Path $v.Path).Path
    $actual = (Get-FileHash -Path $path -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actual -ne $v.Sha256) {
        Remove-Item $path -Force -ErrorAction SilentlyContinue
        throw "$($v.Define): $path has SHA-256 $actual, expected $($v.Sha256) -- refusing to bundle the wrong file (deleted)"
    }
    $sig = Get-AuthenticodeSignature $path
    if ($sig.Status -ne 'Valid') {
        throw "$($v.Define): $path has an Authenticode signature that is $($sig.Status) -- refusing to bundle it"
    }
    $defs += "/D$($v.Define)=$path"
    $bundled = $true
    Write-Host ("bundling {0} (SHA-256 verified, signed by {1})" -f $v.Name, (($sig.SignerCertificate.Subject -split ',')[0]))
}

# --- Inno Setup ------------------------------------------------------------
$iscc = @(
    (Get-Command iscc.exe -ErrorAction SilentlyContinue | ForEach-Object Source),
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) {
    throw 'Inno Setup 6 not found. Install it:  winget install --id JRSoftware.InnoSetup --exact --scope user --version 6.7.3'
}

Write-Host "--- compiling $iss (ds5bridge $version, $(if ($bundled) {'bundled'} else {'download mode'})) ---"
& $iscc /Qp @defs $iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

# The name ds5bridge.iss chooses (its OutputName define), reproduced here.
$outName = if ($bundled) { "ds5bridge-setup-$version-bundled.exe" } else { "ds5bridge-setup-$version.exe" }
$out = Join-Path $OutputDir $outName
if (-not (Test-Path $out)) { throw "ISCC reported success but $out is missing" }
Write-Host ''
Write-Host '--- result ---'
'{0,-40} {1,8:N1} MB' -f $outName, ((Get-Item $out).Length / 1MB)
'SHA-256  ' + (Get-FileHash -Path $out -Algorithm SHA256).Hash
Write-Host ''
Write-Host "silent install:    $out /SILENT /NORESTART /LOG=`"install.log`" [/COMPONENTS=`"app,usbip,hidhide`"] [/TASKS=`"autostart,restorepoint`"]"
Write-Host "silent uninstall:  `"$env:LOCALAPPDATA\ds5bridge\unins000.exe`" /SILENT /NORESTART /LOG=`"uninstall.log`" [/KEEPUSBIP=1] [/KEEPHIDHIDE=1] [/PURGESETTINGS=1]"
