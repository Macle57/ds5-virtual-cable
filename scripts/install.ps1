<#
.SYNOPSIS
    One-paste installer for ds5bridge and the two drivers it stands on.

        irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/install.ps1 | iex

    With options (irm|iex cannot pass parameters, so use this form instead):

        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/install.ps1))) -DryRun

.DESCRIPTION
    What it does, in order, and why in that order:

      1. Checks Windows (10 x64 1903+/11) and elevates itself -- the driver
         installs need admin; nothing else here does.
      2. Creates a System Restore point BEFORE touching drivers. usbip-win2's
         own README asks for one, and it is right to: it installs two kernel
         drivers and restarts every USB 3.0 hub on the machine.
      3. Installs usbip-win2 0.9.7.7 -- EXACTLY 0.9.7.7, pinned by URL and
         SHA-256. Its maintainer warns 0.9.7.8 can corrupt memory, so this
         script will never "helpfully" take the latest. Inno Setup silent
         flags; verified afterwards by running the installed usbip.exe.
      4. Installs HidHide (optional -- only the "hide the Bluetooth pad while
         bridged" feature needs it). winget first, pinned direct download as
         the fallback, and a FAILURE HERE IS A WARNING, NOT A STOP: the bridge
         works fully without it. HidHide's filter driver needs a reboot to
         activate; the summary says so when that applies.
      5. Downloads the latest ds5bridge release from GitHub, verifies it
         against the release's SHA256SUMS, unpacks to %LOCALAPPDATA%\ds5bridge\app
         (the layout the in-app auto-updater owns), makes a Start Menu
         shortcut, optionally registers start-at-login, and launches the tray
         WITHOUT admin rights -- the app never needs them.

    Idempotent by construction: every step checks whether its work is already
    done and says "already installed" instead of doing it again. Re-running
    after a partial failure finishes the remainder; re-running on a healthy
    machine only refreshes the app if a newer release exists.

    -DryRun walks every check and prints every action it WOULD take, changing
    nothing and never asking for elevation. Run that first if you want to see
    the whole plan.

.PARAMETER DryRun
    Print the plan; change nothing. Never elevates, never downloads installers.
.PARAMETER NoHidHide
    Skip HidHide entirely. The hide-while-bridged feature will be unavailable.
.PARAMETER NoRestorePoint
    Skip the System Restore point. Not recommended on a first install.
.PARAMETER Autostart
    Also register the tray to start at login (HKCU Run key -- per-user, no
    admin, visible in Task Manager > Startup apps). Off by default; the tray
    menu has the same switch.
.PARAMETER NoLaunch
    Do not start the tray at the end.
.PARAMETER AppVersion
    Install a specific ds5bridge release tag (e.g. v0.5.0) instead of latest.
.PARAMETER Elevated
    Internal: set by the script when it relaunches itself as administrator.

.NOTES
    Windows PowerShell 5.1 compatible on purpose -- that is what irm|iex runs
    in on a stock machine. Nothing here requires pwsh.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoHidHide,
    [switch]$NoRestorePoint,
    [switch]$Autostart,
    [switch]$NoLaunch,
    [string]$AppVersion = '',
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

# ---------------------------------------------------------------------------
# pinned facts -- the part of this file that a release bump edits
# ---------------------------------------------------------------------------

$AppRepo       = 'Macle57/ds5-virtual-cable'
$RawInstallUrl = "https://raw.githubusercontent.com/$AppRepo/main/scripts/install.ps1"

# usbip-win2: pinned to 0.9.7.7 by its maintainer's own warning about 0.9.7.8.
# The SHA-256 is the one measured on the project dev machine before the
# authenticated Inno Setup installer was run there (docs/install-record.md).
$UsbipVersion  = '0.9.7.7'
$UsbipUrl      = 'https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.7.7/USBip-0.9.7.7-x64.exe'
$UsbipSha256   = '51620FA5F9F8BE5932BC9D786DEEE557CE06D5407A99CAB490DCFAC71F185FEA'
$UsbipExePaths = @("$env:ProgramFiles\USBip\usbip.exe")
$BadUsbip      = '0.9.7.8'   # never install; warn if found

# HidHide: winget is the preferred channel (it keeps its own hash pinning);
# the fallback is the same file winget would fetch, hash from winget's
# manifest for Nefarius.HidHide 1.5.230.
$HidHideWingetId = 'Nefarius.HidHide'
$HidHideUrl      = 'https://github.com/nefarius/HidHide/releases/download/v1.5.230.0/HidHide_1.5.230_x64.exe'
$HidHideSha256   = 'F4BBBCB82E6258641B887C74BC81C4C5F66E4AA811808DFC304347687B7605F6'
# Where HidHideCLI.exe lands -- the same list app/ds5app/hidhide.py probes.
$HidHideCliPaths = @(
    "$env:ProgramFiles\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
    "$env:ProgramFiles\Nefarius Software Solutions e.U\HidHide\x64\HidHideCLI.exe",
    "$env:ProgramFiles\Nefarius Software Solutions\HidHide\HidHideCLI.exe"
)

$InstallRoot   = Join-Path $env:LOCALAPPDATA 'ds5bridge'
$AppDir        = Join-Path $InstallRoot 'app'
$ShortcutPath  = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ds5bridge.lnk'
$RunKey        = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValueName  = 'ds5bridge'   # must match app/ds5app/autostart.py

$script:RebootNeeded  = $false
$script:Warnings      = @()
$script:TempDir       = Join-Path $env:TEMP 'ds5bridge-install'

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

function Say([string]$Text)  { Write-Host "  -  $Text" }
function Good([string]$Text) { Write-Host "[ok]   $Text" -ForegroundColor Green }
function Warn([string]$Text) {
    Write-Host "  !  $Text" -ForegroundColor Yellow
    $script:Warnings += $Text
}
function Fail([string]$Text) { Write-Host " !!! $Text" -ForegroundColor Red; throw $Text }

function Would([string]$Text) {
    # The heart of -DryRun: every mutating action is announced through here
    # first, and in a dry run the announcement is all that happens.
    if ($DryRun) { Write-Host "  ?  would: $Text" -ForegroundColor Cyan; return $false }
    Say $Text
    return $true
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-FileSha256([string]$Path) {
    (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Invoke-Download([string]$Url, [string]$Dest) {
    # BITS is flaky in elevated sessions and Invoke-WebRequest's progress bar
    # makes 5.1 downloads 10x slower; silence it for the transfer only.
    $old = $global:ProgressPreference
    $global:ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
    } finally {
        $global:ProgressPreference = $old
    }
}

function Get-VerifiedInstaller([string]$Url, [string]$Sha256, [string]$Name) {
    New-Item -ItemType Directory -Force -Path $script:TempDir | Out-Null
    $dest = Join-Path $script:TempDir $Name
    if ((Test-Path $dest) -and ((Get-FileSha256 $dest) -eq $Sha256)) {
        Say "$Name already downloaded and verified"
        return $dest
    }
    Say "downloading $Name ..."
    Invoke-Download $Url $dest
    $actual = Get-FileSha256 $dest
    if ($actual -ne $Sha256) {
        Remove-Item $dest -Force -ErrorAction SilentlyContinue
        Fail ("{0}: SHA-256 mismatch (got {1}, expected {2}). Download discarded -- refusing to run an installer that is not the file it should be." -f $Name, $actual, $Sha256)
    }
    Good "$Name verified (SHA-256)"
    return $dest
}

# ---------------------------------------------------------------------------
# 0. environment
# ---------------------------------------------------------------------------

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

function Assert-Elevation {
    if (Test-Admin) { Good 'running as administrator'; return }
    if ($DryRun) {
        Say 'not elevated -- fine for a dry run (a real install will ask once, for the drivers)'
        return
    }
    # irm|iex has no script file to re-run, so fetch our own pinned copy and
    # relaunch that. The user sees exactly one UAC prompt.
    Say 'the driver installs need administrator rights -- relaunching elevated (one UAC prompt) ...'
    New-Item -ItemType Directory -Force -Path $script:TempDir | Out-Null
    $self = Join-Path $script:TempDir 'install.ps1'
    Invoke-Download $RawInstallUrl $self
    # Explicit quotes: Start-Process joins the list with spaces and does not
    # quote for you, and %TEMP% can contain spaces on some accounts.
    $passthru = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                  ('"{0}"' -f $self), '-Elevated')
    if ($NoHidHide)      { $passthru += '-NoHidHide' }
    if ($NoRestorePoint) { $passthru += '-NoRestorePoint' }
    if ($Autostart)      { $passthru += '-Autostart' }
    if ($NoLaunch)       { $passthru += '-NoLaunch' }
    if ($AppVersion)     { $passthru += @('-AppVersion', ('"{0}"' -f $AppVersion)) }
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $passthru -Verb RunAs -Wait -PassThru
    exit $p.ExitCode
}

# ---------------------------------------------------------------------------
# 1. restore point -- usbip-win2's own README asks for one
# ---------------------------------------------------------------------------

function New-InstallRestorePoint {
    if ($NoRestorePoint) { Say 'restore point skipped (-NoRestorePoint)'; return }
    if (-not (Would 'create a System Restore point "Before usbip-win2 (ds5bridge installer)"')) { return }
    try {
        # Windows rate-limits restore points to one per 24h; the documented
        # override makes "the user asked for one right now" actually happen.
        # The previous value is put back whatever happens.
        $srKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore'
        $freqName = 'SystemRestorePointCreationFrequency'
        $prev = (Get-ItemProperty -Path $srKey -Name $freqName -ErrorAction SilentlyContinue).$freqName
        Set-ItemProperty -Path $srKey -Name $freqName -Value 0 -Type DWord
        try {
            Checkpoint-Computer -Description 'Before usbip-win2 (ds5bridge installer)' `
                -RestorePointType 'MODIFY_SETTINGS'
            Good 'restore point created'
        } finally {
            if ($null -ne $prev) { Set-ItemProperty -Path $srKey -Name $freqName -Value $prev -Type DWord }
            else { Remove-ItemProperty -Path $srKey -Name $freqName -ErrorAction SilentlyContinue }
        }
    } catch {
        # System Restore disabled is common (small SSDs, group policy). The
        # install can proceed -- but only if a human says so.
        Warn "could not create a restore point: $_"
        Write-Host ''
        Write-Host '  usbip-win2 installs two kernel drivers, and its own README recommends a'
        Write-Host '  restore point first. System Restore appears to be unavailable here.'
        $answer = Read-Host '  Continue installing the driver WITHOUT one? [y/N]'
        if ($answer -notmatch '^[yY]') { Fail 'stopped at your request -- nothing was installed.' }
    }
}

# ---------------------------------------------------------------------------
# 2. usbip-win2, pinned
# ---------------------------------------------------------------------------

function Get-InstalledUsbip {
    foreach ($p in $UsbipExePaths) {
        if (Test-Path $p) {
            try {
                $v = (& $p --version 2>$null | Select-Object -First 1)
                if ($v) { return @{ Path = $p; Version = "$v".Trim() } }
            } catch { return @{ Path = $p; Version = '?' } }
        }
    }
    return $null
}

function Install-Usbip {
    Write-Host ''
    Write-Host '--- usbip-win2 (the virtual-USB driver) ---'
    $have = Get-InstalledUsbip
    if ($have) {
        if ($have.Version -eq $UsbipVersion) {
            Good "usbip-win2 $UsbipVersion already installed ($($have.Path))"
            return
        }
        if ($have.Version -eq $BadUsbip) {
            Warn ("usbip-win2 $BadUsbip is installed. ITS OWN MAINTAINER WARNS it can corrupt " +
                  "memory and crash Windows. Installing $UsbipVersion over it now.")
        } else {
            Say "usbip-win2 $($have.Version) found; installing the pinned $UsbipVersion over it"
        }
    }
    # The restore point is created only when driver work is actually about to
    # happen -- an idempotent re-run that changes nothing must not mint one.
    New-InstallRestorePoint
    if (-not (Would "install usbip-win2 $UsbipVersion silently (Inno Setup: /VERYSILENT /NORESTART)")) { return }

    $exe = Get-VerifiedInstaller $UsbipUrl $UsbipSha256 "USBip-$UsbipVersion-x64.exe"
    Say 'installing -- your USB 3.0 devices will blink out and come back; that is expected'
    $p = Start-Process -FilePath $exe -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART' `
        -Wait -PassThru
    switch ($p.ExitCode) {
        0     { }
        3010  { $script:RebootNeeded = $true }   # installed; reboot pending
        default { Fail "the usbip-win2 installer exited with code $($p.ExitCode)." }
    }

    # Trust nothing until the installed binary answers for itself.
    $now = Get-InstalledUsbip
    if (-not $now) { Fail 'usbip-win2 reported success but usbip.exe is nowhere to be found.' }
    if ($now.Version -ne $UsbipVersion) {
        Fail "usbip.exe reports '$($now.Version)', expected $UsbipVersion."
    }
    Good "usbip-win2 $UsbipVersion installed and answering ($($now.Path))"
    if ($p.ExitCode -eq 3010) {
        Say 'the installer flagged a pending reboot; on the dev machine it worked before one,'
        Say 'but if the virtual controller misbehaves later, reboot before debugging anything.'
    }
}

function Test-UsbipdConflict {
    # usbipd-win (the Microsoft WSL USB passthrough tool) listens on 3240.
    # People see "another usbip thing is already here" and worry. Nothing
    # conflicts: ds5bridge uses port 3241+ and never touches that service.
    $svc = Get-Service -Name 'usbipd' -ErrorAction SilentlyContinue
    if ($svc) {
        Say ("found the usbipd service (WSL USB passthrough) on this machine -- that is fine. " +
             "It owns port 3240; ds5bridge deliberately uses 3241 and will never touch it.")
    }
}

# ---------------------------------------------------------------------------
# 3. HidHide -- optional, tolerated failure
# ---------------------------------------------------------------------------

function Get-InstalledHidHideCli {
    foreach ($p in $HidHideCliPaths) { if (Test-Path $p) { return $p } }
    return $null
}

function Install-HidHide {
    Write-Host ''
    Write-Host '--- HidHide (optional: hides the real Bluetooth pad while bridged) ---'
    if ($NoHidHide) { Say 'skipped (-NoHidHide)'; return }
    $cli = Get-InstalledHidHideCli
    if ($cli) { Good "HidHide already installed ($cli)"; return }
    if (-not (Would 'install HidHide (winget, falling back to the pinned GitHub download)')) { return }

    $installed = $false
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Say 'installing via winget ...'
        try {
            & winget install --id $HidHideWingetId --exact --silent `
                --accept-package-agreements --accept-source-agreements --disable-interactivity
            if ($LASTEXITCODE -eq 0) { $installed = $true }
            else { Warn "winget exited with $LASTEXITCODE; trying the direct download" }
        } catch { Warn "winget failed ($_); trying the direct download" }
    } else {
        Say 'winget is not available; using the pinned direct download'
    }

    if (-not $installed) {
        try {
            $exe = Get-VerifiedInstaller $HidHideUrl $HidHideSha256 'HidHide_1.5.230_x64.exe'
            # Silent switches per winget's own manifest for this exact package.
            $p = Start-Process -FilePath $exe -ArgumentList '/exenoui', '/qn', '/norestart' -Wait -PassThru
            if ($p.ExitCode -in 0, 1641, 3010) { $installed = $true }
            else { Warn "the HidHide installer exited with $($p.ExitCode)" }
            if ($p.ExitCode -in 1641, 3010) { $script:RebootNeeded = $true }
        } catch {
            Warn "HidHide download/install failed: $_"
        }
    }

    if ($installed -and (Get-InstalledHidHideCli)) {
        Good 'HidHide installed'
        $script:RebootNeeded = $true   # its filter driver activates on the next boot
        Say 'HidHide''s filter driver activates after a REBOOT; hide-while-bridged works from then on.'
    } elseif ($installed) {
        # Installer said yes but the CLI is not where any known version puts it.
        Warn ('HidHide reported success but HidHideCLI.exe was not found in a known location. ' +
              'The bridge works without it; set "hidhide_cli" in the ds5bridge config if you ' +
              'installed it somewhere unusual.')
    } else {
        Warn ('HidHide could not be installed. EVERYTHING ELSE STILL WORKS -- HidHide only ' +
              'powers the optional "hide the Bluetooth pad while bridged" feature. Install it ' +
              'later from https://github.com/nefarius/HidHide/releases if you want that.')
    }
}

# ---------------------------------------------------------------------------
# 4. the app itself
# ---------------------------------------------------------------------------

function Get-AppRelease {
    $api = if ($AppVersion) {
        "https://api.github.com/repos/$AppRepo/releases/tags/$AppVersion"
    } else {
        "https://api.github.com/repos/$AppRepo/releases/latest"
    }
    try {
        $rel = Invoke-RestMethod -Uri $api -UseBasicParsing `
            -Headers @{ 'User-Agent' = 'ds5bridge-installer' }
    } catch {
        # Plain throw, not Fail: the caller decides how to present this (a dry
        # run continues past it; a real run stops), and Fail printing here
        # would say the same thing twice.
        throw ("could not query GitHub for the ds5bridge release ($api): $_ " +
               'If the repository has no releases yet, there is nothing to install.')
    }
    $zip  = @($rel.assets | Where-Object { $_.name -match '^ds5bridge-[0-9].*-win-x64\.zip$' }) | Select-Object -First 1
    $sums = @($rel.assets | Where-Object { $_.name -eq 'SHA256SUMS' }) | Select-Object -First 1
    if (-not $zip) { throw "release $($rel.tag_name) has no ds5bridge-*-win-x64.zip asset." }
    return @{ Tag = $rel.tag_name; Zip = $zip; Sums = $sums }
}

function Get-InstalledAppVersion {
    $exe = Join-Path $AppDir 'ds5bridge.exe'
    if (-not (Test-Path $exe)) { return $null }
    try {
        $line = (& $exe --version 2>$null | Select-Object -First 1)
        if ("$line" -match '(\d+(\.\d+)+)') { return $Matches[1] }
    } catch { }
    return '?'
}

function Install-App {
    Write-Host ''
    Write-Host '--- ds5bridge (the app) ---'
    try {
        $rel = Get-AppRelease
    } catch {
        if ($DryRun) {
            # A dry run against a repo with no releases yet should still show
            # the rest of the plan rather than dying at the API call.
            Warn "$_"
            $null = Would "install the latest ds5bridge release to $AppDir"
            return
        }
        Fail "$_"
    }
    $wanted = ($rel.Tag -replace '^[vV]\.?', '')
    $have = Get-InstalledAppVersion
    if ($have -eq $wanted) {
        Good "ds5bridge $wanted already installed ($AppDir)"
        return
    }
    if ($have) { Say "ds5bridge $have installed; release $wanted available" }
    if (-not (Would "install ds5bridge $wanted to $AppDir")) { return }

    # A running tray holds file locks on $AppDir. Ask it to go away politely
    # (taskkill without /F posts WM_CLOSE, which the tray's teardown handles
    # -- it detaches the virtual pad and unhides everything), then insist.
    $procs = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
    if ($procs.Count -gt 0) {
        Say 'ds5bridge is running -- asking it to close so the files can be replaced ...'
        foreach ($proc in $procs) { & taskkill /PID $proc.Id 2>$null | Out-Null }
        foreach ($proc in $procs) { try { $proc.WaitForExit(15000) | Out-Null } catch { } }
        $procs = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
        if ($procs.Count -gt 0) {
            Fail ('ds5bridge is still running. Quit it from the tray icon (right-click > Quit) ' +
                  'and run this installer again.')
        }
    }

    New-Item -ItemType Directory -Force -Path $script:TempDir | Out-Null
    $zipPath = Join-Path $script:TempDir $rel.Zip.name
    Say "downloading $($rel.Zip.name) ($([math]::Round($rel.Zip.size / 1MB)) MB) ..."
    Invoke-Download $rel.Zip.browser_download_url $zipPath

    if ($rel.Sums) {
        $sumsPath = Join-Path $script:TempDir 'SHA256SUMS'
        Invoke-Download $rel.Sums.browser_download_url $sumsPath
        $expected = $null
        foreach ($line in Get-Content $sumsPath) {
            if ($line -match ('^\s*([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($rel.Zip.name) + '\s*$')) {
                $expected = $Matches[1].ToUpperInvariant(); break
            }
        }
        if (-not $expected) { Fail "the release's SHA256SUMS has no entry for $($rel.Zip.name)." }
        $actual = Get-FileSha256 $zipPath
        if ($actual -ne $expected) {
            Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
            Fail 'the app download failed its checksum -- discarded. Try again.'
        }
        Good 'download verified (SHA-256)'
    } else {
        Warn 'this release publishes no SHA256SUMS; installing on a size check only'
        if ((Get-Item $zipPath).Length -ne $rel.Zip.size) { Fail 'the app download was truncated.' }
    }

    # Unpack to a stage, then swap -- the same discipline the auto-updater
    # uses, so a failed unpack can never leave a half-replaced install.
    $stage = Join-Path $script:TempDir 'stage'
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Expand-Archive -Path $zipPath -DestinationPath $stage
    $inner = @('ds5bridge', '.') | ForEach-Object { Join-Path $stage $_ } |
        Where-Object { Test-Path (Join-Path $_ 'ds5bridge-tray.exe') } | Select-Object -First 1
    if (-not $inner) { Fail 'the release zip does not contain a ds5bridge build.' }

    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    if (Test-Path $AppDir) {
        $old = Join-Path $InstallRoot ('app.old-' + [IO.Path]::GetRandomFileName().Substring(0, 8))
        Move-Item -LiteralPath $AppDir -Destination $old
        Move-Item -LiteralPath $inner -Destination $AppDir
        Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Move-Item -LiteralPath $inner -Destination $AppDir
    }
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

    $now = Get-InstalledAppVersion
    if (-not $now) { Fail "the install landed but $AppDir\ds5bridge.exe does not run." }
    Good "ds5bridge $now installed ($AppDir)"
}

function Install-Shortcut {
    if (-not (Would "create the Start Menu shortcut ($ShortcutPath)")) { return }
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($ShortcutPath)
    $lnk.TargetPath = Join-Path $AppDir 'ds5bridge-tray.exe'
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description = 'ds5bridge -- a Bluetooth DualSense, presented to Windows as a wired one'
    $lnk.Save()
    Good 'Start Menu shortcut created'
}

function Install-Autostart {
    if (-not $Autostart) { return }
    # The exact value autostart.py writes: quoted path, nothing else. Keeping
    # the two writers identical means the tray's checkbox reads this back.
    $value = '"{0}"' -f (Join-Path $AppDir 'ds5bridge-tray.exe')
    if (-not (Would "register start-at-login (HKCU Run value '$RunValueName')")) { return }
    New-Item -Path $RunKey -Force | Out-Null
    Set-ItemProperty -Path $RunKey -Name $RunValueName -Value $value
    Good 'start-at-login registered (Task Manager > Startup apps shows it)'
}

function Start-Tray {
    if ($NoLaunch) { return }
    if (-not (Would 'launch the ds5bridge tray')) { return }
    $tray = Join-Path $AppDir 'ds5bridge-tray.exe'
    if (Test-Admin) {
        # This process is elevated; the tray must NOT be. explorer.exe launches
        # the target with the desktop's ordinary token -- the standard
        # de-elevation trick, and the reason the tray's Run-key entry, its
        # config and its instance lock all end up under the right user.
        Start-Process -FilePath 'explorer.exe' -ArgumentList $tray
    } else {
        Start-Process -FilePath $tray -WorkingDirectory $AppDir
    }
    Good 'tray launched -- look for the gamepad icon next to the clock'
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '  ds5bridge installer -- a Bluetooth DualSense, presented to Windows as wired'
if ($DryRun) { Write-Host '  DRY RUN: showing the plan; changing nothing.' -ForegroundColor Cyan }
Write-Host ''

Assert-Windows
Assert-Elevation
Install-Usbip
Test-UsbipdConflict
Install-HidHide
Install-App
Install-Shortcut
Install-Autostart
Start-Tray

Write-Host ''
Write-Host '--- summary ---'
if ($DryRun) {
    Say 'dry run complete; nothing was changed. Re-run without -DryRun to install.'
} else {
    Good 'install complete'
    Say 'pair your DualSense over Bluetooth (hold CREATE + PS until the light bar'
    Say 'flashes, then Windows Settings > Bluetooth & devices), and the tray does the rest.'
}
if ($script:Warnings.Count -gt 0) {
    Write-Host ''
    foreach ($w in $script:Warnings) { Write-Host "  !  $w" -ForegroundColor Yellow }
}
if ($script:RebootNeeded) {
    Write-Host ''
    Warn 'a REBOOT is recommended before first use (a driver flagged one).'
}
if ($Elevated -and -not $DryRun) {
    # This window was opened by the elevation relaunch and would vanish with
    # everything the user was supposed to read.
    Write-Host ''
    Read-Host 'Press Enter to close this window'
}
