# STATUS — handoff document

Last updated: end of the **Phase 3a** run (E1/E2/E3 on real hardware).
Sections 1–13 are the Phase 0/1 handoff; §14 is the Phase 2 handoff.
**Section 15 is the Phase 3a handoff — read it if you are picking up Phase 3b
(`BridgeBackend`) or anything else after the go/no-go.**

**Read this first.** It is written for an agent starting with no context beyond
this repository. Companion docs: `ARCHITECTURE.md` (the plan), `FINDINGS.md` (the
reverse-engineered protocol, with three corrections listed below),
`usb-ground-truth.md` (real descriptors captured from the wired controller),
`virtualization-options.md` (**Phase 2**: the option study, the risk register,
and the exact list of system changes that need user approval),
`install-record.md` (what was installed on this machine),
**`e1-results.md`** (Phase 3a: the isochronous go/no-go, with numbers) and
**`identity-comparison.md`** (Phase 3a: virtual vs physical device).

> **Phase 3a headline: E1 PASSED. Option A works.** The synthetic emulator
> attaches through usbip-win2, Windows binds `usbccgp` + `usbaudio` + `HidUsb`,
> and it sustains 384 kB/s out + 192 kB/s in of isochronous audio for 60 s with
> **zero underruns** and clean 1 kHz tones in both directions. **No reboot was
> needed.** Three emulator bugs were found and fixed — see §15.3, especially
> the first one, which changes how you must think about the whole design.

---

## 1. Scope of this run

Delivered so far: **Phase 0** (environment + hardware survey, USB ground truth,
BT protocol smoke test), **Phase 1** (user-mode BT bridge core in Python) and
**Phase 2** (virtualization option study + a driver-free USB/IP device-emulator
skeleton in `emulator/`).

Phase 2's conclusion, in one line: **go with Option A (usbip-win2)**. See §14
and `docs/virtualization-options.md`. One relevant fact fell out of Phase 0 and
is recorded in `usb-ground-truth.md`: the device needs **~588 KB/s of
isochronous traffic at 1 ms service intervals** (OUT 392 B/ms + IN 196 B/ms),
so isochronous support is the make-or-break question — and Phase 2 established
that the real device's own descriptors already satisfy every known precondition
for that to work over Windows' UDE stack.

## 2. Environment survey

| tool | state |
|---|---|
| Python | 3.12.5 (`C:\Users\adity\.pyenv\pyenv-win\versions\3.12.5`), pip 24.2 |
| venv | `prototype/.venv` — **gitignored, must be recreated** (see §4) |
| Visual Studio | Community 2022, 17.14.37531.7, MSVC toolset 14.44.35207 |
| Windows SDK | 10.0.26100.0 |
| **WDK** | **NOT INSTALLED** — `Include\10.0.26100.0\km` is absent and there are no `udecx*` libs anywhere under `Windows Kits`. Option B needs a WDK install first. |
| CMake | 3.30.0 |
| Ninja | present at `C:\Users\adity\.mcuxpressotools\ninja\ninja.exe` (not on PATH) |
| Rust | **NOT INSTALLED** (no `rustc`, no `cargo`) |
| git | 2.43.0.windows.1 |

Host: Windows 11 Home Single Language, 10.0.26200, x64.

## 3. Hardware on this machine

Two physical DualSense controllers, both `054C:0CE6`:

| | Bluetooth unit | USB unit |
|---|---|---|
| hidapi serial | `a0fa9c0dd8bb` (the BD address) | `''` (empty) |
| hidapi path contains | `{00001124-0000-1000-8000-00805f9b34fb}` (the HID-over-BT profile GUID) | `VID_054C&PID_0CE6&MI_03` |
| `interface_number` | `-1` | `3` |
| firmware (feature 0x20) | `Jul  4 2025 10:38:40` | `Sep 18 2025 13:15:28` |
| input report | `0x31`, **78 bytes** | `0x01`, 64 bytes |
| measured input rate | **476.4 Hz** | **250.07 Hz** |

Both expose exactly one HID collection (usage page `0x0001`, usage `0x0005`), so
on this machine there is no multi-path ambiguity — but do not rely on that, keep
using the classifier in `ds5bridge/device.py`.

Note: a second BD address `d42f4ba1485d` is *paired* in Windows but not
connected; `Get-PnpDevice` lists stale entries with `Status: Unknown`. Filter
with `-PresentOnly`, and prefer hidapi enumeration over PnP for anything real.

**The BT controller's battery reads 10% / discharging.** Audio playback drains it
fast. Charge it before long test sessions, or results will start drifting.

## 4. Getting a working environment

```powershell
cd D:\Codes\dualSense\ds5-virtual-usb
python -m venv prototype\.venv
prototype\.venv\Scripts\python.exe -m pip install hidapi numpy av sounddevice
```

- `hidapi` 0.15.0 (cython-hidapi) — provides the `hid` module.
- `av` (PyAV) 18.1.0 — Opus codec **and** swresample; see §7.
- `sounddevice` — only needed by `tools/cross_mic_test.py`.

Everything runs from `prototype/`:

```powershell
cd prototype
.venv\Scripts\python.exe -m ds5bridge <command>
```

## 5. What is VERIFIED ON HARDWARE

Each item below has an observation attached. Nothing here is inferred.

### 5.1 Enumeration and feature reads — VERIFIED
`python -m ds5bridge list` returns both controllers, reads feature `0x05`
(41 bytes on both) and `0x20` (64 bytes on both).

### 5.2 BT extended-mode flip — VERIFIED
Reading feature report `0x05` on the BT unit switches it into extended input
mode; 78-byte `0x31` reports with payload-type nibble `1` then flow continuously.
Done automatically by `DualSense.open()` when the transport is BT.

