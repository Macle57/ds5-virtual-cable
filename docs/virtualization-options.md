# Phase 2 — Virtual USB device layer: options, evidence, recommendation

Status: **research + code, no system was modified.** Nothing was installed, no
driver loaded, no certificate imported, no `bcdedit` run, no registry written.
Everything that would need such a change is collected in §7 as an explicit
approval list.

Companion deliverable: `emulator/` — a driver-free USB/IP device-emulator
skeleton with protocol unit tests (`emulator/README.md`).

---

## 0. TL;DR

**Recommend Option A: write a user-mode USB/IP *server* that emulates the
DualSense, and attach it locally through vadimgrn/usbip-win2's signed UDE
driver.**

The decisive evidence is that the three known conditions for isochronous audio
to work over Windows' UDE stack are *already satisfied by the real DualSense's
own descriptors*, and that usbip-win2 does not touch descriptors of a
high-speed device. The gating risk is therefore not "does iso work at all"
(it does — audio adapters, headsets and webcams are on usbip-win2's
known-working list) but "does it hold 1 ms service intervals at 588 KB/s
without dropouts", which is a measurement, not an unknown-unknown.

Option B (custom UDECx driver) is *not* obviously easier or safer: it needs a
multi-GB WDK install, test-signing mode on this machine, **and a second
(bus filter) driver**, because UDECx itself does not implement `QueryBusTime`
and `USBAUDIO.SYS` requires it. There is also **no UDE sample in
microsoft/Windows-driver-samples** to start from (verified — see §3.2).

---

## 1. The requirement being tested

From `docs/usb-ground-truth.md` (read off the physical wired controller):

| endpoint | type | size | bInterval | rate |
|---|---|---|---|---|
| `0x01` OUT | isochronous, **adaptive** | 392 B | 4 | 1 ms |
| `0x82` IN | isochronous, **async** | 196 B | 4 | 1 ms |
| `0x84` IN | interrupt | 64 B | 6 | 4 ms |
| `0x03` OUT | interrupt | 64 B | 6 | 4 ms |

Device speed: **High** (`bcdUSB 0x0200`). 4 interfaces: UAC1 AudioControl,
AudioStreaming OUT (4 ch/48 k/16), AudioStreaming IN (2 ch/48 k/16), HID.

Sustained isochronous load: **392 + 196 = 588 KB/s at 1 ms intervals.**

---

## 2. Option A — usbip-win2 (vadimgrn)

Repo: <https://github.com/vadimgrn/usbip-win2>

### 2.1 What it actually is

usbip-win2 is a USB/IP **client only** — it does not contain a server. Its
kernel side is:

- `usbip2_ude.sys` — a **UDE (USB Device Emulation) class-extension client
  driver** that creates an emulated USB host controller and plugs emulated
  devices into it. It speaks the USB/IP wire protocol itself, from kernel mode,
  over **Winsock Kernel (WSK)** with `WSK_FLAG_NODELAY` (TCP_NODELAY).
- `usbip2_filter.sys` — a device-specific upper filter driver, the companion
  that patches over UDE's gaps (see §2.3).

