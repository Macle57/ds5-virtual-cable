# Wired DualSense — USB ground truth

Everything below was read off the **physical USB-connected DualSense on this
machine**, not copied from a reference project. It is the target identity the
virtual device must reproduce.

## How this was captured

`prototype/tools/usb_descriptors.py` — enumerates USB hubs with SetupAPI
(`GUID_DEVINTERFACE_USB_HUB`) and asks each hub for its per-port descriptors via
`IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX` and
`IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION`. This is the same technique
Microsoft's USBView uses. It is **read-only and needs no driver change** — the
usbaudio and HID class drivers keep owning the device throughout.

Regenerate with:

```
prototype/.venv/Scripts/python.exe prototype/tools/usb_descriptors.py > docs/usb-ground-truth.md
```

## Summary of what must be emulated

| item | value |
|---|---|
| VID:PID | `054C:0CE6` |
| bcdUSB / bcdDevice | 0x0200 / 0x0100 |
| speed | High (480 Mbit/s) |
| configuration | 1 config, **4 interfaces**, self-powered, 500 mA |
| iface 0 | UAC1 **AudioControl** |
| iface 1 alt 1 | UAC1 **AudioStreaming OUT**, 4 ch / 48 kHz / 16-bit, EP `0x01` isochronous **adaptive**, wMaxPacketSize **392**, bInterval 4 (= 1 ms) |
| iface 2 alt 1 | UAC1 **AudioStreaming IN**, 2 ch / 48 kHz / 16-bit, EP `0x82` isochronous **async**, wMaxPacketSize **196**, bInterval 4 (= 1 ms) |
| iface 3 | **HID**, report descriptor **289 bytes**, EP `0x84` IN + EP `0x03` OUT, both interrupt, 64 B, bInterval 6 (= 4 ms) |
| BOS descriptor | **absent** |
| MS OS 1.0 string (index 0xEE) | **absent** |

### Corrections to `FINDINGS.md`

1. **HID polling is 4 ms, not 1 ms.** Both HID interrupt endpoints declare
   `bInterval = 6`, which at high speed means `2^(6-1) = 32` microframes = 4 ms,
   i.e. **250 Hz**. Confirmed by measurement: the wired controller delivers input
   reports at **250.07 Hz** (1501 reports over 6.00 s). The Bluetooth controller,
   by contrast, delivers **476.4 Hz** (3812 reports over 8.00 s) — BT is *faster*
   than USB here, which is the opposite of the usual assumption.
2. **There is no MS OS 2.0 descriptor on the real device.** `FINDINGS.md` cites a
   comment in DS5Dongle's `usb_descriptors.cpp` about opting the audio function
   into selective suspend via MS OS 2.0. That is DS5Dongle's own addition; the
   genuine Sony device exposes neither a BOS descriptor nor the 0xEE magic string.
   A clone aiming for byte-identical enumeration should omit both; a clone that
   wants Windows selective-suspend behaviour may add them, but it then differs
   from ground truth.
3. **The mic input terminal is a Headset (0x0402), not a bare Microphone (0x0201)**,
   and the 4-channel OUT terminal is a **Speaker (0x0301)** fed through one feature
   unit. See the AudioControl topology below.

### AudioControl topology (interface 0)

```
OUT path:  [1] INPUT_TERMINAL  USB Streaming (0x0101), 4 ch, chConfig 0x0033
             -> [2] FEATURE_UNIT (mute + volume)
             -> [3] OUTPUT_TERMINAL Speaker (0x0301)

IN path:   [4] INPUT_TERMINAL  Headset (0x0402), 2 ch, chConfig 0x0003
             -> [5] FEATURE_UNIT (mute + volume)
             -> [6] OUTPUT_TERMINAL USB Streaming (0x0101)
```

`chConfig 0x0033` on the 4-channel OUT terminal = L | R | LS | RS. Per FINDINGS,
channels 0/1 carry speaker/headphone audio and channels 2/3 carry the haptic
signal that the controller internally resamples down to 3 kHz.

### Bandwidth note for the virtualization layer

The OUT endpoint moves 392 B every 1 ms and the IN endpoint 196 B every 1 ms.
Any virtual-USB transport must sustain **~588 KB/s of isochronous traffic with
1 ms service intervals** — this is the single hardest requirement on the layer
chosen in Phase 2, and the reason isochronous support has to be validated before
committing to a path.

### Verified feature reports (read-only, safe)

