<#
.SYNOPSIS
    The worker behind ds5bridge-setup.exe and its uninstaller.

.DESCRIPTION
    ds5bridge.iss (Inno Setup) is the user interface: the wizard, the
    component checkboxes, the download-and-verify step and the summary page.
    Everything that needs to LOOK at the machine or CHANGE driver state is
    here, because PowerShell can read services, PnP devnodes and HidHide's
    lists in a line each, and Pascal Script cannot.

    One verb per call. Every verb appends result lines to the file named by
    -Out, one per fact, in the form

        KIND|label|detail

    where KIND is PASS, FAIL, WARN, INFO or REBOOT. The installer reads that
    file back, shows it on its summary page and copies it into its log. The
    exit code is 1 when a FAIL line was written (or the verb's own action
    failed) and 0 otherwise, so a silent install can also be judged from the
    outside.

    Verbs:

        preflight                what runs before anything is changed:
                                 check-busy, then the orphan-filter repair
        check-busy               FAIL if another installer is at work
                                 (msiexec, an Inno setup/uninstall, devnode,
                                 nefconw, pnputil, or an MSI transaction in
                                 progress). Two driver installers at once
                                 wedged PnP on 2026-09-04; never again.
        restore-point            System Restore point before the driver install
        stop-app                 quit the running tray/CLI (politely, then not)
        teardown                 stop-app, then repay every debt the bridge
                                 can leave behind: `ds5bridge cleanup`,
                                 `ds5bridge unhide`, `usbip attach -X`,
                                 `usbip detach -a`
        hidhide-clear            remove OUR whitelist and hide entries; with
                                 -All, everything HidHide holds (only used
                                 when HidHide itself is about to be removed)
        remove-usbip             remove usbip-win2 -- only with nothing
                                 attached and no other installer running. If
                                 its driver is loaded: disable it and schedule
                                 the removal for the next logon, exit 3
                                 ("reboot, then it finishes"); otherwise:
                                 pnputil /remove-device, the vendor's own
                                 uninstaller, leftovers -- every PnP call under
                                 a timeout and never killed
        remove-hidhide           run HidHide's MSI uninstall silently
        hidhide-attach           after a fresh HidHide install: restart the
                                 HIDClass devnode of every connected Bluetooth
                                 DualSense (only those -- never a keyboard or
                                 mouse) so HidHide's class filter joins their
                                 stacks now, then PROVE it from
                                 DEVPKEY_Device_Stack; the [reboot] line is
                                 written only when a pad still lacks it
        autostart-enable         register the start-at-login task (the same
                                 scheduled task app/ds5app/autostart.py
                                 writes: `ds5bridge`, logon of this user,
                                 RunLevel HighestAvailable, no time limit)
        autostart-disable        delete that task, and the pre-0.5.0 HKCU
                                 Run value if it points at this install
        verify-install           the post-install checks
        verify-removed           the post-uninstall checks

    Every verb that looks at HidHide also runs the ORPHAN-FILTER REPAIR: if
    HidHide is registered as an upper filter on the HID classes but its
    service key is gone (an interrupted MSI leaves exactly that), the entry
    is removed and the devices that failed with Code 32 are restarted. That
    combination killed every keyboard, mouse and pad on the test machine
    after a reboot on 2026-09-04.

    This file is shipped inside the setup exe, extracted to %TEMP% for the
    install and copied to %LOCALAPPDATA%\ds5bridge\installer\ so the
    uninstaller can find it later. scripts\uninstall.ps1 is the standalone
    equivalent for people who never used the exe; keep the two in step.

.NOTES
    Windows PowerShell 5.1 on purpose (Checkpoint-Computer does not exist in
    pwsh 7, and 5.1 is on every Windows 10/11 machine). The installer always
    invokes powershell.exe, never pwsh.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('preflight', 'check-busy', 'restore-point', 'stop-app', 'teardown', 'hidhide-clear',
                 'remove-usbip', 'remove-hidhide', 'hidhide-attach', 'autostart-enable', 'autostart-disable',
                 'verify-install', 'verify-removed')]
    [string]$Verb,
    # Results file (appended). Optional: without it, lines go to stdout only.
    [string]$Out = '',
    # Where ds5bridge.exe lives (the installed app dir).
    [string]$AppDir = '',
    # hidhide-clear: clear EVERY entry, not just ours.
    [switch]$All,
    # verify-install: what the user selected, so absence is a FAIL not an INFO.
    [switch]$ExpectUsbip,
    [switch]$ExpectHidHide,
    [switch]$ExpectApp,
    # verify-removed: what was asked to be removed.
    [switch]$ExpectNoUsbip,
    [switch]$ExpectNoHidHide,
    # verify-removed: remove-usbip timed out (exit 3); the uninstaller is
    # still at work and a reboot finishes it, so its absence is not a FAIL.
    [switch]$UsbipPendingReboot,
    # check-busy, tests only: pretend these process names are running
    # (comma-separated, e.g. "devnode,msiexec"), so the guard can be
    # exercised without starting a real installer.
    [string]$MockBusy = ''
)

# Every step is best-effort and reported, never thrown: a half-broken machine
# is exactly the one an uninstaller has to get through.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# pinned facts -- keep identical to scripts/install.ps1 and ds5bridge.iss
# ---------------------------------------------------------------------------

$UsbipVersion    = '0.9.7.7'
$UsbipDevnode    = 'USBip 3.X Emulated Host Controller'
$UsbipServices   = @('usbip2_filter', 'usbip2_ude')
$HidHideService  = 'HidHide'
$HidHideCliPaths = @(
    "$env:ProgramFiles\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe",
    "$env:ProgramFiles\Nefarius Software Solutions e.U\HidHide\x64\HidHideCLI.exe",
    "$env:ProgramFiles\Nefarius Software Solutions\HidHide\HidHideCLI.exe"
)
# A Bluetooth DualSense's HID devnode, the thing HidHide hides and the thing
# that must be visible again after an uninstall.
$BtPadPrefix     = 'HID\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6'
# The HIDClass node above it -- the BTHENUM HID service node, where HidHide's
# class filter actually sits (measured 2026-09-05: its DEVPKEY_Device_Stack
# reads \Driver\HidHide \Driver\HidBth \Driver\steamxbox \Driver\BthEnum once
# the filter is attached, and without the first entry when it is not). This
# is the devnode `hidhide-attach` restarts; restarting it rebuilds the HID\
# children under it too.
$BtPadHidNodePrefix = 'BTHENUM\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6'
$HidHideDriverObject = '\Driver\HidHide'
# The start-at-login task -- name and shape identical to app/ds5app/autostart.py.
$AutostartTask   = 'ds5bridge'
$LegacyRunKey    = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$LegacyRunValue  = 'ds5bridge'
# The virtual (usbip-attached) pad, as Windows enumerates it. One present
# means something is still attached, and usbip-win2 must not be removed.
$VirtualPadPrefix = 'USB\VID_054C&PID_0CE6\'
$InstallRoot     = Join-Path $env:LOCALAPPDATA 'ds5bridge'
$JournalDir      = Join-Path $env:APPDATA 'ds5bridge\hidden'
# The device classes HidHide's MSI registers itself on as an upper filter
# (nefconw --add-class-filter): HIDClass, XnaComposite, XboxComposite.
# $ClassKeyRoot / $ServicesKeyRoot are variables only so a test can point
# the repair at scratch keys instead of the live class database.
$ClassKeyRoot    = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class'
$ServicesKeyRoot = 'HKLM:\SYSTEM\CurrentControlSet\Services'
$HidHideFilterClasses = [ordered]@{
    'HIDClass'      = '{745a17a0-74d3-11d0-b6fe-00a0c90f57da}'
    'XnaComposite'  = '{d61ca365-5af4-4486-998b-9db4734c6ca3}'
    'XboxComposite' = '{05f5cfe2-4733-4950-a6bb-07aad01a3a84}'
}
# usbip-win2 0.9.7.7 cannot be removed while its driver is loaded: the
# device removal itself succeeds, but usbip2_ude.sys then never returns
# from its DriverUnload (it waits in Wdf01000!FxDestroy for a framework
# object it leaked -- two crash dumps of 2026-09-04/05, see
# docs/installer-handoff.md, Phase C). That call holds the Plug and Play
# lock, so every later PnP operation hangs, and a shutdown then ends after
# 300 s in a DRIVER_POWER_STATE_FAILURE (0x9F) crash that commits nothing.
# Every route reaches that unload (devnode.exe remove, pnputil
# /remove-device, /disable-device, /delete-driver /uninstall), so none of
# them is issued while the driver is loaded. Instead the driver is set to
# not start at the next boot (Start=4 on usbip2_ude -- and only that one:
# usbip2_filter is an upper filter on the PHYSICAL USB 3.0 root hubs too and
# disabling it would leave every USB device dead), and the removal proper
# (pnputil, then the vendor's own uninstaller, whose first step is now a
# no-op) runs after the reboot from a one-shot logon task, or from the next
# run of this verb. Any PnP call is still made under a timeout and never
# killed: killing a process inside a PnP call is how the 2026-09-04
# outage started.
$UsbipUninstallTimeoutSec = 180
$UsbipPnpTimeoutSec       = 120
$UsbipFinishDir  = Join-Path $env:ProgramData 'ds5bridge'
$UsbipFinishTask = 'ds5bridge finish usbip-win2 removal'
$UsbipUdeService = 'usbip2_ude'
$UsbipDevnodeId  = 'ROOT\USB\0000'

