<#
.SYNOPSIS
    Removes ds5bridge -- the app, its shortcut, its start-at-login entry --
    AND the two drivers it installed, unless you ask to keep them.

        irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/uninstall.ps1 | iex

    With options (irm|iex cannot pass parameters, so use this form instead):

        & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/uninstall.ps1))) -KeepHidHide

.DESCRIPTION
    What is removed, in order:

        1. the running bridge is stopped and its debts repaid: the virtual
           pad is detached (`ds5bridge cleanup`, `usbip attach -X`,
           `usbip detach -a`) and any pad ds5bridge hid is made visible
           again (`ds5bridge unhide`, then HidHide's own CLI as a belt to
           those braces). Order matters: these tools live in the exe that is
           about to be deleted.
        2. %LOCALAPPDATA%\ds5bridge\        the app, staged updates, and the
                                            exe installer's uninstaller if
                                            ds5bridge-setup.exe was used
           Start Menu \ ds5bridge.lnk       the shortcut
           HKCU Run \ "ds5bridge"           start-at-login (if enabled)
           Settings > Apps entry            (if ds5bridge-setup.exe was used)
        3. HidHide                          unless kept (see below). Its whole
                                            hide list is cleared first so no
                                            device stays hidden; the MSI is
                                            removed silently; it wants a
                                            reboot to unload the filter.
        4. usbip-win2                       unless kept. Only with nothing
                                            attached; its own uninstaller,
                                            silently, given 180 s. If it has
                                            not returned by then it is left
                                            running and a reboot finishes
                                            the removal. USB 3.0 hubs
                                            restart during it.
        5. %APPDATA%\ds5bridge\             settings -- ONLY with -PurgeSettings

    Which drivers go: ds5bridge-setup.exe records whether it installed a
    driver or found it already there (HKLM\SOFTWARE\ds5bridge\Setup). One it
    installed is removed by default; one it adopted is kept by default; with
    no record (installed by install.ps1) both are removed. -KeepUsbip /
    -KeepHidHide and -RemoveUsbip / -RemoveHidHide override either way (keep
    wins). Keep a driver if something else on this machine uses it:
    DS4Windows and friends use HidHide, and usbip-win2 may be your own tool.
    (`usbipd`, the WSL USB passthrough service on port 3240, is a DIFFERENT
    product and is never touched by anything here.)

    Two guards, both learned the hard way: nothing driver-related starts
    while another installer is running (msiexec, an Inno setup/uninstall,
    devnode, nefconw, pnputil -- two at once wedged Plug and Play), and an
    orphaned "HidHide" class-filter entry without its service is removed
    whenever it is seen (it leaves every keyboard, mouse and pad dead after
    a reboot).

    Every removal is verified afterwards and the result printed as a
    [ok]/[FAIL] list. -DryRun prints the plan and changes nothing.

.PARAMETER DryRun
    Print what would be removed; remove nothing. Never elevates.
.PARAMETER KeepUsbip
    Leave usbip-win2 installed.
.PARAMETER KeepHidHide
    Leave HidHide installed. ds5bridge's own whitelist and hide entries are
    still cleared.
.PARAMETER RemoveUsbip
    Remove usbip-win2 even if ds5bridge-setup.exe found it already installed.
.PARAMETER RemoveHidHide
    Remove HidHide even if ds5bridge-setup.exe found it already installed.
.PARAMETER PurgeSettings
    Also delete %APPDATA%\ds5bridge (your per-controller settings and labels).
    Off by default so a reinstall finds everything the way you left it.
.PARAMETER Elevated
    Internal: set by the script when it relaunches itself as administrator.

.NOTES
    Windows PowerShell 5.1 compatible on purpose. The same steps, driven from
    a wizard, are what ds5bridge-setup.exe's uninstaller does
    (app/packaging/ds5bridge.iss + setup-helper.ps1); keep the two in step.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$KeepUsbip,
    [switch]$KeepHidHide,
    [switch]$RemoveUsbip,
    [switch]$RemoveHidHide,
    [switch]$PurgeSettings,
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'