### 5.3 Input decoding — VERIFIED
`python -m ds5bridge inputs --seconds 8`

- BT: 3812 reports in 8.0 s = **476.42 Hz mean**.
- USB: 1501 reports in 6.0 s = **250.07 Hz mean** (matches `bInterval=6` at high
  speed = 4 ms).
- Sticks, triggers, d-pad, all 15 buttons, gyro, accel, both touch points,
  battery level/state, headphone/mic presence all decode sanely. The USB unit
  correctly reports `100% / charging` while plugged in; the BT unit reports
  `10% / discharging`.

### 5.4 SetState over BT — VERIFIED, and CRC enforcement PROVEN
The strongest evidence in this repo, because it is machine-observable rather than
visual. The adaptive-trigger status bytes (`at_status0/1/2`) in the *input* report
change when a trigger effect is applied:

| sent | observed `(at_status0, at_status1, at_status2)` |
|---|---|
| nothing (idle) | `(9, 9, 0)` |
| mode `0x21` feedback | `(16, 16, 17)` |
| mode `0x26` vibration | `(1, 1, 51)` |
| mode `0x05` off | `(9, 9, 0)` |
| **`0x21` with one CRC byte flipped** | **`(9, 9, 0)` — no change** |
| same `0x21` with a valid CRC | `(16, 16, 17)` immediately |

So the controller validates CRC32 and silently drops bad reports. This is the
regression test to re-run whenever the output path changes.

Lightbar, player LEDs, mute LED and rumble were all sent successfully (no write
errors) but are **visual-only and were not confirmed by an observer** — see §6.

### 5.5 Speaker audio over BT — VERIFIED by closed-loop FFT
`prototype/tools/loopback_test.py` plays a tone out of the controller speaker and
captures it on that same controller's microphone, then FFTs it.

- report `0x36`, 1000 Hz: mic peak at **1000.1 Hz**, **+56.7 dB** over the silent
  baseline.
- report `0x39`, 1500 Hz: mic peak at **1500.0 Hz**, **+65.6 dB**.

Because the test is *frequency-selective* — it checks that the peak lands on the
exact frequency commanded — broadband room noise cannot produce a false pass.

### 5.6 The 45 kHz trick — MEASURED, no longer an assumption
`prototype/tools/rate_trick_test.py`. A 1200 Hz tone:

| how the tone was generated | mic peak | ratio |
|---|---|---|
| sampled at 45000 Hz, encoded as 48000 Hz | 1200.1 Hz | 1.0001 |
| sampled at 48000 Hz, encoded as 48000 Hz | 1125.0 Hz | **0.9375** |

0.9375 = 45000/48000 exactly. The controller consumes the Opus stream at
**45000 Hz**, so the host must resample source audio to 45 kHz and hand it to a
48 kHz encoder. Confirms `FINDINGS.md`.

### 5.7 HD haptics — VERIFIED by closed-loop FFT
`prototype/tools/haptic_test.py` drives a pure sine into the `0x36` haptic
subpacket while sending a **silent** Opus frame, so any sound the mic hears can
only be the voice coils. 250 Hz drive → mic peak at **250.1 Hz**, **+25.8 dB**
and **+32.2 dB** on two runs. Confirms the int8 / stereo-interleaved / 3000 Hz
format and the 64-byte haptic subpacket.

### 5.8 Microphone capture — VERIFIED
`python -m ds5bridge mic out.wav --seconds 5` produced **499 type-0x02 payloads
in 5 s = 99.4/s** (i.e. 10 ms frames), 0 decode errors, and a 4.99 s mono 48 kHz
WAV with plausible room-noise content (peak 0.155, rms 0.0032). Arming uses the
`0x31` mic-state + `0x32` control pair from `microphoneProtocol.ts`.

### 5.9 Frame pacing — VERIFIED
The clock-driven `Pacer` holds its target closely over multi-second runs:

| case | result |
|---|---|
| `0x36`, 6 s WAV | 564 frames in 6.02 s = **93.74 fps** vs 93.75 target, **0 dropped**, 0 write errors |
| `0x39`, 6 s WAV | 282 reports in 6.02 s = **46.87 fps** vs 46.88 target, 0 dropped |
| `haptics-tone`, 2 s | 188 frames in 2.01 s = 93.71 fps, 0 dropped |

### 5.10 USB descriptor capture — VERIFIED
`prototype/tools/usb_descriptors.py` reads the complete descriptor set from the
live wired controller through USB hub IOCTLs. Output is in
`docs/usb-ground-truth.md`. Read-only; no driver was touched.

## 6. What is IMPLEMENTED BUT NOT CONFIRMED

Be honest about these — do not treat them as done.

| item | state |
|---|---|
| lightbar colour | sends cleanly, **never visually confirmed** (no observer). The `at_status` experiment proves the *transport* works, so this is very likely fine, but it is unverified. |
| player LEDs, LED brightness | same |
| mute LED (on/blink/off) | same |
| rumble (`bcVibration`) | same. Not confirmed by feel or by mic. |
| `--target headphone` | code path exercised end to end with 0 write errors, but **nothing was plugged into the 3.5 mm jack**, so no audio was confirmed on that route. The route byte differs (`0x96`/`0x16` vs `0x93`/`0x13`) and the valid-flag differs (`F0_HEADPHONE_VOLUME` vs `F0_SPEAKER_VOLUME`). |
| adaptive-trigger presets other than `feedback`/`vibration`/`off` | the parameter bytes in `cli.TRIGGER_MODES` for `weapon`, `bow`, `machine` are community values, **not validated**. Only `0x21`, `0x26` and `0x05` were checked against `at_status`. |
| USB SetState (report `0x02`) | works — used to unmute the wired controller's mic, which raised its level by ~36 dB — but only that one field combination was exercised. |
| `verify_input_checksum()` in `crc.py` | **never called and never validated.** It guesses at the input-report CRC seed by trying several. Either verify it against real input reports or delete it. |
| touchpad decode | fields decode and look sane at rest, but **no finger was placed on the touchpad**, so active-touch coordinates are unconfirmed. |