if (-not $AppDir) { $AppDir = Join-Path $InstallRoot 'app' }

$script:Fails = 0

# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

function Emit([string]$Kind, [string]$Label, [string]$Detail = '') {
    if ($Kind -eq 'FAIL') { $script:Fails++ }
    # One line, no newlines inside (the installer splits on lines).
    $Detail = ($Detail -replace '[\r\n]+', ' ').Trim()
    $line = '{0}|{1}|{2}' -f $Kind, $Label, $Detail
    Write-Host $line
    if ($Out) {
        # UTF-8 with BOM, so Inno's LoadStringsFromFile reads any device path
        # or user name back exactly. The preamble is written only when the
        # file is created.
        [IO.File]::AppendAllText($Out, $line + "`r`n", (New-Object Text.UTF8Encoding $true))
    }
}

function Run([string]$Exe, [string[]]$Arguments, [int]$TimeoutSec = 120, [switch]$NoKill) {
    # Capture stdout+stderr and the exit code without a console window and
    # without ever hanging: a kernel-driver tool that stops answering looks
    # exactly like a driver fault, and the report must still be written.
    # -NoKill: on timeout leave the process running (a vendor uninstaller
    # mid-way through a device removal must not be shot) and return -2.
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    try {
        $p = [Diagnostics.Process]::Start($psi)
    } catch {
        return @{ Code = -1; Out = "could not start: $_" }
    }
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
    $roots = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
               'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
    foreach ($root in $roots) {
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
            $e = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($e -and $e.DisplayName -and (& $Filter $e)) { $e }
        }
    }
}

function Get-UsbipExe {
    # The Inno uninstall key is where the real install location lives -- the
    # same lookup app/ds5app/usbip.py does. Narrow on purpose: usbipd-win also
    # matches a naive "usbip" search and must never be touched.
    foreach ($e in Get-UninstallEntries { param($x) $x.DisplayName -like 'USBip version*' }) {
        if ($e.InstallLocation) {
            $p = Join-Path $e.InstallLocation 'usbip.exe'
            if (Test-Path $p) { return $p }
        }
    }
    $p = "$env:ProgramFiles\USBip\usbip.exe"
    if (Test-Path $p) { return $p }
    return $null
}

function Get-UsbipVersion([string]$Exe) {
    if (-not $Exe) { return $null }
    $r = Run $Exe @('--version') 15
    if ($r.Code -eq 0 -and $r.Out) { return ($r.Out -split "`r?`n")[0].Trim() }
    return '?'
}

function Get-UsbipUninstaller {
    foreach ($e in Get-UninstallEntries { param($x) $x.DisplayName -like 'USBip version*' }) {
        if ($e.UninstallString) {
            $p = $e.UninstallString.Trim('"')
            if (Test-Path $p) { return $p }
        }
    }
    $p = "$env:ProgramFiles\USBip\unins000.exe"
    if (Test-Path $p) { return $p }
    return $null
}

function Get-HidHideCli {
    foreach ($p in $HidHideCliPaths) { if (Test-Path $p) { return $p } }
    return $null
}

function Get-HidHideMsiCode {
    foreach ($e in Get-UninstallEntries { param($x) $x.DisplayName -eq 'HidHide' }) {
        if ($e.UninstallString -match '\{[0-9A-Fa-f-]{36}\}') { return $Matches[0] }
        if ($e.PSChildName -match '^\{[0-9A-Fa-f-]{36}\}$') { return $e.PSChildName }
    }
    return $null
}

function Get-ServiceState([string]$Name) {
    # sc.exe rather than Get-Service: on Windows PowerShell 5.1 Get-Service
    # does not list kernel drivers, and all three services here are drivers.
    $out = & sc.exe query $Name 2>&1 | Out-String
    if ($LASTEXITCODE -eq 1060) { return 'absent' }
    $key = "HKLM:\SYSTEM\CurrentControlSet\Services\$Name"
    $flag = (Get-ItemProperty $key -Name DeleteFlag -ErrorAction SilentlyContinue).DeleteFlag
    if ($flag -eq 1) { return 'marked for deletion (gone after a reboot)' }
    if ($out -match 'STATE\s*:\s*\d+\s+(\w+)') { return $Matches[1] }
    return "unknown (sc exit $LASTEXITCODE)"
}

function Get-UsbipDevnode {
    try {
        $d = @(Get-PnpDevice -PresentOnly -ErrorAction Stop |
               Where-Object { $_.FriendlyName -eq $UsbipDevnode })
        if ($d.Count -gt 0) { return '{0} ({1}, status {2})' -f $d[0].FriendlyName, $d[0].InstanceId, $d[0].Status }
        return $null
    } catch {
        $out = & pnputil.exe /enum-devices /connected 2>&1 | Out-String
        if ($out -match [regex]::Escape($UsbipDevnode)) { return "$UsbipDevnode (pnputil)" }
        return $null
    }
}

function Get-BtPads {
    # The HID collection PDOs (HID\...) of connected Bluetooth DualSenses; when
    # none is listed -- they re-enumerate for a few seconds after their parent
    # is restarted, and a fresh install restarts exactly those parents -- the
    # parents themselves (the BTHENUM HID service nodes, one per pad) stand in,
    # so the count printed is the number of pads, never a misleading zero.
    try {
        $kids = @(Get-PnpDevice -PresentOnly -Class HIDClass -ErrorAction Stop |
                  Where-Object { $_.InstanceId -like "$BtPadPrefix*" })
        if ($kids.Count -gt 0) { return $kids }
        return @(Get-BtPadHidNodes)
    } catch { @() }
}

