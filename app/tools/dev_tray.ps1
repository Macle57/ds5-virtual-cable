# The tray, from source, in one command -- no PyInstaller, no install step.
#
#   powershell -File app\tools\dev_tray.ps1              console + live logs
#   powershell -File app\tools\dev_tray.ps1 -Windowed    no console, like the exe
#   powershell -File app\tools\dev_tray.ps1 -Isolated    scratch settings, not yours
#   powershell -File app\tools\dev_tray.ps1 -Stop        put it down again
#   powershell -File app\tools\dev_tray.ps1 -Logs        follow the windowed log
#
# Anything after the switches is passed straight through to `ds5bridge tray`,
# so `dev_tray.ps1 -Windowed --serial d42f4ba1485d --no-hotplug` works.
#
# There is nothing here that `pythonw.exe -m ds5app tray` does not already do.
# What this adds is the four things that make a build-free loop usable more
# than once:
#
#   * ONE INSTANCE. Every start stops the previous dev tray first. Two bridges
#     contend for `Local\ds5bridge-<port>` and for the same port range, and the
#     failure looks like a bug in the code you just changed rather than a
#     leftover from the run before it.
#   * A CLEAN STOP. The console form is stopped with CTRL_BREAK, which is a
#     signal `service.py` installs a handler for, so the device is detached and
#     the Bluetooth pad is unhidden on the way out. A hard kill skips all of
#     that and can leave a controller invisible to Windows -- the one failure
#     mode this project treats as unacceptable -- so the windowed form, which
#     has no console to signal, is force-killed and then has its debts repaid
#     with `ds5bridge cleanup`.
#   * SOMEWHERE FOR THE OUTPUT TO GO. `pythonw.exe` has no standard handles;
#     the windowed run is redirected to a log file so a crash leaves evidence.
#   * -Isolated. `DS5_CONFIG` moved aside, so a dev run cannot rewrite the
#     settings -- or the hidhide journal -- that your real install is using.
#
# CONSOLE IS THE DEFAULT because this is the dev launcher and the logs are the
# point. -Windowed is for when the no-console path IS what you are testing
# (start-at-login, stream hardening, a print() that only fires on failure).

[CmdletBinding()]
param(
    [switch]$Windowed,
    [switch]$Stop,
    [switch]$Isolated,
    [switch]$Logs,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$TrayArgs = @()
)

$ErrorActionPreference = 'Stop'

$repo      = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$appDir    = Join-Path $repo 'app'
$py        = Join-Path $repo 'prototype\.venv\Scripts\python.exe'
$pyw       = Join-Path $repo 'prototype\.venv\Scripts\pythonw.exe'
$stateDir  = Join-Path $env:LOCALAPPDATA 'ds5bridge-dev'
$statePath = Join-Path $stateDir 'dev-tray.json'
$logPath   = Join-Path $stateDir 'tray.log'
$isoDir    = Join-Path $stateDir 'config'

if (-not (Test-Path $py)) {
    throw "no venv python at $py -- create it first (see CONTRIBUTING.md)"
}
if (-not (Test-Path $stateDir)) {
    New-Item -ItemType Directory -Path $stateDir | Out-Null
}

# ---------------------------------------------------------------------------
# who is running
# ---------------------------------------------------------------------------

function Read-State {
    if (-not (Test-Path $statePath)) { return $null }
    try { $s = Get-Content -Raw $statePath | ConvertFrom-Json } catch { return $null }
    if (-not $s.pid) { return $null }
    $p = Get-Process -Id $s.pid -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    # A recycled PID belonging to something else must never be signalled, let
    # alone killed. The start time is recorded for exactly this check.
    if ($s.started) {
        $drift = ([datetime]$s.started - $p.StartTime).Duration().TotalSeconds
        if ($drift -gt 2) { return $null }
    }
    return $s
}