$RawUrl        = 'https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main/scripts/uninstall.ps1'
$InstallRoot   = Join-Path $env:LOCALAPPDATA 'ds5bridge'
$AppDir        = Join-Path $InstallRoot 'app'
$ConfigDir     = Join-Path $env:APPDATA 'ds5bridge'
$JournalDir    = Join-Path $ConfigDir 'hidden'
$ShortcutPath  = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\ds5bridge.lnk'
$RunKey        = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValueName  = 'ds5bridge'
# The AppId in app/packaging/ds5bridge.iss, as Inno registers it.
$InnoUninstKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{7B6E2D6A-3C39-4E0B-9C7E-2F8B1C2D5A61}_is1'
$UsbipServices = @('usbip2_filter', 'usbip2_ude')
$UsbipDevnode  = 'USBip 3.X Emulated Host Controller'
$HidHideCliPaths = @(
    "$env:ProgramFiles\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
    "$env:ProgramFiles\Nefarius Software Solutions e.U\HidHide\x64\HidHideCLI.exe",
    "$env:ProgramFiles\Nefarius Software Solutions\HidHide\HidHideCLI.exe"
)
$BtPadPrefix   = 'HID\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6'
# The virtual (usbip-attached) pad as Windows enumerates it; one present
# means something is still attached and usbip-win2 must not be removed.
$VirtualPadPrefix = 'USB\VID_054C&PID_0CE6\'
# ds5bridge-setup.exe's record of what it installed vs. adopted.
$SetupKey      = 'HKLM:\SOFTWARE\ds5bridge\Setup'
# The classes HidHide registers itself on as an upper filter.
$ClassKeyRoot  = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class'
$ServicesKeyRoot = 'HKLM:\SYSTEM\CurrentControlSet\Services'
$HidHideFilterClasses = [ordered]@{
    'HIDClass'      = '{745a17a0-74d3-11d0-b6fe-00a0c90f57da}'
    'XnaComposite'  = '{d61ca365-5af4-4486-998b-9db4734c6ca3}'
    'XboxComposite' = '{05f5cfe2-4733-4950-a6bb-07aad01a3a84}'
}
$UsbipUninstallTimeoutSec = 180
$UsbipPnpTimeoutSec       = 120
$UsbipFinishDir  = Join-Path $env:ProgramData 'ds5bridge'
$UsbipFinishTask = 'ds5bridge finish usbip-win2 removal'
$RawBase         = 'https://raw.githubusercontent.com/Macle57/ds5-virtual-cable/main'

$script:Fails = 0
$script:RebootNeeded = $false

function Say([string]$Text)  { Write-Host "  -  $Text" }
function Good([string]$Text) { Write-Host "[ok]   $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "  !  $Text" -ForegroundColor Yellow }
function Bad([string]$Text)  { Write-Host "[FAIL] $Text" -ForegroundColor Red; $script:Fails++ }
function Would([string]$Text) {
    if ($DryRun) { Write-Host "  ?  would: $Text" -ForegroundColor Cyan; return $false }
    Say $Text
    return $true
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Run([string]$Exe, [string[]]$Arguments, [int]$TimeoutSec = 120, [switch]$NoKill) {
    # -NoKill: on timeout leave the process running (a vendor uninstaller
    # mid-way through a device removal must not be shot) and return -2.
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    try { $p = [Diagnostics.Process]::Start($psi) } catch { return @{ Code = -1; Out = "could not start: $_" } }
    $stdout = $p.StandardOutput.ReadToEndAsync()
    $stderr = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        if (-not $NoKill) { try { $p.Kill() } catch { } }
        return @{ Code = -2; Out = "timed out after ${TimeoutSec}s"; Pid = $p.Id }
    }
    $p.WaitForExit()
    return @{ Code = $p.ExitCode; Out = ($stdout.Result + $stderr.Result) }
}

# ---------------------------------------------------------------------------
# looking at the machine
# ---------------------------------------------------------------------------

function Get-UninstallEntries([scriptblock]$Filter) {
    foreach ($root in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                      'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall') {
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
            $e = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($e -and $e.DisplayName -and (& $Filter $e)) { $e }
        }
    }
}

function Get-UsbipExe {
    # Narrow on purpose: usbipd-win also matches a naive "usbip" search.
    foreach ($e in Get-UninstallEntries { param($x) $x.DisplayName -like 'USBip version*' }) {
        if ($e.InstallLocation -and (Test-Path (Join-Path $e.InstallLocation 'usbip.exe'))) {
            return (Join-Path $e.InstallLocation 'usbip.exe')
        }
    }
    if (Test-Path "$env:ProgramFiles\USBip\usbip.exe") { return "$env:ProgramFiles\USBip\usbip.exe" }
    return $null
}

function Get-UsbipUninstaller {
    foreach ($e in Get-UninstallEntries { param($x) $x.DisplayName -like 'USBip version*' }) {
        if ($e.UninstallString) { $p = $e.UninstallString.Trim('"'); if (Test-Path $p) { return $p } }
    }
    if (Test-Path "$env:ProgramFiles\USBip\unins000.exe") { return "$env:ProgramFiles\USBip\unins000.exe" }
    return $null
}

function Get-HidHideCli {
    foreach ($p in $HidHideCliPaths) { if (Test-Path $p) { return $p } }
    return $null
}

function Get-HidHideMsiCode {
    foreach ($e in Get-UninstallEntries { param($x) $x.DisplayName -eq 'HidHide' }) {
        if ($e.UninstallString -match '\{[0-9A-Fa-f-]{36}\}') { return $Matches[0] }
    }
    return $null
}

function Get-ServiceState([string]$Name) {
    # sc.exe, not Get-Service: Windows PowerShell 5.1's Get-Service does not
    # list kernel drivers, and all three of these are drivers.
    $out = & sc.exe query $Name 2>&1 | Out-String
    if ($LASTEXITCODE -eq 1060) { return 'absent' }
    $flag = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$Name" -Name DeleteFlag -ErrorAction SilentlyContinue).DeleteFlag
    if ($flag -eq 1) { return 'marked for deletion (gone after a reboot)' }
    if ($out -match 'STATE\s*:\s*\d+\s+(\w+)') { return $Matches[1] }
    return 'unknown'
}