function Get-VirtualPads {
    try {
        @(Get-PnpDevice -PresentOnly -ErrorAction Stop |
          Where-Object { $_.InstanceId -like "$VirtualPadPrefix*" })
    } catch { @() }
}

function Get-BtPadHidNodes {
    # The HIDClass nodes of connected Bluetooth DualSenses -- and nothing
    # else in HIDClass. Keyboards and mice are never restarted by anything
    # in this file.
    try {
        @(Get-PnpDevice -PresentOnly -Class HIDClass -ErrorAction Stop |
          Where-Object { $_.InstanceId -like "$BtPadHidNodePrefix*" })
    } catch { @() }
}

function Test-HidHideInStack([string]$InstanceId) {
    # $true / $false from DEVPKEY_Device_Stack; $null when it cannot be read.
    try {
        $stack = @((Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName 'DEVPKEY_Device_Stack' -ErrorAction Stop).Data)
        if ($stack.Count -eq 0) { return $null }
        return [bool]($stack | Where-Object { "$_".Trim() -ieq $HidHideDriverObject })
    } catch { return $null }
}

function Report-HidHideFilter([bool]$Restart) {
    # Is HidHide's filter actually IN the stack of every connected DualSense?
    # HidHide's class filter joins a HID device's stack only when that stack
    # is built, so a pad already paired when HidHide was installed carries a
    # correct hide-list entry that does nothing until the device is restarted
    # or the PC rebooted (the 2026-09-05 "UI says hidden, games see the pad"
    # report). With -Restart, the pads that lack it are restarted here --
    # pnputil under a timeout, never killed -- and checked again; the REBOOT
    # line is written only for a pad that still lacks it afterwards.
    $nodes = Get-BtPadHidNodes
    if ($nodes.Count -eq 0) {
        Emit INFO 'HidHide filter' "no Bluetooth DualSense connected right now; HidHide joins a controller's device stack when it connects, so no reboot should be needed"
        return
    }
    $missing = @($nodes | Where-Object { (Test-HidHideInStack $_.InstanceId) -eq $false })
    if ($Restart -and $missing.Count -gt 0) {
        foreach ($d in $missing) {
            $r = Run "$env:SystemRoot\System32\pnputil.exe" @('/restart-device', $d.InstanceId) 60 -NoKill
            if ($r.Code -eq -2) { Emit WARN 'HidHide filter' "pnputil /restart-device $($d.InstanceId) still running after 60 s (pid $($r.Pid)); left running -- do not end it" }
            elseif ($r.Code -ne 0) { Emit WARN 'HidHide filter' ("could not restart $($d.InstanceId) (pnputil exit $($r.Code)): " + ($r.Out -replace '\s+', ' ').Trim()) }
        }
        Start-Sleep -Seconds 2
        $nodes = Get-BtPadHidNodes
    }
    $attached = @($nodes | Where-Object { (Test-HidHideInStack $_.InstanceId) -eq $true })
    $still = @($nodes | Where-Object { (Test-HidHideInStack $_.InstanceId) -eq $false })
    $unknown = $nodes.Count - $attached.Count - $still.Count
    if ($still.Count -eq 0 -and $unknown -eq 0) {
        Emit PASS 'HidHide filter' ("attached to {0} DualSense device(s), no reboot needed{1}" -f $attached.Count,
            $(if ($Restart -and $missing.Count -gt 0) { " (restarted $($missing.Count) device(s) to make it so)" } else { '' }))
    } elseif ($still.Count -eq 0) {
        Emit WARN 'HidHide filter' "attached to $($attached.Count) DualSense device(s); the stack of $unknown could not be read"
    } else {
        Emit REBOOT 'HidHide' ("its filter is not attached to {0} of {1} connected DualSense device(s) ({2}); hide-while-bridged works for them after the next reboot, or after switching the controller off and on" -f
            $still.Count, $nodes.Count, (($still | ForEach-Object { $_.InstanceId }) -join '; '))
    }
}

function Test-MsiexecClient([string]$CommandLine) {
    # msiexec.exe is busy only as a CLIENT: /i /x /f* /p /a /j* /package
    # /uninstall /update, or a custom-action server (-Embedding) that only
    # exists during a transaction. The Windows Installer service host
    # ("msiexec.exe /V", session 0, SYSTEM) idles for ~10 minutes after
    # every boot and after every install and must not count; an open
    # transaction is caught by the InProgress key instead.
    if (-not $CommandLine) { return $false }
    # "/X{product-code}" has no space after the switch, hence the "{".
    return ($CommandLine -match '(^|\s)[/-](i|x|f[a-z]*|p|a|j[um]?|package|uninstall|update)(\s|$|\{)') -or
           ($CommandLine -match '(^|\s)-Embedding(\s|$)')
}

function Get-BusyInstallers {
    # Names (without extension) of processes that mean "somebody is changing
    # driver or installer state right now". Our own ancestors are excluded:
    # this helper is normally a child of the very Inno setup/uninstall it is
    # working for (ds5bridge-setup-x.y.z.tmp, _iu14D2N.tmp, unins000.exe).
    $names = '^(msiexec|unins\d+|_iu[0-9a-z]+|_unins.*|devnode|nefconw|pnputil|usbip-[0-9.]+-x64|hidhide_[0-9._]+x64)$'
    $busy = @()
    if ($MockBusy) {
        # "name" or "name:command line", comma-separated.
        foreach ($n in ($MockBusy -split ',')) {
            $n = $n.Trim(); if (-not $n) { continue }
            $name, $cmd = $n -split ':', 2
            if (($name -ieq 'msiexec') -and -not (Test-MsiexecClient $cmd)) { continue }
            $busy += ('{0} (mock)' -f $name)
        }
        return $busy
    }
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
        $isSetupTmp = ($p.Name -match '\.tmp$') -and ($base -match 'setup')   # an Inno setup's running copy
        if (-not ($base -match $names) -and -not $isSetupTmp) { continue }
        if (($base -ieq 'msiexec') -and -not (Test-MsiexecClient $p.CommandLine)) { continue }
        $busy += ('{0} (pid {1})' -f $p.Name, $p.ProcessId)
    }
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\InProgress') {
        $busy += 'a Windows Installer transaction (HKLM\...\Installer\InProgress)'
    }
    return $busy
}

function Assert-NotBusy([string]$What) {
    # Returns $true when it is safe to go on. Emits the FAIL line otherwise.
    $busy = @(Get-BusyInstallers)
    if ($busy.Count -eq 0) { return $true }
    Emit FAIL $What ("not started: another installer is running -- " + ($busy -join ', ') +
        ". Two driver installers at once can hang Plug and Play. Wait for it to finish and try again")
    return $false
}