function Write-State($proc, $mode) {
    @{
        pid     = $proc.Id
        mode    = $mode
        started = $proc.StartTime.ToString('o')
        repo    = $repo
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $statePath
}

function Test-InstalledTrayRunning {
    $installed = Get-Process -Name 'ds5bridge-tray', 'ds5bridge' -ErrorAction SilentlyContinue
    if ($installed) {
        Write-Warning ("the INSTALLED build is running (pid $($installed.Id -join ', ')). " +
            'Two bridges contend for the same instance mutex and port range -- ' +
            'quit it from its tray menu before trusting what you see here.')
    }
}

# ---------------------------------------------------------------------------
# stopping it
# ---------------------------------------------------------------------------

# CTRL_BREAK to another process's console, from a throwaway interpreter. It has
# to be a child process: sending the event means attaching to the target's
# console, and attaching means FreeConsole() first -- which, run in this
# PowerShell, would detach the terminal the user is sitting in.
$CtrlBreak = @'
import ctypes, sys
k = ctypes.windll.kernel32
pid = int(sys.argv[1])
k.FreeConsole()
if not k.AttachConsole(pid):
    sys.exit(2)                      # no console at all: a pythonw process
HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
swallow = HANDLER(lambda t: 1)       # the event reaches the whole console;
k.SetConsoleCtrlHandler(swallow, 1)  # do not let it take this helper with it
sys.exit(0 if k.GenerateConsoleCtrlEvent(1, 0) else 3)
'@

function Stop-DevTray {
    $s = Read-State
    if (-not $s) {
        Remove-Item $statePath -ErrorAction SilentlyContinue
        return $false
    }
    $proc = Get-Process -Id $s.pid -ErrorAction SilentlyContinue
    Write-Host "--- stopping dev tray (pid $($s.pid), $($s.mode)) ---"

    $polite = $false
    if ($s.mode -eq 'console' -and $proc) {
        & $py -c $CtrlBreak $s.pid | Out-Null
        $polite = ($LASTEXITCODE -eq 0)
        if ($polite -and -not $proc.WaitForExit(15000)) { $polite = $false }
    }

    if (-not $polite) {
        # No signal reached it, so nothing ran its teardown. Kill, then repay
        # the two debts by hand: the usbip attach (which re-arms itself unless
        # it is stopped) and any Bluetooth pad left hidden.
        Write-Host '    no clean stop available -- killing, then running cleanup'
        Stop-Process -Id $s.pid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 700
        $env:PYTHONPATH = if ($env:PYTHONPATH) { "$appDir;$env:PYTHONPATH" } else { $appDir }
        # To the host, not down the pipeline: this function's value is the
        # boolean below, and cleanup's chatter would otherwise be part of it.
        & $py -m ds5app cleanup 2>&1 | ForEach-Object { Write-Host "    $_" }
    }
    Remove-Item $statePath -ErrorAction SilentlyContinue
    Write-Host '    stopped'
    return $true
}

if ($Stop) {
    if (-not (Stop-DevTray)) { Write-Host 'no dev tray is running' }
    exit 0
}

if ($Logs) {
    if (-not (Test-Path $logPath)) {
        throw "no log yet at $logPath -- only -Windowed runs write one"
    }
    Get-Content -Wait -Tail 60 $logPath
    exit 0
}

# ---------------------------------------------------------------------------
# starting it
# ---------------------------------------------------------------------------

Stop-DevTray | Out-Null
Test-InstalledTrayRunning

# `ds5app` is not installed anywhere, so the path goes in the environment
# rather than relying on where we happen to be standing. Both variables are set
# on THIS process and inherited by the child; the script exits straight after,
# so nothing leaks into the shell that called it.
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$appDir;$env:PYTHONPATH" } else { $appDir }
if ($Isolated) {
    if (-not (Test-Path $isoDir)) { New-Item -ItemType Directory -Path $isoDir | Out-Null }
    $env:DS5_CONFIG = $isoDir
    Write-Host "--- settings: $isoDir (isolated) ---"
}

$argList = @('-m', 'ds5app', 'tray') + $TrayArgs

if ($Windowed) {
    if (Test-Path $logPath) {
        Move-Item -Force $logPath (Join-Path $stateDir 'tray.prev.log')
    }
    # The log is opened by the PROGRAM, not by `Start-Process
    # -RedirectStandardOutput`. Redirecting from here means UseShellExecute=0,
    # which hands the child a set of inherited handles and leaves the calling
    # shell waiting on them -- a dev launcher that does not give the prompt
    # back is not a dev launcher. `-WindowStyle Hidden` detaches completely,
    # and the program reopens its own streams onto the file.
    #
    # It is also the same SHAPE as the real login command
    # (`autostart.build_command`): pythonw, `-c`, an injected sys.path, no
    # console. Testing the windowed path therefore tests that path too. Extra
    # arguments arrive as `sys.argv[1:]`, so nothing has to be quoted twice.
    #
    # THE PROGRAM IS QUOTED BY HAND. `-WindowStyle` means UseShellExecute, and
    # under UseShellExecute PowerShell joins -ArgumentList with spaces and
    # leaves the quoting to you -- an unquoted `-c` program arrives at python
    # cut off at its first space, which fails as a SyntaxError with no console
    # to print it on. Single quotes are used inside the program for the same
    # reason: the outer pair has to be the double ones.
    $prog = "import sys;sys.path.insert(0,r'$appDir');" +
            "sys.stdout=sys.stderr=open(r'$logPath','w',buffering=1," +
            "encoding='utf-8',errors='replace');" +
            "from ds5app.cli import main;sys.exit(main(['tray']+sys.argv[1:]))"
    $childArgs = @('-c', ('"' + $prog + '"')) + $TrayArgs
    $proc = Start-Process -FilePath $pyw -ArgumentList $childArgs -WorkingDirectory $appDir -WindowStyle Hidden -PassThru
    $mode = 'windowed'
} else {
    # Its own console window, which is also its own console: that is what lets
    # -Stop signal it without touching this terminal's process group.
    $proc = Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $appDir -PassThru
    $mode = 'console'
}

Start-Sleep -Milliseconds 1500
if ($proc.HasExited) {
    Write-Host ''
    Write-Host "it exited immediately (code $($proc.ExitCode))." -ForegroundColor Red
    if ($Windowed -and (Test-Path $logPath)) {
        Get-Content $logPath | Select-Object -Last 30
    }
    Write-Host ''
    Write-Host 'the usual causes:'
    Write-Host "  * pystray/Pillow missing   $py -m pip install pystray pillow"
    Write-Host '  * something else is bridging already'
    Write-Host "  * the machine is not set up   $py -m ds5app doctor"
    exit 1
}

Write-State $proc $mode
Write-Host ''
Write-Host "--- dev tray running (pid $($proc.Id), $mode) ---"
if ($Windowed) {
    Write-Host "    log    $logPath   (follow it: dev_tray.ps1 -Logs)"
    Write-Host '    stop   its tray menu -> Quit, or dev_tray.ps1 -Stop'
} else {
    Write-Host '    logs   in its own console window'
    Write-Host '    stop   Ctrl+C there, its tray menu -> Quit, or dev_tray.ps1 -Stop'
}
Write-Host '    edit a file, run this again -- it replaces the running one.'