Source: README, <https://github.com/vadimgrn/usbip-win2/blob/master/README.md>
("UDE driver is an USB/IP client", "A device-specific upper filter driver
usbip2_filter is used as companion", "Winsock Kernel NPI is used").

So **we write the server.** That is exactly the Phase-2 deliverable in
`emulator/`, and it stays entirely in user mode.

### 2.2 License

`BSD-2-Clause`. (The mission brief guessed GPLv3 — that is wrong; it is
BSD-2-Clause, which is *more* permissive and imposes no obligations on us even
if we later redistribute alongside it. We would still not be redistributing
their binaries without checking, but there is no copyleft issue.)
Source: <https://github.com/vadimgrn/usbip-win2/blob/master/LICENSE.txt>

### 2.3 Isochronous support — the load-bearing finding

**Verdict: isochronous works, and the specific failure modes that broke audio
for years are all conditions the real DualSense already satisfies.**

Issue #35, "Insights on why audio devices don't work with UDE"
(<https://github.com/vadimgrn/usbip-win2/issues/35>, opened by *nefarius*,
closed **completed** 2024-04-28) is the single best source on this. Read in
full, it establishes:

1. **UDE does support isochronous endpoints.** Iso endpoints are created
   (`endpoint_add ... Isoch Out[6] ... Interval 1`), URBs flow, and the whole
   `usbaudio.sys` → `usbccgp` → UDE path is exercised. Corroborated on OSR:
   *"Yes, UDE supports isochronous transfers **but** you need to be careful of
   certain pitfalls with the descriptors."*
   (<https://community.osr.com/discussion/293301/isochronous-endpoints-msft-ude>)

2. **Pitfall 1 — `QueryBusTime`.** `USBAUDIO.SYS` calls the USB bus interface's
   `QueryBusTime` to compare frame times. UDECx does not implement it; when it
   returns `STATUS_NOT_SUPPORTED` the audio stack falls into an endless
   `{ABORT_PIPE, SYNC_RESET_PIPE_AND_CLEAR_STALL, ISOCH OUT}` loop. nefarius'
   fix is a bus filter that fake-succeeds it with `*CurrentUsbFrame = 0`.
   **usbip-win2 ships that fix in `usbip2_filter.sys`** — we get it for free.
   (nefarius' standalone demo of the same fix, for Option B readers:
   <https://git.nefarius.at/nefarius/nssudeaudio> — "USB Bus Class Filter Driver
   for fixing UDE compatibility with USBAUDIO.SYS", explicitly labelled
   education/lab only.)

3. **Pitfall 2 — device speed.** `bozax` diagnosed it precisely
   (2024-03-12): *"udecx default set usb2.0 port status is
   HighSpeedDeviceAttached, but usb audio device is full speed device, UsbHub3
   check isoch_transfer packets number alignment is not match 64 (for High
   speed), so irp complete status USBD_STATUS_INVALID_PARAMETER, these urb
   packets is not send to usbip-ude drivers."*
   **The DualSense is genuinely High-Speed**, so this class of bug does not
   apply to us at all.

4. **Pitfall 3 — iso `bInterval`.** nefarius (2024-03-15): *"UDE (perhaps due
   to its dependency on USBHUB3) does not support 1ms polling, it **always**
   treats `bInterval` as 0.125ms intervals ... I had to set it to **at least**
   4 to get my device(s) to work."* **The DualSense declares `bInterval = 4` on
   both iso endpoints** — i.e. exactly the value that works, and at high speed
   `2^(4-1) = 8` microframes = the intended 1 ms.

5. usbip-win2 fixed the issue in commit
   [`2cee7e0`](https://github.com/vadimgrn/usbip-win2/commit/2cee7e0874e2ba84881365aa526afd7197763e68)
   ("Fix isoch out transfers").

**Descriptor rewriting — and why it does not bite us.** usbip-win2's
`patch_config()` in `drivers/ude/wsk_receive.cpp` mutates the configuration
descriptor received from the server: bulk EPs forced to `wMaxPacketSize = 512`,
interrupt EPs converted to high-speed interval encoding, and **iso EPs get
`bInterval = min(bInterval + 3, 16)`** (the correct full-speed→high-speed
conversion). Applied to our `bInterval = 4` that would give 7 → 8 ms, which
would be badly wrong. It is guarded:

```cpp
if (dev.speed() < USB_SPEED_HIGH) {
        patch_config(&d);
}
```

Our emulator reports `speed = USB_SPEED_HIGH (3)` in the `OP_REP_IMPORT`
`usbip_usb_device.speed` field, so **`patch_config` is skipped and our
descriptors are presented verbatim.** This is now a hard requirement recorded
in `emulator/ds5emu/wire.py` and covered by a unit test.

**Iso is fully implemented on the client side**, not stubbed:
`drivers/ude/device_ioctl.cpp` handles `URB_FUNCTION_ISOCH_TRANSFER` and
`..._USING_CHAINED_MDL`, repacks `USBD_ISO_PACKET_DESCRIPTOR[]` into USB/IP
`iso_packet_descriptor[]`, and `drivers/ude/wsk_receive.cpp::isoch_transfer`
parses the reply. Caps: `max_iso_packets = 1024`.

**Empirical device list.** The project wiki's "devices known to work" includes
an *Audio devices* section (Meizu Lifeme HA02 and UGREEN USB audio adapters,
C-Media CM102S+ amplifier, and headsets: Apple EarPods USB-C, Huawei CM33,
Koss CS300 USB, Microsoft LifeChat LX-6000, Sennheiser ADAPT 160T USB-C II)
and cameras (AVerMedia Live Streamer CAM 513 — an iso-IN-heavy device).
<https://github.com/vadimgrn/usbip-win2/wiki>

**Honest caveat.** Every one of those is a *passthrough* of a real remote
device, over a real network, and none of them is a 4-channel 392 B/ms OUT plus
2-channel 196 B/ms IN simultaneous stream. **No source found confirms a
fully-synthetic USB/IP server driving UDE iso audio at this rate.** That is the
top risk (§6, R1) and the reason for experiment E1 in §8.

### 2.4 Driver signing, Windows 11, install

| release | date | signing |
|---|---|---|
| `v.0.9.7.4` | 2025-10-26 | attestation signed (x64), test-signed ARM64 |
| **`v.0.9.7.5`** | 2026-01-31 | **WHLK certified for x64**; "The installer and all binaries are signed by Microsoft" (build from [OSSign](https://github.com/OSSign/vadimgrn--usbip-win2)) |
| `v.0.9.7.7` | 2026-04-21 | attestation signed; fixes "Invalid Configuration Descriptor" for low/full-speed devices, BSOD at high IRQL |
| `v.0.9.7.8` | 2026-07-04 | **carries the maintainer's own warning: "this release has a bug that can cause memory corruption and BSOD. Probably you should use previous release."** |

Source: <https://github.com/vadimgrn/usbip-win2/releases>

- **No test-signing mode is required** for the signed x64 releases. The README's
  `bcdedit /set testsigning on` step is explicitly conditional: *"Enable Windows
  Test Signing Mode **if drivers are not signed by Microsoft**"*. This is the
  single biggest practical advantage over Option B.
- Requirement: Windows 10 x64 build 18362+ / Windows 11. This host is
  Windows 11 26200 x64 → supported.
- The installer is an `.exe` (`USBip-<ver>-x64.exe`). Two side effects the
  README calls out and the user must accept:
  - **all USB 3.0 Hub devices are restarted during installation** (every USB
    device briefly disconnects);
  - a scheduled task *"USBip: detach all imported devices on system reboot or
    shutdown"* is created (0.9.7.8+).
- README strongly recommends creating a **System Restore point** first.
- Uninstall is documented (app uninstaller, or `devnode.exe remove
  ROOT\USBIP_WIN2\UDE root` + `pnputil /delete-driver ... /uninstall`).

**Recommended version to install: `0.9.7.7` (x64)** — newest release without a
maintainer-declared memory-corruption bug. `0.9.7.5` is the alternative if a
WHLK-certified (rather than attestation-signed) driver is preferred; it is
older and lacks the low/full-speed config-descriptor fix, which we do not need
anyway since we are high-speed.

### 2.5 Transport and latency

- Transport is plain TCP to port **3240**; nothing prevents the target being
  `127.0.0.1`. The driver connects from kernel mode via WSK. There is **no
  documented shared-memory/local transport** — loopback TCP is the only option.
  I found no source that states loopback is unsupported, and no source that
  states it is explicitly supported either; the code is address-agnostic.
  (Risk R3, experiment E2.)
- Latency ingredients we can reason about:
  - Windows loopback TCP RTT is typically tens of microseconds; `TCP_NODELAY`
    is set by the driver on send (`WSK_FLAG_NODELAY`), and our server must set
    it too.
  - Two receive strategies exist in the driver: *Zero Copy* (default; a
    dedicated thread per device doing two blocking WSK receives) and
    *Low Latency* (WSK event callbacks into a ring buffer, no context switch).
    Issue #173 ("Performance: per-URB receive path does 2 blocking WSK
    receives + thread wakeup; consider WSK_EVENT_RECEIVE for low-latency
    streams") was **closed completed 2026-07-15**, so the low-latency path is
    current. Which mode is used is a per-attach option (`wsk_events`).
  - The dominant latency is not the transport but `usbaudio.sys` buffering plus
    our Bluetooth 10.67 ms Opus frame quantum (Phase 1). Phase 1 already
    measured the BT side holding 93.75 fps with 0 drops.

**Message rate the server must sustain** (worst case, both audio directions
streaming + HID at full rate): `usbaudio.sys` typically batches ~10 iso packets
per URB (observed in issue #35: `TransferBufferLength 1920, NumberOfPackets
10`), so ≈100 URBs/s per iso direction, plus 250 interrupt-IN URBs/s and ~250
interrupt-OUT/s. **≈700 request/response pairs per second and ≈600 KB/s.** That
is comfortably inside CPython's reach (see `emulator/README.md` §Language
choice).

### 2.6 Device identity — will games/SDL/Sony's SDK see a plain USB device?

This is the question the whole project rests on, and there **is** direct
evidence, from the `USBTreeView` dumps vadimgrn posted in issue #35 comparing a
UDE-attached device against the same physical device:

What is **identical** between virtual and real:

- `Enumerator: USB`, `Class: USB`, `Service: usbccgp`, `Driver Inf:
  C:\WINDOWS\inf\usb.inf`
- `Device ID: USB\VID_0D8C&PID_0103\2&2B7483FE&0&1` — a normal
  `USB\VID_xxxx&PID_xxxx` devnode with a normal instance path
- `Hardware IDs: USB\VID_0D8C&PID_0103&REV_0010`, `USB\VID_0D8C&PID_0103`
- **`Container ID` is present and well-formed** (`{33b69b3a-e201-11ee-...}`)
- `Location Info: Port_#0001.Hub_#0003`
- `Capabilities: 0x84 (Removable, SurpriseRemovalOK)`
- Class drivers load and produce real child devices — in that dump,
  `usbaudio` → an `AudioEndpoint` (`Speakers (USB Sound Device)`), with the
  standard `AM_KSCATEGORY_AUDIO` / `AM_KSCATEGORY_RENDER` interfaces.

Known **tells** (differences):

1. The real device's dump has `Location IDs:` and `LocationPaths:` lines
   (`PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(4)...`). **The virtual device's dump
   has neither.** So `DEVPKEY_Device_LocationPaths` is absent — that is the
   cleanest programmatic tell.
2. The emulated host controller's own devnode is `ROOT\USBIP_WIN2\UDE`, i.e.
   the parent chain terminates in a root-enumerated software device rather than
   a PCI xHCI controller. Anything walking up the device tree can see this.
3. `usbip attach --serial TEXT` exists (0.9.7.8+) precisely because serials can
   otherwise be wrong/absent — for us that is fine, the real wired DualSense has
   **`iSerialNumber = 0`** (no serial string) anyway, which we replicate.

**Assessment (reasoned, not verified):** SDL, Windows.Gaming.Input, and Sony's
PC SDK identify a DualSense by VID/PID over HID/RawInput and distinguish wired
from Bluetooth by the *transport of the HID interface* and the input report
shape (`0x01`/64 B on USB vs `0x31`/78 B on BT — see `docs/STATUS.md` §3).
None of those need `LocationPaths`. Sony's SDK enabling "full features" hinges
on the audio endpoints belonging to the same device container as the HID
interface, and the container ID *is* correctly formed and shared across the
composite device's children.

But I have **not verified this on a DualSense**, and I have found no report of
anyone doing so. It is risk R2, and experiment E3 settles it cheaply in Phase 3.

### 2.7 Prior art: user-mode USB/IP device emulators

| project | language | notes |
|---|---|---|
| [jiegec/usbip](https://github.com/jiegec/usbip) | Rust | Library for running a USB/IP **server**, both simulating devices and sharing real ones. Examples: `hid_keyboard`, `cdc_acm_serial`, `host`. Cleanest published architecture for "emulate a device as a USB/IP server". **No isochronous support found** in its docs/examples. |
| [lcgamboa/USBIP-Virtual-USB-Device](https://github.com/lcgamboa/USBIP-Virtual-USB-Device) (also `jtornosm/…`, the older `smulikHakipod/USB-Emulation`, `Frazew/PythonUSBIP`, `shadowbq/usbip-python3`) | Python + C | The canonical "emulate a USB HID mouse over USB/IP in ~600 lines of Python" lineage, all descended from a 2014 blog post. Proves **a pure-Python USB/IP server is enough to enumerate a device on Windows** for control + interrupt traffic. All of them are **HID/control only — none implements `number_of_packets` or `iso_packet_descriptor` at all.** |
| Linux `usbip-vudc` | C (kernel) | Exports a USB *gadget* as a USB/IP server. Confirms the protocol direction we need is a first-class, supported use of USB/IP. |
| [dorssel/usbipd-win](https://github.com/dorssel/usbipd-win) | C# | A USB/IP **server for Windows** that shares *real* local devices (for WSL/Hyper-V). Not an emulator, so not usable here — but its issue [#530](https://github.com/dorssel/usbipd-win/issues/530) ("USB alternate mode setting to enable isochronous endpoint not working") is a data point that iso is the weak spot of every USB/IP implementation. |

**Conclusion on prior art: nobody has published a USB/IP device emulator with
isochronous endpoints.** We would be first. The wire format for iso is small
and well-specified (§2.8), so this is a "no reference implementation to copy"
risk, not a "protocol is undocumented" risk.

### 2.8 Wire-protocol facts extracted from usbip-win2's own headers

These are what `emulator/ds5emu/wire.py` implements, taken from
`include/usbip/{proto.h,proto_op.h,consts.h}` at
<https://github.com/vadimgrn/usbip-win2/tree/master/include/usbip> rather than
from prose:

- `USBIP_VERSION = 0x0111`; TCP port `3240`; `DEV_PATH_MAX = 256`,
  `BUS_ID_SIZE = 32`. All op-phase and header fields are **network byte order**.
- `op_common{u16 version; u16 code; u32 status}`; `OP_REQ_DEVLIST = 0x8005`,
  `OP_REP_DEVLIST = 0x0005`, `OP_REQ_IMPORT = 0x8003`, `OP_REP_IMPORT = 0x0003`.
- `usbip_usb_device` is **exactly 312 bytes** (`path[256]`, `busid[32]`,
  `busnum`, `devnum`, `speed`, `idVendor`, `idProduct`, `bcdDevice`,
  `bDeviceClass/SubClass/Protocol`, `bConfigurationValue`,
  `bNumConfigurations`, `bNumInterfaces`).
- `OP_REQ_DEVLIST` carries **no request body** — the client sends only the
  8-byte `op_common` (verified in `userspace/libusbip/src/remote.cpp`:
  `send_op_common(s, OP_REQ_DEVLIST)`).
- `OP_REQ_IMPORT` carries a 32-byte busid, and the **driver verifies the busid
  echoed back in the reply matches what it asked for** (`recv_rep_import` in
  `drivers/ude/vhci_ioctl.cpp`: *"Received busid '%s' != '%s'"*). Echo it.
- Command header is **48 bytes**: `header_basic{command, seqnum, devid,
  direction, ep}` (20 B) + a 28-byte union. `CMD_SUBMIT` fills all 28
  (`transfer_flags, transfer_buffer_length, start_frame, number_of_packets,
  interval, setup[8]`); `RET_SUBMIT` fills 20 (`status, actual_length,
  start_frame, number_of_packets, error_count`) and **8 bytes of padding**.
- `number_of_packets = -1` means non-isochronous (`number_of_packets_non_isoch`).
  The driver normalises `-1` → `0` on receive and otherwise requires
  `0 <= n <= 1024`.
- `iso_packet_descriptor{u32 offset, length, actual_length, status}`, 16 bytes.
- **CMD_SUBMIT payload layout** = `[transfer buffer, OUT only][iso descriptors,
  iso only]` — confirmed by the MDL chain built in
  `device_ioctl.cpp::prepare_wsk_buf` (header MDL → transfer-buffer MDL if OUT →
  iso MDL).
- **RET_SUBMIT payload layout** = `[transfer buffer, IN only][iso descriptors,
  iso only]`, and the driver's own comment nails the iso contract:
  *"Buffer from the server has no gaps (compacted), SUM(src->actual_length) ==
  actual_length, ... as the packet offsets are not changed there will be padding
  between the packets. To optimally use the bandwidth the padding is not
  transmitted."* Concretely, from `validate()`/`fill_isoc_data()`:
  - each returned descriptor's `offset` **must equal the offset the client
    sent** (echo it unchanged — the driver hard-fails on mismatch);
  - `actual_length <= length` per packet;
  - `RET_SUBMIT.actual_length` must equal `SUM(per-packet actual_length)` for IN
    and must be `<= TransferBufferLength`;
  - the IN data payload is **compacted** (packets concatenated with no padding);
  - if `error_count == number_of_packets` the whole URB is failed with
    `USBD_STATUS_ISOCH_REQUEST_FAILED`;
  - `start_frame` in the reply is written back into `URB.StartFrame` when the
    request had `USBD_START_ISO_TRANSFER_ASAP` — which the driver **always**
    sets (*"USBD_START_ISO_TRANSFER_ASAP is appended because
    URB_GET_CURRENT_FRAME_NUMBER is not implemented"*). So we own the frame
    counter and must return a sane, monotonically increasing value.

---

## 3. Option B — custom UDECx (USB Device Emulation) KMDF driver

### 3.1 Does UDECx support isochronous endpoints?

Yes, with the caveats in §2.3 — same evidence, since usbip-win2 *is* a UDECx
client. Extra data points specific to writing our own:

- Microsoft Q&A "UDE: Isochronous endpoints"
  (<https://learn.microsoft.com/en-us/answers/questions/689656/ude-isochronous-endpoints>,
  Bojan Janjic, 2022-01-10): a developer builds a virtual USB **audio** device
  with `UdecxEndpointTypeDynamic`; interrupt and bulk work, iso callbacks never
  fire; `USBVIEW` shows the iso pipe opened. **Microsoft never answered — the
  thread has 0 comments.** So there is no official statement of support.
- The official docs page "Write a UDE Client Driver"
  (<https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/writing-a-ude-client-driver>)
  documents `UdecxUsbEndpointCreate`, simple vs dynamic endpoints, and
  `EVT_UDECX_USB_DEVICE_ENDPOINTS_CONFIGURE`, **but never mentions
  isochronous transfers at all** — neither supporting nor excluding them.
- The `EVT_UDECX_USB_DEVICE_ENDPOINTS_CONFIGURE` ordering problem vadimgrn hit
  (issue #35, 2023-04-10: *"The kernel sets interface 1.1 and issues ISOCH
  transfers, but UDE has not called yet EVT_UDECX_USB_DEVICE_ENDPOINTS_CONFIGURE
  to set 1.1 ... I still can't comprehend a logic of
  EVT_UDECX_USB_DEVICE_ENDPOINTS_CONFIGURE"*) is a real, undocumented hazard we
  would have to solve ourselves — usbip-win2 has already solved it.

### 3.2 No sample to start from — verified

I enumerated the full file tree of `microsoft/Windows-driver-samples` (3679
files, git tree API, `main`). **There is no sample containing
`UdecxUsbEndpointCreate`, no path containing `udecx`, and the `usb/` directory
contains only** `UcmCxUcsi`, `UcmTcpciCxClientSample`, `UcmUcsiAcpiSample`,
`kmdf_enumswitches`, `kmdf_fx2`, `ufxclientsample`, `umdf2_fx2`, `usbsamp`,
`usbview`, `wdf_osrfx2_lab`. (`usbsamp` is a *host-side* generic USB driver
with iso support; `ufxclientsample` is USB **Function** (device-side
controller), a different class extension.)

The closest usable references are third-party: usbip-win2's `drivers/ude/`
itself, and nefarius' `nssudeaudio` bus filter.

### 3.3 Toolchain and system changes required

- **WDK is not installed on this machine** (`docs/STATUS.md` §2: no
  `Include\10.0.26100.0\km`, no `udecx*` libs). Installing it means either
  the WDK installer matched to SDK **10.0.26100** (build numbers must match the
  installed SDK), or the standalone **EWDK** (~15 GB per OSR community
  reports — I could not find an authoritative figure for the plain WDK, so
  treat "multi-GB" as the honest bound).
  Sources: <https://learn.microsoft.com/en-us/windows-hardware/drivers/download-the-wdk>,
  <https://community.osr.com/t/using-wdk-10-in-vs-2022/58092>
- A self-built driver is **test-signed** ⇒ `bcdedit /set testsigning on` +
  reboot on this machine, with the watermark and the security posture change
  that implies. For distribution: attestation signing needs an EV code-signing
  certificate (hundreds of USD/yr) and a Partner Center account; WHLK
  certification is heavier still. usbip-win2 solved this by going through the
  Open Source Codesigning Initiative — we would have to do the same, or ship
  test-signed and require every user to enable test signing.
- We would need **two** drivers, not one: the UDECx client *and* a bus filter
  supplying `QueryBusTime`, exactly as nefarius' `nssudeaudio` does.

### 3.4 Effort estimate

Rough, and deliberately pessimistic because kernel debugging cycles are slow
(BSOD → reboot → WinDbg):

| task | Option A | Option B |
|---|---|---|
| toolchain/env | install one signed .exe | WDK install + test signing + kernel debugging setup (2nd machine or VM strongly advised) |
| virtual bus/controller | 0 (theirs) | ~1–2 weeks (UDECx host controller + device plug-in, endpoint config callbacks) |
| iso endpoints | 0 (theirs) | ~1–3 weeks, mostly on the undocumented `ENDPOINTS_CONFIGURE` ordering and `QueryBusTime` filter |
| descriptor serving + control transfers | ours either way | ours either way |
| user↔kernel plumbing for the BT bridge | 0 (it's a TCP socket) | new IOCTL/ring-buffer interface + a user-mode service |
| crash blast radius | process exit | BSOD |
| signing for others | none | EV cert or OSSign |

**Option B is a 4–8 week kernel project with a hard signing story; Option A is
a 1–2 week user-mode project with an install-one-exe story.** Option B only
becomes attractive if Option A measurably fails the iso timing test (E1) and
the failure is attributable to the USB/IP hop rather than to UDECx itself —
and if it *is* UDECx, Option B would fail identically.

---

## 4. Option C — other approaches briefly considered

**cezanne/usbip-win (the original, WDM-based vhci).** Predecessor of
usbip-win2, unmaintained. Its issue
[#284 "usb audio class device"](https://github.com/cezanne/usbip-win/issues/284)
reports a UAC device connecting fine but *"playing music results in
discontinuous sound with glitches"*, with the client issuing `CLEAR_FEATURE`
every 10 iso packets. That is a different (WDM) driver, so it is not evidence
about usbip-win2 — but it is a fair warning that "iso enumerates" and "iso
streams cleanly" are different milestones. Also: test-signing only. **Rejected.**

**VirtualHere.** Commercial, closed-source; its Windows client is a kernel
driver that attaches devices exported by *its own* server protocol. There is no
documented device-emulation API — the server shares real hardware. Even if the
protocol were reverse-engineered, we would be depending on a proprietary,
paid, closed component with no iso guarantees. **Rejected.**

**dorssel/usbipd-win.** A USB/IP *server* for Windows, sharing real local
devices to WSL/Hyper-V. Wrong direction entirely (it needs a real device to
share; we have no real wired device to share — the whole point is that the
controller is on Bluetooth). **Rejected**, but useful as a reference for
USB/IP-on-Windows behaviour.

**Virtual audio driver (VB-CABLE / VAC / a custom APO) + separate HID
emulation.** Already rejected in `ARCHITECTURE.md`; Phase 2 confirms *why* with
a concrete mechanism: Sony's PC SDK and similar integrations locate the
controller's speaker/mic by walking from the HID device to sibling audio
endpoints **within the same device container**. A standalone virtual audio
driver has a different `ContainerId` and a different devnode parent, so the
association cannot be made. Separately, ViGEmBus has no DualSense target and is
deprecated. **Rejected.**

**Linux `usbip-vudc` / QEMU USB emulation.** Both would require the controller
and the game to live in different OS instances. **Not applicable.**

---

## 5. Recommendation

**Option A.** Build the user-mode USB/IP server (`emulator/`), attach it via
usbip-win2's Microsoft-signed UDE driver over loopback TCP.

Reasons, ranked:

1. **The iso preconditions are already met by ground truth.** High-speed device,
   iso `bInterval = 4`, and `QueryBusTime` supplied by `usbip2_filter.sys`. All
   three known killers are neutralised without us writing a line of kernel code.
2. **No test-signing, no WDK, no certificate.** A Microsoft-signed installer is
   the entire system change.
3. **Everything we build stays in user mode, in Python, next to the Phase-1
   bridge** — same process can own both the USB/IP socket and the hidapi
   handles, so the audio pump is a queue hand-off rather than an IPC design.
4. **Failure is cheap.** A bug kills a Python process; it does not bugcheck the
   machine. Iterating on descriptors is an edit + restart, not a reboot.
5. **The fallback is not lost.** The emulator's descriptor tables, control-
   transfer dispatch, HID report translation and audio framing are all
   transport-agnostic. If E1 shows the USB/IP hop cannot hold 1 ms iso, that
   work ports to a UDECx driver essentially unchanged; only the transport shell
   is rewritten.

---

## 6. Risk register

| id | risk | severity | likelihood | evidence | mitigation / how it gets settled |
|---|---|---|---|---|---|
| **R1** | **UDE + USB/IP cannot sustain 588 KB/s of iso at 1 ms without dropouts/glitches**, giving crackly speaker audio or a broken mic. | **high** — it is the whole point of the project | medium | Audio adapters/headsets/webcams are on the known-working list, but all are passthrough of real devices; the closest negative (cezanne/usbip-win #284, glitchy UAC playback) is a *different* driver. **No source either way for a synthetic server.** | **Experiment E1.** If it fails: reduce to 2-channel OUT first to halve the load; try the `wsk_events` low-latency receive mode; then re-evaluate Option B. |
| **R2** | The attached device is detectably not a real USB device and Sony's SDK / a game refuses full features. | high | low–medium | Verified identical: `USB\VID&PID` devnode, `Enumerator: USB`, `usbccgp`, well-formed `ContainerId`, class drivers load and create audio endpoints. Verified different: **no `LocationPaths` / `Location IDs`**; parent controller is `ROOT\USBIP_WIN2\UDE`. | **Experiment E3.** If a specific check fails, the tell is likely `LocationPaths`, which cannot be faked from user mode — that would force Option B. |
| **R3** | usbip-win2's kernel WSK client misbehaves against a `127.0.0.1` server (kernel→user loopback), or the UDE driver's own send path deadlocks against a slow server. | high | low | The code is address-agnostic and uses standard WSK connect; no source says loopback is unsupported, and none says it is tested. | **Experiment E2** (cheap: run the emulator, `usbip list -r 127.0.0.1`, before attaching anything). |
| **R4** | CPython jitter (GIL, Windows scheduler) makes RET_SUBMIT latency spiky enough to underrun `usbaudio.sys`. | medium | medium | Phase 1 measured the Python `Pacer` holding 93.75 fps with 0 drops over 6 s using `timeBeginPeriod(1)`. Required rate here is ~700 msg/s, well inside CPython. But that was one thread doing one job. | Measure in E1. Escape hatch: the wire layer is pure functions over `bytes`, so the hot loop can be moved to C++ without touching descriptors/HID/audio logic. |
| **R5** | usbip-win2 release quality — 0.9.7.8 has a maintainer-declared memory-corruption/BSOD bug; the project churns. | medium | medium | Release notes, verbatim. | Pin **0.9.7.7**. Create a System Restore point. Never install the newest release blind; read the notes. |
| **R6** | We must generate correct input-report CRCs / mimic USB report shapes precisely enough that `dualsense-tester` classifies the device as USB. | medium | low | `STATUS.md` §13 open question 1: `crc.verify_input_checksum()` is unvalidated. USB HID reports (`0x01`, 64 B) carry **no** CRC — CRC is a Bluetooth-transport concern — so this likely does not apply on the emulated side at all. | Confirm during Phase 3 integration; the emulator's HID path currently passes report bodies through untouched. |
| **R7** | Windows' `usbaudio.sys` may probe UAC1 controls we return wrong values for (volume MIN/MAX/RES on the two feature units), causing a mixer that behaves oddly or an endpoint that fails to start. | low–medium | medium | The real device's values were **not** captured — hub IOCTLs give descriptors, not class control responses. Our values are **assumed** (see `emulator/ds5emu/uac.py`). | Once the virtual device is up (Phase 3), capture the real controller's responses with USBPcap/Wireshark on the physical wired unit and replace the constants. |
| **R8** | The 4-channel OUT stream: Windows will expose a 4-channel render endpoint (L/R/LS/RS per `chConfig 0x0033`). Applications may downmix or refuse. The haptics live on ch2/3. | medium | medium | This is true of the *real* device too, so whatever the real one does is the target. | Compare mixer/endpoint properties side by side with the physical wired controller in Phase 3. |

---

## 7. EXACT list of system changes needing user approval

**Nothing below has been done.** Each item is copy-pasteable. Items 1–3 are the
minimum for Phase 3; items 4–5 are optional diagnostics.

### 1. Create a System Restore point (do this first)

The upstream README explicitly asks for it, because a bad USB filter driver can
make the machine hard to boot into a usable state.

```powershell
# Run in an ELEVATED PowerShell
Enable-ComputerRestore -Drive "C:\"
Checkpoint-Computer -Description "Before usbip-win2 install (ds5-virtual-usb Phase 3)" -RestorePointType MODIFY_SETTINGS
```

### 2. Install usbip-win2 0.9.7.7 (x64)

- Download page: <https://github.com/vadimgrn/usbip-win2/releases/tag/v.0.9.7.7>
- Direct asset: `USBip-0.9.7.7-x64.exe`
- Verify before running:

```powershell
# Adjust the path to wherever the browser saved it
$exe = "$env:USERPROFILE\Downloads\USBip-0.9.7.7-x64.exe"
Get-FileHash $exe -Algorithm SHA256
Get-AuthenticodeSignature $exe | Format-List Status, SignerCertificate, TimeStamperCertificate
# Expect Status = Valid and a Microsoft / OSSign signer chain.
```

- Then run the installer (it requires elevation).

**What this changes on the system — the user must accept all of it:**

| change | detail |
|---|---|
| installs 2 kernel drivers | `usbip2_ude.sys` (UDE client / emulated host controller), `usbip2_filter.sys` (device upper filter). Both attestation-signed by Microsoft in this release. |
| creates a root devnode | `ROOT\USBIP_WIN2\UDE` — a permanent virtual USB host controller in Device Manager, with 30 USB2.0 + 30 USB3.0 virtual ports by default |
| **restarts every USB 3.0 hub during install** | all USB devices briefly disconnect. Do not install during a call, a copy to a USB drive, etc. |
| creates a scheduled task | *"USBip: detach all imported devices on system reboot or shutdown"* |
| installs files | `C:\Program Files\USBip` (`usbip.exe`, `wusbip.exe` GUI, `devnode.exe`) |
| adds registry keys | `HKLM\SYSTEM\CurrentControlSet\Services\usbip2_ude\{Parameters,State}` |
| PATH | the installer offers a PATH entry (current-user only in 0.9.7.7+) |

**No `bcdedit`, no test-signing, no certificate import is required** for this
signed release. Do not enable test signing.

### 3. Allow the emulator to listen on loopback TCP 3240

The server binds `127.0.0.1:3240` only. If Windows Firewall prompts, **Cancel /
deny** — loopback needs no firewall rule. Only if it turns out to be needed:

```powershell
# ELEVATED. Loopback-only, so this should NOT be necessary. Included for completeness.
New-NetFirewallRule -DisplayName "ds5-virtual-usb USBIP loopback" -Direction Inbound -Protocol TCP -LocalPort 3240 -LocalAddress 127.0.0.1 -RemoteAddress 127.0.0.1 -Action Allow
```

Note: port 3240 conflicts with `usbipd-win` if that is ever installed. It is not
installed here (verify with `Get-Service usbipd -ErrorAction SilentlyContinue`).

### 4. (Optional, diagnostics) usbip-win2 WPP tracing

Only if E1/E2 fail and we need to see what the driver did. Requires the WDK's
`tracelog`/`tracefmt`, which we do not have — so prefer to skip. Verbose mode
also needs a reboot:

```powershell
# ELEVATED — only on request, and revert afterwards
reg.exe add HKLM\SYSTEM\CurrentControlSet\Services\usbip2_ude\Parameters\Wdf /v VerboseOn /t REG_DWORD /d 1 /f
# ... reboot, reproduce, then:
reg.exe delete HKLM\SYSTEM\CurrentControlSet\Services\usbip2_ude\Parameters\Wdf /v VerboseOn /f
```

### 5. Uninstall / rollback

```powershell
# Normal path: Settings > Apps > USBip > Uninstall
# If the uninstaller is broken (run ELEVATED, from cmd.exe for the FOR loop):
"C:\Program Files\USBip\devnode.exe" remove ROOT\USBIP_WIN2\UDE root
FOR /f %P IN ('findstr /M /L /Q:u "usbip2_filter usbip2_ude" C:\WINDOWS\INF\oem*.inf') DO pnputil.exe /delete-driver %~nxP /uninstall
rd /S /Q "C:\Program Files\USBip"
```

### NOT requested, and explicitly not needed

- `bcdedit /set testsigning on` — **no.** Only needed for unsigned/self-built
  drivers, i.e. Option B.
- Any WDK / EWDK install — **no**, unless we pivot to Option B.
- Any certificate import — **no.**
- Disabling driver signature enforcement, Secure Boot changes — **no.**

---

## 8. Cheap Phase-3 experiments to settle what research could not

Run in this order; each is designed to fail fast and cheap.

### E1 — Does UDE+USB/IP hold 1 ms isochronous at 588 KB/s? *(settles R1, R4)*

**This is the go/no-go for Option A and must be the first thing done after
install.** It does **not** need the Bluetooth controller.

1. Start `python -m ds5emu serve --backend synthetic`. The synthetic backend
   emits a 1 kHz sine on the mic IN endpoint and counts/timestamps every iso OUT
   packet it receives, writing them to a CSV.
2. `usbip.exe attach -r 127.0.0.1 -b 1-1`
3. Confirm in Device Manager / `usbview` that a `DualSense Wireless Controller`
   appears with a render endpoint and a capture endpoint.
4. Play a known 1 kHz WAV to the virtual render endpoint for 60 s.
5. **Pass criteria, measured server-side from the CSV:**
   - packets received per second = 1000 ± 1 over the whole run;
   - **zero** gaps > 2 ms between consecutive iso OUT service intervals;
   - p99 of (inter-arrival interval) < 2 ms;
   - decoded audio is a clean 1 kHz sine with no dropouts (reuse the FFT
     machinery from `prototype/tools/loopback_test.py`);
   - the mic side: record the virtual capture endpoint for 60 s and FFT it —
     the synthetic 1 kHz must come back at the right frequency with no gaps.
6. If it fails: retry with the `wsk_events` (low-latency) receive mode; then
   retry with interface 1 alt 1 declared as 2-channel to halve the OUT load. If
   both still fail, the USB/IP hop is not viable → re-open Option B.

### E2 — Does the kernel WSK client connect to a user-mode loopback server? *(settles R3)*

Trivially cheap, run before E1:

```
python -m ds5emu serve --backend synthetic     # terminal 1
usbip.exe list -r 127.0.0.1                     # terminal 2 — expect our device listed
```

If `list` works but `attach` hangs, the problem is the kernel WSK path, not the
protocol.

### E3 — Does the attached device look like a real wired DualSense? *(settles R2)*

With the virtual device attached **and the physical wired DualSense also
plugged in**, so every check is a side-by-side diff:

```powershell
# devnode properties, virtual vs real
Get-PnpDevice -PresentOnly | Where-Object InstanceId -like "USB\VID_054C&PID_0CE6*" |
  ForEach-Object {
    $_.InstanceId
    Get-PnpDeviceProperty -InstanceId $_.InstanceId |
      Where-Object KeyName -in @(
        'DEVPKEY_Device_ContainerId','DEVPKEY_Device_LocationPaths',
        'DEVPKEY_Device_LocationInfo','DEVPKEY_Device_Parent',
        'DEVPKEY_Device_BusReportedDeviceDesc','DEVPKEY_Device_Service') |
      Format-Table KeyName, Data -AutoSize
  }
```

Then:

- `prototype/tools/enum_hid.py` — must classify the virtual device as **USB**
  (path contains `MI_03`, `interface_number == 3`, empty serial) exactly like
  the physical one, and its report descriptor must be the 289-byte one.
- `python -m ds5bridge --transport USB inputs` against the virtual device —
  must decode and report ~250 Hz.
- `dualsense-tester` (the sibling repo, WebHID) — must classify it as USB.
- Audio: both a render and a capture endpoint must appear, named after the
  device, in the **same device container** as the HID interface (compare the
  `ContainerId` values from the command above).
- Finally, a Sony PC-SDK title — the real acceptance test.

Record the results in `STATUS.md` with the same discipline as Phase 0/1: what
was observed, not what was expected.

---

## 9. Sources

- usbip-win2 repo / README / releases / wiki — <https://github.com/vadimgrn/usbip-win2>,
  <https://github.com/vadimgrn/usbip-win2/releases>,
  <https://github.com/vadimgrn/usbip-win2/wiki>
- usbip-win2 issue #35, "Insights on why audio devices don't work with UDE" —
  <https://github.com/vadimgrn/usbip-win2/issues/35> (the QueryBusTime /
  device-speed / `bInterval >= 4` trio, and the iso URB traces)
- usbip-win2 commit `2cee7e0`, "Fix isoch out transfers" —
  <https://github.com/vadimgrn/usbip-win2/commit/2cee7e0874e2ba84881365aa526afd7197763e68>
- usbip-win2 sources read directly: `include/usbip/{proto.h,proto_op.h,consts.h}`,
  `drivers/ude/{device_ioctl.cpp,wsk_receive.cpp,vhci_ioctl.cpp}`,
  `userspace/libusbip/src/remote.cpp`
- usbip-win2 issue #173 (WSK low-latency receive path, closed completed 2026-07-15)
- OSR NTDEV, "Isochronous endpoints MSFT UDE" —
  <https://community.osr.com/discussion/293301/isochronous-endpoints-msft-ude>
- Microsoft Q&A, "UDE: Isochronous endpoints" (unanswered) —
  <https://learn.microsoft.com/en-us/answers/questions/689656/ude-isochronous-endpoints>
- Microsoft Learn, "Write a UDE Client Driver" —
  <https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/writing-a-ude-client-driver>
- Microsoft Learn, "Download the WDK" —
  <https://learn.microsoft.com/en-us/windows-hardware/drivers/download-the-wdk>
- nefarius, `nssudeaudio` (UDE↔USBAUDIO.SYS bus filter) —
  <https://git.nefarius.at/nefarius/nssudeaudio>
- microsoft/Windows-driver-samples — enumerated via the git tree API; no UDE sample
- jiegec/usbip (Rust USB/IP server library) — <https://github.com/jiegec/usbip>
- lcgamboa/USBIP-Virtual-USB-Device (Python/C USB/IP device emulator) —
  <https://github.com/lcgamboa/USBIP-Virtual-USB-Device>
- dorssel/usbipd-win, issue #530 (iso alternate setting) —
  <https://github.com/dorssel/usbipd-win/issues/530>
- cezanne/usbip-win, issue #284 (UAC device glitchy playback) —
  <https://github.com/cezanne/usbip-win/issues/284>
- Linux USB/IP protocol doc —
  <https://www.kernel.org/doc/html/latest/usb/usbip_protocol.html>
