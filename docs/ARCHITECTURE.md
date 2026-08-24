# Virtual Wired DualSense — Architecture Plan

## Goal
A software-only equivalent of DS5Dongle: take a Bluetooth-connected DualSense and present it to Windows as a **wired USB DualSense** (VID 054C / PID 0CE6, HID + 4ch/2ch USB audio), so games with Sony PC-SDK support enable full features (HD haptics via audio, adaptive triggers, speaker/jack, mic) automatically.

```
[DualSense over BT]
      ^ HID reports over L2CAP (Windows BT stack, accessed via hid.dll)
      |
[Bridge service, user mode]
  - input 0x31 -> translate to USB 0x01 input reports
  - USB output 0x02 -> translate to BT 0x31 SetState
  - USB audio OUT 4ch: ch0/1 -> Opus 200B/10ms frames, ch2/3 -> 3kHz int8 PCM -> BT 0x39 (or 0x36)
  - BT mic Opus 71B frames -> decode -> USB audio IN
      |
      v
[Virtual USB device layer]  <- the hard part, two candidate paths
      |
      v
[Windows enumerates a "wired DualSense": HID device + audio render/capture endpoints]
```

## Virtual USB device layer — candidate paths

### Option A: usbip-win2 (vadimgrn/usbip-win2)
Ship a user-mode USB/IP *server* that emulates the DualSense device (descriptors copied from DS5Dongle `fake_ds5.h`); attach it locally through usbip-win2's signed `vhci` driver.
- + No kernel development, driver is maintained & signed by someone else
- + Whole bridge stays in user mode (debuggable, crash-safe)
- ? Isochronous endpoint emulation quality/latency over loopback — must be validated early (audio is iso!)
- ? Installation UX (certificate/driver install)

### Option B: custom UDECx (USB Device Emulation) KMDF driver
Kernel driver creating a virtual USB host controller + emulated device; bridge service talks to it via IOCTLs.
- + The "correct" native mechanism (it's what shared USB/IP stacks build on)
- - WDK build chain, test-signing mode on the dev machine, driver signing story for distribution

### Rejected
- ViGEmBus: no DualSense target, no audio, deprecated.
- Virtual audio driver + BT HID alone: controller still looks BT-connected to games; identity check fails (see FINDINGS).

## Phases

- **Phase 0 — environment + hardware survey**: toolchains present? Enumerate both controllers; dump wired controller's real descriptors/report behavior as ground truth; verify BT controller: feature 0x05 read flips to 0x31 input mode, SetState lightbar change works.
- **Phase 1 — BT bridge core (user mode, no driver)**: CLI proving the full protocol engine on this hardware: decode input @ full rate, SetState passthrough (rumble/triggers/LEDs), play WAV -> speaker + haptics via 0x39/0x36 with correct 45k/48k handling and 10.667ms pacing, record mic -> WAV. This engine is reused verbatim under either Option A or B.
- **Phase 2 — virtualization spike**: research + prototype Option A vs B; success = a hello-world virtual USB device visible in Device Manager. Any system modification (driver install, test signing, certificates) is proposed first, not executed unilaterally.
- **Phase 3 — integration**: full descriptor clone, audio pump USB<->BT, latency/jitter tuning, validation: dualsense-tester must classify the virtual device as USB; a Sony PC-SDK title must enable full features.

## Safety rails
- Never write feature reports related to pairing (0x09) or firmware to the physical controllers.
- Output reports (0x31/0x36/0x39/0x32) and reading feature 0x05/0x20 are safe — proven by the reference projects.