function Test-UsbipDevnode {
    try { return [bool]@(Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object { $_.FriendlyName -eq $UsbipDevnode }).Count }
    catch { return ((& pnputil.exe /enum-devices /connected 2>&1 | Out-String) -match [regex]::Escape($UsbipDevnode)) }
}

function Parse-HidHideList([string]$Text, [string]$Flag) {
    $items = @()
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match ('^\s*' + [regex]::Escape($Flag) + '\s+"?(.+?)"?\s*$')) { $items += $Matches[1] }
    }
    return $items
}

function Get-VirtualPads {
    try { @(Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object { $_.InstanceId -like "$VirtualPadPrefix*" }) } catch { @() }
}

function Get-RecordedOrigin([string]$Name) {
    # 1 = ds5bridge-setup.exe installed it, 0 = it found it already there,
    # -1 = no record (install.ps1, or an older setup exe).
    $v = (Get-ItemProperty $SetupKey -Name "${Name}InstalledByUs" -ErrorAction SilentlyContinue)."${Name}InstalledByUs"
    if ($null -eq $v) { return -1 }
    return [int]$v
}

function Get-BusyInstallers {
    # Processes that mean "somebody is changing driver or installer state".
    # This script's own ancestors are excluded (it may itself have been
    # launched from an installer's post-uninstall step).
    $names = '^(msiexec|unins\d+|_iu[0-9a-z]+|_unins.*|devnode|nefconw|pnputil|usbip-[0-9.]+-x64|hidhide_[0-9._]+x64)$'
    $busy = @()
    try { $procs = @(Get-CimInstance Win32_Process -ErrorAction Stop) } catch { $procs = @() }
    $byId = @{}
    foreach ($p in $procs) { $byId[[int]$p.ProcessId] = $p }
    $ancestors = @{}
    $id = $PID
    for ($i = 0; $i -lt 32 -and $byId.ContainsKey($id); $i++) {
        $ancestors[$id] = $true
        $id = [int]$byId[$id].ParentProcessId
        if ($id -eq 0 -or $ancestors.ContainsKey($id)) { break }
    }
    foreach ($p in $procs) {
        if ($ancestors.ContainsKey([int]$p.ProcessId)) { continue }
        $base = [IO.Path]::GetFileNameWithoutExtension($p.Name)
        $isSetupTmp = ($p.Name -match '\.tmp$') -and ($base -match 'setup')
        if (-not ($base -match $names) -and -not $isSetupTmp) { continue }
        # msiexec counts only as a client (/i /x /f /p /a /j /package
        # /uninstall /update) or a custom-action server (-Embedding); the
        # service host "msiexec /V" idles for minutes after every boot and
        # install, and a real transaction shows up as the InProgress key.
        if (($base -ieq 'msiexec') -and -not (
                ($p.CommandLine -match '(^|\s)[/-](i|x|f[a-z]*|p|a|j[um]?|package|uninstall|update)(\s|$|\{)') -or
                ($p.CommandLine -match '(^|\s)-Embedding(\s|$)'))) { continue }
        $busy += ('{0} (pid {1})' -f $p.Name, $p.ProcessId)
    }
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\InProgress') {
        $busy += 'a Windows Installer transaction (HKLM\...\Installer\InProgress)'
    }
    return $busy
}

function Assert-NotBusy([string]$What) {
    $busy = @(Get-BusyInstallers)
    if ($busy.Count -eq 0) { return $true }
    Bad "$What not started: another installer is running ($($busy -join ', ')). Two driver installers at once can hang Plug and Play; wait for it to finish and run this again"
    return $false
}

function Repair-OrphanHidHideFilter {
    # An interrupted HidHide MSI can remove the service but leave "HidHide"
    # in the HID classes' UpperFilters; from the next reboot every keyboard,
    # mouse and pad fails with Code 32. Remove the entry when its service
    # does not exist, and restart whatever already failed.
    if (Test-Path (Join-Path $ServicesKeyRoot 'HidHide')) { return }
    $fixed = @()
    foreach ($name in $HidHideFilterClasses.Keys) {
        $key = Join-Path $ClassKeyRoot $HidHideFilterClasses[$name]
        $uf = @((Get-ItemProperty $key -Name UpperFilters -ErrorAction SilentlyContinue).UpperFilters)
        if (-not ($uf | Where-Object { $_ -ieq 'HidHide' })) { continue }
        if (-not (Would "remove the orphaned HidHide filter entry from the $name class (its service is gone)")) { continue }
        $rest = @($uf | Where-Object { $_ -and ($_ -ine 'HidHide') })
        try {
            if ($rest.Count -gt 0) { Set-ItemProperty -Path $key -Name UpperFilters -Value ([string[]]$rest) -Type MultiString -ErrorAction Stop }
            else { Remove-ItemProperty -Path $key -Name UpperFilters -ErrorAction Stop }
            $fixed += $name
        } catch { Bad "orphaned HidHide filter entry on $name could not be removed ($($_.Exception.Message.Trim())); input devices will fail after the next reboot until it is" }
    }
    if ($fixed.Count -eq 0) { return }
    Warn "orphaned HidHide filter entry removed from $($fixed -join ', ') (HidHide's service was gone but its filter was still registered; every device in those classes would have failed with Code 32)"
    $broken = @()
    try { $broken = @(Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object { $_.Problem -eq 'CM_PROB_DISABLED_SERVICE' }) } catch { }
    $n = 0
    foreach ($d in $broken) {
        $r = Run "$env:SystemRoot\System32\pnputil.exe" @('/restart-device', $d.InstanceId) 60
        if ($r.Code -eq 0) { $n++ } else { Warn "could not restart $($d.InstanceId) (pnputil exit $($r.Code)); a reboot will" }
    }
    if ($broken.Count -gt 0) { Good "restarted $n of $($broken.Count) device(s) that had failed with Code 32" }
}

