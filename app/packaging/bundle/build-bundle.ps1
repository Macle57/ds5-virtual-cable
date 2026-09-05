# Build the single-exe "bundle" installer: ds5bridge + usbip-win2 + HidHide.
#
#   powershell -File app\packaging\bundle\build-bundle.ps1              build
#   powershell -File app\packaging\bundle\build-bundle.ps1 -SkipApp     do not rebuild dist\ds5bridge first
#
# What it does:
#   1. (unless -SkipApp) runs app\packaging\build.ps1 so dist\ds5bridge is fresh
#   2. downloads the two vendor installers into app\packaging\bundle\vendor\
#      (git-ignored) and REFUSES to build unless their SHA-256 match the pins
#      below -- the same pins scripts\install.ps1 uses
#   3. compiles ds5bridge-bundle.iss with ISCC into dist\bundle\
#
# The pins are the whole point: the bundle embeds exactly the bytes the
# upstream authors published, verified, and nothing else.

param(
    [switch]$SkipApp,
    [string]$Iscc = ''
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$repo = (Resolve-Path (Join-Path $here '..\..\..')).Path
$vendor = Join-Path $here 'vendor'
$dist = Join-Path $repo 'dist'
$out = Join-Path $dist 'bundle'

$Pins = @(
    @{ Name = 'USBip-0.9.7.7-x64.exe'
       Url  = 'https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.7.7/USBip-0.9.7.7-x64.exe'
       Sha  = '51620FA5F9F8BE5932BC9D786DEEE557CE06D5407A99CAB490DCFAC71F185FEA' },
    @{ Name = 'HidHide_1.5.230_x64.exe'
       Url  = 'https://github.com/nefarius/HidHide/releases/download/v1.5.230.0/HidHide_1.5.230_x64.exe'
       Sha  = 'F4BBBCB82E6258641B887C74BC81C4C5F66E4AA811808DFC304347687B7605F6' }
)

function Find-Iscc {
    if ($Iscc -and (Test-Path $Iscc)) { return $Iscc }
    $c = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $c) {
        throw 'ISCC.exe not found. Install Inno Setup 6: winget install JRSoftware.InnoSetup --silent'
    }
    return $c
}

if (-not $SkipApp) {
    Write-Host '--- app (PyInstaller) ---'
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repo 'app\packaging\build.ps1')
    if ($LASTEXITCODE -ne 0) { throw "app build failed ($LASTEXITCODE)" }
}
$appDist = Join-Path $dist 'ds5bridge'
if (-not (Test-Path (Join-Path $appDist 'ds5bridge-tray.exe'))) {
    throw "no app build at $appDist (run without -SkipApp)"
}

Write-Host '--- vendor installers (pinned) ---'
New-Item -ItemType Directory -Force -Path $vendor | Out-Null
$old = $global:ProgressPreference
$global:ProgressPreference = 'SilentlyContinue'
try {
    foreach ($p in $Pins) {
        $dest = Join-Path $vendor $p.Name
        $ok = (Test-Path $dest) -and ((Get-FileHash $dest -Algorithm SHA256).Hash -eq $p.Sha)
        if (-not $ok) {
            Write-Host "  downloading $($p.Name) ..."
            Invoke-WebRequest -Uri $p.Url -OutFile $dest -UseBasicParsing
            $got = (Get-FileHash $dest -Algorithm SHA256).Hash
            if ($got -ne $p.Sha) {
                Remove-Item $dest -Force
                throw "$($p.Name): SHA-256 mismatch (got $got, expected $($p.Sha)). Refusing to bundle it."
            }
        }
        $sig = Get-AuthenticodeSignature $dest
        if ($sig.Status -ne 'Valid') {
            throw "$($p.Name): Authenticode signature is $($sig.Status); refusing to bundle it."
        }
        Write-Host ("  {0,-28} sha256 ok, signed by {1}" -f $p.Name, (($sig.SignerCertificate.Subject -split ',')[0]))
    }
} finally { $global:ProgressPreference = $old }

Write-Host '--- ISCC ---'
$iscc = Find-Iscc
New-Item -ItemType Directory -Force -Path $out | Out-Null
$verLine = (& (Join-Path $appDist 'ds5bridge.exe') --version 2>$null | Select-Object -First 1)
$version = if ("$verLine" -match '(\d+(\.\d+)+)') { $Matches[1] } else { '0.0.0' }
& $iscc "/DAppVersion=$version" "/DDistDir=$appDist" "/DVendorDir=$vendor" "/DOutputDir=$out" (Join-Path $here 'ds5bridge-bundle.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }

Write-Host ''
Write-Host '--- result ---'
Get-ChildItem $out -Filter '*.exe' | ForEach-Object { '{0,-40} {1,8:N1} MB' -f $_.Name, ($_.Length / 1MB) }
Write-Host ''
Write-Host 'silent install:    ds5bridge-bundle-setup.exe /SILENT /NORESTART /LOG="%TEMP%\ds5bridge-setup.log"'
Write-Host '  opt out:         ... /COMPONENTS="app,usbip"        (no HidHide)'
Write-Host 'silent uninstall:  "%ProgramFiles%\ds5bridge\unins000.exe" /SILENT /NORESTART [/KEEPUSBIP=1] [/KEEPHIDHIDE=1]'
