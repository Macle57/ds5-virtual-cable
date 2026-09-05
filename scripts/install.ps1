<#
.SYNOPSIS
    One-paste install of ds5bridge: fetches the release installer, checks it,
    runs it.

        irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/install.ps1 | iex

    With options (irm|iex cannot pass parameters, so use this form instead):

        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/install.ps1))) -Silent -NoHidHide

.DESCRIPTION
    Since 0.5.0 every release carries exactly one installer,
    ds5bridge-setup-<version>-bundled.exe, and THAT is the install: it carries
    usbip-win2 0.9.7.7 and HidHide 1.5.230 inside it, installs the app,
    verifies everything and shows the result (docs/installer.md). This script
    is the thin bootstrapper around it, for people who would rather paste one
    line than find a download button:

      1. Checks Windows (10 x64 1903+/11).
      2. Asks GitHub for the latest release of Macle57/ds5-virtual-cable (or
         the tag you name with -AppVersion) and finds the bundled installer
         and its SHA256SUMS in it.
      3. Downloads the installer to %TEMP%\ds5bridge-install\ and REFUSES to
         run it unless its SHA-256 matches the one the release publishes.
         Nothing here is code-signed, so this check is the whole of the
         download's integrity; a mismatch deletes the file.
      4. Runs it. Interactive by default -- the wizard appears, with one UAC
         prompt of its own (the drivers need administrator rights). With
         -Silent it runs unattended (/SILENT /NORESTART /SUPPRESSMSGBOXES)
         and this script prints the installer's own verification list
         (%LOCALAPPDATA%\ds5bridge\installer\last-install-check.txt) when
         it is done.

    Everything the installer does -- the restore point, the driver installs,
    attaching HidHide's filter to your connected controllers so no reboot is
    needed, the Start Menu shortcut, the start-at-login task, the checks --
    is the installer's, and is described in docs/installer.md. This script
    installs nothing itself and never needs to be run as administrator.

    Re-running it is safe: the installer is idempotent, and a machine that
    already has everything only gets its app refreshed.

.PARAMETER Silent
    Unattended: /SILENT /NORESTART /SUPPRESSMSGBOXES. A refusal (another
    installer running, a usbip-win2 removal waiting for a reboot) is exit
    code 7 with the reason in the log, not a message box.
.PARAMETER NoHidHide
    Untick HidHide (/COMPONENTS=app,usbip). The hide-while-bridged feature
    will be unavailable.
.PARAMETER NoUsbip
    Untick usbip-win2 (you have it already, or are installing it yourself).
.PARAMETER NoRestorePoint
    Untick the System Restore point (/MERGETASKS=!restorepoint). Not
    recommended on a first install.
.PARAMETER Autostart
    Tick "Start ds5bridge at login" (a scheduled task with highest
    privileges, so the elevated tray starts with no prompt; the tray menu
    has the same switch).
.PARAMETER NoLaunch
    After a -Silent install, do not start the tray. (A silent installer
    never starts it on its own; this script does, when it can do so without
    a UAC prompt -- i.e. when you ran it from an administrator PowerShell.)
.PARAMETER AppVersion
    Install a specific release tag (e.g. v0.5.0) instead of the latest.
.PARAMETER Setup
    Run this local ds5bridge-setup-*.exe instead of downloading one (no hash
    check possible then; for testing a build).
.PARAMETER DryRun
    Look the release up and print what would be downloaded and run;
    download nothing, run nothing.

.NOTES
    Windows PowerShell 5.1 compatible on purpose -- that is what irm|iex runs
    in on a stock machine. Nothing here requires pwsh.
