# Which Windows audio/HID endpoints belong to the VIRTUAL DualSense?
#
# NEVER guess from the "2-"/"3-" prefix in the friendly name -- it moves between
# runs and between the physical and virtual units (STATUS.md 15.5 trap 2).
#
# And do not stop at the parent, or even the grandparent: BOTH controllers'
# MI_00 says `USB\VID_054C&PID_0CE6\...` one level up, and BOTH say
# `USB\ROOT_HUB30\...` two levels up, because usbip-win2's UDE emulates a root
# hub too. The distinguishing fact is FOUR levels up, at the host controller:
#
#   virtual   ROOT\USB\0000                     service usbip2_ude
#   physical  PCI\VEN_8086&DEV_43ED&...         service USBXHCI
#
# Prints a block per composite device, then a machine-readable summary.

$ErrorActionPreference = 'Continue'

function Prop($id, $key) {
    (Get-PnpDeviceProperty -InstanceId $id -ErrorAction SilentlyContinue |
        Where-Object KeyName -eq $key).Data
}

# MI_00 -> composite -> hub -> host controller
function HostControllerService($mi) {
    $composite = Prop $mi 'DEVPKEY_Device_Parent'
    $hub = Prop "$composite" 'DEVPKEY_Device_Parent'
    $ctrl = Prop "$hub" 'DEVPKEY_Device_Parent'
    return @{ composite = "$composite"; hub = "$hub"; ctrl = "$ctrl"
              svc = "$(Prop "$ctrl" 'DEVPKEY_Device_Service')" }
}

$render = $null
$capture = $null
$hidpath = $null

foreach ($d in (Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
                Where-Object InstanceId -like 'USB\VID_054C&PID_0CE6&MI_00\*')) {
    $t = HostControllerService $d.InstanceId
    $isVirtual = ($t.svc -eq 'usbip2_ude')
    Write-Host ''
    Write-Host "MI_00   $($d.InstanceId)"
    Write-Host "  composite $($t.composite)"
    Write-Host "  hub       $($t.hub)"
    Write-Host "  hostctrl  $($t.ctrl)  service=$($t.svc)"
    Write-Host "  ==> $(if ($isVirtual) { 'VIRTUAL (ours)' } else { 'physical' })"
    foreach ($c in (Prop $d.InstanceId 'DEVPKEY_Device_Children')) {
        $name = "$(Prop $c 'DEVPKEY_Device_FriendlyName')"
        Write-Host "  endpoint `"$name`""
        Write-Host "           $c"
        if ($isVirtual) {
            if ($c -like '*{0.0.0.00000000}*') { $render = $name }
            if ($c -like '*{0.0.1.00000000}*') { $capture = $name }
        }
    }
}

foreach ($d in (Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
                Where-Object InstanceId -like 'USB\VID_054C&PID_0CE6&MI_03\*')) {
    $t = HostControllerService $d.InstanceId
    $isVirtual = ($t.svc -eq 'usbip2_ude')
    Write-Host ''
    Write-Host "MI_03   $($d.InstanceId)  service=$($t.svc)"
    Write-Host "  ==> $(if ($isVirtual) { 'VIRTUAL (ours)' } else { 'physical' })"
    foreach ($c in (Prop $d.InstanceId 'DEVPKEY_Device_Children')) {
        Write-Host "  hid   $c"
        if ($isVirtual) { $hidpath = "$c" }
    }
}

Write-Host ''
Write-Host '=== VIRTUAL ENDPOINTS ==='
Write-Host "RENDER=$render"
Write-Host "CAPTURE=$capture"
Write-Host "HIDNODE=$hidpath"