# ---------------------------------------------------------------------------
# 0. elevation -- the drivers and the Settings > Apps entry need it
# ---------------------------------------------------------------------------

function Assert-Elevation {
    if (Test-Admin) { Good 'running as administrator'; return }
    if ($DryRun) { Say 'not elevated -- fine for a dry run (a real run will ask once, for the drivers)'; return }
    Say 'removing the drivers needs administrator rights -- relaunching elevated (one UAC prompt) ...'
    $self = $PSCommandPath
    if (-not $self) {
        # irm|iex has no file to re-run; fetch our own pinned copy.
        $tmp = Join-Path $env:TEMP 'ds5bridge-install'
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $self = Join-Path $tmp 'uninstall.ps1'
        $old = $global:ProgressPreference; $global:ProgressPreference = 'SilentlyContinue'
        try { Invoke-WebRequest -Uri $RawUrl -OutFile $self -UseBasicParsing } finally { $global:ProgressPreference = $old }
    }
    $passthru = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $self), '-Elevated')
    if ($KeepUsbip)     { $passthru += '-KeepUsbip' }
    if ($KeepHidHide)   { $passthru += '-KeepHidHide' }
    if ($RemoveUsbip)   { $passthru += '-RemoveUsbip' }
    if ($RemoveHidHide) { $passthru += '-RemoveHidHide' }
    if ($PurgeSettings) { $passthru += '-PurgeSettings' }
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $passthru -Verb RunAs -Wait -PassThru
    exit $p.ExitCode
}

# ---------------------------------------------------------------------------
# 1. stop the bridge and repay its debts
# ---------------------------------------------------------------------------

function Stop-Bridge {
    Write-Host ''
    Write-Host '--- stopping the bridge ---'
    $exe = Join-Path $AppDir 'ds5bridge.exe'
    $procs = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
    if ($procs.Count -gt 0) {
        if (Would 'quit the running ds5bridge (it detaches the virtual pad on the way out)') {
            foreach ($p in $procs) { & taskkill.exe /PID $p.Id 2>&1 | Out-Null }
            foreach ($p in $procs) { try { $p.WaitForExit(15000) | Out-Null } catch { } }
            $left = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
            if ($left.Count -gt 0) {
                Warn 'ds5bridge did not exit; forcing it (the cleanup below repairs anything left over)'
                foreach ($p in $left) { & taskkill.exe /F /PID $p.Id 2>&1 | Out-Null }
                Start-Sleep -Seconds 2
            }
            Good 'ds5bridge stopped'
        }
    } else { Say 'ds5bridge is not running' }

    if (Test-Path $exe) {
        if (Would 'run "ds5bridge cleanup" and "ds5bridge unhide" (detach leftovers, un-cloak pads)') {
            # Best effort by design: a broken half-install must still be removable.
            try { & $exe cleanup 2>&1 | ForEach-Object { "       $_" } } catch { Warn "cleanup: $_" }
            try { & $exe unhide  2>&1 | ForEach-Object { "       $_" } } catch { Warn "unhide: $_" }
        }
    } else { Say 'no installed exe found -- skipping its cleanup/unhide verbs' }

    $usbip = Get-UsbipExe
    if ($usbip) {
        if (Would 'stop usbip auto-re-attach and detach every attached device (usbip attach -X; usbip detach -a)') {
            $null = Run $usbip @('attach', '-X') 20
            if ((Run $usbip @('port') 20).Out -match 'Port\s+\d+') { $null = Run $usbip @('detach', '-a') 30; Start-Sleep -Seconds 1 }
            if ((Run $usbip @('port') 20).Out -match 'Port\s+\d+') { Warn 'usbip still reports an attached device' }
            else { Good 'nothing attached over usbip' }
        }
    }
}

# ---------------------------------------------------------------------------
# 2. what is ours
# ---------------------------------------------------------------------------