function Repair-OrphanHidHideFilter {
    # An interrupted HidHide MSI (install or uninstall) can roll back the
    # service and driver-store entry but leave "HidHide" in the UpperFilters
    # of the HID classes. From the next reboot every keyboard, mouse and pad
    # then fails with Code 32 (CM_PROB_DISABLED_SERVICE). Take the entry out
    # when the service it names does not exist, and restart what broke.
    if (Test-Path (Join-Path $ServicesKeyRoot $HidHideService)) { return }
    $fixed = @()
    foreach ($name in $HidHideFilterClasses.Keys) {
        $key = Join-Path $ClassKeyRoot $HidHideFilterClasses[$name]
        $uf = @((Get-ItemProperty $key -Name UpperFilters -ErrorAction SilentlyContinue).UpperFilters)
        if (-not ($uf | Where-Object { $_ -ieq $HidHideService })) { continue }
        $rest = @($uf | Where-Object { $_ -and ($_ -ine $HidHideService) })
        try {
            if ($rest.Count -gt 0) { Set-ItemProperty -Path $key -Name UpperFilters -Value ([string[]]$rest) -Type MultiString -ErrorAction Stop }
            else { Remove-ItemProperty -Path $key -Name UpperFilters -ErrorAction Stop }
            $fixed += $name
        } catch {
            Emit FAIL 'HidHide filter' "orphaned UpperFilters entry on $name could not be removed ($($_.Exception.Message.Trim())); input devices will fail after the next reboot until it is"
        }
    }
    if ($fixed.Count -eq 0) { return }
    Emit WARN 'HidHide filter' ("orphaned UpperFilters entry removed from " + ($fixed -join ', ') +
        " (HidHide's service is gone but its filter was still registered; every device in those classes would fail with Code 32)")
    $broken = @()
    try { $broken = @(Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object { $_.Problem -eq 'CM_PROB_DISABLED_SERVICE' }) } catch { }
    $restarted = 0
    foreach ($d in $broken) {
        $r = Run "$env:SystemRoot\System32\pnputil.exe" @('/restart-device', $d.InstanceId) 60
        if ($r.Code -eq 0) { $restarted++ } else { Emit WARN 'HidHide filter' "could not restart $($d.InstanceId) (pnputil exit $($r.Code)); a reboot will" }
    }
    if ($broken.Count -gt 0) { Emit PASS 'HidHide filter' "restarted $restarted of $($broken.Count) device(s) that had failed with Code 32" }
}

function Get-Port3240Owner {
    try {
        $c = Get-NetTCPConnection -LocalPort 3240 -State Listen -ErrorAction Stop | Select-Object -First 1
    } catch { return $null }
    if (-not $c) { return $null }
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if ($p) { return '{0} (pid {1})' -f $p.ProcessName, $c.OwningProcess }
    return "pid $($c.OwningProcess)"
}

function Parse-HidHideList([string]$Text, [string]$Flag) {
    # `--dev-list` / `--app-list` print one `--dev-hide "X"` / `--app-reg "X"`
    # per line; the value is what we want, not the flag.
    $items = @()
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match ('^\s*' + [regex]::Escape($Flag) + '\s+"?(.+?)"?\s*$')) { $items += $Matches[1] }
    }
    return $items
}

# ---------------------------------------------------------------------------
# verbs
# ---------------------------------------------------------------------------

function Invoke-CheckBusy {
    $busy = @(Get-BusyInstallers)
    if ($busy.Count -eq 0) { Emit PASS 'other installers' 'none running'; return 0 }
    Emit FAIL 'other installers' ("running: " + ($busy -join ', ') +
        ". Two driver installers at once can hang Plug and Play; wait for it to finish, then run this again")
    return 1
}

function Test-UsbipRemovalPendingFail {
    # A usbip-win2 removal that is waiting for its reboot must not be
    # installed over: the driver would come back half-alive.
    switch (Get-UsbipRemovalPending) {
        'disabled' {
            Emit FAIL 'usbip-win2' ("a removal of usbip-win2 is waiting for a reboot (its driver service is disabled). " +
                "Reboot, let it finish (it runs at logon; log in $UsbipFinishDir\usbip-removal.txt), then run this setup again")
            return $true
        }
        'deleting' {
            Emit FAIL 'usbip-win2' ("its driver services from a previous install are still marked for deletion (usbip2_ude / usbip2_filter, DeleteFlag=1); " +
                "Windows lets go of the names only at boot, and installing now would fail half-way. Reboot, then run this setup again")
            return $true
        }
    }
    return $false
}

function Invoke-Preflight {
    $rc = Invoke-CheckBusy
    if (Test-UsbipRemovalPendingFail) { $rc = 1 }
    Repair-OrphanHidHideFilter
    return $rc
}

