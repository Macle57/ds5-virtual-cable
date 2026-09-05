# DualSense BT Protocol — Findings

> **SUPERSEDED IN PARTS — read `STATUS.md` first.** Phase 0/1 hardware verification corrected three claims below and found two protocol bugs:
> 1. Real wired DualSense USB HID polls at **4 ms / 250 Hz** (bInterval=6 @ high speed), not 1 ms — BT at ~476 Hz is the *faster* link.
> 2. The real device has **no BOS descriptor / no MS OS string**; the MS OS 2.0 selective-suspend note was DS5Dongle's own addition, not Sony's.
> 3. USB mic input terminal is **Headset (0x0402)**, not Microphone (0x0201).
> 4. `0x36` byte `p[68]` is a **mic-active flag** — the tester's hardcoded `0xFE` kills mic streaming during playback; use `0xFF`-style active for full duplex.
> 5. `0x39` `audio_buffer_length` (pkt[5..8]) must be in **[16,128]**; out-of-range values make the controller silently discard every report.
>
> `docs/usb-ground-truth.md` holds the real wired controller's verbatim descriptors — use it, not `fake_ds5.h`, as the emulation source of truth.

Reverse-engineering notes extracted from two working implementations, both cloned as siblings of this repo:

- `../dualsense-tester` — browser WebHID app; does **everything over BT** (input, rumble, HD haptics, adaptive triggers, LEDs, speaker/headphone audio, **and mic**). Key files:
  - `src/utils/dualsense/btAudioStream.ts` — 0x36 audio/haptic report builder
  - `src/utils/dualsense/microphoneProtocol.ts` — BT mic (0x31/0x32)
  - `src/utils/dualsense/crc32.util.ts` — CRC seeds
  - `src/utils/dualsense/ds.util.ts` — 0x31 output report path
- `../DS5Dongle` — Pico 2 W firmware: BT Classic HID host <-> fake wired USB DualSense. Key files:
  - `src/audio.cpp` — Opus transcode both ways, 0x39 packet builder, 4ch USB split
  - `src/bt.cpp` — L2CAP HID (PSM 0x11 control / 0x13 interrupt), BR/EDR only
  - `src/fake_ds5.h`, `src/usb_descriptors.cpp` — the complete wired-DualSense USB identity (HID descriptors + UAC1 audio) — **descriptor source of truth**
  - `src/tusb_config.h` — audio function shape: 4ch OUT @48k/16bit, 2ch IN @48k/16bit
  - `tools/wireshark_dualsense_setstate.lua` — dissector for sniffing

## Transport

- Bluetooth **Classic (BR/EDR)** HID over L2CAP. NOT BLE, NOT A2DP. Everything is HID reports.
- Full-duplex total bandwidth < 1 Mbps; EDR gives 2–3 Mbps usable. Not a constraint.
- Controller boots in minimal input report 0x01 mode over BT; **reading feature report 0x05 (calibration) switches it to extended input report 0x31**.

## CRC32

Standard CRC-32 (zlib polynomial) over a prefix byte + report id + payload, little-endian in the last 4 bytes of every BT report:
- Output reports: prefix `0xA2`
- SET feature reports: prefix `0x53`
- (GET feature: `0xA3` per community docs; tester only uses 0xA2/0x53)

## Reports (BT)

### Input 0x31 (controller -> host), 78 bytes
Extended input: buttons, sticks, triggers, IMU, touchpad, battery. Byte 0 low nibble = payload type:
- type 0x01 = control/state payload (mute button bit at offset 10 mask 0x04; headset-mic-present at offset 54 mask 0x02 — offsets relative to HID data after report id)
- type 0x02 = **mic audio payload**: 71-byte Opus frame at offset 2

### Output 0x31 (host -> controller), 78 bytes
The "SetState" report: rumble emulation, adaptive trigger effects, lightbar, player LEDs, mute LED, volume, audio routing flags. Same field layout as the USB 0x02 report body, wrapped with seq nibble + CRC.