## 7. Design decisions and why

### Opus binding: PyAV
Chosen over `opuslib`. Reasons, in order:

1. PyAV's Windows wheels bundle `libopus` (`av.libs/libopus-0-*.dll`) *and*
   swresample, so nothing has to be built and no `opus.dll` has to be sourced
   separately. `opuslib` is a bare ctypes binding that ships no DLL on Windows.
2. ffmpeg's `libopus` encoder exposes exactly the knobs required:
   `application=lowdelay` (pure CELT), `frame_duration=10`, `vbr=off`, plus
   `bit_rate`. **Verified to emit exactly 200-byte packets** — every frame of
   every test run had size 200.
3. The same library decodes the 71-byte mono mic frames.

Encoder settings live in `audio.make_encoder()`. They match
`btAudioStream.ts:encodeOpusFrames` (WebCodecs `bitrateMode:'constant'`,
`application:'lowdelay'`) and `DS5Dongle/src/audio.cpp:473-481`
(`OPUS_SET_BITRATE(200*8*100)`, `OPUS_SET_VBR(false)`, 10 ms frames).

### Rate arithmetic
Everything lines up on a **10.6667 ms** frame:

```
audio    45000 Hz, 480 samples/frame  -> 10.6667 ms   (encoded as nominal 48 kHz)
haptics   3000 Hz,  32 samples/ch     -> 10.6667 ms   (64 bytes, int8 stereo)
```

So one `0x36` = one frame (93.75 reports/s) and one `0x39` = two frames
(46.875 reports/s). `audio.FRAME_MS` is the single source of truth.

### Pacing
`pacing.Pacer` is clock-driven, not sleep-driven: each tick it computes how many
frames *should* have been emitted by `elapsed / frame_ms` and emits exactly that
many, capping a burst at `max_burst=4` and dropping backlog beyond
`max_backlog=8`. `TimerResolution(1)` raises the Windows timer to 1 ms via
`timeBeginPeriod` for the duration of a playback.

## 8. Empirical gotchas — READ BEFORE TOUCHING THE I/O PATH

These each cost real debugging time.

1. **`0x36` byte `p[68]` is a mic-active flag, and the tester gets it wrong for
   full duplex.** `dualsense-tester` hardcodes `0xFE`. That is the same
   active/inactive flag report `0x32` carries in its byte `[3]` (`0xFF`/`0xFE`)
   and report `0x39` carries in `pkt[4]` (`0x7F`/`0x7E`). Observed directly: with
   `0xFE`, mic payloads dropped from ~105/s to **exactly 0** the instant playback
   started; with `0xFF` both streams ran together (316 mic payloads captured
   during playback). `build_report_36(..., mic_active=True)` is required for
   simultaneous playback and capture — which the final USB bridge will need,
   since a wired DualSense does both at once.

2. **Report `0x39`'s `audio_buffer_length` must be in [16, 128].** It was
   initially set to 4 and the controller **silently discarded every report** — no
   error, no sound, 0 write failures. DS5Dongle clamps it to that range and
   defaults to 48 (`DS5Dongle/src/config.cpp:99`). 48 is now the default here and
   `0x39` passes loopback at +65.6 dB. Out-of-range values fail invisibly, so do
   not treat "no write errors" as evidence a report was accepted.

3. **A bare `pass` spin-wait starves other Python threads.** `Pacer` originally
   spun on `while time.perf_counter() < t: pass`, which never releases the GIL.
   A concurrent PortAudio capture then returned near-silence and it looked
   exactly like a hardware failure. It now spins on `time.sleep(0)`. Any future
   spin loop in this codebase must do the same.

4. **hidapi's `get_report_descriptor()` on Windows is a reconstruction, not the
   real bytes.** It returned **467 bytes** for the wired controller; the device's
   actual HID report descriptor is **289 bytes** (captured via hub IOCTL, now in
   `usb-ground-truth.md`). They are functionally equivalent — hidapi rebuilds it
   from the parsed HIDP caps, expanding shared global items — but an emulator
   must replay the **289-byte** version. Same trap applies to the BT unit
   (hidapi said 511 bytes).

5. **`hid.write()` needs the report ID as byte 0**, and on Windows its return
   value is **not** the number of bytes sent — it returns the padded buffer size.
   Writing a 78-byte `0x31` report returns `547`, because hidapi pads every write
   to the largest output report in the descriptor (`0x39` at 547). Writing on USB
   returns 48. **Never use the return value to validate a write.**

6. **Feature `0x05` is 41 bytes on both transports.** `FINDINGS.md` implies BT
   feature reports carry a 4-byte CRC tail on top of the USB payload; in practice
   Windows reports the same 41-byte length for both, and on BT the last 4 bytes
   (`b0 3d a5 24`) look like the CRC occupying space *within* the same 40-byte
   report body rather than extending it.

7. **The controller's own mic is the best available instrument.** It is loud
   enough to hear the speaker (+56 dB) and the voice coils (+26 dB), and it is
   frequency-selective, so ambient noise cannot fake a pass.

8. **Cross-controller acoustic testing did NOT work in this room** — see §9.

## 9. Failed / inconclusive experiment: cross-controller mic

