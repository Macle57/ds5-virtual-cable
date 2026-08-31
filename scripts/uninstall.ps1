<#
.SYNOPSIS
    Removes ds5bridge -- the app, its shortcut, its start-at-login entry --
    and tells you exactly how to remove the two drivers if you want that too.

        irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/uninstall.ps1 | iex

.DESCRIPTION
    What is removed automatically (all of it belongs only to ds5bridge):

        %LOCALAPPDATA%\ds5bridge\        the app and its update staging
        Start Menu \ ds5bridge.lnk       the shortcut
        HKCU Run \ "ds5bridge"           start-at-login (if enabled)
        %APPDATA%\ds5bridge\             settings -- ONLY with -PurgeSettings

    What is deliberately NOT removed: the two driver packages. usbip-win2 and
    HidHide are shared, independently-installed products -- HidHide in
    particular is used by DS4Windows and friends, and ripping it out because
    ONE of its consumers is leaving would break the others silently. The
    script prints the exact uninstall pointers instead and leaves the decision
    with you.

    Before deleting anything it shuts the bridge down properly: quits the
    tray, then runs `ds5bridge cleanup` and `ds5bridge unhide` so no virtual
    pad stays attached and no real pad stays hidden. Order matters -- cleanup
    needs the exe that is about to be deleted.

.PARAMETER DryRun
    Print what would be removed; remove nothing.
.PARAMETER PurgeSettings
    Also delete %APPDATA%\ds5bridge (your per-controller settings and labels).
    Off by default so a reinstall finds everything the way you left it.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$PurgeSettings
)

$ErrorActionPreference = 'Stop'

$InstallRoot  = Join-Path $env:LOCALAPPDATA 'ds5bridge'
$AppDir       = Join-Path $InstallRoot 'app'
$ConfigDir    = Join-Path $env:APPDATA 'ds5bridge'
$ShortcutPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ds5bridge.lnk'
$RunKey       = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValueName = 'ds5bridge'

function Say([string]$Text)  { Write-Host "  -  $Text" }
function Good([string]$Text) { Write-Host "[ok]   $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "  !  $Text" -ForegroundColor Yellow }
function Would([string]$Text) {
    if ($DryRun) { Write-Host "  ?  would: $Text" -ForegroundColor Cyan; return $false }
    Say $Text
    return $true
}

Write-Host ''
Write-Host '  ds5bridge uninstaller'
if ($DryRun) { Write-Host '  DRY RUN: showing the plan; changing nothing.' -ForegroundColor Cyan }
Write-Host ''

# --- 1. stop the bridge and put the machine back -------------------------
# The teardown tools live in the exe being removed, so this MUST come first.
$exe = Join-Path $AppDir 'ds5bridge.exe'
$procs = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
if ($procs.Count -gt 0) {
    if (Would 'quit the running ds5bridge (it detaches the virtual pad on the way out)') {
        foreach ($p in $procs) { & taskkill /PID $p.Id 2>$null | Out-Null }
        foreach ($p in $procs) { try { $p.WaitForExit(15000) | Out-Null } catch { } }
        $left = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
        if ($left.Count -gt 0) {
            Warn 'ds5bridge did not exit; forcing it (the cleanup below repairs anything left over)'
            foreach ($p in $left) { & taskkill /F /PID $p.Id 2>$null | Out-Null }
            Start-Sleep -Seconds 2
        }
    }
}
if (Test-Path $exe) {
    if (Would 'run "ds5bridge cleanup" and "ds5bridge unhide" (detach leftovers, un-cloak pads)') {
        # Best effort by design: a broken half-install must still be removable.
        try { & $exe cleanup 2>&1 | ForEach-Object { "       $_" } } catch { Warn "cleanup: $_" }
        try { & $exe unhide  2>&1 | ForEach-Object { "       $_" } } catch { Warn "unhide: $_" }
    }
} else {
    Say 'no installed exe found -- skipping the teardown commands'
}

# --- 2. remove what is ours ----------------------------------------------
try {
    $run = Get-ItemProperty -Path $RunKey -Name $RunValueName -ErrorAction SilentlyContinue
} catch { $run = $null }
if ($run) {
    if (Would "remove the start-at-login entry (HKCU Run \ $RunValueName)") {
        Remove-ItemProperty -Path $RunKey -Name $RunValueName
        Good 'start-at-login removed'
    }
} else { Say 'no start-at-login entry (nothing to remove)' }

if (Test-Path $ShortcutPath) {
    if (Would 'remove the Start Menu shortcut') {
        Remove-Item $ShortcutPath -Force
        Good 'shortcut removed'
    }
} else { Say 'no Start Menu shortcut (nothing to remove)' }

if (Test-Path $InstallRoot) {
    if (Would "remove the app ($InstallRoot)") {
        Remove-Item $InstallRoot -Recurse -Force
        Good 'app removed'
    }
} else { Say "no install at $InstallRoot (nothing to remove)" }

if (Test-Path $ConfigDir) {
    if ($PurgeSettings) {
        if (Would "remove your settings ($ConfigDir)") {
            Remove-Item $ConfigDir -Recurse -Force
            Good 'settings removed'
        }
    } else {
        Say "settings kept at $ConfigDir (use -PurgeSettings to remove them too)"
    }
}

# --- 3. the drivers: pointers, not removal --------------------------------
Write-Host ''
Write-Host '--- the two drivers (left installed on purpose) ---'
Write-Host ''
Write-Host '  Other software may be using them, so removing them is your call, not this'
Write-Host '  script''s. If nothing else on this machine needs them:'
Write-Host ''
if (Test-Path "$env:ProgramFiles\USBip\unins000.exe") {
    Write-Host '  usbip-win2:  run  "C:\Program Files\USBip\unins000.exe"'
    Write-Host '               (or Settings > Apps > Installed apps > USBip > Uninstall)'
} else {
    Write-Host '  usbip-win2:  not detected -- nothing to do'
}
$hidhide = @(
    "$env:ProgramFiles\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
    "$env:ProgramFiles\Nefarius Software Solutions e.U\HidHide\x64\HidHideCLI.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($hidhide) {
    Write-Host '  HidHide:     winget uninstall Nefarius.HidHide'
    Write-Host '               (or Settings > Apps > Installed apps > HidHide > Uninstall;'
    Write-Host '               it will ask for a reboot -- note that DS4Windows and similar'
    Write-Host '               tools also use HidHide, so only remove it if nothing else does)'
} else {
    Write-Host '  HidHide:     not detected -- nothing to do'
}

Write-Host ''
if ($DryRun) { Say 'dry run complete; nothing was changed.' }
else { Good 'ds5bridge is uninstalled.' }
