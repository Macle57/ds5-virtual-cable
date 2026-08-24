# DualSense BT Protocol — Findings

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

## Machine/test setup

- Windows 11, two physical DualSense controllers available: one connected via Bluetooth, one via USB (ground truth for descriptor/behavior comparison).