`prototype/tools/cross_mic_test.py` plays a tone from the BT controller and
listens on the *wired* controller's UAC1 mic via WASAPI, to rule out the
theoretical possibility that same-controller loopback is internal crosstalk
rather than real sound.

**It did not produce a usable result and should be treated as inconclusive, not
as a negative.** Findings:

- The wired controller's mic boots gated at about **-98 dBFS**. Sending a USB
  SetState with `micVolume` + `audioControl` + `powerSaveMuteControl=0x0F` lifts
  it by roughly **36 dB**. `enable_wired_mic()` in that tool does this; it is
  reusable and correct.
- Even then, ambient room noise drifted by **13 dB or more between consecutive
  seconds**, swamping the signal. A gated ON/OFF pattern inside one continuous
  recording was added to cancel drift, and the ON windows did read up to +28 dB
  above OFF — but the extra energy sat in a **6–8 kHz band that did not track the
  played frequency at all** (playing 1000 Hz and 3000 Hz both produced 6–8 kHz
  spikes; playing *silence* at the same packet rate produced +11.7 dB in the same
  band). That is ambient, not signal.
- Conclusion: the DualSense speaker is too quiet and this room too noisy for
  across-the-desk acoustic measurement. The same-controller loopback stays the
  load-bearing evidence precisely because it is frequency-selective.

Anyone retrying this should use a quiet room and put the controllers in contact.

## 10. Corrections to FINDINGS.md discovered this run

All three are evidence-backed and also recorded in `usb-ground-truth.md`:

1. **USB HID polling is 4 ms / 250 Hz, not 1 ms.** Both HID interrupt endpoints
   declare `bInterval = 6`, which at high speed is `2^(6-1) = 32` microframes =
   4 ms. Measured 250.07 Hz. Bluetooth runs at 476 Hz — **BT is the faster link**,
   the opposite of the usual assumption.
2. **The real device has no BOS descriptor and no MS OS 1.0 string at index
   0xEE.** The MS OS 2.0 selective-suspend descriptor mentioned in FINDINGS is
   DS5Dongle's own addition, not something Sony ships.
3. **The mic input terminal is Headset (`0x0402`), not Microphone (`0x0201`)**,
   and the 4-channel OUT terminal is Speaker (`0x0301`) behind one feature unit.

## 11. File map

```
docs/
  ARCHITECTURE.md          the phased plan (unchanged)
  FINDINGS.md              BT protocol notes (see §10 for corrections)
  STATUS.md                this file
  usb-ground-truth.md      real descriptors from the wired controller + how to regenerate
  virtualization-options.md  PHASE 2: option study, risk register, approval list
emulator/                  PHASE 2: user-mode USB/IP device emulator (see §14)
  README.md
  ds5emu/{wire,descriptors,uac,device,backend,server,__main__}.py
  tests/{test_wire,test_descriptors,test_device,test_server}.py
samples/                   gitignored (`*.wav`); regenerate the test signal with
                           `-m ds5bridge gen-wav ../samples/test-sweep.wav`, which
                           is deterministic, so nothing is lost by not committing it
prototype/
  .venv/                   gitignored, recreate per §4
  ds5bridge/
    __init__.py
    __main__.py            `python -m ds5bridge`
    cli.py                 all subcommands
    crc.py                 seeded CRC32 (0xA2 output / 0x53 set-feature)
    protocol.py            SetState body + valid flags, BT 0x31 wrapper,
                           0x36 / 0x39 / 0x32 builders, input decoding + offsets
    device.py              hidapi wrapper, BT/USB classification, extended-mode flip
    pacing.py              Pacer, RateMeter, TimerResolution
    audio.py               WAV I/O, resampling, Opus encode/decode, haptics, test-signal gen
  tools/
    enum_hid.py            enumerate + classify, dump HID descriptors and features
    usb_descriptors.py     USB hub IOCTL descriptor dumper (USBView technique)
    loopback_test.py       speaker -> own mic FFT proof (0x36 and 0x39)
    rate_trick_test.py     measures the 45 kHz consumption rate
    haptic_test.py         actuators -> own mic FFT proof
    cross_mic_test.py      BT speaker -> wired controller's mic (inconclusive, see §9)
```

## 12. CLI reference

All from `prototype/`, via `.venv\Scripts\python.exe -m ds5bridge <cmd>`.
`--transport BT|USB` is a **global** flag and goes *before* the subcommand.
`play`, `haptics-tone` and `mic` default to the BT controller.

```powershell
# enumerate both controllers, read feature 0x05 and 0x20
-m ds5bridge list

# live input decode + rate measurement (add --raw to hexdump instead)
-m ds5bridge --transport BT inputs --seconds 8 --print-hz 2
-m ds5bridge --transport USB inputs --seconds 6

# outputs; --hold N keeps the process alive N seconds then clears rumble/triggers
-m ds5bridge --transport BT setstate --lightbar 00FF80 --player 0b01110 `
    --brightness 0 --mute-led blink --trigger-right feedback --rumble 80,80 --hold 3

# generate the sweep+beats test WAV
-m ds5bridge gen-wav ../samples/test-sweep.wav --seconds 6

# WAV -> speaker + haptics
-m ds5bridge play ../samples/test-sweep.wav --target speaker --volume 120
-m ds5bridge play ../samples/test-sweep.wav --report 39          # two frames per report
-m ds5bridge play ../samples/test-sweep.wav --no-audio --haptic-gain 4   # haptics only

# pure sine into the actuators (3 kHz native rate, so >1500 Hz folds)
-m ds5bridge haptics-tone 160 240 --seconds 2

