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

> **PHASE 2 RESOLVED THIS: Option A was chosen.** See
> `virtualization-options.md` for the evidence, the risk register and the
> approval list, and `STATUS.md` §14 for the operational handoff. Two
> corrections to the text below: descriptors come from
> `usb-ground-truth.md` (the real device), **not** DS5Dongle's `fake_ds5.h`;
> and usbip-win2's driver is a **UDE (UDECx) client**, not a WDM `vhci`.

### Option A: usbip-win2 (vadimgrn/usbip-win2)  — CHOSEN
Ship a user-mode USB/IP *server* that emulates the DualSense device (descriptors from `usb-ground-truth.md`); attach it locally through usbip-win2's Microsoft-signed UDE driver.
- + No kernel development, driver is maintained & signed by someone else
- + Whole bridge stays in user mode (debuggable, crash-safe)
- ? Isochronous endpoint emulation quality/latency over loopback — must be validated early (audio is iso!). Phase 2 established the *preconditions* are met (high speed + iso bInterval 4 + QueryBusTime supplied by usbip2_filter.sys); the sustained-rate question is Phase 3 experiment E1.
- + Installation is one Microsoft-signed installer; no test signing, no certificate. Licence is BSD-2-Clause.

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
- **Phase 2 — virtualization spike** *(DONE, research + code only)*: chose Option A; wrote `emulator/`, a driver-free USB/IP device-emulator skeleton with 91 protocol unit tests. No system modification was made; the required changes are listed for approval in `virtualization-options.md` §7. Making a virtual device actually appear in Device Manager needs that approval and is now the first task of Phase 3.
- **Phase 3 — install, validate, integrate**: (a) approval + install usbip-win2; (b) experiment E2 (loopback reachability), then **E1, the go/no-go isochronous timing test**; (c) E3, device identity vs the physical wired controller; (d) only then the audio pump USB<->BT via `BridgeBackend`, latency/jitter tuning, and the acceptance tests: dualsense-tester must classify the virtual device as USB, and a Sony PC-SDK title must enable full features.

## Safety rails
- Never write feature reports related to pairing (0x09) or firmware to the physical controllers.
- Output reports (0x31/0x36/0x39/0x32) and reading feature 0x05/0x20 are safe — proven by the reference projects.

## Product layer (app/ds5app): tray, manager, dashboard

The tray (`ds5bridge-tray.exe`, elevated) owns one `BridgeManager`, which
spawns one `ds5bridge run` child per controller and serves the dashboard on
`http://127.0.0.1:<dashboard_port>` (default 8765). The child publishes live
input over a loopback UDP telemetry socket the tray owns; the page streams it
as Server-Sent Events. `dashboard.py`'s module docstring is the reference for
the HTTP API; the shape in one screen:

    GET  /api/state      {ts, controllers{serial: ...}, aggregate{present,
                          running, degraded, ...}, update{available, url,
                          checked_at, installing}, autostart{enabled, mode},
                          tray{version, hidhide, elevated, pid}}
    GET  /api/stream     the same, 30 Hz SSE
    GET  /api/config     {path, config}      POST /api/config  partial merge
    GET  /api/actions    picker vocabulary for the settings page
    POST /api/action     {"op": rescan | unhide_all | update_check |
                          update_install | open_logs}  ->  {ok, text}

Config keys the tray applies LIVE on every `/api/config` save, exactly as the
menu rows do: `enabled`, `controllers.<serial>.enabled`,
`controllers.<serial>.hide_bluetooth`, `hide_bluetooth_default`,
`autostart_on_login`, `update_check`. The menu re-renders from the same state.

### Presence -- who says a controller is there (`manager.poll_once`)

Three witnesses, because each one is blind somewhere:

1. **Enumeration** (`hid_enumerate`, listing only, never opening). Blind to a
   pad HidHide has cloaked unless this exe is on HidHide's whitelist -- which
   it is, but a whitelist entry is one moved folder away from being wrong.
2. **The bridge child.** It holds the pad's HID handle and reports DEGRADED
   the moment the Bluetooth link dies; a handle works through a cloak. Blind
   once the bridge is torn down.
3. **The PnP tree** (`CM_Locate_DevNode` on the pad's BTHENUM HID-service
   node, found by its address). HidHide filters the HID device set, not the
   devnode tree, so this sees a cloaked pad -- and a switched-off pad has no
   present BTHENUM node. Blind to nothing on Windows; answers "cannot say"
   elsewhere.

The HidHide journal (what WE hid) is a fourth voice with a specific job: a
cloaked pad the whitelist does not let us enumerate must not be declared
gone, or hide -> "gone" -> unhide -> "appeared" -> hide oscillates for ever.
The journal is trusted only for a pad the child is not calling DEGRADED, the
PnP tree is not calling absent, and the manager has not itself watched go
(a kept cloak, `_kept_cloaks`). A pad that was switched off therefore leaves
`aggregate.present` within `offline_grace` (12 s; 3 s when two witnesses
agree) and keeps a `present: false` row in `controllers` for the dashboard.
