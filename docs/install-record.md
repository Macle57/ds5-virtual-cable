# System install record — 2026-08-24 (user-approved)

- System Restore point #248 "Before usbip-win2 install (ds5-virtual-usb Phase 3)" created (rate-limit reg override applied and reverted).
- usbip-win2 **0.9.7.7 x64** installed silently (Inno Setup, exit 0). SHA256 `51620FA5F9F8BE5932BC9D786DEEE557CE06D5407A99CAB490DCFAC71F185FEA`, Authenticode Valid (OSSign / Cloudyne Systems EV, GlobalSign chain).
- Verified live WITHOUT reboot: `usbip2_filter` Running, `usbip2_ude` Running, devnode `ROOT\USB\0000` "USBip 3.X Emulated Host Controller" Status OK, `usbip.exe --version` = 0.9.7.7.
- **Caveat 1 — reboot flag**: filter driver install returned 3010 (reboot required) though both drivers are running now. If USBAUDIO.SYS fails on the virtual device with QueryBusTime-like symptoms, suspect pending-reboot state BEFORE declaring iso a no-go.
- **Caveat 2 — port 3240 conflict is REAL**: `usbipd` (usbipd-win, for WSL passthrough) is installed, Running, StartType Automatic, listening dual-stack on :3240. Our emulator must use an alternate port if usbip.exe supports one, else temporarily `Stop-Service usbipd` during experiments and ALWAYS `Start-Service usbipd` after. Do not disable or uninstall it.
