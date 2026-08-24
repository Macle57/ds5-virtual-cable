# Teardown for the Phase 3c full-system test. Idempotent, safe to run twice,
# safe to run when nothing is up. Leaves `usbip port` empty.
#
#   -X IS NOT OPTIONAL. `usbip attach` arms a background auto-re-attach, so
#   detaching without stopping it silently reacquires the device on the next
#   port the moment the emulator comes back (STATUS.md 15.5 trap 1).
#
#   usbipd IS NEVER TOUCHED. It owns port 3240; we own 3241.

$ErrorActionPreference = 'Continue'
$usbip = 'C:\Program Files\USBip\usbip.exe'

Write-Host '--- stopping the auto-re-attach ---'
& $usbip attach -X 2>&1 | Out-String | Write-Host

Write-Host '--- detaching every attached port ---'
$ports = & $usbip port 2>&1 | Out-String
Write-Host $ports
foreach ($m in [regex]::Matches($ports, 'Port\s+(\d+):')) {
    $p = [int]$m.Groups[1].Value
    Write-Host "detach -p $p"
    & $usbip detach -p $p 2>&1 | Out-String | Write-Host
}

Write-Host '--- stopping any ds5emu server ---'
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -match 'ds5emu' } |
    ForEach-Object {
        Write-Host "kill $($_.ProcessId): $($_.CommandLine)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Milliseconds 700

Write-Host '--- final state ---'
$final = & $usbip port 2>&1 | Out-String
if ($final.Trim()) { Write-Host $final } else { Write-Host 'usbip port: EMPTY (clean)' }
Get-Service usbipd | Format-List Name, Status, StartType