function Remove-App {
    Write-Host ''
    Write-Host '--- the app ---'
    try { $run = (Get-ItemProperty -Path $RunKey -Name $RunValueName -ErrorAction SilentlyContinue).$RunValueName } catch { $run = $null }
    if ($run -and ("$run".IndexOf($InstallRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)) {
        if (Would "remove the start-at-login entry (HKCU Run \ $RunValueName)") {
            Remove-ItemProperty -Path $RunKey -Name $RunValueName
            Good 'start-at-login removed'
        }
    } elseif ($run) {
        # Somebody else's (a source checkout, a zip unpacked elsewhere): not ours to delete.
        Say "start-at-login entry points outside $InstallRoot ($run); left alone"
    } else { Say 'no start-at-login entry (nothing to remove)' }

    if (Test-Path $ShortcutPath) {
        if (Would 'remove the Start Menu shortcut') { Remove-Item $ShortcutPath -Force; Good 'shortcut removed' }
    } else { Say 'no Start Menu shortcut (nothing to remove)' }

    if (Test-Path $InnoUninstKey) {
        if (Would 'remove the Settings > Apps entry left by ds5bridge-setup.exe') {
            Remove-Item $InnoUninstKey -Recurse -Force
            Good 'Settings > Apps entry removed'
        }
    }

    if (Test-Path $InstallRoot) {
        if (Would "remove the app ($InstallRoot)") {
            Remove-Item $InstallRoot -Recurse -Force
            if (Test-Path $InstallRoot) { Bad "$InstallRoot could not be removed completely" }
            else { Good 'app removed' }
        }
    } else { Say "no install at $InstallRoot (nothing to remove)" }
}

function Remove-Settings {
    if (-not (Test-Path $ConfigDir)) { return }
    if ($PurgeSettings) {
        if (Would "remove your settings ($ConfigDir)") { Remove-Item $ConfigDir -Recurse -Force; Good 'settings removed' }
    } else {
        Say "settings kept at $ConfigDir (use -PurgeSettings to remove them too)"
    }
}

# ---------------------------------------------------------------------------
# 3. HidHide
# ---------------------------------------------------------------------------

function Clear-HidHideEntries([bool]$Everything) {
    $cli = Get-HidHideCli
    if (-not $cli) { return }
    $what = if ($Everything) { 'EVERY HidHide hide entry (HidHide itself is being removed)' } else { "ds5bridge's own HidHide whitelist and hide entries" }
    if (-not (Would "clear $what")) { return }
    $apps = Parse-HidHideList (Run $cli @('--app-list') 20).Out '--app-reg'
    $mine = @($apps | Where-Object { $_.StartsWith($InstallRoot, [StringComparison]::OrdinalIgnoreCase) })
    foreach ($p in $mine) { $null = Run $cli @('--app-unreg', $p) 20 }
    if ($mine.Count -gt 0) { Good "removed $($mine.Count) ds5bridge whitelist entry(ies)" }
    $n = 0
    if (Test-Path $JournalDir) {
        foreach ($f in Get-ChildItem $JournalDir -Filter '*.json' -ErrorAction SilentlyContinue) {
            try {
                $rec = Get-Content -Raw $f.FullName | ConvertFrom-Json
                foreach ($id in @($rec.instance_ids)) { if ($id) { $null = Run $cli @('--dev-unhide', "$id") 20; $n++ } }
                Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
            } catch { Warn "could not read $($f.Name): $_" }
        }
    }
    if ($n -gt 0) { Good "unhid $n device(s) ds5bridge had recorded" }
    if ($Everything) {
        $devs = Parse-HidHideList (Run $cli @('--dev-list') 20).Out '--dev-hide'
        foreach ($d in $devs) { $null = Run $cli @('--dev-unhide', $d) 20 }
        $null = Run $cli @('--cloak-off') 20
        Good "unhid $($devs.Count) device(s) in total, cloak off"
    }
    $left = Parse-HidHideList (Run $cli @('--dev-list') 20).Out '--dev-hide'
    if ($left.Count -eq 0) { Good 'HidHide is hiding nothing' }
    elseif ($Everything) { Warn "HidHide still lists: $($left -join '; ')" }
    else { Say "HidHide still hides $($left.Count) device(s) that are not ours (left alone): $($left -join '; ')" }
}

function Remove-HidHide {
    Write-Host ''
    Write-Host '--- HidHide ---'
    $cli = Get-HidHideCli
    $code = Get-HidHideMsiCode
    if (-not $cli -and -not $code) { Say 'not installed'; Repair-OrphanHidHideFilter; return }
    $origin = Get-RecordedOrigin 'HidHide'
    $keep = $KeepHidHide -or (($origin -eq 0) -and -not $RemoveHidHide)
    if ($keep) {
        if ($KeepHidHide) { Say 'kept (-KeepHidHide)' }
        else { Say 'kept: it was already installed before ds5bridge-setup.exe ran (use -RemoveHidHide to remove it anyway)' }
        Clear-HidHideEntries $false
        return
    }
    Clear-HidHideEntries $true
    if (-not (Would 'uninstall HidHide silently (msiexec /X ... /qn /norestart)')) { return }
    if (-not (Assert-NotBusy 'HidHide removal')) { return }
    $done = $false
    if ($code) {
        $r = Run 'msiexec.exe' @('/X', $code, '/qn', '/norestart') 600
        if ($r.Code -in 0, 3010, 1641) { $done = $true; Good "HidHide uninstaller ran (exit $($r.Code))" }
        else { Warn "msiexec exited with $($r.Code); trying winget" }
        if ($r.Code -in 3010, 1641) { $script:RebootNeeded = $true }
    }
    if (-not $done -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        $r = Run 'winget' @('uninstall', '--id', 'Nefarius.HidHide', '--exact', '--silent', '--disable-interactivity', '--accept-source-agreements') 600
        if ($r.Code -eq 0) { $done = $true; Good 'HidHide removed via winget' } else { Warn "winget exited with $($r.Code)" }
    }
    if (-not $done) { Bad 'HidHide could not be uninstalled; use Settings > Apps > HidHide'; return }
    Start-Sleep -Seconds 2
    if (Get-HidHideCli) { Bad 'HidHideCLI.exe is still present' } else { Good 'HidHideCLI.exe gone' }
    $st = Get-ServiceState 'HidHide'
    if ($st -eq 'absent') { Good 'HidHide service gone' }
    else { Warn "HidHide service: $st -- its filter driver unloads on the next reboot"; $script:RebootNeeded = $true }
    Repair-OrphanHidHideFilter
    Remove-ItemProperty -Path $SetupKey -Name HidHideInstalledByUs -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# 4. usbip-win2
# ---------------------------------------------------------------------------

function Remove-Usbip {
    Write-Host ''
    Write-Host '--- usbip-win2 ---'
    $exe = Get-UsbipExe
    $un = Get-UsbipUninstaller
    if (-not $exe -and -not $un -and -not (Test-UsbipDevnode) -and ((Get-ServiceState 'usbip2_ude') -eq 'absent')) { Say 'not installed'; return }
    $origin = Get-RecordedOrigin 'Usbip'
    if ($KeepUsbip) { Say 'kept (-KeepUsbip)'; return }
    if (($origin -eq 0) -and -not $RemoveUsbip) { Say 'kept: it was already installed before ds5bridge-setup.exe ran (use -RemoveUsbip to remove it anyway)'; return }
    if (-not $un) { Warn 'its own uninstaller (unins000.exe) is missing; the driver, files and Apps entry are removed directly' }
    if (-not (Would 'remove usbip-win2: if its driver is loaded, disable it and schedule the removal for the next logon (a REBOOT is needed -- the driver cannot be unloaded while Windows runs); otherwise remove it now (USB 3.0 hubs restart briefly)')) { return }
    # The safe sequence: no other installer at work, the bridge stopped,
    # nothing attached (neither a usbip port in use nor a virtual pad
    # enumerated), HidHide already dealt with above, and the vendor's
    # uninstaller last -- under a timeout it is NOT killed at, because its
    # devnode.exe removes the emulated host controller in the kernel and has
    # been seen to never return; a reboot finishes that.
    if (-not (Assert-NotBusy 'usbip-win2 removal')) { return }
    if (@(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue).Count -gt 0) { Bad 'usbip-win2 not removed: ds5bridge is still running'; return }
    if ($exe) {
        $null = Run $exe @('attach', '-X') 20
        if ((Run $exe @('port') 20).Out -match 'Port\s+\d+') { $null = Run $exe @('detach', '-a') 30 }
    }
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $ports = if ($exe) { (Run $exe @('port') 20).Out } else { '' }
        $vpads = Get-VirtualPads
        $attached = ($ports -match 'Port\s+\d+') -or ($vpads.Count -gt 0)
        if ($attached) { Start-Sleep -Seconds 1 }
    } while ($attached -and (Get-Date) -lt $deadline)
    if ($attached) {
        Bad ("usbip-win2 not removed: a virtual controller is still attached (" +
            ((@(($ports -replace '\s+', ' ').Trim()) + @($vpads | ForEach-Object { $_.InstanceId }) | Where-Object { $_ }) -join '; ') +
            "). Quit ds5bridge, power the controller off, and run this again")
        return
    }
    Good 'nothing attached over usbip'

    # usbip-win2 0.9.7.7 cannot be removed while its driver is loaded: the
    # device goes, but usbip2_ude.sys never returns from its DriverUnload
    # (Wdf01000!FxDestroy waits for a leaked framework object -- two crash
    # dumps, see docs/installer-handoff.md Phase C). That holds the Plug and
    # Play lock; every later PnP call hangs and a shutdown ends in a 0x9F
    # crash that commits nothing. So while the driver is loaded NOTHING in
    # the kernel is asked to remove it: the service is set to not start at
    # the next boot (usbip2_ude only -- usbip2_filter also sits on the
    # physical USB 3.0 root hubs and disabling it would kill every USB
    # device), a one-shot logon task finishes the job after the reboot, and
    # this function does the same when it finds the driver not loaded.
    $udeState = Get-ServiceState 'usbip2_ude'
    $loaded = ($udeState -eq 'RUNNING') -or (Test-UsbipDevnode)
    if ($loaded) {
        $r = Run "$env:SystemRoot\System32\sc.exe" @('config', 'usbip2_ude', 'start=', 'disabled') 30
        $start = (Get-ItemProperty "$ServicesKeyRoot\usbip2_ude" -Name Start -ErrorAction SilentlyContinue).Start
        if ($r.Code -ne 0 -or $start -ne 4) { Bad "could not disable the usbip2_ude service (sc exit $($r.Code))"; return }
        Good 'usbip2_ude set to not start at the next boot (it cannot be unloaded while Windows is running)'
        $finisher = Get-UsbipFinisherScript
        if ($finisher) {
            try {
                $log = Join-Path $UsbipFinishDir 'usbip-removal.txt'
                $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
                $action = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$finisher`" remove-usbip -Out `"$log`""
                $trigger = New-ScheduledTaskTrigger -AtLogOn
                $trigger.Delay = 'PT30S'
                $principal = New-ScheduledTaskPrincipal -GroupId 'BUILTIN\Administrators' -RunLevel Highest
                # No time limit: the scheduler would otherwise kill a pnputil blocked in the kernel.
                $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
                $null = Register-ScheduledTask -TaskName $UsbipFinishTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop
                Good "usbip-win2 removal scheduled for the next logon (task '$UsbipFinishTask'; log: $log)"
                Warn "REBOOT required: usbip-win2's driver is disabled and is removed automatically at your next logon (USB 3.0 devices blink out and back once). 'USBip' stays in Settings > Apps until then"
            } catch {
                Warn "could not schedule the logon task ($($_.Exception.Message))"
                Warn "REBOOT required: usbip-win2's driver is disabled; after the reboot run this script again (or Settings > Apps > USBip > Uninstall) to finish the removal"
            }
        } else {
            Warn "REBOOT required: usbip-win2's driver is disabled; after the reboot run this script again (or Settings > Apps > USBip > Uninstall) to finish the removal"
        }
        $script:RebootNeeded = $true
        return
    }

    # Driver not loaded: the removal proper. PnP calls under a timeout, never killed.
    if (Test-UsbipDevnode) {
        $r = Run "$env:SystemRoot\System32\pnputil.exe" @('/remove-device', 'ROOT\USB\0000', '/subtree') $UsbipPnpTimeoutSec -NoKill
        if ($r.Code -eq -2) { Warn "pnputil /remove-device is still running after $UsbipPnpTimeoutSec s (pid $($r.Pid)); left running -- do not end it. REBOOT, then run this again"; $script:RebootNeeded = $true; return }
        if ($r.Code -in 0, 3010, 259) { Good "emulated host controller devnode removed (pnputil exit $($r.Code))" } else { Warn "pnputil /remove-device exit $($r.Code): $(($r.Out -replace '\s+',' ').Trim())" }
    }
    if ($un) {
        $r = Run $un @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') $UsbipUninstallTimeoutSec -NoKill
        if ($r.Code -eq -2) { Warn "the usbip-win2 uninstaller is still running after $UsbipUninstallTimeoutSec s (pid $($r.Pid)); left running -- do not end it. REBOOT, then run this again"; $script:RebootNeeded = $true; return }
        if ($r.Code -eq 0) { Good 'usbip-win2 uninstaller ran (exit 0)'; Start-Sleep -Seconds 2 } else { Warn "the usbip-win2 uninstaller exited with $($r.Code); removing leftovers directly" }
    }
    $infs = @()
    foreach ($f in Get-ChildItem "$env:SystemRoot\INF\oem*.inf" -ErrorAction SilentlyContinue) {
        try { $head = Get-Content $f.FullName -TotalCount 60 -ErrorAction Stop } catch { continue }
        if (($head -join "`n") -match 'usbip2_(ude|filter)') { $infs += $f.Name }
    }
    foreach ($inf in $infs) {
        $r = Run "$env:SystemRoot\System32\pnputil.exe" @('/delete-driver', $inf, '/uninstall', '/force') $UsbipPnpTimeoutSec -NoKill
        if ($r.Code -eq -2) { Warn "pnputil /delete-driver $inf is still running after $UsbipPnpTimeoutSec s (pid $($r.Pid)); left running. REBOOT, then run this again"; $script:RebootNeeded = $true; return }
        if ($r.Code -in 0, 3010, 259) { Good "driver package $inf removed (pnputil exit $($r.Code))"; if ($r.Code -ne 0) { $script:RebootNeeded = $true } } else { Warn "pnputil /delete-driver $inf exit $($r.Code): $(($r.Out -replace '\s+',' ').Trim())" }
    }
    $dir = if ($un) { Split-Path $un -Parent } else { "$env:ProgramFiles\USBip" }
    if (Test-Path $dir) {
        Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $dir) { Warn "$dir could not be removed completely; delete it after a reboot" } else { Good "$dir removed" }
    }
    foreach ($e in Get-UninstallEntries { param($x) $x.DisplayName -like 'USBip version*' }) {
        Remove-Item $e.PSPath -Recurse -Force -ErrorAction SilentlyContinue; Good 'USBip Apps entry removed'
    }
    try { if (Get-ScheduledTask -TaskName $UsbipFinishTask -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $UsbipFinishTask -Confirm:$false -ErrorAction Stop; Good 'usbip-win2 removal task removed' } } catch { Warn "could not remove the task '$UsbipFinishTask': $($_.Exception.Message)" }
    Remove-Item (Join-Path $UsbipFinishDir 'setup-helper.ps1') -Force -ErrorAction SilentlyContinue

    if (Get-UsbipExe) { Bad 'usbip.exe is still present' } else { Good 'usbip.exe gone' }
    foreach ($s in $UsbipServices) {
        $st = Get-ServiceState $s
        if ($st -eq 'absent') { Good "service $s gone" }
        elseif ($st -like 'marked for deletion*') { Good "service $s $st"; $script:RebootNeeded = $true }
        else { Bad "service $s is still $st" }
    }
    if (Test-UsbipDevnode) { Bad "'$UsbipDevnode' is still present" } else { Good "'$UsbipDevnode' gone" }
    Remove-ItemProperty -Path $SetupKey -Name UsbipInstalledByUs -ErrorAction SilentlyContinue
}