function Invoke-RestorePoint {
    $srKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore'
    $freqName = 'SystemRestorePointCreationFrequency'
    $prev = $null
    try {
        # Windows rate-limits restore points to one per 24h; the documented
        # override makes "the user asked for one now" actually happen, and
        # the previous value is put back whatever happens.
        $prev = (Get-ItemProperty -Path $srKey -Name $freqName -ErrorAction SilentlyContinue).$freqName
        Set-ItemProperty -Path $srKey -Name $freqName -Value 0 -Type DWord -ErrorAction Stop
        Checkpoint-Computer -Description 'Before usbip-win2 (ds5bridge installer)' `
            -RestorePointType 'MODIFY_SETTINGS' -ErrorAction Stop
        Emit PASS 'restore point' 'created: "Before usbip-win2 (ds5bridge installer)"'
        return 0
    } catch {
        Emit WARN 'restore point' "could not create one ($($_.Exception.Message.Trim())). System Restore may be disabled on this machine."
        return 1
    } finally {
        try {
            if ($null -ne $prev) { Set-ItemProperty -Path $srKey -Name $freqName -Value $prev -Type DWord }
            else { Remove-ItemProperty -Path $srKey -Name $freqName -ErrorAction SilentlyContinue }
        } catch { }
    }
}

function Invoke-StopApp {
    $procs = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { Emit INFO 'ds5bridge' 'not running'; return 0 }
    # taskkill without /F posts WM_CLOSE, which the tray's teardown handles:
    # it detaches the virtual pad and unhides the real one on the way out.
    foreach ($p in $procs) { & taskkill.exe /PID $p.Id 2>&1 | Out-Null }
    foreach ($p in $procs) { try { $p.WaitForExit(15000) | Out-Null } catch { } }
    $left = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
    if ($left.Count -gt 0) {
        foreach ($p in $left) { & taskkill.exe /F /PID $p.Id 2>&1 | Out-Null }
        Start-Sleep -Seconds 2
        $left = @(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue)
    }
    if ($left.Count -gt 0) {
        Emit FAIL 'ds5bridge' "still running (pid $($left.Id -join ', ')) -- quit it from the tray icon and try again"
        return 1
    }
    Emit PASS 'ds5bridge' "stopped ($($procs.Count) process(es))"
    return 0
}

function Invoke-Teardown {
    $rc = Invoke-StopApp
    $exe = Join-Path $AppDir 'ds5bridge.exe'
    if (Test-Path $exe) {
        # The app's own rescue verbs know the journal format and the port
        # layout; they are the authoritative way to repay the hide debt.
        $r = Run $exe @('cleanup') 60
        Emit INFO 'ds5bridge cleanup' ("exit {0}: {1}" -f $r.Code, ($r.Out -replace '\s+', ' ').Trim())
        $r = Run $exe @('unhide') 60
        Emit INFO 'ds5bridge unhide' ("exit {0}: {1}" -f $r.Code, ($r.Out -replace '\s+', ' ').Trim())
    } else {
        Emit INFO 'ds5bridge' "no exe at $exe; skipping its cleanup/unhide verbs"
    }
    $usbip = Get-UsbipExe
    if ($usbip) {
        # `attach -X` first: an attach arms a background auto-re-attach, and
        # detaching without stopping it silently reacquires the device.
        $r = Run $usbip @('attach', '-X') 20
        $ports = Run $usbip @('port') 20
        if ($ports.Out -match 'Port\s+\d+') {
            $r = Run $usbip @('detach', '-a') 30
            Start-Sleep -Seconds 1
            $ports = Run $usbip @('port') 20
        }
        if ($ports.Out -match 'Port\s+\d+') {
            Emit WARN 'usbip' ("something is still attached: " + ($ports.Out -replace '\s+', ' ').Trim())
        } else {
            Emit PASS 'usbip' 'auto-re-attach stopped, nothing attached'
        }
    } else {
        Emit INFO 'usbip' 'not installed; nothing to detach'
    }
    return $rc
}

function Invoke-HidHideClear {
    $cli = Get-HidHideCli
    if (-not $cli) { Emit INFO 'HidHide' 'not installed; nothing to clear'; return 0 }

    # 1. Our whitelist entries. Anything under the install root is ours, which
    #    also catches entries left by an earlier app.old-* directory.
    $apps = Parse-HidHideList (Run $cli @('--app-list') 20).Out '--app-reg'
    $mine = @($apps | Where-Object {
        $_.StartsWith($InstallRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $_.StartsWith($AppDir, [StringComparison]::OrdinalIgnoreCase) })
    foreach ($p in $mine) { $null = Run $cli @('--app-unreg', $p) 20 }
    if ($mine.Count -gt 0) { Emit PASS 'HidHide whitelist' "removed $($mine.Count) ds5bridge entry(ies)" }
    else { Emit INFO 'HidHide whitelist' 'no ds5bridge entries' }

    # 2. Our hide journal, directly through the CLI -- the exe may already be
    #    gone, and a record that outlives its unhide is the one thing this
    #    feature must never leave behind.
    $n = 0
    if (Test-Path $JournalDir) {
        foreach ($f in Get-ChildItem $JournalDir -Filter '*.json' -ErrorAction SilentlyContinue) {
            try {
                $rec = Get-Content -Raw $f.FullName | ConvertFrom-Json
                foreach ($id in @($rec.instance_ids)) { if ($id) { $null = Run $cli @('--dev-unhide', "$id") 20; $n++ } }
                Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
            } catch { Emit WARN 'HidHide journal' "could not read $($f.Name): $_" }
        }
    }
    if ($n -gt 0) { Emit PASS 'HidHide journal' "unhid $n device(s) ds5bridge had recorded" }

    # 3. Everything, when HidHide itself is about to go: a device left in a
    #    blacklist whose driver is being removed is harmless in theory, but
    #    HidHide's own uninstaller does not clear the list, and a later
    #    reinstall would resurrect it. The caller only asks for this when the
    #    user chose to remove HidHide, so DS4Windows-style entries go too.
    if ($All) {
        $devs = Parse-HidHideList (Run $cli @('--dev-list') 20).Out '--dev-hide'
        foreach ($d in $devs) { $null = Run $cli @('--dev-unhide', $d) 20 }
        $null = Run $cli @('--cloak-off') 20
        Emit PASS 'HidHide all entries' "unhid $($devs.Count) device(s), cloak off"
    }

    $left = Parse-HidHideList (Run $cli @('--dev-list') 20).Out '--dev-hide'
    if ($left.Count -eq 0) { Emit PASS 'HidHide hidden devices' 'none' }
    elseif ($All) { Emit WARN 'HidHide hidden devices' "still listed: $($left -join '; ')" }
    else { Emit INFO 'HidHide hidden devices' "$($left.Count) entry(ies) not ours, left alone: $($left -join '; ')" }
    return 0
}

function Invoke-RemoveUsbip {
    # The sequence that is safe (docs/bundle-handoff.md section 4): the
    # bridge stopped, nothing attached, no other installer at work, HidHide
    # already dealt with by the caller, and the vendor's uninstaller last of
    # all under a timeout it is not killed at.
    $un = Get-UsbipUninstaller
    $exe = Get-UsbipExe
    if (-not $un -and -not $exe -and -not (Get-UsbipDevnode) -and ((Get-ServiceState $UsbipUdeService) -eq 'absent')) {
        Emit INFO 'usbip-win2' 'not installed'; return 0
    }
    if (-not $un) { Emit WARN 'usbip-win2' 'its own uninstaller (unins000.exe) is missing; removing the driver, files and Apps entry directly' }
    if (-not (Assert-NotBusy 'usbip-win2')) { return 1 }

    # 1. the bridge, if the caller has not stopped it already
    if (@(Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue).Count -gt 0) {
        if ((Invoke-StopApp) -ne 0) { Emit FAIL 'usbip-win2' 'not removed: ds5bridge is still running'; return 1 }
    }
    # 2. nothing attached: neither a usbip port in use nor a virtual pad
    #    enumerated. Removing the host controller under an attached device
    #    is the one thing the vendor's uninstaller cannot be trusted with.
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
        Emit FAIL 'usbip-win2' ("not removed: a virtual controller is still attached (" +
            (@(($ports -replace '\s+', ' ').Trim()) + @($vpads | ForEach-Object { $_.InstanceId }) | Where-Object { $_ }) -join '; ' +
            "). Quit ds5bridge, power the controller off, and try again")
        return 1
    }
    Emit PASS 'usbip-win2' 'nothing attached'

    # 3. Is the driver loaded? Then nothing in the kernel may be asked to
    #    remove it now (see the note at $UsbipUninstallTimeoutSec): stage A.
    if (Test-UsbipDriverLoaded) { return (Invoke-UsbipRemovalStageA) }
    # 4. Not loaded (after the reboot stage A asked for, or the driver
    #    never started): stage B, the removal proper.
    return (Invoke-UsbipRemovalStageB -Uninstaller $un)
}

function Test-UsbipDriverLoaded {
    # sc.exe reports RUNNING for a loaded kernel driver; the devnode being
    # started (no problem code) means the same from the PnP side.
    if ((Get-ServiceState $UsbipUdeService) -eq 'RUNNING') { return $true }
    try {
        $d = Get-PnpDevice -InstanceId $UsbipDevnodeId -ErrorAction Stop
        if ($d -and $d.Present -and $d.Status -eq 'OK') { return $true }
    } catch { }
    return $false
}

function Get-UsbipRemovalPending {
    # '' when nothing is pending; otherwise why a reboot must come first:
    #  'disabled' -- stage A left usbip2_ude with Start=4 (never a normal
    #                state); the removal proper runs at the next logon.
    #  'deleting' -- stage B ran and the service keys are DeleteFlag=1
    #                tombstones (files gone, driver not loaded) that the
    #                Service Control Manager only lets go of at boot;
    #                CreateService for the names still answers 1073 and the
    #                vendor installer's AddService would fail (2026-09-05,
    #                phaseC-09b-createservice-probe.txt).
    foreach ($svc in $UsbipServices) {
        $key = Join-Path $ServicesKeyRoot $svc
        if (-not (Test-Path $key)) { continue }
        $p = Get-ItemProperty $key -ErrorAction SilentlyContinue
        if ($p.DeleteFlag -eq 1) { return 'deleting' }
        if (($svc -eq $UsbipUdeService) -and ($p.Start -eq 4)) { return 'disabled' }
    }
    return ''
}

function Test-UsbipRemovalPending { return ((Get-UsbipRemovalPending) -eq 'disabled') }

function Invoke-UsbipRemovalStageA {
    # Disable the UDE driver's service so it does not start at the next
    # boot, leave a copy of this script where the uninstaller cannot delete
    # it, and schedule stage B for the next logon. Nothing PnP is touched.
    $r = Run "$env:SystemRoot\System32\sc.exe" @('config', $UsbipUdeService, 'start=', 'disabled') 30
    if ($r.Code -ne 0 -or -not (Test-UsbipRemovalPending)) {
        Emit FAIL 'usbip-win2' ("could not disable the $UsbipUdeService service (sc exit $($r.Code)): " + ($r.Out -replace '\s+', ' ').Trim())
        return 1
    }
    Emit PASS 'usbip-win2 driver' "$UsbipUdeService set to not start at the next boot (it cannot be unloaded while Windows is running)"
    $scheduled = $false
    try {
        New-Item -ItemType Directory -Force $UsbipFinishDir | Out-Null
        $copy = Join-Path $UsbipFinishDir 'setup-helper.ps1'
        Copy-Item $PSCommandPath $copy -Force
        $log = Join-Path $UsbipFinishDir 'usbip-removal.txt'
        $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $action = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$copy`" remove-usbip -Out `"$log`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $trigger.Delay = 'PT30S'
        $principal = New-ScheduledTaskPrincipal -GroupId 'BUILTIN\Administrators' -RunLevel Highest
        # No execution time limit: the Task Scheduler would otherwise KILL a
        # pnputil that is blocked in the kernel -- the one thing never to do.
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
        $null = Register-ScheduledTask -TaskName $UsbipFinishTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop
        $scheduled = $true
        Emit PASS 'usbip-win2 removal' "scheduled for the next logon (task '$UsbipFinishTask'; log: $log)"
    } catch {
        Emit WARN 'usbip-win2 removal' ("could not schedule the logon task ($($_.Exception.Message)); after the reboot remove 'USBip' from Settings > Apps, or run this uninstaller again")
    }
    $how = if ($scheduled) { 'it is removed automatically at your next logon (USB 3.0 devices blink out and back once)' } else { 'then remove it from Settings > Apps > USBip, or run this uninstaller again' }
    Emit REBOOT 'usbip-win2' "its driver can only be removed after a REBOOT; $how. Until then 'USBip' stays listed in Settings > Apps -- that is expected"
    return 3
}

function Invoke-UsbipRemovalStageB([string]$Uninstaller) {
    # The driver is not loaded, so removing the devnode and the packages
    # does not reach the unload that hangs. Every PnP call still runs under
    # a timeout and is never killed; a hang here is reported, not fought.
    $stuck = $false
    if (Get-UsbipDevnode) {
        $r = Run "$env:SystemRoot\System32\pnputil.exe" @('/remove-device', $UsbipDevnodeId, '/subtree') $UsbipPnpTimeoutSec -NoKill
        if ($r.Code -eq -2) {
            Emit WARN 'usbip-win2 devnode' "pnputil /remove-device still running after $UsbipPnpTimeoutSec s (pid $($r.Pid)); left running -- do not end it"
            $stuck = $true
        } elseif ($r.Code -eq 0 -or $r.Code -eq 3010 -or $r.Code -eq 259) {
            Emit PASS 'usbip-win2 devnode' ("pnputil /remove-device exit $($r.Code): " + ($r.Out -replace '\s+', ' ').Trim())
        } else {
            Emit WARN 'usbip-win2 devnode' ("pnputil /remove-device exit $($r.Code): " + ($r.Out -replace '\s+', ' ').Trim())
        }
    } else {
        Emit PASS 'usbip-win2 devnode' "'$UsbipDevnode' not present"
    }
    if (-not $stuck -and $Uninstaller) {
        # The vendor's uninstaller: its devnode.exe step finds nothing now,
        # then it deletes both driver packages (pnputil /delete-driver
        # /uninstall -- the filter comes off the USB 3.0 root hubs, which
        # restart) and its files and Apps entry.
        $r = Run $Uninstaller @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') $UsbipUninstallTimeoutSec -NoKill
        if ($r.Code -eq -2) {
            Emit WARN 'usbip-win2 uninstaller' "still running after $UsbipUninstallTimeoutSec s (pid $($r.Pid)); left running -- do not end it"
            $stuck = $true
        } elseif ($r.Code -eq 0) {
            Emit PASS 'usbip-win2 uninstaller' 'ran (exit 0)'
            Start-Sleep -Seconds 2
        } else {
            Emit WARN 'usbip-win2 uninstaller' "exited with $($r.Code)"
        }
    }
    if ($stuck) {
        Emit REBOOT 'usbip-win2' 'a removal step is blocked inside Windows; reboot, then remove USBip from Settings > Apps if it is still listed'
        return 3
    }
    # Leftovers, one by one (the vendor uninstaller may be missing or may
    # have given up part-way).
    $infs = @()
    foreach ($f in Get-ChildItem "$env:SystemRoot\INF\oem*.inf" -ErrorAction SilentlyContinue) {
        try { $head = Get-Content $f.FullName -TotalCount 60 -ErrorAction Stop } catch { continue }
        if (($head -join "`n") -match 'usbip2_(ude|filter)') { $infs += $f.Name }
    }
    foreach ($inf in $infs) {
        $r = Run "$env:SystemRoot\System32\pnputil.exe" @('/delete-driver', $inf, '/uninstall', '/force') $UsbipPnpTimeoutSec -NoKill
        if ($r.Code -eq -2) { Emit WARN "driver package $inf" "pnputil /delete-driver still running after $UsbipPnpTimeoutSec s (pid $($r.Pid)); left running"; Emit REBOOT 'usbip-win2' 'a driver package removal is blocked inside Windows; reboot'; return 3 }
        elseif ($r.Code -eq 0 -or $r.Code -eq 3010 -or $r.Code -eq 259) { Emit PASS "driver package $inf" "removed (pnputil exit $($r.Code))"; if ($r.Code -ne 0) { Emit REBOOT 'usbip-win2' "pnputil asked for a reboot after removing $inf" } }
        else { Emit WARN "driver package $inf" ("pnputil /delete-driver exit $($r.Code): " + ($r.Out -replace '\s+', ' ').Trim()) }
    }
    $dir = Split-Path $Uninstaller -Parent
    if (-not $dir) { $dir = "$env:ProgramFiles\USBip" }
    if (Test-Path $dir) {
        Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $dir) { Emit WARN 'usbip-win2 files' "$dir could not be removed completely (in use?); delete it after a reboot" }
        else { Emit PASS 'usbip-win2 files' "$dir removed" }
    }
    foreach ($root in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall') {
        Get-ChildItem $root -ErrorAction SilentlyContinue | Where-Object {
            (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DisplayName -like 'USBip version*'
        } | ForEach-Object { Remove-Item $_.PSPath -Recurse -Force -ErrorAction SilentlyContinue; Emit PASS 'usbip-win2 Apps entry' 'removed' }
    }
    # The one-shot task and the script copy stage A left behind.
    try {
        if (Get-ScheduledTask -TaskName $UsbipFinishTask -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $UsbipFinishTask -Confirm:$false -ErrorAction Stop
            Emit PASS 'usbip-win2 removal task' 'removed'
        }
    } catch { Emit WARN 'usbip-win2 removal task' "could not remove the task '$UsbipFinishTask': $($_.Exception.Message)" }
    Remove-Item (Join-Path $UsbipFinishDir 'setup-helper.ps1') -Force -ErrorAction SilentlyContinue
    # Stage B runs after a reboot, which is exactly when HidHide's leftover
    # driver file (if HidHide was removed in the same uninstall) is free.
    if (-not (Get-HidHideCli)) { Remove-HidHideLeftoverSys }
    return 0
}