# capture the controller mic
-m ds5bridge mic ../samples/mic-capture.wav --seconds 5
```

Verification harnesses (run from `prototype/tools/`, they self-insert the parent
on `sys.path`):

```powershell
..\.venv\Scripts\python.exe loopback_test.py --freq 1000 --seconds 3 --volume 140
..\.venv\Scripts\python.exe loopback_test.py --freq 1500 --report 39 --volume 140
..\.venv\Scripts\python.exe loopback_test.py --freq 1000 --mic-off   # shows the p[68] bug
..\.venv\Scripts\python.exe rate_trick_test.py --freq 1200
..\.venv\Scripts\python.exe haptic_test.py
..\.venv\Scripts\python.exe ..\tools\usb_descriptors.py --all        # every USB device
```

## 13. Open questions for the next agent

1. **Is the input-report CRC seed `0xA1`?** `crc.verify_input_checksum()` guesses.
   Confirm it against captured `0x31` input reports, or remove the function. The
   USB bridge will need to *generate* correct input CRCs if anything downstream
   validates them.
2. **Headphone routing is unverified.** Plug something into the 3.5 mm jack and
   re-run `play --target headphone`. Also worth checking whether the controller
   auto-switches when `status1 & 0x01` (headphone present) goes high, and whether
   the host is expected to follow.
3. **Adaptive-trigger parameter encodings** beyond `0x21`/`0x26`/`0x05` are
   unvalidated. `at_status0/1/2` in the input report is a cheap oracle — extend
   the §5.4 experiment to cover the rest.
4. **Simultaneous full duplex at full rate is only partly exercised.** Playback +
   mic capture ran together for ~3 s at a time in the loopback tests. The real
   bridge must sustain playback, mic capture, input polling and SetState
   concurrently, indefinitely. Threading model and whether one hidapi handle can
   serve all of it under load is untested — `DualSense` has a write lock but
   reads are unsynchronised.
5. **No SetState is sent periodically.** A real host sends SetState continuously.
   Whether the controller times out effects (rumble/triggers) without refresh is
   unknown.
6. **Battery.** The BT unit is at 10%. Some flakiness late in a session may be
   power-related rather than protocol-related.
7. **`0x36` vs `0x39` for the final bridge.** Both work. `0x36` gives 10.67 ms
   granularity (lower latency, 93.75 reports/s); `0x39` halves the report rate at
   21.33 ms granularity. DS5Dongle chose `0x39`. Latency vs BT airtime has not
   been measured here.

*(All seven are still open. Phase 2 did not touch the hardware.)*

---

# 14. PHASE 2 HANDOFF — virtualization layer

Written for an agent starting Phase 3 with no context but this repo.
The full study, with URLs, is `docs/virtualization-options.md`. This section is
the operational summary.

## 14.1 What this run did and did not do

Did: web research, source reading of vadimgrn/usbip-win2, and wrote
`emulator/` with 91 passing unit tests.

Did **not**: touch the hardware, install anything, load a driver, run
`bcdedit`, write a registry key, create a service, or attach a virtual device.
**No system change was made.** Everything that needs one is in
`virtualization-options.md` §7, ready to copy-paste, and is still un-run.

The two physical controllers were not used at all this run.

## 14.2 The decision: Option A (usbip-win2)

Write a **user-mode USB/IP server** that emulates the DualSense; attach it
locally through usbip-win2's Microsoft-signed UDE driver over loopback TCP.

The reasoning that actually decided it — and the single most useful thing
carried out of this run:

**Isochronous audio over Windows' UDE stack fails for exactly three reasons,
and the real DualSense already avoids all three.** From usbip-win2 issue #35
(nefarius + vadimgrn + bozax, closed completed 2024-04-28):

| known killer | our situation |
|---|---|
| device presented as full-speed while UDE/USBHUB3 treats the port as high-speed → `USBD_STATUS_INVALID_PARAMETER` on every iso URB | the DualSense **is genuinely High-Speed** (`bcdUSB 0x0200`) |
| iso `bInterval < 4` — UDE always interprets bInterval as 125 µs microframes | the DualSense declares **`bInterval = 4`** on both iso endpoints |
| `USBAUDIO.SYS` needs `QueryBusTime`, which UDECx does not implement → endless `ABORT_PIPE`/`SYNC_RESET_PIPE_AND_CLEAR_STALL` loop | usbip-win2 ships `usbip2_filter.sys`, which implements it |

Plus: usbip-win2's `patch_config()` rewrites iso `bInterval` to
`min(bInterval + 3, 16)` — which would wreck us — but **only for devices
reporting below `USB_SPEED_HIGH`**. We report high speed, so our descriptors
pass through verbatim. This is asserted by a unit test
(`tests/test_device.py::IdentityTests`), because it is the kind of thing a
future refactor could silently break.

Secondary reasons: signed driver (no test-signing, no WDK, no certificate);
everything stays in user mode in the same process as the Phase-1 bridge;
a bug is a process crash, not a bugcheck; and the emulator's descriptor tables
and control-transfer logic port unchanged to Option B if Option A fails.

Option B (custom UDECx driver) would need a multi-GB WDK, test-signing mode,
**two** drivers (the UDE client plus a QueryBusTime bus filter), and there is
**no UDE sample in microsoft/Windows-driver-samples** to start from — verified
by enumerating that repo's whole file tree. Estimated 4–8 weeks vs 1–2.

## 14.3 What is VERIFIED vs ASSUMED

**Verified by reading primary sources** (usbip-win2's own C++, its issue
tracker, its release notes, Microsoft Learn, the git tree of
Windows-driver-samples) — every claim in `virtualization-options.md` carries a
URL:

- usbip-win2 is a USB/IP **client only**; we must write the server.
- Its licence is **BSD-2-Clause**, not GPLv3 as the Phase-2 brief guessed.
- Releases 0.9.7.5 (WHLK certified) and 0.9.7.7 (attestation signed) are signed
  by Microsoft. **Test signing is not required.** 0.9.7.8 carries the
  maintainer's own memory-corruption/BSOD warning — do not install it.
- Isochronous is fully implemented client-side (`URB_FUNCTION_ISOCH_TRANSFER`,
  iso descriptor repacking, `max_iso_packets = 1024`).
- The complete iso wire contract (compaction, offset echoing, `start_frame`,
  `error_count` semantics) — extracted from `wsk_receive.cpp` and
  `device_ioctl.cpp` and implemented in `emulator/ds5emu/`.
- USB audio adapters, headsets and webcams are on usbip-win2's known-working
  device list.

**Verified by execution on this machine:**

- 91 unit tests pass (`emulator/`, ~1.3 s, stdlib only, no driver).
- The descriptor tables are byte-identical to the hexdumps in
  `docs/usb-ground-truth.md` — the test re-parses that markdown file, so the two
  cannot drift.

**ASSUMED / UNVERIFIED — treat as open:**

1. **Nobody has published a USB/IP device emulator with isochronous
   endpoints.** Every prior-art project found (jiegec/usbip in Rust, the
   lcgamboa/PythonUSBIP lineage) is HID/control only. All the known-working
   audio devices are *passthrough of real hardware*. We would be first, and no
   source says whether a synthetic server can hold 1 ms iso at 588 KB/s. **This
   is the top risk (R1).**
2. **Loopback**: no source confirms or denies that usbip-win2's kernel WSK
   client works against a `127.0.0.1` user-mode server. The code is
   address-agnostic. (R3.)
3. **Identity**: a UDE-attached device gets a normal `USB\VID&PID` devnode, the
   `USB` enumerator, `usbccgp`, a well-formed `ContainerId`, and real class
   drivers — all confirmed from USBTreeView dumps in issue #35. But it has
   **no `LocationPaths` / `Location IDs`**, and its parent controller is
   `ROOT\USBIP_WIN2\UDE`. Whether Sony's PC SDK cares is **not known**. (R2.)
4. **UAC1 volume MIN/MAX/RES** in `emulator/ds5emu/uac.py` are plausible
   defaults, **not captured from the physical controller** — hub IOCTLs return
   descriptors, not class-request responses. (R7.)
5. `emulator/ds5emu/backend.py::BridgeBackend` is a **stub that raises
   `NotImplementedError`**. It has never run.

## 14.4 `emulator/` — what exists

```powershell
cd D:\Codes\dualSense\ds5-virtual-usb\emulator
..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .   # 91 tests
..\prototype\.venv\Scripts\python.exe -m ds5emu descr                      # dump descriptors
..\prototype\.venv\Scripts\python.exe -m ds5emu serve                      # 127.0.0.1:3240
```

Stdlib only — no new pip packages were installed. `prototype/.venv` is reused
purely for convenience.

Layering (see `emulator/README.md` for detail): `wire.py` (pure USB/IP bytes) →
`descriptors.py` (ground truth) → `uac.py` + `device.py` (pure
`CMD_SUBMIT → RET_SUBMIT`) → `backend.py` (the Phase-1 seam) → `server.py`
(thin asyncio shell). Everything protocol-shaped is a pure function, so the
whole thing is testable without a driver, and the C++ escape hatch (if Python
jitter turns out to matter) touches only the transport shell.

## 14.5 Phase 3: do these in this order

**Step 0 — get approval.** Print `docs/virtualization-options.md` §7 to the
user. It is a copy-pasteable list: create a restore point, install
`USBip-0.9.7.7-x64.exe` (verify its Authenticode signature first), and note the
side effects (2 kernel drivers, a `ROOT\USBIP_WIN2\UDE` devnode, **all USB 3.0
hubs restart during install**, a scheduled task). Do not proceed without it.

**Step 1 — experiment E2 (5 minutes).** Start `python -m ds5emu serve`, run
`usbip.exe list -r 127.0.0.1`. Settles whether the kernel WSK client talks to a
loopback user-mode server at all. Cheap; do it before anything else.

**Step 2 — experiment E1, THE GO/NO-GO.** Attach with the synthetic backend and
measure isochronous timing for 60 s. Pass criteria are written out in
`virtualization-options.md` §8: 1000 ± 1 iso packets/s, zero gaps > 2 ms,
p99 inter-arrival < 2 ms, clean 1 kHz FFT in both directions.
`SyntheticBackend` already timestamps every iso OUT packet and `ds5emu serve`
prints median/p99/max gaps on exit, so the instrumentation exists.
**If E1 fails, stop and re-open Option B before writing any more integration
code.**

**Step 3 — experiment E3, identity.** With the virtual device attached *and*
the physical wired one plugged in, diff their devnode properties, run
`prototype/tools/enum_hid.py` and `dualsense-tester` against both. §8 has the
exact PowerShell.

**Step 4 — only then** implement `BridgeBackend` and do the real integration.
Its docstring lists what is needed. The Phase-1 gotchas in §8 above apply
verbatim, especially #1 (`mic_active=True` in report `0x36`, mandatory for the
full-duplex a wired DualSense always does) and #3 (never spin on a bare `pass`).

## 14.6 Open questions Phase 2 could not settle

1. **R1 above** — the iso timing question. Only E1 answers it.
2. Which usbip-win2 receive mode (Zero Copy vs the WSK-event Low Latency path)
   to attach with, and whether it measurably matters. Issue #173, which added
   the low-latency path, closed 2026-07-15.
3. Whether Windows will expose the 4-channel render endpoint usefully, or
   downmix it — the haptics live on channels 2/3. Compare against the physical
   wired controller. (R8.)
4. The real UAC1 volume control ranges (R7). USBPcap on the physical wired unit
   would capture them.
5. Whether the emulated HID input path needs to *generate* anything CRC-like.
   USB HID reports carry no CRC — that is a Bluetooth-transport concern — so
   probably not, but it is untested. (R6, and §13 question 1.)

---

# 15. PHASE 3a HANDOFF — the go/no-go is settled

Written for an agent starting with no context but this repository.
Full detail: `docs/e1-results.md` and `docs/identity-comparison.md`.
This section is the operational summary and the list of traps.

## 15.1 What this run did

Ran experiments E2, E1 and E3 from `virtualization-options.md` §8 against the
usbip-win2 0.9.7.7 install recorded in `install-record.md`, using
`SyntheticBackend` (no Bluetooth). Fixed three emulator bugs the hardware
exposed. Did **not** touch `BridgeBackend`, the Bluetooth controller, or any
system configuration.

**Results in one table:**

| experiment | question | answer |
|---|---|---|
| **E2** | does the kernel WSK client talk to a user-mode loopback server? | **YES**, first try. Risk R3 closed. |
| **E1** | can UDE + USB/IP hold 1 ms isochronous at full rate? | **YES.** 60 s duplex, 0 underruns, 0.9995 ms mean service interval, FFT-clean both ways. Risks R1 and R4 closed. |
| **E3** | does it look like a real wired DualSense? | **Byte-identical descriptors**; exactly two differences, both location metadata. Risk R2 narrowed to "no Sony SDK title tested". |
| — | does Windows expose 4 render channels or downmix? | **4 channels @ 48 kHz.** Risk R8 closed favourably — the haptics on ch2/3 are reachable. |

## 15.2 Two install-record caveats, both resolved

1. **Pending reboot (filter driver returned 3010)** — did **not** bite.
   `USBAUDIO.SYS` never showed the `QueryBusTime`/`ABORT_PIPE` symptom from
   usbip-win2 issue #35. `usbip2_filter.sys` works as running-installed. **No
   reboot has been performed and none is needed.**
2. **Port 3240 conflict with `usbipd`** — avoided entirely. `usbip.exe` takes a
   **global** `--tcp-port` option, which must come *before* the subcommand:

   ```powershell
   & "C:\Program Files\USBip\usbip.exe" --tcp-port 3241 attach -r 127.0.0.1 -b 1-1
   ```

   The emulator serves on `--port 3241`. **`usbipd` was never stopped** and is
   still Running / Automatic. Do it this way; there is no need to touch it.

## 15.3 The three bugs — READ THE FIRST ONE BEFORE WRITING BridgeBackend

### (1) There is no bus clock. The emulator IS the audio clock. **(critical)**

On a real bus the host controller paces isochronous traffic from SOF. Over
USB/IP + UDE there is no SOF, and `usbip2_filter.sys` fakes `QueryBusTime` with
a constant — that is precisely *why* `USBAUDIO.SYS` works over UDE at all.
Consequence: **nothing imposes a rate except how fast we answer URBs.**

Measured before any pacing existed: a 48 kHz stream ran at **174 429 frames/s —
3.63x real time — and Windows reported zero underruns.** It will not tell you
you are wrong.

Fixed by `emulator/ds5emu/timing.py::FrameClock`: every isochronous URB reserves
N consecutive 1 ms service intervals on its endpoint and `server.py` sleeps
until the last is due. `DualSenseDevice.handle_submit_ex()` returns a
`SubmitResult(reply, deadline)`; the old `handle_submit()` still returns just
the bytes, which is why the whole existing test suite kept working unchanged.

**What this means for `BridgeBackend`:** the USB side is a hard 48 kHz clock you
do not control and must not block. The Bluetooth side runs at 45 kHz in
10.667 ms Opus frames. The rate conversion and the jitter buffer live in the
backend; `write_audio_out` / `read_audio_in` are called from the URB path and
must return immediately, always, with silence on underrun.

### (2) Interrupt IN must NAK between service intervals

`SyntheticBackend.read_input_report()` used to return a report on every call, so
the URB completed instantly and Windows resubmitted at once: **15 526 reports/s**
through hidapi, 62x the real 250 Hz. Now clock-paced at 250 Hz
(`bInterval = 6` = 4 ms at high speed). `None` means "nothing queued" and the
server holds the URB (up to `hid_in_timeout`, now 200 ms) rather than completing
it empty.

### (3) Isochronous IN returns one service interval, not `wMaxPacketSize`

The host always asks for 196 B; 196 B is **49** stereo frames, so the microphone
ran at 49 kHz (measured 48 983 frames/s). Now clamped to `ISO_IN_BYTES_PER_MS` =
192 B = 48 frames. The spare 4 bytes in `wMaxPacketSize` exist only so an
*asynchronous* endpoint can express clock drift; our frame clock does not drift.

All three are covered by `emulator/tests/test_timing.py`. **102 tests pass.**

## 15.4 How to reproduce E1 in five minutes

```powershell
# 1. server (terminal 1). --record-out is optional; it dumps the received
#    speaker stream as raw 4ch s16le 48 kHz for offline FFT.
cd D:\Codes\dualSense\ds5-virtual-usb\emulator
..\prototype\.venv\Scripts\python.exe -m ds5emu serve --port 3241 --stats-json C:\Temp\stats.json --stats-every 2