function Get-UsbipFinisherScript {
    # A copy of app/packaging/setup-helper.ps1 in %ProgramData%\ds5bridge,
    # where the app removal cannot delete it: from the install (cached at
    # start, before Remove-App), a repo checkout next to this script, or
    # the raw file on GitHub. $null when none can be had.
    New-Item -ItemType Directory -Force $UsbipFinishDir -ErrorAction SilentlyContinue | Out-Null
    $dest = Join-Path $UsbipFinishDir 'setup-helper.ps1'
    if ($script:HelperCopy -and (Test-Path $script:HelperCopy)) { Copy-Item $script:HelperCopy $dest -Force; return $dest }
    if ($PSScriptRoot) {
        $repo = Join-Path (Split-Path $PSScriptRoot -Parent) 'app\packaging\setup-helper.ps1'
        if (Test-Path $repo) { Copy-Item $repo $dest -Force; return $dest }
    }
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$RawBase/app/packaging/setup-helper.ps1" -OutFile $dest -ErrorAction Stop
        if ((Get-Item $dest).Length -gt 10000) { return $dest }
    } catch { }
    return $null
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '  ds5bridge uninstaller'
if ($DryRun) { Write-Host '  DRY RUN: showing the plan; changing nothing.' -ForegroundColor Cyan }
Write-Host ''