| report | USB | BT | notes |
|---|---|---|---|
| `0x05` calibration | 41 B | 41 B | reading it on BT is what flips the controller into extended 0x31 input mode |
| `0x20` firmware info | 64 B | 64 B | USB unit build `Sep 18 2025 13:15:28`; BT unit build `Jul  4 2025 10:38:40` |

The BT unit's `0x20` payload matches DS5Dongle's hardcoded `report20[63]` in
`src/fake_ds5.h` byte for byte in its build-date prefix, confirming that block is
a genuine capture and can be replayed by the emulator.

---


## USB device 054C:0CE6 (port 2, High speed)

- hub: `\\?\usb#root_hub30#4&123f7a67&0&0#{f18a0e88-c30c-11d0-8815-00a0c906bed8}`
- manufacturer: `Sony Interactive Entertainment`
- product: `DualSense Wireless Controller`
- serial: ``

### Device Descriptor

| field | value |
|---|---|
| bcdUSB | 0x0200 |
| bDeviceClass | 0x00 (per-interface) |
| bDeviceSubClass | 0x00 |
| bDeviceProtocol | 0x00 |
| bMaxPacketSize0 | 64 |
| idVendor | 0x054C |
| idProduct | 0x0CE6 |
| bcdDevice | 0x0100 |
| iManufacturer | 1 |
| iProduct | 2 |
| iSerialNumber | 0 |
| bNumConfigurations | 1 |

<details><summary>device descriptor raw (18 bytes)</summary>

```
0000  12 01 00 02 00 00 00 40 4c 05 e6 0c 00 01 01 02
0010  00 01
```
</details>

### Configuration Descriptor

- wTotalLength: 227
- bNumInterfaces: 4
- bConfigurationValue: 1
- bmAttributes: 0xC0 (self-powered)
- bMaxPower: 500 mA

#### Interface 0 alt 0 -- class 0x01 Audio / AUDIOCONTROL, subclass 0x01, protocol 0x00, 0 endpoint(s)
- AC HEADER: bcdADC=0x0100 wTotalLength=73 streaming ifaces=[1, 2]   raw[0a 24 01 00 01 49 00 02 01 02]
- AC INPUT_TERMINAL: id=1 type=0x0101 (USB Streaming) nrChannels=4 chConfig=0x0033   raw[0c 24 02 01 01 01 06 04 33 00 00 00]
- AC FEATURE_UNIT: id=2 source=1 controlSize=1 controls=03 00 00 00 00   raw[0c 24 06 02 01 01 03 00 00 00 00 00]
- AC OUTPUT_TERMINAL: id=3 type=0x0301 (Speaker) source=2   raw[09 24 03 03 01 03 04 02 00]
- AC INPUT_TERMINAL: id=4 type=0x0402 (Headset) nrChannels=2 chConfig=0x0003   raw[0c 24 02 04 02 04 03 02 03 00 00 00]
- AC FEATURE_UNIT: id=5 source=4 controlSize=1 controls=03 00   raw[09 24 06 05 04 01 03 00 00]
- AC OUTPUT_TERMINAL: id=6 type=0x0101 (USB Streaming) source=5   raw[09 24 03 06 01 01 01 05 00]

#### Interface 1 alt 0 -- class 0x01 Audio / AUDIOSTREAMING, subclass 0x02, protocol 0x00, 0 endpoint(s)

#### Interface 1 alt 1 -- class 0x01 Audio / AUDIOSTREAMING, subclass 0x02, protocol 0x00, 1 endpoint(s)
- AS AS_GENERAL: terminalLink=1 delay=1 formatTag=0x0001   raw[07 24 01 01 01 01 00]
- AS FORMAT_TYPE: formatType=1 nrChannels=4 subframeSize=2 bitResolution=16 freqs=[48000]   raw[0b 24 02 01 04 02 10 01 80 bb 00]
- EP 0x01 OUT: Isochronous (Adaptive, Data), wMaxPacketSize=392, bInterval=4, bRefresh=0, bSynchAddress=0x00
- CS_ENDPOINT EP_GENERAL: bmAttributes=0x00 lockDelayUnits=0 lockDelay=0   raw[07 25 01 00 00 00 00]

#### Interface 2 alt 0 -- class 0x01 Audio / AUDIOSTREAMING, subclass 0x02, protocol 0x00, 0 endpoint(s)

