# E3 — does the attached device look like a real wired DualSense?

Side-by-side comparison of the **virtual** device (synthetic USB/IP emulator
attached through usbip-win2 0.9.7.7) against the **physical** wired DualSense,
both present on the machine at the same time. Run 2026-08-24.

> **Verdict: indistinguishable at the USB and HID layers. Exactly two
> observable differences, both in device-tree *location* metadata, neither of
> which any known controller-detection path reads.** Risk R2 is narrowed to a
> single unproven case: a Sony PC SDK title, which was not available to test.

The two instances compared:

| | virtual | physical |
|---|---|---|
| composite devnode | `USB\VID_054C&PID_0CE6\2&3B7C36A2&0&1` | `USB\VID_054C&PID_0CE6\5&24B2294E&0&2` |
| audio child | `…&MI_00\3&253044F8&0&0000` | `…&MI_00\6&3111DE1&1&0000` |
| HID child | `…&MI_03\3&253044F8&0&0003` | `…&MI_03\6&3111DE1&1&0003` |

Note on the hardware: the user swapped the two physical controllers partway
through Phase 3. The physical unit used here is the one with firmware
`Jul  4 2025 10:38:40` (formerly the Bluetooth unit), reading **0 % / charging**
over USB. It behaved normally throughout — 250.30 Hz input rate — but a
depleted battery is worth ruling out before blaming the driver for anything odd.

---

## 1. USB descriptors as Windows reads them — **byte-identical**

`prototype/tools/usb_descriptors.py` walks the hub tree with
`IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION`, i.e. it reads what the USB
stack itself has, not what we claim. Diffing its output for the two devices:

```
1c1
< ## USB device 054C:0CE6 (port 2, High speed)
---
> ## USB device 054C:0CE6 (port 1, High speed)
3c3
< - hub: `\\?\usb#root_hub30#4&123f7a67&0&0#{f18a0e88-...}`
---
> - hub: `\\?\usb#root_hub30#1&2b53a856&0&0#{f18a0e88-...}`
```

**That is the entire diff — the port number and which hub.** 114 lines of
descriptors are identical, including:

- the 18-byte device descriptor (`bcdUSB 0x0200`, `bcdDevice 0x0100`,
  `iSerialNumber = 0`);
- the full 227-byte configuration descriptor, byte for byte — all four
  interfaces, the whole UAC1 topology, both isochronous endpoints with
  `wMaxPacketSize` 392/196 and **`bInterval = 4` unmodified** (confirming
  `patch_config()` was skipped because we report High speed);
- the **289-byte HID report descriptor**, verbatim;
- string descriptors: `Sony Interactive Entertainment` /
  `DualSense Wireless Controller` / empty serial;
- **no BOS descriptor and no MS OS 1.0 string at index 0xEE** on either, matching
  `usb-ground-truth.md`.

Both enumerate at **High speed**.

## 2. Device-node properties

Identical on both (virtual = physical):

| property | value |
|---|---|
| `DEVPKEY_Device_EnumeratorName` | `USB` |
| `DEVPKEY_Device_Service` (composite) | `usbccgp` |
| `DEVPKEY_Device_DriverInfPath` | `usb.inf` |
| `DEVPKEY_Device_HardwareIds` | `USB\VID_054C&PID_0CE6&REV_0100`, `USB\VID_054C&PID_0CE6` |
| `DEVPKEY_Device_CompatibleIds` | `USB\COMPAT_VID_054C&…`, `USB\COMPOSITE` |
| `DEVPKEY_Device_Capabilities` | `132` (Removable + SurpriseRemovalOK) |
| `DEVPKEY_Device_BusReportedDeviceDesc` | `DualSense Wireless Controller` |
| `DEVPKEY_Device_DeviceDesc` | `USB Composite Device` |
| `DEVPKEY_Device_RemovalPolicy` | `3` |
| `DEVPKEY_Device_ContainerId` | **present and well-formed on both**, and **shared by the composite parent and both children** — see §3 |
| MI_00 child | Class `MEDIA`, service `usbaudio`, inf `wdma_usb.inf`, desc `USB Audio Device`, capabilities `160` |
| MI_03 child | Class `HIDClass`, service `HidUsb`, inf `input.inf`, desc `USB Input Device`, capabilities `128` |
| `DEVPKEY_Device_LocationInfo` | **present on both** (`Port_#0001.Hub_#0003` vs `Port_#0002.Hub_#0001`) |

### The two differences

**Difference 1 — `DEVPKEY_Device_LocationPaths` is absent on the virtual
device**, on the composite node and on both children.

```
virtual  composite : <ABSENT>
physical composite : PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)
                   | ACPI(_SB_)#ACPI(PC00)#ACPI(XHCI)#ACPI(RHUB)#ACPI(HS02)
virtual  MI_03     : <ABSENT>
physical MI_03     : PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)#USBMI(3)
                   | ACPI(_SB_)#…#ACPI(HS02)#USBMI(3)
```

This is the cleanest programmatic tell, exactly as Phase 2 predicted from
issue #35's USBTreeView dumps. It **cannot be faked from user mode** — location
paths are synthesised by the PnP manager from the physical bus topology, and a
root-enumerated software controller has none. Note that `LocationInfo` (the
human-readable `Port_#…Hub_#…` string) *is* present, so code that reads that
property rather than `LocationPaths` sees nothing unusual.

**Difference 2 — the parent chain terminates in a software root device.**

