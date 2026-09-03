# Build ds5bridge.exe and ds5bridge-tray.exe with PyInstaller.
#
#   powershell -File app\packaging\build.ps1              one-dir  (default)
#   powershell -File app\packaging\build.ps1 -OneFile     one-file (comparison)
#   powershell -File app\packaging\build.ps1 -Clean       rebuild from scratch
#
# ONE-DIR IS THE DEFAULT, and the reasons are measured rather than folklore --
# the numbers are in docs/STATUS.md section 18. In short:
#
#   * A one-file exe is a self-extracting archive. It unpacks ~90 MB of numpy,
#     PyAV and libopus into %TEMP%\_MEIxxxxx on EVERY launch, which costs
#     seconds of startup and writes a fresh copy of a hundred DLLs into a temp
#     directory each time.
#   * That behaviour -- a packed executable that writes and then executes DLLs
#     from a temp path -- is exactly the heuristic shape SmartScreen and
#     third-party AV score badly, and PyInstaller one-file builds are a
#     long-standing false-positive magnet. A one-dir build's DLLs sit on disk
#     where they were installed.
#   * Neither is signed here, so BOTH get a SmartScreen "Windows protected your
#     PC" prompt on first run on a machine that has not seen them before. That
#     is a signing problem, not a packaging one, and it is unsolved in this
#     phase -- see docs/USER-GUIDE.md.
#
# The one-file build is kept working because it is genuinely nicer to hand to
# somebody as a single attachment, and because the choice should stay a choice.

param(
    [switch]$OneFile,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$py = Join-Path $repo 'prototype\.venv\Scripts\python.exe'
$spec = Join-Path $PSScriptRoot 'ds5bridge.spec'
$dist = Join-Path $repo 'dist'
$work = Join-Path $repo 'build'

if (-not (Test-Path $py)) { throw "no venv python at $py" }

if ($Clean) {
    Write-Host '--- clean ---'
    foreach ($d in @($dist, $work)) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    }
}

# The dashboard page is built from app/dashboard-ui (see app/README.md). Do it
# here when the toolchain is set up, so a stale committed page cannot ship by
# accident; a checkout without node just packages the committed file.
$ui = Join-Path $repo 'app\dashboard-ui'
if ((Test-Path (Join-Path $ui 'node_modules')) -and (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Host '--- dashboard page (npm run build) ---'
    Push-Location $ui
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "dashboard build failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
} else {
    Write-Host '--- dashboard page: no node_modules, packaging the committed ds5app\dashboard.html ---'
}

$env:DS5_ONEFILE = if ($OneFile) { '1' } else { '0' }
Write-Host "--- building ($(if ($OneFile) {'one-file'} else {'one-dir'})) ---"

& $py -m PyInstaller --noconfirm --distpath $dist --workpath $work $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

Write-Host ''
Write-Host '--- result ---'
if ($OneFile) {
    Get-ChildItem $dist -Filter 'ds5bridge*.exe' |
        ForEach-Object { '{0,-24} {1,10:N1} MB' -f $_.Name, ($_.Length / 1MB) }
} else {
    $d = Join-Path $dist 'ds5bridge'
    $total = (Get-ChildItem $d -Recurse -File | Measure-Object Length -Sum).Sum
    Get-ChildItem $d -Filter 'ds5bridge*.exe' |
        ForEach-Object { '{0,-24} {1,10:N1} MB' -f $_.Name, ($_.Length / 1MB) }
    '{0,-24} {1,10:N1} MB  ({2} files)' -f 'TOTAL (folder)', ($total / 1MB),
        (Get-ChildItem $d -Recurse -File).Count
    Write-Host ''
    Write-Host "run it:  $d\ds5bridge.exe doctor"
}