function Invoke-RemoveHidHide {
    $code = Get-HidHideMsiCode
    $cli = Get-HidHideCli
    if (-not $code -and -not $cli) { Emit INFO 'HidHide' 'not installed'; Repair-OrphanHidHideFilter; return 0 }
    if (-not (Assert-NotBusy 'HidHide')) { return 1 }
    $done = $false
    if ($code) {
        $r = Run 'msiexec.exe' @('/X', $code, '/qn', '/norestart') 600
        switch ($r.Code) {
            0     { $done = $true; Emit PASS 'HidHide uninstaller' "msiexec /X$code exit 0" }
            3010  { $done = $true; Emit PASS 'HidHide uninstaller' "msiexec /X$code exit 3010"; Emit REBOOT 'HidHide' 'its uninstaller asked for a reboot to finish removing the filter driver' }
            1641  { $done = $true; Emit PASS 'HidHide uninstaller' "msiexec /X$code exit 1641"; Emit REBOOT 'HidHide' 'its uninstaller asked for a reboot' }
            default { Emit WARN 'HidHide uninstaller' "msiexec /X$code exited with $($r.Code); trying winget" }
        }
    }
    if (-not $done -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        $r = Run 'winget' @('uninstall', '--id', 'Nefarius.HidHide', '--exact', '--silent',
                            '--disable-interactivity', '--accept-source-agreements') 600
        if ($r.Code -eq 0) { $done = $true; Emit PASS 'HidHide uninstaller' 'winget uninstall exit 0' }
        else { Emit WARN 'HidHide uninstaller' "winget exited with $($r.Code)" }
    }
    if (-not $done) { Emit FAIL 'HidHide' 'could not be uninstalled; use Settings > Apps > HidHide'; return 1 }
    Start-Sleep -Seconds 2
    # The MSI removes its class filters itself; this catches the case where
    # it did not get that far.
    Repair-OrphanHidHideFilter
    return 0
}