#### Interface 2 alt 1 -- class 0x01 Audio / AUDIOSTREAMING, subclass 0x02, protocol 0x00, 1 endpoint(s)
- AS AS_GENERAL: terminalLink=6 delay=1 formatTag=0x0001   raw[07 24 01 06 01 01 00]
- AS FORMAT_TYPE: formatType=1 nrChannels=2 subframeSize=2 bitResolution=16 freqs=[48000]   raw[0b 24 02 01 02 02 10 01 80 bb 00]
- EP 0x82 IN: Isochronous (Async, Data), wMaxPacketSize=196, bInterval=4, bRefresh=0, bSynchAddress=0x00
- CS_ENDPOINT EP_GENERAL: bmAttributes=0x00 lockDelayUnits=0 lockDelay=0   raw[07 25 01 00 00 00 00]

#### Interface 3 alt 0 -- class 0x03 HID, subclass 0x00, protocol 0x00, 2 endpoint(s)
- HID descriptor: bcdHID 0x0111, country 0, type 0x22 len 289
- EP 0x84 IN: Interrupt, wMaxPacketSize=64, bInterval=6
- EP 0x03 OUT: Interrupt, wMaxPacketSize=64, bInterval=6

<details><summary>full configuration descriptor raw (227 bytes)</summary>

```
0000  09 02 e3 00 04 01 00 c0 fa 09 04 00 00 00 01 01
0010  00 00 0a 24 01 00 01 49 00 02 01 02 0c 24 02 01
0020  01 01 06 04 33 00 00 00 0c 24 06 02 01 01 03 00
0030  00 00 00 00 09 24 03 03 01 03 04 02 00 0c 24 02
0040  04 02 04 03 02 03 00 00 00 09 24 06 05 04 01 03
0050  00 00 09 24 03 06 01 01 01 05 00 09 04 01 00 00
0060  01 02 00 00 09 04 01 01 01 01 02 00 00 07 24 01
0070  01 01 01 00 0b 24 02 01 04 02 10 01 80 bb 00 09
0080  05 01 09 88 01 04 00 00 07 25 01 00 00 00 00 09
0090  04 02 00 00 01 02 00 00 09 04 02 01 01 01 02 00
00a0  00 07 24 01 06 01 01 00 0b 24 02 01 02 02 10 01
00b0  80 bb 00 09 05 82 05 c4 00 04 00 00 07 25 01 00
00c0  00 00 00 09 04 03 00 02 03 00 00 00 09 21 11 01
00d0  00 01 22 21 01 07 05 84 03 40 00 06 07 05 03 03
00e0  40 00 06
```
</details>

#### HID report descriptor, interface 3 (289 bytes) -- verbatim from the device

```
0000  05 01 09 05 a1 01 85 01 09 30 09 31 09 32 09 35
0010  09 33 09 34 15 00 26 ff 00 75 08 95 06 81 02 06
0020  00 ff 09 20 95 01 81 02 05 01 09 39 15 00 25 07
0030  35 00 46 3b 01 65 14 75 04 95 01 81 42 65 00 05
0040  09 19 01 29 0f 15 00 25 01 75 01 95 0f 81 02 06
0050  00 ff 09 21 95 0d 81 02 06 00 ff 09 22 15 00 26
0060  ff 00 75 08 95 34 81 02 85 02 09 23 95 2f 91 02
0070  85 05 09 33 95 28 b1 02 85 08 09 34 95 2f b1 02
0080  85 09 09 24 95 13 b1 02 85 0a 09 25 95 1a b1 02
0090  85 0b 09 41 95 29 b1 02 85 0c 09 42 95 29 b1 02
00a0  85 20 09 26 95 3f b1 02 85 21 09 27 95 04 b1 02
00b0  85 22 09 40 95 3f b1 02 85 80 09 28 95 3f b1 02
00c0  85 81 09 29 95 3f b1 02 85 82 09 2a 95 09 b1 02
00d0  85 83 09 2b 95 3f b1 02 85 84 09 2c 95 3f b1 02
00e0  85 85 09 2d 95 02 b1 02 85 a0 09 2e 95 01 b1 02
00f0  85 e0 09 2f 95 3f b1 02 85 f0 09 30 95 3f b1 02
0100  85 f1 09 31 95 3f b1 02 85 f2 09 32 95 0f b1 02
0110  85 f4 09 35 95 3f b1 02 85 f5 09 36 95 03 b1 02
0120  c0
```