**The body is valid-flag driven, and that makes it MERGEABLE.** Bytes 0/1/38 are
`validFlag0/1/2`; the firmware applies a later field only when its gating bit is
present. So two SetState bodies can be folded into one without loss: OR the flag
bytes, and for each field take the newer body's value when the newer body sets
that field's flag, otherwise the older body's. The gating map (this repo's
`prototype/ds5bridge/protocol.py`, cross-checked against Linux
`drivers/hid/hid-playstation.c` and the tester's `OutputPanel.vue`):

| flag | bit | body bytes it gates |
|---|---|---|
| validFlag0 | 0 compatible vibration | 2,3 (right, left motor) |
| validFlag0 | 1 haptics select | none (mode bit) |
| validFlag0 | 2 right trigger FFB | 10..20 |
| validFlag0 | 3 left trigger FFB | 21..31 |
| validFlag0 | 4/5/6/7 hp vol / spk vol / mic vol / audio control | 4 / 5 / 6 / 7 |
| validFlag1 | 0 mic mute LED | 8 |
| validFlag1 | 1 power-save mute | 9 |
| validFlag1 | 2 lightbar control | 44,45,46 (R,G,B) |
| validFlag1 | 3 **release LEDs** | none -- hands the LEDs back to the firmware |
| validFlag1 | 4 **player indicator** | 43 (5-bit LED mask, bit0 = leftmost) |
| validFlag1 | 5 overall effect power / 7 audio control 2 | 36,37 |
| validFlag2 | 1 lightbar setup (**verified**); 0 ledBrightness (tester) | 41 (lightbarSetup), 42 (ledBrightness) |

Two disagreements found, both SETTLED on hardware 2026-09-03 (two pads over
Bluetooth, webcam-verified):

1. `validFlag2` "lightbar setup": the tester uses **bit 0**
   (`setValidFlag2(0)` in `OutputPanel.vue` for LED brightness), Linux
   `hid-playstation` uses **bit 1**
   (`DS_OUTPUT_VALID_FLAG2_LIGHTBAR_SETUP_CONTROL_ENABLE = BIT(1)`, with
   `DS_OUTPUT_LIGHTBAR_SETUP_LIGHT_OUT = BIT(1)` as the value). **Bit 1 is
   the one the pad honours** for byte 41: `flag2=0x01, setup=0x02` changed
   nothing, `flag2=0x02, setup=0x02` did, on both pads. `protocol.
   F2_LIGHTBAR_SETUP` is bit 1; bit 0 is kept as `F2_LED_BRIGHTNESS` (the
   tester's convention, unverified). The merge still gates bytes 41/42 on
   either bit, which is safe because carrying a byte forward only matters when
   some bit is set.
2. **A Bluetooth DualSense ignores every lightbar colour until a host has
   sent the lightbar-setup control once per connection.** Colour writes
   (`validFlag1` bit 2 + RGB) were applied by neither pad -- not one, not
   fifty a second -- while rumble (audible on a microphone) and the player
   LEDs (`validFlag1` bit 4 + byte 43, no setup needed) from the very same
   handle were. One `flag2=0x02, lightbarSetup=0x02` report and every colour
   after it worked; setup and colour in the SAME report work too, and the
   colour is what ends up shown. `hid-playstation` sends exactly this at
   connect (`dualsense_reset_leds`), and libScePad titles do it themselves --
   which is why "Miles Morales set the lightbar fine" had looked like proof
   the prime was unnecessary. `bridge.PRIME_LIGHTBAR_FADE_OUT` is therefore
   ON: the connect-time prime carries the setup plus `P.DEFAULT_LIGHTBAR`
   (hid-playstation's player-1 blue), so the pad is never left dark.

### Output 0x36 (host -> controller), 398 bytes — audio+haptics, 1 frame
Built in `btAudioStream.ts:buildReportSix`. Payload (index = report offset - 1):
- `[0]` = seq<<4 | flags(0)
- Control subpacket: `[1]=0x90 [2]=0x3F [3]=0xA0` (speaker: bit5 spk-vol + bit7 audio-ctl) or `0x90` (headphone: bit4 hp-vol + bit7), `[7]`=hp volume / `[8]`=spk volume, `[10]=0x09` audioControl
- Audio subpacket: `[66]=0x91 [67]=0x07 [68]=0xFE [69..73]=0x40 x5` (delay/mix), `[74]`=frame counter (u8, ++ per packet), `[75]`=route tag `0x93` speaker / `0x96` headphone, `[76]=0xC8` (opus len 200), `[77..276]` = 200-byte Opus frame
- Haptic subpacket: `[277]=0x92 [278]=0x40` (len 64), `[279..342]` = 64 bytes PCM
- last 4 bytes CRC32

### Output 0x39 (host -> controller), 547 bytes — audio+haptics, 2 frames (what DS5Dongle uses)
Built in `DS5Dongle/src/audio.cpp:audio_bt_task`:
- `pkt[0]=0x39, pkt[1]=seq<<4, pkt[2]=0x11|1<<7, pkt[3]=6, pkt[4]=0x7F` (mic on) / `0x7E` (mic off)
- `pkt[5..8]` = audio buffer length knob, `pkt[9]` = packetCounter += 2
- `pkt[10]=0x12|1<<6|1<<7, pkt[11]=64`, `pkt[12..75]` + `pkt[76..139]` = 2x 64B haptic PCM frames
- `pkt[140]=(0x13 speaker / 0x16 headphone)|1<<6|1<<7, pkt[141]=200`, `pkt[142..341]` + `pkt[342..541]` = 2x 200B Opus frames
- CRC32 tail

### Output 0x32 (host -> controller), 141 bytes — mic/audio-control companion
`microphoneProtocol.ts:buildBtMicControlReport`: `[1]=0x91 [2]=0x07 [3]=0xFF` (active) / `0xFE`, `[4..8]=0x40 [9]=seq [10]=0x92 [11]=0x40`, CRC tail. Tester sends 0x31-state + 0x32 pair to start/stop mic streaming.

## Codec parameters

### Speaker/headphone audio (host -> controller)
- Opus, 2ch, **CBR 160 kbps, 10 ms frames = exactly 200 bytes/frame**, low-delay mode (pure CELT — `application:'lowdelay'` in WebCodecs; `OPUS_APPLICATION_AUDIO` + complexity 0 also works per DS5Dongle)
- **45 kHz trick**: controller consumes at ~45 kHz while frames are encoded as nominal 48k. Tester: resample source to 45k, encode as "48k" => each frame is really 480/45000 ≈ 10.667 ms. DS5Dongle equivalently resamples 51200->48000 on input.
- Pacing: one 0x36 per ~10.667 ms, or one 0x39 per two frames (~21.3 ms).

### HD haptics
- Raw PCM **int8, stereo interleaved [L,R,...], 3000 Hz**, 64 bytes per frame (32 samples/ch)
- 3 kHz is the actuators' NATIVE rate (DS5Dongle author verified: no low-pass filter, response folds every 3 kHz). No fidelity loss vs USB.

### Mic (controller -> host)
- Opus, **mono, 71 bytes / 10 ms frame** (~57 kbps), decode at 48k. Arrives as input report 0x31 type-0x02 payloads.

## USB identity of a wired DualSense (what we must emulate)

- VID 0x054C, PID 0x0CE6 — composite device:
  - Interfaces 0-2: **UAC1 audio**: control + streaming OUT (4ch, 48 kHz, 16-bit) + streaming IN (2ch, 48 kHz, 16-bit)
  - Interface 3: HID (input 0x01 64B @ bInterval 1 ms, output 0x02, feature reports 0x05 calibration / 0x09 pairing / 0x20 firmware info, etc.)
- **USB 4ch OUT mapping**: ch0/1 = audio L/R (routed to speaker OR jack by SetState flag), ch2/3 = haptics L/R (controller internally resamples to 3 kHz)
- USB mic IN: 2ch descriptor, mono duplicated to L/R
- Windows quirk (from `usb_descriptors.cpp` comments): MS OS 2.0 descriptor opts the audio function into selective suspend so an idle audio stream doesn't block sleep.
- Sony PC-SDK games check for a **USB-connected** DualSense and its matching audio endpoint; BT-connected controllers get reduced features. Faking the wired identity is the entire point of this project.

## Factory-test feature reports 0x80 / 0x81 (Factory Info + Diagnostics)

Researched 2026-08-25 from the `daidr/dualsense-tester` sources (local clone at
`../dualsense-tester`, HEAD `6af3280`). This is what the tester's **Factory
Info** and **Diagnostics** panels actually issue, and it is the same sequence on
USB and on Bluetooth apart from the CRC.

### The request/response pair

Everything factory-related is one command channel:

| direction | report | length (data, report id excluded) | contents |
|---|---|---|---|
| host -> device | **SET feature `0x80`** | 63 (`85 80 09 28 95 3f b1 02` in the HID report descriptor) | `[0]=deviceId  [1]=actionId  [2..]=params` |
| device -> host | **GET feature `0x81`** | 63 (`85 81 09 29 95 3f b1 02`) | `[0]=deviceId  [1]=actionId  [2]=status  [3..58]=56-byte payload page` |

Offsets above are *after* the report id. The tester works on a `DataView` that
includes the report id, hence its `report.getUint8(0) === 0x81`,
`getUint8(1)===deviceId`, `getUint8(2)===actionId`, `getUint8(3)===status`,
payload at byte offset 4 for 56 bytes. `1 + 3 + 56 + 4 = 64`, i.e. on Bluetooth
the 4-byte CRC lands exactly in the last 4 bytes of the same 64-byte report --
consistent with STATUS.md §5 gotcha 6 ("feature 0x05 is 41 bytes on BOTH
transports, the CRC sits *inside* the body, it does not extend it").

`status` values (`TestStatus` in `src/utils/dualsense/ds.type.ts`):
`0` IDLE, `1` RUNNING, `2` COMPLETE (last page), `3` COMPLETE_2 (more pages
follow), `0xFF` TIMEOUT.

### USB vs Bluetooth

The only difference is the checksum, exactly as for output reports:

- **USB**: `sendFeatureReport(0x80, data)` with `data` zero-padded to 63 bytes.
  No CRC.
- **Bluetooth**: the same 63-byte buffer, then
  `fillFeatureReportChecksum(0x80, buf)` = CRC32 seeded `0x53, 0x80` over
  `buf[0..58]`, written little-endian into `buf[59..62]`. The report is *not*
  made longer; the CRC occupies the tail of the same 63 payload bytes.
  (`src/utils/dualsense/crc32.util.ts`, `ds.util.ts:sendFeatureReport`.)
  **VERIFIED on hardware 2026-08-25 — signed is accepted and answered, unsigned
  is refused outright. See "Measured on hardware" at the end of this section.**
- Reads (`0x81`, and the plain reports below) are issued identically on both
  transports; the tester never verifies the CRC that Bluetooth appends to the
  device's answer, and neither do we.

The tester decides USB vs BT purely from the HID report-descriptor size
(`checkConnectionType`: max input report 504 bits = USB, 616 bits = BT). Our
emulator presents the *wired* descriptor, so the tester takes the **USB** path
and sends `0x80` with **no CRC** -- the bridge has to add the `0x53` CRC itself
when it forwards to the real controller over Bluetooth.

### Multi-page reads

One `0x80` can be answered by several `0x81` pages of 56 bytes each. The tester
polls `0x81` in a loop, sleeping 10 ms between reads, with a 1000 ms wall-clock
cap per command (`TEST_COMMAND_TIMEOUT_MS`):

- `sendTestCommand` (`ds.util.ts`): repeat until `status == COMPLETE`,
  concatenating each `COMPLETE_2` page.
- `readPagedTestBlock` (used only for telemetry): the device answers every page
  with `COMPLETE_2` and never `COMPLETE`, then clears the `0x81` header; the
  loop stops when the header no longer echoes `deviceId/actionId`, or after
  `maxPages` (4 for a DualSense, 6 for an Edge).

**Each GET of `0x81` must reach the real controller.** Caching one answer and
replaying it would break every multi-page read.

### Command ids used by the two panels

`deviceId` values (`DualSenseTestDeviceId`): `1` SYSTEM, `2` POWER, `3` MEMORY,
`4` ANALOG_DATA, `5` TOUCH, `6` AUDIO, `7` ADAPTIVE_TRIGGER, `9` BLUETOOTH,
`0x70` TELEMETRY (a factory-test extension, not in the public enum).

**Factory Info** (`src/router/DualSense/views/_ConnectPanel/FactoryInfo.vue`):

| field | how it is read | result length |
|---|---|---|
| build time, hw info, device info, fw type, sw series, update version, SBL/main/DSP/MCU-DSP fw versions | plain **GET feature `0x20`** | 64 |
| BT patch version | plain **GET feature `0x22`**, `u32` at offset 31 | 64 |
| PCBA id (legacy) | `0x80` device `1` action `4` (`READ_PCBAID`) | 6 |
| PCBA id (new) | `0x80` device `1` action `17` (`READ_PCBAID_FULL`) | 24 |
| serial number, colour, board version | `0x80` device `1` action `19` (`READ_SERIAL_NUMBER`), Shift-JIS | 32 |
| assemble parts info | `0x80` device `1` action `21` (`READ_ASSEMBLE_PARTS_INFO`) | 32 |
| battery barcode | `0x80` device `1` action `24` (`READ_BATTERY_BARCODE`) | 32 |
| VCM barcode L / R | `0x80` device `1` action `26` / `28` | 32 each |
| unique id | `0x80` device `1` action `9` (`GET_MCU_UNIQUE_ID`) | 9 |
| BD MAC address | `0x80` device `9` action `2` (`READ_BDADR`) | 6 |
| AT serial no / motor info | `0x80` device `7` action `37` + 1 param byte (`1`=left, `2`=right) | 43 |
| touchpad id / fw version | `0x80` device `5` action `2` / `4` -- **USB only**, the tester skips these on BT because "the device does not echo the command over Bluetooth" | 8 each |
| battery voltage (mV) | `0x80` device `4` action `3` (`BATTERY`), `u16` LE | 4 |

Gating: the whole panel runs only when `fwType` (from `0x20`) is 2 or 3. The
"new traceability" branch needs `(hwInfo & 0xFFFF) >= 777 && mainFwVersion >=
65655`; otherwise only the legacy `READ_PCBAID` is used.

**Diagnostics** (`src/utils/dualsense/telemetry.util.ts`): one single command,

    0x80 -> deviceId 0x70 (TELEMETRY), actionId 1 (GET_INFO), no params
    then up to 4 pages of 0x81 (6 for an Edge)

The concatenated pages are one struct; byte 0 is a status byte and the struct
starts at byte 1. Layout (little-endian, offsets relative to the struct start):

    0    17  ASCII peripheral serial number
    17    4  total record count
    21    3  total active time (seconds, u24 in a u32)
    25    3  total charge time (seconds)
    29    3  haptic device active time (seconds)
    33    4  adaptive trigger left active time
    37    4  adaptive trigger right active time
    41    2  battery charge count          43  2  USB SDP detect count
    45    2  USB CDP detect count          47  2  USB DCP detect count
    49    2  USB Type-C 1.5 A detect       51  2  USB Type-C 3.0 A detect
    53    2  USB unknown detect            55  2  battery charger connect count
    57    1  charge voltage errors         58  1  charge temperature errors
    59    1  charge PMIC errors            60  2  battery full-charge count
    62    2  headset detect count          64  2  headphone detect count
    66    2  USB connect count             68  2  Bluetooth connect count
    70    2  Bluetooth connect timeouts    72  2  sub-CPU startup errors
    74    4  auth challenge count          78  4  auth success count
    82    4  auth fail count
    86.. 24  stick round-trip / dynamic-range counters (LX, LY, RX, RY)
    110..189 20 button press counters, 4 bytes each, in the order
             dpad U/D/L/R, triangle, cross, square, circle, L1, L2, L3,
             R1, R2, R3, touchpad touch, touchpad press, options, create,
             PS, mute

The full struct is only parsed when `updateVersion` (u16 at offset 44 of the
`0x20` report) is `> 1106`; below that the tester shows only "total active
time". The "This controller's firmware doesn't expose diagnostic counters"
message means `readPagedTestBlock` returned nothing at all -- i.e. the `0x80`
never reached the controller or `0x81` stalled, which is exactly what our
record-and-drop feature-write policy produced.

### Not used by these two panels, and deliberately still blocked here

- `0x84` SET / `0x85` GET -- "individual data verify" (`getIndividualDataVerifyStatus`).
  Commented out in the panel. Never forwarded.
- `0x09` pairing, `0x68` and the `0x60`-range Edge profile writes, and every
  `WRITE_*` / `ERASE_*` / `AGING_*` / `TEST_ACTION_BOOTLOADER_*` action id.
- `setTestCommandWithParams` is a *fire and forget* write with no result read;
  the tester uses it only for `controlWaveOut` (audio device `6`). Not a read,
  not forwarded.

### Uncertain, and why we allow it anyway

- **device `7` action `37`**: the `DualSenseTestActionId` enum gives `37` two
  names, `CYPRESS_SIGNAL_STEP_2` and `READ_TRACABILITY_INFO`. The neighbours
  make the intent clear -- `36 = WRITE_TRACABILITY_INFO`, `38 =
  ERASE_TRACABILITY_INFO` -- and the tester's only caller is named
  `type2TracabilityInfoRead` and treats the answer as read-only data. Allowed,
  but it is the one entry on the allowlist whose name is ambiguous.
- **device `5` actions `2` / `4`** (touchpad UID and version): read-only by
  name, but the tester says the controller does not answer them over Bluetooth.
  Allowed; expect a stall, which is also what real hardware does.
  **Superseded — see below: both answered fine over Bluetooth.**

### MEASURED ON HARDWARE, 2026-08-25

Everything above was read out of the tester's source. This section is what two
real controllers actually did, via `emulator/tools/factory_probe.py` (direct
hidapi) and `emulator/tools/bridge_factory_test.py` (the same reads through
`BridgeBackend`). Pads: `A0:FA:9C:0D:D8:BB` over **Bluetooth** (fw `Jul 4 2025`,
main `1.16.42`, update version `6.30`) and `D4:2F:4B:A1:48:5D` over **USB**
(fw `Sep 18 2025`, main `1.13.2`, update version `6.33`).

**The Bluetooth framing assumption was RIGHT, and it is now proven, not
extrapolated.** Three independent confirmations:

1. The controller's **Bluetooth** HID report descriptor (511 bytes, read with
   `hid.device().get_report_descriptor()`) declares

       FEATURE report 0x80  count=63  size=8
       FEATURE report 0x81  count=63  size=8

   byte for byte the same as the wired descriptor. A BT feature report is *not*
   longer than its USB twin, so there is nowhere for an appended CRC to live.
2. `send_feature_report(bytes([0x80]) + 63-byte body)` with the `0x53`-seeded
   CRC32 written little-endian into body bytes `59..62` is **accepted** (hidapi
   returns 64) and answered. The identical body **without** the CRC is
   **refused** by the HID stack — and `0x81` keeps returning the previous,
   all-zero header, so the command demonstrably never ran.
3. hidapi does **not raise** on that refusal; it returns `-1`. `bridge.py` now
   checks the return value and counts `feature_test_errors`, because a refused
   query that looks sent leaves the host polling `0x81` for a second and
   reading stale bytes. (Fixed 2026-08-25 as a direct result of this probe.)

The device's own `0x81` answer is likewise 64 bytes (id + 63) with its CRC in
the last 4 — an *unanswered* poll comes back
`81 00 00 ... 00 ee 7a c6 78`, header cleared, CRC intact. Neither the tester
nor the bridge verifies that CRC.

Other measured facts:

| thing | measured |
|---|---|
| `0x81` page payload | 56 bytes at buffer offset 4 (`63 - 3 header - 4 CRC`) |
| status byte | `2` COMPLETE for every single-page read; telemetry answers `3` COMPLETE_2 on **all four** pages and then clears the header, exactly as `readPagedTestBlock` assumes |
| telemetry pages | **4 distinct** 56-byte pages on both pads; the struct parses with the layout above and the peripheral serial matches the `READ_SERIAL_NUMBER` answer |
| poll cost | every command answered on the **first or second** `0x81` poll; nothing came close to the 1000 ms cap |
| `0x20` over BT | 64 bytes (id + 63), no CRC needed for a GET |
| `0x22` over BT | 64 bytes (id + 63); `u32` LE at offset 31 is the BT patch version (`0x19` on the BT pad, `0x05` on the USB pad). Static — prefetching it at connect is correct. It also carries the BD address at bytes 17..22 and echoes the touchpad UID (44..51) and touchpad fw version (35..42). |
| `0x05` over BT | 41 bytes, as Phase 1 already had it |

Corrections to the table earlier in this section:

- **device `5` actions `2` / `4` DO answer over Bluetooth.** The tester's
  comment is wrong, at least for this firmware: both returned 8 bytes over BT
  (`3e 7d ec 9d cd cd b4 86` and `a6 0a 11 aa 00 06 01 02`), and the same bytes
  appear inside feature `0x22`. No stall. The allowlist entries stay; the
  expectation of a stall does not.
- **device `7` action `37` (`READ_TRACABILITY_INFO`) answers, but with a
  non-zero error byte** on both pads (`0xFF` on the BT pad, `0x01` on the USB
  one), which is the "no data" path the tester's `flag` check already handles.
  It behaves as a read: it returns a status and never changes anything. The
  ambiguity noted above is unresolved by measurement, but nothing observable
  was written.
- **`READ_PCBAID` (legacy, device 1 action 4) returns all zeroes** on both
  pads; the real id comes from `READ_PCBAID_FULL`, reversed Shift-JIS —
  `32614604020950` on the USB pad, which is the value its owner read from the
  browser tester. `getPcbaIdFullString` hardcodes `isAsciiPcbaId = true`, so
  the reverse-the-string path is the only one that runs.
- **`GET_MCU_UNIQUE_ID` is firmware-dependent**: the BT pad answered
  `0xBD04703900BDF39A`, the USB pad returned error byte `0x02` (the tester
  drops the field when byte 0 is non-zero).
- **Board version** is `serial[1]`, and a real serial can have a digit outside
  `1..5` there (`F66105WA510599031` -> `6`), which the tester renders as
  "unknown". Not a parsing bug.

Cross-check against the values the pad's owner had read with the browser
tester: build time, update version `6.33`, main fw `1.13.2`, PCBA id, serial
`F66105WA510599031`, battery barcode, both VCM barcodes, BD MAC and the colour
"Starlight Blue" all matched **exactly**, and every monotonic counter had grown
(active time `1d 6h 53m` -> `1d 10h 21m 51s`, USB connects 15 -> 16, BT
connects 10 -> 13, Cross 1297 -> 2052, Square 1349 -> 1524). The parse in
`factory_probe.py` is therefore known-good against an independent
implementation, not just self-consistent.

## Machine/test setup

- Windows 11, two physical DualSense controllers available: one connected via Bluetooth, one via USB (ground truth for descriptor/behavior comparison).