# 2. attach (terminal 2)
& "C:\Program Files\USBip\usbip.exe" --tcp-port 3241 attach -r 127.0.0.1 -b 1-1 --once

# 3. which endpoints are the virtual ones? Walk the devnode tree -- do NOT
#    guess from the "2-"/"3-" name prefixes, they move between runs.
$mi0 = Get-PnpDevice -PresentOnly | Where-Object InstanceId -like "USB\VID_054C&PID_0CE6&MI_00\*"
$mi0 | ForEach-Object {
  (Get-PnpDeviceProperty -InstanceId $_.InstanceId | Where-Object KeyName -eq "DEVPKEY_Device_Children").Data |
    ForEach-Object { (Get-PnpDeviceProperty -InstanceId $_ | Where-Object KeyName -eq "DEVPKEY_Device_FriendlyName").Data } }

# 4. play / record against those endpoints, and read C:\Temp\stats.json while it runs.

# 5. TEARDOWN -- the -X is not optional, see 15.5
& "C:\Program Files\USBip\usbip.exe" attach -X
& "C:\Program Files\USBip\usbip.exe" detach -p 1
```

`stats.json` is rewritten atomically every `--stats-every` seconds, so a run can
be measured without stopping it. The fields that matter:
`endpoints.0x01.resyncs` and `.resync_times_s` (**this is the underrun
counter** — one at stream start is expected, more is a real glitch),
`packets_per_s`, `bytes_per_s`, `gap_ms_*`.

## 15.5 Operational traps found the hard way

1. **`usbip attach` arms an automatic background re-attach.** Restart the
   emulator and you silently acquire a *second* attached device on port 02.
   Always `usbip.exe attach -X` (`--stop-all`) before detaching, and verify with
   `usbip.exe port` — it prints nothing when clean.
2. **Never guess which audio endpoint is virtual from its `2-`/`3-` prefix.**
   Walk `MI_00 -> DEVPKEY_Device_Children -> FriendlyName`. Wrong-endpoint
   measurements look entirely plausible and are worthless.
3. **Verify microphone content in WASAPI *exclusive* mode.** Shared mode runs a
   capture enhancement chain that gates a steady tone to digital silence within
   ~250 ms. The **physical** controller shows the same behaviour (23 of 60
   windows at exact zero), so it is Windows, not us — but it will cost you an
   afternoon if you do not know.
4. **`Select-Object -First N` truncates a `Tee-Object` pipeline**, silently
   discarding the rest of the output. Redirect to a file, then read the file.
   (Cost here: an hour believing the virtual device was invisible to the hub
   IOCTL dumper when it had been there all along.)
5. **Give every hardware-facing command an explicit timeout**, and never use a
   bare blocking `hid.read()` — use `read(size, timeout_ms=...)`. A controller
   that is absent or dead hangs forever and looks exactly like a driver fault.
6. **`usbip list` prints every interface as `(00/00/00)`** even when the server
   sends correct values. Cosmetic client display bug; ignore it.

## 15.6 Hardware state — RE-ENUMERATE, DO NOT TRUST §3

**The user swapped the two controllers during this run.** The mapping in §3 is
stale. As of the end of Phase 3a:

| | wired (USB) | Bluetooth |
|---|---|---|
| firmware (feature `0x20`) | `Jul  4 2025 10:38:40` | `Sep 18 2025 13:15:28` |
| serial / BD address | `''` (empty, as always on USB) | `d42f4ba1485d` |
| battery | **0 % / charging** | not read by this agent |
| measured input rate | 250.30 Hz | — |

`a0fa9c0dd8bb` still appears in `hid.enumerate()` as a stale paired entry whose
feature reads fail. Ignore it. Always re-run `python -m ds5bridge list` before
trusting any per-unit identifier, and **read the battery early** — a dying
controller produces failure modes that look like driver bugs. (The terminal
crash that interrupted this run may have been triggered by the Bluetooth unit's
battery dying.)

## 15.7 State left behind

- **Nothing is attached.** `usbip.exe port` prints nothing.
- **No emulator process is running.**
- **`usbipd` is Running / Automatic** — it was never stopped.
- `usbip2_ude` and `usbip2_filter` Running; `ROOT\USB\0000` present, Status OK.
- The only present `VID_054C&PID_0CE6` devnodes are the physical wired unit and
  its two children. Several `Unknown`-status ghosts from attach cycles remain in
  the registry; they are inert and Windows reuses them on the next attach.
- **No system configuration was changed by this run.** No driver installed, no
  service reconfigured, no registry write, no reboot.

## 15.8 What Phase 3b should do, in order

1. **Implement `BridgeBackend`** (`emulator/ds5emu/backend.py`) — a sibling
   agent was working on this concurrently in a worktree; merge before starting.
   §15.3(1) is the design constraint that matters most.
2. **Capture the real UAC1 volume ranges** with USBPcap on the physical wired
   unit and replace the assumed constants in `uac.py` (risk R7 — still open.
   Windows accepted our guesses and built a working mixer, which is weaker
   evidence than the real answers).
3. **Proxy the real feature reports.** `SyntheticBackend` returns correctly
   *sized* but zero-filled `0x05` / `0x20`, so anything parsing calibration or
   firmware strings sees garbage.
4. **Run a Sony PC SDK title.** The last unproven piece of R2. Everything
   structural it is believed to need — a shared, well-formed `ContainerId`
   linking HID to audio — is in place; see `identity-comparison.md` §3.
5. Cheap and worth doing: `dualsense-tester` (WebHID) and an SDL/pygame check
   against the virtual device.

## 15.9 Phase 2 open questions, updated

Of the six in §14.6: **R1 is closed** (E1 passed). The **receive-mode question
is moot** — the default mode held the 1 ms cadence with zero underruns, so the
`wsk_events` low-latency path was never needed. **R8 is closed favourably** —
Windows exposes all four render channels rather than downmixing. R7 (the real
UAC1 volume ranges) is still open. R6 and §13 question 1 (input-report CRC) are
still untested, but E1 gives no reason to think USB HID needs one.