```
virtual : USB\VID_054C&PID_0CE6\2&3B7C36A2&0&1
       -> USB\ROOT_HUB30\1&2b53a856&0&0     (service USBHUB3)
       -> ROOT\USB\0000                      (service usbip2_ude,
                                              "USBip 3.X Emulated Host Controller")
       -> HTREE\ROOT\0

physical: USB\VID_054C&PID_0CE6\5&24B2294E&0&2
       -> USB\ROOT_HUB30\4&123f7a67&0&0      (service USBHUB3)
       -> PCI\VEN_8086&DEV_43ED&SUBSYS_381717AA&REV_11\3&11583659&1&A0
```

Note the intermediate node is a genuine `USBHUB3` root hub in both cases — the
emulated controller is only visible if something walks *two* levels up. Anything
inspecting the immediate parent sees a normal USB root hub.

Everything else about the parent chain is normal: `\Device\USBPDO-8` vs
`\Device\USBPDO-2`, `DEVPKEY_Device_Address` 1 vs 2.

## 3. Device container — the one that matters for Sony's SDK

`virtualization-options.md` §4 rejected the "virtual audio driver + separate HID
emulation" approach precisely because Sony's PC SDK locates the controller's
speaker and microphone by walking from the HID interface to sibling audio
endpoints **within the same device container**. That association works here:

| node | virtual ContainerId | physical ContainerId |
|---|---|---|
| composite | `{F24E6B53-9FAC-11F1-A013-F89E94ED0E1C}` | `{FF04CFB4-9CDF-11F1-A011-F89E94ED0E1C}` |
| MI_00 (audio) | **same** `{F24E6B53-…}` | **same** `{FF04CFB4-…}` |
| MI_03 (HID) | **same** `{F24E6B53-…}` | **same** `{FF04CFB4-…}` |

The virtual device's HID interface and its audio endpoints are in one
well-formed container, exactly like the real one. This is the structural
property the whole architecture rests on, and it holds.

## 4. HID layer

`hid.enumerate(0x054C, 0x0CE6)`:

| field | virtual | physical |
|---|---|---|
| `interface_number` | `3` | `3` |
| `serial_number` | `''` | `''` |
| `release_number` | `256` | `256` |
| `usage_page` / `usage` | `0x1` / `0x5` | `0x1` / `0x5` |
| `manufacturer_string` | `Sony Interactive Entertainment` | `Sony Interactive Entertainment` |
| `product_string` | `DualSense Wireless Controller` | `DualSense Wireless Controller` |
| path | `\\?\HID#VID_054C&PID_0CE6&MI_03#4&127b94db&0&0000#{4d1e55b2-…}` | `\\?\HID#VID_054C&PID_0CE6&MI_03#7&4b40afc&0&0000#{4d1e55b2-…}` |

`ds5bridge/device.py`'s classifier keys on exactly these fields — `MI_03` in the
path, `interface_number == 3`, empty serial — so it classifies the virtual
device as **USB**, which is the required behaviour. Both expose exactly one HID
collection.

Report descriptor: hidapi returns its 467-byte Windows reconstruction for
**both** devices (`STATUS.md` gotcha #4); the real 289-byte descriptor is
identical on both when read through the hub IOCTL (§1).

Behaviour (see `e1-results.md` §4.4): 249–250 Hz input reports with a 3.999 ms
median gap on the virtual device vs 250.13 Hz / 4.000 ms on the physical one;
feature reports `0x05` (41 B) and `0x20` (64 B) both answer with the right
lengths; an output report `0x02` written with `hid.write()` arrives at the
emulator on interrupt OUT `0x03`.

The **content** of the virtual device's feature reports is placeholder zeroes
(`SyntheticBackend` returns correctly-sized blanks), so anything that parses
calibration or firmware strings will see garbage until `BridgeBackend` proxies
the real controller's answers. That is a backend gap, not an identity gap.

## 5. Audio endpoints

| | virtual | physical |
|---|---|---|
| render | `Speakers (3- DualSense Wireless Controller)` | `Speakers (2- DualSense Wireless Controller)` |
| render format | **4 ch @ 48 kHz** | **4 ch @ 48 kHz** |
| capture | `Headset Microphone (3- DualSense Wireless Controller)` | `Headset Microphone (2- DualSense Wireless Controller)` |
| capture format | 2 ch @ 48 kHz | 2 ch @ 48 kHz |
| host APIs listing them | MME, DirectSound, WASAPI, WDM-KS | MME, DirectSound, WASAPI, WDM-KS |

Both are named after the device, both appear under every host API, both expose
the same channel counts and sample rate. The `2-`/`3-` prefixes are Windows'
standard disambiguation for two endpoints with the same name — that is what a
second *physical* DualSense would get too.

They even share a quirk: in WASAPI **shared** mode both endpoints' capture
streams are gated to digital silence by the enhancement chain (23 of 60
windows at exact zero on the physical unit; the virtual unit's steady tone
decays to zero in ~250 ms). Detail in `e1-results.md` §7.2.

## 6. What is still unproven

1. **A Sony PC SDK title has not been run.** This is the real acceptance test
   for R2 and nothing here substitutes for it. The structural precondition the
   SDK is believed to rely on — a shared, well-formed `ContainerId` linking HID
   to audio — is satisfied (§3).
2. **SDL / Windows.Gaming.Input were not exercised.** Both identify a DualSense
   by VID/PID over HID/RawInput, all of which is byte-identical here, so the
   expectation is that they work; it is an expectation, not a measurement.
3. **`dualsense-tester` (WebHID) was not run** against the virtual device.
4. **`LocationPaths` absence is unfixable from user mode.** If a specific
   consumer turns out to require it, that forces Option B (a custom UDECx
   driver) — and note that Option B would have the *same* problem, since its
   controller would also be root-enumerated. A real fix would need the device
   to hang off a physical bus.