#>
[CmdletBinding()]
param(
    [switch]$Silent,
    [switch]$NoHidHide,
    [switch]$NoUsbip,
    [switch]$NoRestorePoint,
    [switch]$Autostart,
    [switch]$NoLaunch,
    [string]$AppVersion = '',
    [string]$Setup = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

# ---------------------------------------------------------------------------
# pinned facts
# ---------------------------------------------------------------------------

$AppRepo     = 'Macle57/ds5-virtual-cable'
# The one release asset, as .github/workflows/release.yml names it and as
# app/ds5app/update.py matches it. Anchored: the download-mode exe and the
# old zip, if either ever reappears on a release, must not match.
$AssetRegex  = '^ds5bridge-setup-[0-9][^/\\]*-bundled\.exe$'
$SumsName    = 'SHA256SUMS'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'ds5bridge'
$CheckFile   = Join-Path $InstallRoot 'installer\last-install-check.txt'
$TempDir     = Join-Path $env:TEMP 'ds5bridge-install'

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

function Say([string]$Text)  { Write-Host "  -  $Text" }
function Good([string]$Text) { Write-Host "[ok]   $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "  !  $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { Write-Host " !!! $Text" -ForegroundColor Red; throw $Text }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Download([string]$Url, [string]$Dest) {
    # Invoke-WebRequest's progress bar makes 5.1 downloads 10x slower;
    # silence it for the transfer only.
    $old = $global:ProgressPreference
    $global:ProgressPreference = 'SilentlyContinue'
    try { Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing }
    finally { $global:ProgressPreference = $old }
}

function Assert-Windows {
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Fail 'This installer is for Windows.' }
    if (-not [Environment]::Is64BitOperatingSystem) { Fail 'ds5bridge needs 64-bit Windows.' }
    $build = [Environment]::OSVersion.Version.Build
    # 18362 = Windows 10 1903, the floor usbip-win2 documents.
    if ($build -lt 18362) {
        Fail "Windows build $build is older than Windows 10 1903 (18362), which usbip-win2 requires."
    }
    Good "Windows build $build, 64-bit"
}

# ---------------------------------------------------------------------------
# the release
# ---------------------------------------------------------------------------

function Get-Release {
    $api = if ($AppVersion) {
        "https://api.github.com/repos/$AppRepo/releases/tags/$AppVersion"
    } else {
        "https://api.github.com/repos/$AppRepo/releases/latest"
    }
    try {
        $rel = Invoke-RestMethod -Uri $api -UseBasicParsing -Headers @{ 'User-Agent' = 'ds5bridge-installer' }
    } catch {
        $status = $null
        try { $status = [int]$_.Exception.Response.StatusCode } catch { }
        if ($status -eq 404) {
            Fail ("GitHub answered 404 for $api -- the repository is private, or has no release yet" +
                  $(if ($AppVersion) { " (or there is no release tagged '$AppVersion')" } else { '' }) +
                  ". Nothing to install; download the setup exe from the releases page by hand once there is one.")
        }
        Fail "could not query GitHub for the ds5bridge release ($api): $_"
    }
    $exe  = @($rel.assets | Where-Object { $_.name -match $AssetRegex }) | Select-Object -First 1
    $sums = @($rel.assets | Where-Object { $_.name -eq $SumsName }) | Select-Object -First 1
    if (-not $exe) { Fail "release $($rel.tag_name) carries no ds5bridge-setup-*-bundled.exe -- not a release this script can install." }
    return @{ Tag = $rel.tag_name; Exe = $exe; Sums = $sums; Page = $rel.html_url }
}

function Get-VerifiedSetup($Rel) {
    New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
    $dest = Join-Path $TempDir $Rel.Exe.name
    $expected = $null
    if ($Rel.Sums) {
        $sumsPath = Join-Path $TempDir $SumsName
        Invoke-Download $Rel.Sums.browser_download_url $sumsPath
        foreach ($line in Get-Content $sumsPath) {
            if ($line -match ('^\s*([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($Rel.Exe.name) + '\s*$')) {
                $expected = $Matches[1].ToUpperInvariant(); break
            }
        }
        if (-not $expected) { Fail "the release's $SumsName has no entry for $($Rel.Exe.name) -- refusing to install from a release whose checksums do not cover its installer." }
    } else {
        Warn "release $($Rel.Tag) publishes no $SumsName; the download can only be checked by size"
    }
    if ((Test-Path $dest) -and $expected -and ((Get-FileHash $dest -Algorithm SHA256).Hash.ToUpperInvariant() -eq $expected)) {
        Say "$($Rel.Exe.name) already downloaded and verified"
        return $dest
    }
    Say ("downloading {0} ({1} MB) ..." -f $Rel.Exe.name, [math]::Round($Rel.Exe.size / 1MB))
    Invoke-Download $Rel.Exe.browser_download_url $dest
    if ($expected) {
        $actual = (Get-FileHash $dest -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actual -ne $expected) {
            Remove-Item $dest -Force -ErrorAction SilentlyContinue
            Fail ("{0}: SHA-256 mismatch (got {1}, expected {2}). Download discarded -- refusing to run an installer that is not the file the release says it is." -f $Rel.Exe.name, $actual, $expected)
        }
        Good 'installer verified (SHA-256 matches the release''s SHA256SUMS)'
    } elseif ((Get-Item $dest).Length -ne $Rel.Exe.size) {
        Remove-Item $dest -Force -ErrorAction SilentlyContinue
        Fail 'the download was truncated -- discarded. Try again.'
    }
    return $dest
}

# ---------------------------------------------------------------------------
# the installer's command line
# ---------------------------------------------------------------------------

function Get-SetupArgs([string]$LogPath) {
    # Inno Setup switches (docs/installer.md, "Silent install"). Components
    # and tasks are only passed when a switch asks for a change, so a plain
    # run keeps the installer's own defaults (everything ticked on a first
    # install, the previous selection on a re-run).
    $sw = @()
    if ($Silent) { $sw += @('/SILENT', '/NORESTART', '/SUPPRESSMSGBOXES') }
    $components = @('app')
    if (-not $NoUsbip)   { $components += 'usbip' }
    if (-not $NoHidHide) { $components += 'hidhide' }
    if ($NoUsbip -or $NoHidHide) { $sw += ('/COMPONENTS=' + ($components -join ',')) }
    $tasks = @()
    if ($NoRestorePoint) { $tasks += '!restorepoint' }
    if ($Autostart)      { $tasks += 'autostart' }
    if ($tasks.Count -gt 0) { $sw += ('/MERGETASKS=' + ($tasks -join ',')) }
    $sw += ('/LOG="{0}"' -f $LogPath)
    return $sw
}

function Describe-ExitCode([int]$Code) {
    switch ($Code) {
        0 { 'finished' }
        1 { 'could not initialise (another instance of Setup running, or a corrupt download)' }
        2 { 'cancelled by you' }
        3 { 'a fatal error before installing' }
        4 { 'a fatal error during installing' }
        5 { 'cancelled at the administrator (UAC) prompt, or refused to elevate' }
        6 { 'aborted' }
        7 { 'refused to install: another installer is running, or a usbip-win2 removal is waiting for its reboot (the log says which)' }
        8 { 'needs a restart before it can run' }
        default { "exited with code $Code" }
    }
}

function Show-CheckFile {
    if (-not (Test-Path $CheckFile)) { return }
    Write-Host ''
    Write-Host '--- the installer''s own checks ---'
    Get-Content $CheckFile | ForEach-Object { Write-Host "       $_" }
    $reboot = @(Get-Content $CheckFile | Where-Object { $_ -like '`[reboot`]*' })
    $fails  = @(Get-Content $CheckFile | Where-Object { $_ -like '`[FAIL`]*' })
    Write-Host ''
    if ($fails.Count -gt 0) { Warn "$($fails.Count) check(s) FAILED -- see above, and run 'ds5bridge doctor'" }
    if ($reboot.Count -gt 0) { Warn 'a REBOOT is recommended before relying on hide-while-bridged (see the [reboot] line). Nothing restarts on its own.' }
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '  ds5bridge installer -- a Bluetooth DualSense, presented to Windows as wired'
if ($DryRun) { Write-Host '  DRY RUN: showing the plan; changing nothing.' -ForegroundColor Cyan }
Write-Host ''

Assert-Windows

$setupExe = $null
if ($Setup) {
    if (-not (Test-Path $Setup)) { Fail "-Setup: $Setup does not exist" }
    $setupExe = (Resolve-Path $Setup).Path
    Warn "using the local $setupExe (no release hash to check it against)"
} else {
    Write-Host ''
    Write-Host "--- the release ($AppRepo) ---"
    $rel = Get-Release
    Good ("release {0}: {1} ({2} MB){3}" -f $rel.Tag, $rel.Exe.name, [math]::Round($rel.Exe.size / 1MB),
          $(if ($rel.Sums) { ', with SHA256SUMS' } else { ', NO SHA256SUMS' }))
    if ($DryRun) {
        Say "would download $($rel.Exe.browser_download_url) to $TempDir and verify it against $SumsName"
    } else {
        $setupExe = Get-VerifiedSetup $rel
    }
}

$logPath = Join-Path $TempDir ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
$setupArgs = Get-SetupArgs $logPath
Write-Host ''
Write-Host '--- the installer ---'
if ($DryRun) {
    $name = if ($setupExe) { $setupExe } else { $rel.Exe.name }
    Say ("would run: {0} {1}" -f $name, ($setupArgs -join ' '))
    Say $(if ($Silent) { 'unattended; its verification list would be printed here afterwards' }
          else { 'interactively: the wizard appears, one UAC prompt for the drivers, a checkbox per component' })
    Write-Host ''
    Say 'dry run complete; nothing was changed. Re-run without -DryRun to install.'
    exit 0
}

New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
Say ("running {0} {1}" -f (Split-Path $setupExe -Leaf), ($setupArgs -join ' '))
if (-not $Silent) {
    Say 'the setup wizard opens now (Windows may first show "Windows protected your PC" -- More info > Run anyway; nothing is code-signed yet)'
    Say 'one UAC prompt follows: the drivers need administrator rights'
} else {
    Say 'unattended install; one UAC prompt (the drivers need administrator rights) unless this PowerShell is already elevated'
}
# The exe elevates itself (PrivilegesRequired=admin), so no -Verb RunAs here:
# ShellExecute shows the one UAC prompt and this script keeps its own token.
$p = Start-Process -FilePath $setupExe -ArgumentList $setupArgs -Wait -PassThru
$code = $p.ExitCode
Write-Host ''
if ($code -eq 0) {
    Good "the installer $(Describe-ExitCode $code) (log: $logPath)"
    Show-CheckFile
    if ($Silent -and -not $NoLaunch) {
        $tray = Join-Path $InstallRoot 'app\ds5bridge-tray.exe'
        if (Test-Path $tray) {
            if (Test-Admin) {
                # Elevated already: the elevated tray starts with no prompt.
                Start-Process -FilePath $tray -WorkingDirectory (Split-Path $tray -Parent)
                Good 'tray started -- look for the gamepad icon next to the clock'
            } else {
                Say 'start ds5bridge from the Start Menu (it runs as administrator, so that is one UAC prompt); with "Start at login" on, it starts by itself from the next logon'
            }
        }
    }
    Write-Host ''
    Say 'pair your DualSense over Bluetooth (hold CREATE + PS until the light bar flashes, then'
    Say 'Windows Settings > Bluetooth & devices), and the tray does the rest.'
    exit 0
}
Warn ("the installer {0}" -f (Describe-ExitCode $code))
Say "its log: $logPath"
Show-CheckFile
exit $code