Assert-Elevation
if (-not $DryRun) {
    # Nothing starts while another installer is at work; a driver removal
    # mid-way through somebody else's driver install wedged PnP once.
    $busy = @(Get-BusyInstallers)
    if ($busy.Count -gt 0) {
        Bad "another installer is running ($($busy -join ', ')). Wait for it to finish, then run this again. Nothing was changed."
        exit 1
    }
}
Repair-OrphanHidHideFilter
# The installed helper is what finishes a usbip-win2 removal after the
# reboot; keep a copy before Remove-App deletes it.
$script:HelperCopy = $null
if (-not $DryRun) {
    $installedHelper = Join-Path $InstallRoot 'installer\setup-helper.ps1'
    if (Test-Path $installedHelper) {
        $script:HelperCopy = Join-Path $env:TEMP 'ds5bridge-setup-helper.ps1'
        Copy-Item $installedHelper $script:HelperCopy -Force -ErrorAction SilentlyContinue
    }
}
Stop-Bridge
Remove-App
Remove-Settings
Remove-HidHide
Remove-Usbip
if (-not $DryRun) {
    Repair-OrphanHidHideFilter
    foreach ($k in $SetupKey, 'HKLM:\SOFTWARE\ds5bridge') {
        if ((Test-Path $k) -and -not (Get-ChildItem $k -ErrorAction SilentlyContinue) -and -not ((Get-Item $k).GetValueNames())) { Remove-Item $k -ErrorAction SilentlyContinue }
    }
}