function Invoke-HidHideAttach {
    if ((Get-ServiceState $HidHideService) -ne 'RUNNING') {
        Emit REBOOT 'HidHide' "its service is $(Get-ServiceState $HidHideService); the filter driver activates after the next reboot"
        return 0
    }
    Report-HidHideFilter $true
    return 0
}

function Get-AutostartTaskXml([string]$Command, [string]$WorkingDir, [string]$User) {
    # Byte-for-byte the shape app/ds5app/autostart.py writes (task_xml):
    # keep the two in step. Element order is the schema's.
    $esc = { param($s) [System.Security.SecurityElement]::Escape($s) }
    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>ds5bridge</Author>
    <Description>Starts the ds5bridge tray when you log in, with administrator rights and no prompt. Written by ds5bridge (the tray menu's "Start at login" switch, or its installer); ds5bridge removes it when that switch is turned off or it is uninstalled.</Description>
    <URI>\$AutostartTask</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$(& $esc $User)</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(& $esc $User)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$(& $esc $Command)</Command>
      <WorkingDirectory>$(& $esc $WorkingDir)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

function Remove-LegacyRunValue {
    # The pre-0.5.0 HKCU Run entry. Windows silently refuses to start an
    # elevated program from it, so it is dead weight next to the task.
    try { $run = (Get-ItemProperty -Path $LegacyRunKey -Name $LegacyRunValue -ErrorAction SilentlyContinue).$LegacyRunValue } catch { $run = $null }
    if (-not $run) { return }
    if ("$run".IndexOf($InstallRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or "$run" -match 'ds5bridge') {
        Remove-ItemProperty -Path $LegacyRunKey -Name $LegacyRunValue -ErrorAction SilentlyContinue
        Emit PASS 'start-at-login' 'old Run-key entry removed (the task replaces it)'
    } else {
        Emit INFO 'start-at-login' "a Run-key entry named '$LegacyRunValue' points elsewhere ($run); left alone"
    }
}

function Invoke-AutostartEnable {
    $tray = Join-Path $AppDir 'ds5bridge-tray.exe'
    if (-not (Test-Path $tray)) { Emit WARN 'start-at-login' "not registered: $tray is missing"; return 0 }
    $user = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
    $xml = Join-Path $env:TEMP ('ds5bridge-task-' + [IO.Path]::GetRandomFileName() + '.xml')
    try {
        [IO.File]::WriteAllText($xml, (Get-AutostartTaskXml $tray $AppDir $user), [Text.Encoding]::Unicode)
        $r = Run "$env:SystemRoot\System32\schtasks.exe" @('/Create', '/TN', $AutostartTask, '/XML', $xml, '/F') 60
        if ($r.Code -eq 0) {
            Emit PASS 'start-at-login' "scheduled task '$AutostartTask' registered (at logon of $user, highest privileges, no prompt)"
            Remove-LegacyRunValue
        } else {
            Emit WARN 'start-at-login' ("could not register the task (schtasks exit $($r.Code)): " + ($r.Out -replace '\s+', ' ').Trim() + ". The tray menu's 'Start at login' switch can do it later")
        }
    } finally { Remove-Item $xml -Force -ErrorAction SilentlyContinue }
    return 0
}

function Invoke-AutostartDisable {
    $q = Run "$env:SystemRoot\System32\schtasks.exe" @('/Query', '/TN', $AutostartTask) 30
    if ($q.Code -eq 0) {
        $r = Run "$env:SystemRoot\System32\schtasks.exe" @('/Delete', '/TN', $AutostartTask, '/F') 30
        if ($r.Code -eq 0) { Emit PASS 'start-at-login' "scheduled task '$AutostartTask' removed" }
        else { Emit WARN 'start-at-login' ("could not remove the task '$AutostartTask' (schtasks exit $($r.Code)): " + ($r.Out -replace '\s+', ' ').Trim() + "; delete it in Task Scheduler") }
    } else {
        Emit INFO 'start-at-login' 'no scheduled task registered'
    }
    Remove-LegacyRunValue
    return 0
}

function Invoke-VerifyInstall {
    Repair-OrphanHidHideFilter
    # usbip-win2
    $exe = Get-UsbipExe
    $ver = Get-UsbipVersion $exe
    if ($exe -and $ver -eq $UsbipVersion) { Emit PASS 'usbip.exe --version' "$ver ($exe)" }
    elseif ($exe) { Emit $(if ($ExpectUsbip) { 'FAIL' } else { 'WARN' }) 'usbip.exe --version' "$ver, expected $UsbipVersion ($exe)" }
    else { Emit $(if ($ExpectUsbip) { 'FAIL' } else { 'INFO' }) 'usbip.exe' 'not installed' }
    foreach ($s in $UsbipServices) {
        $st = Get-ServiceState $s
        if ($st -eq 'RUNNING') { Emit PASS "service $s" 'running' }
        elseif ($st -eq 'absent') { Emit $(if ($ExpectUsbip) { 'FAIL' } else { 'INFO' }) "service $s" 'absent' }
        else { Emit $(if ($ExpectUsbip) { 'WARN' } else { 'INFO' }) "service $s" $st }
    }
    $dn = Get-UsbipDevnode
    $null = Test-UsbipRemovalPendingFail
    if ($dn) { Emit PASS 'devnode' $dn }
    else { Emit $(if ($ExpectUsbip) { 'FAIL' } else { 'INFO' }) 'devnode' "'$UsbipDevnode' not present" }
    $owner = Get-Port3240Owner
    if ($owner) { Emit INFO 'port 3240' "in use by $owner -- that is usbipd-win (WSL USB passthrough); fine, ds5bridge uses 3241 and never touches it" }

    # HidHide
    $cli = Get-HidHideCli
    $st = Get-ServiceState $HidHideService
    if ($cli) {
        $v = (Run $cli @('--version') 15).Out.Trim()
        Emit PASS 'HidHideCLI.exe' "$v ($cli)"
        if ($st -eq 'RUNNING') {
            Emit PASS "service $HidHideService" 'running'
            # Running is not attached: the stack of each connected pad is
            # the proof (no restart here; hidhide-attach did that when the
            # driver was installed in this run).
            Report-HidHideFilter $false
        }
        else {
            Emit WARN "service $HidHideService" "$st -- the filter driver activates after a reboot"
            Emit REBOOT 'HidHide' 'its filter driver activates after the next reboot; hide-while-bridged works from then on'
        }
    } else {
        Emit $(if ($ExpectHidHide) { 'FAIL' } else { 'INFO' }) 'HidHideCLI.exe' 'not installed (the bridge works without it; only hide-while-bridged needs it)'
        if ($st -ne 'absent') { Emit INFO "service $HidHideService" $st }
    }

    # the app
    $app = Join-Path $AppDir 'ds5bridge.exe'
    if (Test-Path $app) {
        $r = Run $app @('--version') 30
        if ($r.Code -eq 0 -and $r.Out -match '(\d+(\.\d+)+)') { Emit PASS 'ds5bridge.exe --version' "$($Matches[1]) ($app)" }
        else { Emit FAIL 'ds5bridge.exe --version' "exit $($r.Code): $(($r.Out -replace '\s+',' ').Trim())" }
        if (Test-Path (Join-Path $AppDir 'ds5bridge-tray.exe')) { Emit PASS 'ds5bridge-tray.exe' 'present' }
        else { Emit FAIL 'ds5bridge-tray.exe' 'missing' }
    } else {
        Emit $(if ($ExpectApp) { 'FAIL' } else { 'INFO' }) 'ds5bridge.exe' "not at $app"
    }
    $pads = Get-BtPads
    Emit INFO 'Bluetooth DualSense' "$($pads.Count) connected right now"
    return $(if ($script:Fails -gt 0) { 1 } else { 0 })
}

function Remove-HidHideLeftoverSys {
    # HidHide's MSI leaves System32\drivers\HidHide.sys behind (in use until
    # the reboot that unloads it). Once no HidHide service exists any more
    # the file is dead weight; remove it when that is possible, say so
    # either way. Never touched while a service key exists (a reinstall, or
    # the pending unload, may still own it).
    $sys = "$env:SystemRoot\System32\drivers\HidHide.sys"
    if (-not (Test-Path $sys)) { return }
    if (Test-Path (Join-Path $ServicesKeyRoot $HidHideService)) {
        Emit INFO 'HidHide.sys' "still at $sys until the reboot unloads it"
        return
    }
    Remove-Item $sys -Force -ErrorAction SilentlyContinue
    if (Test-Path $sys) { Emit INFO 'HidHide.sys' "left over at $sys (in use; harmless, deletable after a reboot)" }
    else { Emit INFO 'HidHide.sys' "leftover file removed ($sys)" }
}

function Invoke-VerifyRemoved {
    Repair-OrphanHidHideFilter
    if ($ExpectNoHidHide -or -not (Get-HidHideCli)) { Remove-HidHideLeftoverSys }
    $exe = Get-UsbipExe
    if ($UsbipPendingReboot) {
        Emit INFO 'usbip-win2' 'its driver is disabled and the removal runs after the reboot (not checked now); USBip stays in Settings > Apps until then'
    } elseif ($ExpectNoUsbip) {
        if ($exe) { Emit FAIL 'usbip.exe' "still present at $exe" } else { Emit PASS 'usbip.exe' 'gone' }
        foreach ($s in $UsbipServices) {
            $st = Get-ServiceState $s
            if ($st -eq 'absent') { Emit PASS "service $s" 'gone' }
            elseif ($st -like 'marked for deletion*') { Emit PASS "service $s" $st; Emit REBOOT 'usbip-win2' 'its driver services disappear on the next reboot' }
            else { Emit FAIL "service $s" "still $st" }
        }
        $dn = Get-UsbipDevnode
        if ($dn) { Emit FAIL 'devnode' "still present: $dn" } else { Emit PASS 'devnode' "'$UsbipDevnode' gone" }
        $un = Get-UsbipUninstaller
        if ($un) { Emit WARN 'usbip-win2' "uninstaller still registered at $un" }
    } else {
        $ver = Get-UsbipVersion $exe
        if ($exe) { Emit INFO 'usbip-win2' "kept ($ver at $exe)" } else { Emit INFO 'usbip-win2' 'was not installed' }
    }

    $cli = Get-HidHideCli
    $st = Get-ServiceState $HidHideService
    if ($ExpectNoHidHide) {
        if ($cli) { Emit FAIL 'HidHideCLI.exe' "still present at $cli" } else { Emit PASS 'HidHideCLI.exe' 'gone' }
        if ($st -eq 'absent') { Emit PASS "service $HidHideService" 'gone' }
        elseif ($st -like 'marked for deletion*') { Emit PASS "service $HidHideService" $st; Emit REBOOT 'HidHide' 'its filter driver is unloaded on the next reboot' }
        else { Emit WARN "service $HidHideService" "still $st -- HidHide's filter driver unloads on the next reboot" ; Emit REBOOT 'HidHide' 'its filter driver is unloaded on the next reboot' }
        if (Get-HidHideMsiCode) { Emit WARN 'HidHide' 'still registered in Settings > Apps' }
    } else {
        if ($cli) {
            Emit INFO 'HidHide' "kept ($cli, service $st)"
            $left = Parse-HidHideList (Run $cli @('--dev-list') 20).Out '--dev-hide'
            if ($left.Count -eq 0) { Emit PASS 'HidHide hidden devices' 'none' }
            else { Emit INFO 'HidHide hidden devices' "$($left.Count) entry(ies) not ours: $($left -join '; ')" }
        } else { Emit INFO 'HidHide' 'was not installed' }
    }

    $pads = Get-BtPads
    $bad = @($pads | Where-Object { $_.Status -ne 'OK' })
    if ($pads.Count -eq 0) { Emit INFO 'Bluetooth DualSense' 'none connected right now' }
    elseif ($bad.Count -gt 0) { Emit WARN 'Bluetooth DualSense' ("{0} present, {1} not OK: {2}" -f $pads.Count, $bad.Count, (($bad | ForEach-Object { "$($_.InstanceId)=$($_.Status)" }) -join '; ')) }
    else { Emit PASS 'Bluetooth DualSense' "$($pads.Count) present and OK" }
    return $(if ($script:Fails -gt 0) { 1 } else { 0 })
}

# ---------------------------------------------------------------------------

$rc = switch ($Verb) {
    'preflight'      { Invoke-Preflight }
    'check-busy'     { Invoke-CheckBusy }
    'restore-point'  { Invoke-RestorePoint }
    'stop-app'       { Invoke-StopApp }
    'teardown'       { Invoke-Teardown }
    'hidhide-clear'  { Invoke-HidHideClear }
    'remove-usbip'   { Invoke-RemoveUsbip }
    'remove-hidhide' { Invoke-RemoveHidHide }
    'hidhide-attach' { Invoke-HidHideAttach }
    'autostart-enable'  { Invoke-AutostartEnable }
    'autostart-disable' { Invoke-AutostartDisable }
    'verify-install' { Invoke-VerifyInstall }
    'verify-removed' { Invoke-VerifyRemoved }
}
# 3 (remove-usbip: uninstaller still running, reboot finishes it) is kept
# distinct from 1 so the caller can verify the rest without expecting usbip
# gone. A FAIL line always wins.
if ($script:Fails -gt 0) { $rc = 1 }
exit [int]$rc