if (-not $DryRun -and -not (Get-HidHideCli)) {
    # HidHide's MSI leaves System32\drivers\HidHide.sys behind; it is dead
    # weight once no HidHide service exists (after the reboot that unloads
    # it). Never touched while a service key exists.
    $sys = "$env:SystemRoot\System32\drivers\HidHide.sys"
    if ((Test-Path $sys) -and -not (Test-Path "$ServicesKeyRoot\HidHide")) {
        Remove-Item $sys -Force -ErrorAction SilentlyContinue
        if (Test-Path $sys) { Say "HidHide.sys left over at $sys (in use; harmless, deletable after a reboot)" } else { Say "leftover $sys removed" }
    } elseif (Test-Path $sys) { Say "HidHide.sys stays at $sys until the reboot unloads it" }
}

Write-Host ''
Write-Host '--- summary ---'
if (-not $DryRun) {
    try {
        $pads = @(Get-PnpDevice -PresentOnly -Class HIDClass -ErrorAction Stop | Where-Object { $_.InstanceId -like "$BtPadPrefix*" })
        $bad = @($pads | Where-Object { $_.Status -ne 'OK' })
        if ($pads.Count -eq 0) { Say 'no Bluetooth DualSense connected right now' }
        elseif ($bad.Count -gt 0) { Warn "$($bad.Count) of $($pads.Count) Bluetooth DualSense devnode(s) not OK -- power-cycle the controller" }
        else { Good "$($pads.Count) Bluetooth DualSense HID devnode(s) present and OK" }
    } catch { }
}
if ($DryRun) { Say 'dry run complete; nothing was changed.' }
elseif ($script:Fails -gt 0) { Bad "$($script:Fails) step(s) failed -- see above" }
else { Good 'ds5bridge is uninstalled.' }
if ($script:RebootNeeded) { Warn 'a REBOOT finishes the driver removal. Nothing restarts on its own.' }
if ($Elevated -and -not $DryRun) {
    Write-Host ''
    Read-Host 'Press Enter to close this window'
}
exit $(if ($script:Fails -gt 0) { 1 } else { 0 })
