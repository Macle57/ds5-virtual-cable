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
<!-- ===================== BEGIN PHASE 3b SECTION ===================== -->
<!-- Owned by the Phase 3b agent (BridgeBackend). Self-contained on purpose:
     it is safe to merge this whole block without reading anything above. -->

# 16. PHASE 3b HANDOFF — `BridgeBackend`, the live Bluetooth seam

> **Merged into master by Phase 3c.** This section was written as "§15" by the
> Phase 3b agent working in parallel with Phase 3a; it is renumbered to §16 here
> and its internal `§15.x` cross-references were rewritten to `§16.x`. Where it
> says something is untested through `usbip.exe`, read §17 — Phase 3c attached
> this backend to Windows for real and superseded those caveats.

Written for an agent with no context but this repository. This section covers
**only** `BridgeBackend` and what it took to make it real. It touched no
driver, no service, no `usbip.exe`, and never the wired controller — those
belonged to the concurrent Phase 3a agent.

## 16.1 One-line status

`BridgeBackend` **is implemented and verified on hardware.** All four pipes run
full duplex at once: input, SetState, speaker+haptics out, microphone in. The
`NotImplementedError` stub is gone. 132 unit tests pass; four live harnesses
pass against a real Bluetooth DualSense.

## 16.2 Hardware used, and a warning about it

| | |
|---|---|
| controller | Bluetooth DualSense, BD address **`d42f4ba1485d`** |
| firmware (feature 0x20) | `Sep 18 2025 13:15:28` |
| battery during the verified runs | **90 % / discharging**, unchanged start to finish |
| BT input rate, steady state | **483.0 Hz** |

**The controllers were physically swapped mid-phase.** The unit §3 calls "the
Bluetooth unit" (`a0fa9c0dd8bb`, 10 % battery) died and was replaced by
`d42f4ba1485d`, which §3 lists as merely *paired*. Every number in §16.5 is
from the **new** unit unless it says otherwise.

Two things follow, and they cost real time:

1. **A dying controller mimics protocol bugs.** The first session's tail looked
   like dropped reports and timeouts; it was a flat battery. Read the battery
   level out of the input report the moment you claim the device, log it, and
   re-read it whenever behaviour turns strange. §3's warning was right and is
   worth repeating louder.
2. **Never cache a HID path or serial across sessions.** Both changed. Always
   re-enumerate through `ds5bridge.device.enumerate_devices()`.

## 16.3 What was built

```
emulator/ds5emu/
  _bootstrap.py   puts <repo>/prototype on sys.path, once, so emulator/ can
                  IMPORT Phase-1 code instead of copy-pasting it
  translate.py    pure BT<->USB report translation, stdlib only
  bridge.py       BridgeBackend: 3 threads, 4 ring buffers, the rate conversions
  backend.py      MODIFIED: 3 additive hooks; BridgeBackend re-exported lazily
  device.py       MODIFIED: calls the 3 new hooks
emulator/tests/
  test_translate.py  19 tests, no hardware, no third-party packages
  test_bridge.py     22 tests, no hardware, needs numpy/PyAV (skipped if absent)
emulator/tools/
  bridge_input_soak.py      live test (a)
  bridge_setstate_test.py   live test (b)
  bridge_audio_loopback.py  live tests (c) + (d)
  bridge_e2e.py             live test (e): all four pipes through the real USB
                            request path (handle_submit), not the backend API
```

### The input translation is one fact, not an offset table

`ds5bridge.protocol.Offsets` is constructed with `n = 0` for USB and `n = 1`
for BT, so **every** field in the BT `0x31` payload sits exactly one byte later
than the same field in the USB `0x01` body. The translation is therefore a
single slice:

```
usb_body[0:63] == bt_payload[1:64]
```

`translate.py` implements exactly that and asserts the invariant at import.
The tests then check it *field by field* by running `protocol.decode_input()`
over both shapes, rather than against a hand-copied table that could drift.
What the BT report has and USB does not — payload bytes 64..72 plus the 4-byte
CRC32 at 73..76 — is Bluetooth transport and is dropped. What USB has that BT
does not: nothing. The USB body's last 8 bytes are the AES-CMAC field, which
lands at BT payload 56..63 and copies through verbatim.

Output is even simpler. The USB `0x02` body and the BT `0x31` SetState body are
the *same 47 bytes*, so `usb02_to_bt31()` is unwrap-then-rewrap, and a unit test
asserts it is byte-identical to `protocol.build_bt_setstate()` — the builder
Phase 1 already proved on hardware.

### Threading

| thread | job |
|---|---|
| `ds5-bt-reader` | `hid.read()` loop. Translates control payloads, decodes 71 B mic Opus frames. `hid.read()` releases the GIL. |
| `ds5-audio-pump` | clock-driven `ds5bridge.pacing.Pacer` at **46.875 reports/s**; resample, Opus-encode, pack `0x39`, write. |
| `ds5-setstate` | coalescing SetState writer. |

The asyncio server thread only ever touches lock-guarded byte rings, so
`write_audio_out`, `read_audio_in` and `read_input_report` are O(n) memcpy and
can never block on Bluetooth.

### The rate arithmetic is exact — no drift, no accumulator

```
one 0x39 report = 2 frames = 21.3333 ms
                = 1024 samples of 48 kHz USB audio
                =  960 samples at 45 kHz  (48000/45000 = 16/15)
                =   64 samples at 3 kHz per haptic channel (48000/3000 = 16)
```

Both ratios are integer, so a 1024-sample USB block maps onto exactly one `0x39`
report with nothing left over. `tests/test_bridge.py` asserts this.

The 48 kHz → 3 kHz haptic path is a **stateful** `av.AudioResampler`, not
sample picking. Decimating by 16 needs a real anti-alias filter with memory
across blocks, or everything above 1.5 kHz folds straight back into the audible
haptic band.

## 16.4 Interface changes to the `Backend` contract

**All additive.** Nothing that already existed changed shape, so Phase 3a work
against this contract is unaffected.

| addition | default | why |
|---|---|---|
| `latest_input_report(max_len)` | delegates to `read_input_report` | control `GET_REPORT(INPUT)` must read state without consuming freshness; only the interrupt endpoint has "nothing new" semantics |
| `set_alt_setting(interface, alt)` | no-op | `SET_INTERFACE` alt 1 on a UAC1 streaming interface is the host actually opening the stream — the correct moment to arm the Bluetooth mic, and not a moment earlier on a battery-powered device |
| `on_uac_control(unit, selector, value)` | no-op | a UAC1 `SET_CUR` on a feature unit (mute/volume) mirrored onto the DualSense |

`device.py` now calls all three. `SyntheticBackend` records them
(`alt_settings`, `uac_controls`), so they are testable.

**One structural change worth knowing:** `BridgeBackend` now *lives* in
`ds5emu/bridge.py` and `backend.py` re-exports it through a module-level
`__getattr__`. `from .backend import BridgeBackend` and
`backend.BridgeBackend` both still work. The reason is that `bridge.py` needs
numpy + PyAV + hidapi and `backend.py` deliberately needs none, which is what
keeps the stdlib-only unit tests importable on a bare Python.

## 16.5 What is VERIFIED ON HARDWARE — with numbers

All runs below: BT unit `d42f4ba1485d`, battery 90 %, no driver attached.

### (a) Input pipe soak — `tools/bridge_input_soak.py --seconds 15`

| measure | result |
|---|---|
| polls at the real 250 Hz USB cadence | 3749 = **249.93/s** |
| reports delivered | 3749 = **249.93/s**, zero `None` |
| inter-report gap | median **4.000 ms**, p99 **4.025 ms**, max 5.086 ms |
| **field parity vs the Phase-1 decoder** | **3749/3749 exact** |
| device sequence byte | **0 discontinuities** |
| BT control reports consumed | 5853 |

The parity check is the load-bearing part: every report handed to the USB side
is re-decoded in its USB form and compared field by field against the Phase-1
decode of the *Bluetooth payload it came from*, live. A translation regression
fails the run.

### (b) SetState passthrough — `tools/bridge_setstate_test.py --crc-negative`

The Phase-1 §5.4 trigger-status oracle, re-run through `write_output_report()`
so the whole USB-shaped path is under test:

| sent as USB report `0x02` | observed `(at_status0, at_status1, at_status2)` | Phase 1 expected |
|---|---|---|
| triggers off (`0x05`) | `(9, 9, 0)` | `(9, 9, 0)` |
| `0x21` feedback | `(16, 16, 17)` | `(16, 16, 17)` |
| `0x26` vibration | `(1, 1, 51)` | `(1, 1, 51)` |
| `0x05` off | `(9, 9, 0)` | `(9, 9, 0)` |
| **`0x21` with one CRC bit flipped** | **`(9, 9, 0)` — ignored** | ignored |
| same `0x21`, CRC intact | `(16, 16, 17)` | `(16, 16, 17)` |

So the CRC32 this backend generates is genuinely being validated by the
controller, not merely tolerated.

### (c) + (d) Audio out and microphone in, simultaneously — `tools/bridge_audio_loopback.py`

Driven at the real 1 ms isochronous cadence: 384 B of 4-channel/48 kHz/s16 into
`write_audio_out()` and 192 B out of `read_audio_in()` per millisecond, exactly
what `device._iso_out` / `._iso_in` do per packet. Judged by FFT of what
`read_audio_in()` serves — i.e. the controller's own microphone, heard through
the emulated USB capture endpoint. 3 s baseline + 3 s playing, each mode.

| mode | ch0/1 band @1500 Hz | ch2/3 band @250 Hz | loudest bin |
|---|---|---|---|
| both driven | **+65.4 dB** | **+32.7 dB** | 1500.0 Hz |
| speaker only | **+68.9 dB** | +1.6 dB *(not driven)* | 1500.0 Hz |
| haptics only | +11.6 dB *(not driven)* | **+38.5 dB** | 250.1 Hz |

The two single-channel rows are the **channel-mapping proof**. In `--mode
haptics` the Opus stream carries literal digital silence, so the 250 Hz peak can
only be the voice coils; and the 1500 Hz band moves +1.6 dB when only the
speaker pair is silent. USB ch0/1 → speaker and ch2/3 → haptics is confirmed,
not assumed. The test is frequency-selective — it only passes if energy rises at
the exact frequency commanded — so room noise cannot fake it.

Transport health during those runs: `0x39` at **46.00/s** (target 46.88),
**0 write errors**, **0 underrun frames**, and **300 mic payloads in 3 s =
100.0/s** arriving *while playing*.

That last number is the live proof of gotcha #1: the mic-active flag in `0x39`
`pkt[4]` is being set to `0x7F`. With `0x7E` the controller stops sending mic
payloads the instant playback starts and this test reports zero samples.

### (e) All four pipes through the real USB request path — `tools/bridge_e2e.py --seconds 6`

Not the backend API — hand-built `USBIP_CMD_SUBMIT` frames through
`DualSenseDevice.handle_submit()`, so the isochronous packing rules are
exercised too. Only the TCP socket and usbip-win2's kernel client separate this
from a real attach.

| measure | result |
|---|---|
| enumeration | device/config/HID-report descriptors byte-exact; feature `0x05` 41 B, `0x20` = `Sep 18 202513:15:28`, **both read live off the controller** |
| iso OUT | 750 URBs = **1000 packets/s**, **0 offset-echo errors** |
| iso IN | 750 URBs, **0 compaction errors**, **0 short packets** (every packet a full 192 B) |
| interrupt IN | 1499 = **249.8/s**, **0 empty**, **0 sequence discontinuities** |
| interrupt OUT (SetState) | 12, all delivered |
| mic during playback | 599 = **99.8/s** |
| `0x39` | 279 = 46.5/s, 0 errors, 0 underrun frames |
| device stats | `control 8, hid_in 2998, hid_out 24, iso_out 6000, iso_in 12000, stalled 0` |
| FFT | speaker **+65.2 dB**, haptics **+33.6 dB**, loudest bin 1500.0 Hz |

## 16.6 New empirical gotchas — these cost time

Numbered continuing from §8.

9. **A 1 ms pure-Python pacing loop starves the Bluetooth reader thread.**
   `Pacer.sleep_until_next()` at `frame_ms = 1.0` never sleeps — the delta is
   always under its 1.5 ms threshold — so it spends the entire millisecond in
   the `time.sleep(0)` spin. Measured, in `bridge_e2e.py`: microphone payloads
   fell to **52.7/s** with a 1 ms tick and recovered to **99.8/s** at a 4 ms
   tick, with BT read errors appearing only in the 1 ms case. This is §8 gotcha
   #3 seen from the other side: `sleep(0)` releases the GIL, but releasing it
   100 000 times a second is its own denial of service. Pace host emulation at
   the endpoint's real `bInterval` (4 ms) and batch isochronous URBs (8 packets
   = 8 ms), which is what Windows does anyway.

10. **Bluetooth delivery is bursty, so "only forward new reports" cannot hit
    250 Hz.** With strict new-data-only semantics the emulated interrupt IN
    endpoint delivered **191.9/s**, 23 % of polls found nothing, and gaps
    reached 20 ms. A real wired DualSense emits a report every 4 ms whether or
    not anything moved, so `BridgeBackend` repeats the current state when a poll
    lands inside a burst gap (`repeat_stale_input`, default on). Result:
    **249.93/s, p99 gap 4.025 ms, 0 empty polls**. About **32 %** of reports are
    repeats — that is the Bluetooth burst structure, not a bug.

11. **The BT input rate reads low for the first seconds after connect.** The
    same unit measured 331 Hz, then 390 Hz, then a steady **483 Hz**. Do not
    conclude anything about link health from a short measurement taken right
    after `open()` or right after the extended-mode flip.

12. **The device sequence byte has to be renumbered.** Bluetooth runs ~483 Hz
    and the USB host polls 250 Hz, so passing the controller's own sequence byte
    through would make it jump by 2 and stall on repeats. `BridgeBackend`
    renumbers it to increment by exactly one per delivered report
    (`renumber_input_seq`, default on) — verified: 0 discontinuities over 3749
    reports, and over 1499 more in the e2e run.

13. **The mic ring overflows whenever nothing is draining it.** Harmless — it
    drops the oldest bytes by design — but `mic_ring_drop` will be non-zero
    after any pause between capture passes. Do not read it as a fault.

## 16.7 Design decisions specific to this phase

- **Report `0x39`, not `0x36`.** Two frames per report halves the report rate
  (46.875/s vs 93.75/s) for the same audio, which matters because the same
  Bluetooth link is simultaneously carrying ~483 Hz of input and ~100 Hz of mic
  payloads. `audio_buffer_length = 48`, inside the mandatory [16, 128].
- **SetState is coalesced, not forwarded verbatim at 250 Hz.** Windows re-sends
  an unchanged SetState at the full HID rate; forwarding all of it would burn
  Bluetooth airtime the audio stream needs, and the controller would apply
  identical bytes. Identical bodies are dropped and there is a 6 ms floor
  between writes. In the e2e run: 24 in, 24 sent (all genuinely different); in
  the SetState test, 7 in / 6 sent / 1 coalesced.
- **Pure passthrough of the SetState body.** The controller applies a field only
  when its valid-flag bit is set, so wrapping the host's bytes verbatim cannot
  clobber state the host did not ask to change. The backend's own priming
  SetState sets only the audio valid-flag bits.
- **Feature *writes* are recorded and never forwarded.** Feature writes are how
  a DualSense is re-paired (`0x09`) and how its firmware is touched. Nothing
  here needs one, so a stray host-issued write must not reach the physical
  controller. Feature *reads* are served from a cache primed at `start()`
  (`0x05` calibration, `0x20` firmware info — the two Phase 1 proved safe).

## 16.8 Known gaps — be honest about these

1. **Never attached through usbip.** Everything above is the emulator's own
   request path in-process. The `usbip.exe attach` half was Phase 3a's and is
   not covered by any evidence here. Experiment E1 (isochronous timing under the
   real driver, risk R1) remains the go/no-go.
2. **Feature reports other than `0x05` and `0x20` STALL.** `get_feature_report`
   counts the misses in `feature_misses`, so a future run can see exactly which
   ids a real host asks for and extend the prefetch list. Nothing is read
   lazily, because a blocking feature read from the request path would stall
   isochronous traffic.
3. **`on_uac_control`'s volume mapping is a heuristic** (`_uac_db_to_byte`:
   amplitude = 10^(dB/20), scaled). It cannot be better than `uac.py`'s
   MIN/MAX/RES, which are themselves assumed values — risk R7. Untested against
   a real host mixer.
4. **Headphone routing is still unverified** (§6 carries over). `target` is
   settable but nothing was plugged into the 3.5 mm jack.
5. **Reconnect is implemented but only lightly exercised.** The reader thread
   reopens the device with 2 s backoff and re-arms the mic; a real
   disconnect/reconnect cycle was not forced.
6. **`--transport USB` is accepted but pointless.** `0x36`/`0x39` are Bluetooth
   reports; `BridgeBackend` is a BT-side adapter by construction.
7. **Long-run stability beyond ~30 s per test was not measured.** The longest
   single run here was 15 s of input soak and 2 × 6 s of full duplex.

## 16.9 How to re-run everything

```powershell
# unit tests -- no hardware, no driver, ~1.3 s, 132 tests
cd D:\Codes\dualSense\ds5-virtual-usb\emulator
..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .

# live, needs the Bluetooth controller (and only it)
..\prototype\.venv\Scripts\python.exe tools\bridge_input_soak.py --seconds 15 --print-hz 0
..\prototype\.venv\Scripts\python.exe tools\bridge_setstate_test.py --crc-negative
..\prototype\.venv\Scripts\python.exe tools\bridge_audio_loopback.py --mode both
..\prototype\.venv\Scripts\python.exe tools\bridge_audio_loopback.py --mode speaker
..\prototype\.venv\Scripts\python.exe tools\bridge_audio_loopback.py --mode haptics
..\prototype\.venv\Scripts\python.exe tools\bridge_e2e.py --seconds 6
```

Each live tool prints `RESULT: PASS` or `FAIL` and exits accordingly, so they
chain in CI style. Check the battery line first if anything fails.

To serve the real backend over USB/IP instead of the synthetic one, construct
`UsbIpServer(BridgeBackend())`. `ds5emu/__main__.py`'s `serve` command still
defaults to `SyntheticBackend`, deliberately, because experiment E1 wants the
hardware-free one.

<!-- ====================== END PHASE 3b SECTION ====================== -->

<!-- ===================== BEGIN PHASE 3c SECTION ===================== -->

# 17. PHASE 3c HANDOFF — the merge, the clocks, and the first full system test

Written for an agent starting with no context but this repository.
Full detail: `docs/e2e-results.md`. This section is the operational summary.

## 17.1 What this run did

Merged Phase 3a (`master`: `usbip-win2` transport validated with
`SyntheticBackend`) and Phase 3b (worktree branch: `BridgeBackend`, live
Bluetooth), reconciled their two clocking designs, and attached the combined
system to Windows for the first time.

**A virtual *wired* DualSense, backed live by a physical *Bluetooth* one, is
real and works.** All five e2e stages pass. Full numbers in
`docs/e2e-results.md`.

| | |
|---|---|
| merge | 3 conflicts, all resolved keeping both behaviours; §17.2 |
| clock reconciliation | three domains, not two; §17.3 |
| **(a) INPUT** | **PASS** — 249.90 Hz, **100.00 %** field parity vs a live direct BT read, 0 sequence discontinuities |
| **(b) OUTPUT** | **PASS** — adaptive triggers driven from a hidapi write to the virtual device, 4/4 byte-exact |
| **(c) AUDIO OUT** | **PASS** — +64.4 dB speaker, +27.8 dB haptic, channel mapping *proven* |
| **(d) MIC IN** | **PASS** — controller's own mic at the virtual capture endpoint, WASAPI exclusive |
| **(e) SOAK** | **PASS** — 120 s concurrent; 250.33 Hz HID, 1000.3/1001.1 iso packets/s, **1 resync per endpoint, both at stream start** |
| defects found | **3**, all needed a live attach; §17.4 |
| tests | **173**, up from 3a's 102 and 3b's 132 |

## 17.2 The merge — what conflicted and how it was resolved

`worktree-agent-a93cf7482d1451b3b` into `master`. Three real conflicts:

1. **`device.py` `_hid_class` GET_REPORT(Input).** 3b routes it to the new
   non-consuming `latest_input_report()`; 3a added a `_last_input_report` cache
   because the paced `SyntheticBackend` answers `None` most of the time. **Kept
   both** — the base-class `latest_input_report()` delegates to the paced
   `read_input_report()`, so the cache still covers the `None` windows, while
   `BridgeBackend`'s override makes it unnecessary.
2. **`backend.py`.** 3a's input-report pacer vs 3b's module `__getattr__`
   re-export of `BridgeBackend` from `bridge.py`. Non-overlapping; git merged
   them and both survive, so the stdlib-only test suite still never imports
   numpy/PyAV/hidapi.
3. **`docs/STATUS.md` — both sides wrote a "§15".** 3a's stays as §15; 3b's
   block is renumbered to **§16**, its internal `§15.x` cross-references
   rewritten, and it carries a banner pointing here for what Phase 3c
   superseded.

The merged suite was the exact union — 143 tests — before Phase 3c added its
own.

## 17.3 The clock reconciliation — THERE ARE THREE CLOCKS, NOT TWO

3a's `FrameClock` paces URB completion at 1 ms. 3b's `BridgeBackend` paces the
Bluetooth side with a 21.3 ms tick. They compose, and the reason is worth
knowing before touching either:

| domain | what | drifts against U? |
|---|---|---|
| **U** — USB | `timing.FrameClock`, 1 ms service intervals, `perf_counter` | — |
| **B** — Bluetooth send | `pacing.Pacer`, one tick per `0x39`, `perf_counter` | **no** |
| **C** — the controller | its own crystal; sets microphone arrival | **yes** |

**U and B are the same oscillator related by an exact integer ratio:**

    46.875 reports/s x 1024 samples/report = 48 000 samples/s

The pump is a **1/1024 divider of the frame clock, not a second clock.** No
rate conversion, no accumulator, no possible drift. Only *phase* varies (the
host delivers audio in ~10 ms bursts against a 21.3 ms tick), and phase is a
buffering problem — solved with buffering, never with rate steering.

**C is genuinely independent** and is the only place drift accumulates. Two
crystals at ±100 ppm differ by ~9.6 samples/s: 0.7 s per hour, past any sane
buffer. So the mic ring is *steered*.

Policies (all in `ds5emu/bridge.py`, all unit-tested by `ClockSeamTests`):

* **U→B** encoded-frame queue: target **4 frames (42.7 ms)**, ceiling 8
  (85.3 ms), transmission starts *at* target. Starting at "anything available"
  meant the steady-state latency was whatever the startup burst happened to
  leave — and because U and B never drift, it stayed there forever. Above the
  ceiling the oldest frames are dropped back to target and **counted**
  (`audio_q_drop_frames`): dropping encoded audio is audible and must never be
  silent in the logs. The 120 ms out-ring is the hard ceiling behind it.
* **C→U** mic ring: outside **[10 ms, 90 ms]** the governor adds or removes
  exactly **one** 48 kHz frame per `read_audio_in()` call. At 1000 calls/s that
  is ±2 % authority against a crystal error of order 0.01 % — always a slew,
  never a jump, and inside the band it does nothing at all. Skew corrections
  are counted separately from overflow so the two can never be confused.
* `clock_seam()` publishes the whole seam as JSON under `--stats-json`.

**Both policies are now confirmed on hardware** (120 s soak, full duplex):
U→B recorded **0 underrun frames and 0 queue drops** with the out-ring peaking
at 50 ms of its 120 ms cap — which is what "same oscillator, exact integer
ratio" predicts. C→U made 842 single-frame corrections in 5 760 000 samples
(**0.015 %**), sitting at 56 ms inside its [10, 90] ms band and never
overflowing. Depth settles above the 25 ms target because the governor
deliberately does nothing inside the band; that is latency traded for not
correcting when it does not have to.

**One real bug fell out of this.** `set_alt_setting` and `on_uac_control` are
called from the asyncio thread that owes the isochronous endpoints a 1 ms
deadline, and both did blocking hidapi I/O. Mic arming is two writes with a
20 ms settle between them, so `SET_INTERFACE alt 1` stalled *every* endpoint
for ~40 ms at exactly the moment the host opens the audio stream. Phase 3b
never saw it: its harnesses called the backend from their own thread, with no
event loop to block. All such work is now posted to a bounded queue drained by
the writer thread; the state flag still flips synchronously so the next `0x39`
carries `mic_enabled`.

## 17.4 Three defects — READ THIS BEFORE CHANGING PACING OR INSTRUMENTATION

The first two are merge regressions and share a shape: **two independently
correct pieces that are wrong together.** Neither unit tests nor either agent's
own harness could have caught them. The third was not a merge regression at
all — it had been latent since Phase 3a and only a live, loaded, long run could
expose it.

### (1) The interrupt IN endpoint had no clock — 13 415 reports/s, 54x too fast

3a fixed the free-running endpoint *inside* `SyntheticBackend`. 3b's
`BridgeBackend` never inherited it and **cannot**: `repeat_stale_input` is
correct — a wired DualSense really does emit a report every 4 ms whether or not
anything moved — so the backend answers every poll by design.

The data was already perfect in that run (988/988 = 100 % parity). The bridge
worked; the endpoint had no clock.

**The gate now lives in `DualSenseDevice`, where `bInterval = 6` comes from**,
so no backend can opt out of physics by accident. The backend is not even
*asked* while the interval is open, which keeps its own bookkeeping honest —
`BridgeBackend` renumbers the device sequence byte per delivered report, and it
had been advancing ~50 counts between two reports the host kept.

### (2) The gate then under-delivered — 220.2/s, with 10 % of gaps at 8 ms

Two causes, both "scheduling latency turned into rate error":

* **The schedule absorbed its own lateness.** `max(now, next) + period` re-adds
  the delivery latency every cycle, so the period becomes 4 ms + latency
  permanently. It advances **on the grid** now (`next += period`) — what
  `FrameClock.reserve()` already does with integer frames — and resynchronises
  only when a whole interval has genuinely lapsed (to `now + period`, so two
  reports can never leave back to back).
* **The transport polled when it could have known.** `asyncio.sleep(0.001)`
  overshoots on Windows, so the open interval was found late — often late
  enough that it had lapsed. `SubmitResult` grew **`retry_at`**: when the reply
  is empty *because the service interval has not come round*, the device says
  exactly when it will and the server sleeps to that instant. When the backend
  is merely empty, `retry_at` is `None` and the polling fallback applies,
  because nobody can predict that.

**220.27 → 249.90 Hz; empty polls 10.3 % → 0.027 %.**

### (3) The underrun counter was reporting its own instrumentation

The best find of the run. The first 120 s soak logged **39 iso OUT resyncs**
while the audio was demonstrably fine, and the timestamps were spaced at
**exactly 2.00 s** — the `--stats-every 2` interval.

`UrbMeter.summary()` sorted every URB gap it had ever recorded and
`gap_histogram()` re-walked the deque, for both endpoints, every two seconds:
~15 ms on the asyncio thread that owes the isochronous endpoints a slot every
1 ms. **The measurement manufactured the fault it existed to report.** A
control run with the first snapshot deferred past the audio gave 1 resync
instead of 41.

**`asyncio.to_thread` did not fix it, and that is the lesson.** `sorted()` on a
list of floats is one C call that never releases the GIL, so CPU-bound work
blocks the event loop wherever it runs. Threads move blocking *I/O*; they do
nothing for computation. The fix was to make the work cheap: exact totals are
now O(1) running counters updated in `record()`, and the distribution stats are
computed over the last `UrbMeter.SUMMARY_WINDOW = 4000` URBs via
`itertools.islice`.

| same `--stats-every 2`, same soak | before | after |
|---|---|---|
| iso OUT resyncs | 39 | **1** (stream start) |
| iso OUT packets/s | 514.3 | **1000.3** |
| U→B underrun frames | 32 | **0** |
| HID rate | 248.49/s | **250.33/s** |
| HID gap max | 30.8 ms | **12.4 ms** |

The reported *throughput* was wrong too, because the snapshot's own stalls sat
inside the span it measured — and the fix improved the real system, not just
the metric: every endpoint was losing those 15 ms, only one had a counter that
noticed.

## 17.5 Hardware state — THE CONTROLLERS SWAP, AND BOTH ENUMERATE

§15.6 warned about this; it happened twice more during Phase 3c.

| | 2026-08-24 (§15.6) | 3c stages (a)(b) | 3c stages (c)(d)(e) | end state |
|---|---|---|---|---|
| `d42f4ba1485d` (fw `Sep 18 2025`) | BT, 90 % | USB cable, 100 % | **BT, 90 %** | **BT, 80 %** |
| `a0fa9c0dd8bb` (fw `Jul  4 2025`) | stale entry | **BT, 10 %** | USB cable | USB, charging |

Stages (a) and (b) ran on the 10 % unit — acceptable, because they drive no
speaker, no haptic actuator and no microphone, and the battery read 10 % before
and after. Stages (c)(d)(e) waited for the healthy unit, because they drive
both actuators continuously for minutes and `a0fa9c0dd8bb` is the unit §16.2
records as having *died* at exactly 10 %.

**SELECT THE CONTROLLER BY SERIAL.** A unit charging over USB *still enumerates
over Bluetooth*, as a stale entry whose feature reads fail, and
`enumerate_devices()` orders by path — so the dead one can sort first:

```
[1] BT a0fa9c0dd8bb   feature read failed: read error      <- charging on USB
[2] BT d42f4ba1485d   fw 'Sep 18 2025 13:15:28'            <- the live one
```

`serve --bt-serial d42f4ba1485d` (or `BridgeBackend(serial=...)`) exists for
exactly this. "First BT match" would have claimed the dying unit.

**Never cache a serial, a HID path or a battery level across sessions.** Always
`python -m ds5bridge list`, then read the battery out of the input report at
claim and log it.

## 17.6 What the next agent should do, in order

The concept is proven end to end. What remains is breadth, not feasibility.

1. **Run a game, or a Sony PC SDK title.** This is now the only thing standing
   between "the pipe works" and "it is a controller". Everything structural such
   a title is believed to need — a shared, well-formed `ContainerId` linking HID
   to audio — is in place; see `identity-comparison.md` §3. Cheap first steps:
   `dualsense-tester` (WebHID) and an SDL/pygame check.
2. **Measure audio *fidelity*, not just presence.** Every audio number in
   `e2e-results.md` is band energy at a commanded frequency against a silent
   baseline. That proves the path carries the signal and the channel mapping is
   right; it says nothing about distortion, latency, or the quality of the
   48→45 kHz conversion. Use `serve --record-out` to dump what the emulator
   actually received and compare it sample-for-sample against what was played.
3. **Run longer than four minutes.** The longest continuous run was 120 s.
   Thermal behaviour, long-run drift and battery sag are unmeasured — and the
   C→U governor is exactly the thing a multi-hour run would stress.
4. Capture the real UAC1 volume ranges with USBPcap on the physical wired unit
   and replace the assumed constants in `uac.py` (risk R7, still open). The
   `on_uac_control` mapping cannot be better than those constants are.
5. Extend the feature-report prefetch list. Only `0x05` and `0x20` are cached;
   everything else STALLs and is counted in `feature_misses`. Both sessions
   recorded **no** misses, so Windows itself asks for nothing else — but a game
   may.
6. Force a Bluetooth disconnect/reconnect. Implemented, never tested;
   `reconnects` stayed 0 throughout.
7. Headphone routing is still unverified — nothing has ever been plugged into
   the 3.5 mm jack. `target='headphone'` is settable and untested.

## 17.7 How to bring the whole thing up

```powershell
# 1. server. --backend bridge is the live Bluetooth one; the default is still
#    synthetic, deliberately, because experiment E1 wants the hardware-free one.
cd D:\Codes\dualSense\ds5-virtual-usb\emulator
..\prototype\.venv\Scripts\python.exe -m ds5emu serve --backend bridge --bt-serial d42f4ba1485d --port 3241 --stats-json C:\Temp\ds5c_stats.json --stats-every 2

# 2. attach.  usbipd owns 3240 and is NEVER touched; --tcp-port is a GLOBAL
#    option and must come before the subcommand.
& "C:\Program Files\USBip\usbip.exe" --tcp-port 3241 attach -r 127.0.0.1 -b 1-1

# 3. which endpoints are the virtual ones?  Do NOT guess, and do NOT stop at
#    the hub -- see 17.8 trap 9.
powershell -File tools\e2e_endpoints.ps1

# 4. the stages
..\prototype\.venv\Scripts\python.exe tools\e2e_input_parity.py --seconds 30
..\prototype\.venv\Scripts\python.exe tools\e2e_setstate.py
..\prototype\.venv\Scripts\python.exe tools\e2e_audio.py --mode both
..\prototype\.venv\Scripts\python.exe tools\e2e_audio.py --mode speaker
..\prototype\.venv\Scripts\python.exe tools\e2e_audio.py --mode haptics
..\prototype\.venv\Scripts\python.exe tools\e2e_soak.py --seconds 120

# 5. TEARDOWN -- idempotent, safe to run twice, safe when nothing is up
powershell -File tools\e2e_teardown.ps1
```

Unit tests, no hardware, ~4 s, 173 tests:

```powershell
cd emulator
..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

## 17.8 Operational traps — continuing §15.5's numbering

7. **A stale emulator is invisible to a command-line-based kill, and the new
   one starts anyway.** `Win32_Process.CommandLine` comes back **empty** for
   some python processes (the venv `python.exe` launcher spawns a child whose
   command line the query cannot read), so a teardown matching on
   `CommandLine -match 'ds5emu'` misses the process actually holding the socket.
   The stale server keeps port 3241, a new one appears to start normally, and
   **you measure the old emulator while reading the new one's stats file.** The
   give-away here was `connections: 0` in a stats file written by a process that
   could not possibly have been serving the device that was plainly working.
   `tools/e2e_teardown.ps1` now kills by `Get-NetTCPConnection -LocalPort 3241`
   **first**; the command-line match is the fallback.
8. **A process started from a `nohup ... &` background shell can be
   un-killable** from a later shell in the same session — `Stop-Process` says
   *Access is denied*, and so does `taskkill /T /F`. Start the emulator with
   `Start-Process` from PowerShell and it stays under your control.
9. **Do not tell the virtual device from the physical one by its hub.** BOTH
   report `USB\ROOT_HUB30\...` two levels up (UDE emulates a root hub too) and
   BOTH report `USB\VID_054C&PID_0CE6\...` one level up. The distinguishing
   fact is **four** levels up, at the host controller — virtual:
   `ROOT\USB\0000`, service **`usbip2_ude`**; physical:
   `PCI\VEN_8086&DEV_43ED&…`, service `USBXHCI`. `tools/e2e_endpoints.ps1` does
   the walk and prints `RENDER=`, `CAPTURE=`, `HIDNODE=`. Together with §15.5
   trap 2 (never trust the `2-`/`3-` name prefix) this is the only reliable way
   to know what you are measuring.

10. **Instrumentation can manufacture the fault it measures, and a thread will
    not save you.** See §17.4(3). Two rules fall out of it: anything that runs
    on the emulator's event loop must cost well under one 1 ms service
    interval, and `asyncio.to_thread` is not an escape hatch for CPU-bound work
    because the GIL is held by the C call either way. If a periodic readout
    grows with run length, bound its window.

11. **Verify the controller you claimed is the one you meant.** Both units
    enumerate over Bluetooth even when one is charging over USB, and the stale
    entry can sort first. Pass `--bt-serial`, and check the emulator's
    `opened BT serial=...` log line before trusting a measurement (§17.5).

## 17.9 State left behind

- **Nothing is attached.** `usbip.exe port` prints nothing.
- **No emulator process is running**; TCP 3241 is free.
- **`usbipd` is Running / Automatic** — never stopped or reconfigured.
- The only present `VID_054C&PID_0CE6` devnodes are the physically-plugged unit
  and its two children.
- Controllers: `d42f4ba1485d` on Bluetooth at **80 %**; `a0fa9c0dd8bb` on the
  USB cable, charging.
- **No system configuration was changed.** No driver installed, no service
  reconfigured, no registry write, no reboot.
- `master` is at the Phase 3c merge; the `worktree-agent-a93cf7482d1451b3b`
  branch and its worktree are gone (fully merged).

<!-- ====================== END PHASE 3c SECTION ====================== -->

<!-- ===================== BEGIN PHASE 4a SECTION ===================== -->

# 18. PHASE 4a HANDOFF — from a developer tool to something a gamer can install

Written for an agent starting with no context but this repository.
End-user documentation is `docs/USER-GUIDE.md`; the layer's own notes are
`app/README.md`. This section is the operational summary and the numbers.

## 18.1 What this run did

Phase 3c proved the pipeline; the user then validated it in *Spider-Man: Miles
Morales* ("all DualSense features worked straight away"). Phase 4a turns that
into a product: **one command**, teardown that survives being crashed, the
robustness §17.6 listed as untested, a tray app, a packaged exe, and a guide.

| | |
|---|---|
| new tree | `app/` — `ds5app` (product layer), `tests/`, `tools/`, `packaging/` |
| entry point | `ds5bridge` — run / devices / cleanup / doctor / tray |
| tests | **208** (185 in `emulator/`, up from 173; **23** new in `app/`) |
| launcher lifecycle | **17/17** on hardware, four scenarios |
| reconnect | **20/20** on hardware, three cycles — §17.6(6) closed |
| soak | **30 minutes** continuous — §17.6(3) partially closed |
| packaged | one-dir and one-file both built and run end to end |
| defects found | **6**, every one of them only by running the thing |

## 18.2 What an end user does now

Two installs, then one double-click.

```
1. Install usbip-win2 0.9.7.7 from
   https://github.com/vadimgrn/usbip-win2/releases/tag/v0.9.7.7
   (NOT 0.9.7.8 -- its own maintainer warns it corrupts memory)
2. Unzip the ds5bridge folder anywhere
3. Pair the DualSense over Bluetooth; UNPLUG THE CABLE
4. Double-click ds5bridge.exe   (or ds5bridge-tray.exe for a tray icon)
5. Play
6. Ctrl+C, close the window, or Quit in the tray -- all three tear down cleanly
```

`ds5bridge doctor` checks the whole setup and names anything wrong.
`ds5bridge cleanup` is the rescue path after a crash. `ds5bridge devices` lists
controllers with battery levels and marks the ones that only *look* connected.

Everything the two-terminal Phase-3 procedure did by hand is now one object,
`app/ds5app/service.py::BridgeService`, shared byte for byte by the CLI and the
tray. **The USB/IP server runs in-process on its own asyncio thread**, not as a
child, which deletes the entire failure class §17.8 traps 7 and 8 describe.

## 18.3 The six defects, all found by running it

The pattern is worth naming: **none of these could be found by reading, and
none by unit tests.** Four were found by tests that deliberately crash,
double-launch or race the thing; two by running the packaged build.

### (1) A hard kill leaves the auto-re-attach ARMED, and hides it

`taskkill /F` a running bridge and both `usbip port` and TCP 3241 come back
empty — the driver detaches when the socket dies. The machine looks clean. It
is not: the background auto-re-attach §15.5 trap 1 describes is still armed, and
it fires the instant *any* server listens on that port again. Measured
2026-08-25: start `ds5emu serve` with no attach command at all, and 15 s later
`usbip port` lists a device. Our own attach then adds a second.

**Nothing about the state of the machine reveals this beforehand**, so
`attach -X` now runs **unconditionally at every start**, not only when something
looks stale. Plus a post-attach check that detaches extras if one slipped in.

### (2) `usbip port` is machine-wide — "attached" is not "mine"

The worst one. Testing the frozen exe on port 3242 while a 30-minute soak ran on
3241 **killed the soak six minutes in**: the exe's stale-state cleanup read the
soak's device as leftovers and detached it. "Is anything attached?" was being
used to answer "is anything of *mine* attached?".

Each `usbip port` entry carries `-> usbip://host:port/busid`.
`Usbip.parse_ports()` reads it and `our_ports()` filters on it; every cleanup
and teardown path uses that now. Seven pure tests, including that `3241` must
not match `32410`.

### (3) A second launch used to kill the first

Port held + device attached is indistinguishable from "leftovers from a crashed
run" — so the second instance's auto-cleanup killed the healthy first one and
detached the controller out from under whatever was using it. Port state cannot
tell a corpse from a sibling; a **named mutex** (`Local\ds5bridge-<port>`) can,
so the instance check happens before anything looks at the port.

### (4) `Server.wait_closed()` hangs forever on every normal detach

Teardown took exactly the caller's timeout, every time, and never reached
`BridgeBackend.stop()`. When the usbip driver detaches it **resets** the TCP
connection; the proactor transport raises `ConnectionResetError` from inside
`_call_connection_lost`, the transport's closed-future is never resolved, so
`writer.wait_closed()` in the connection handler never returns — and
`Server.wait_closed()`, which waits for every handler, never returns either.

Both are bounded at 1 s in `ds5emu/server.py`. **Teardown: 15.08 s → 2.09 s.**
`BridgeBackend.stop()` also joins its three threads against one shared 3 s
deadline rather than 3 s each (a 9 s worst case, on the thread holding up exit).

### (5) The tray tore down correctly and then would not go away

`raise KeyboardInterrupt` from a signal handler does **not** escape pystray's
Win32 `GetMessage` loop. Ctrl+Break detached the device properly and left a live
process with a dead bridge and a stale icon by the clock. `service.ON_TEARDOWN`
is a hook list every teardown path runs; the tray registers one that stops its
icon. Exit 0 in 1.2 s afterwards.

Note for whoever touches this next: **Ctrl+Break does not reach the FROZEN
windowed build at all** — a GUI-subsystem process has no console for the event
to arrive at. The frozen tray is quit through its menu (verified end to end),
Task Manager, or a shutdown. A hard kill remains safe because of (1)'s
unconditional `attach -X` and the driver's detach-on-socket-loss.

### (6) A zero-filled input report reads as "d-pad UP held"

The low nibble of `digital_keys` is a hat switch, and 0 means NORTH. Clearing a
report by zeroing bytes therefore pins the d-pad up forever. `DPAD_RELEASED = 8`
exists for this and there is a test that asserts the wrong version is wrong.

## 18.4 Robustness — what was measured

### Bluetooth dropout and reconnect (§17.6(6): "implemented, never force-tested")

`app/tools/reconnect_test.py`, three cycles, virtual device attached, a 250 Hz
reader on it behaving like a game. **20/20 checks.**

| | |
|---|---|
| virtual device through the drop | **stays attached**, `ports=[1]` every cycle |
| reports while the link is down | **249.6 – 250.2/s** (nominal 250) |
| every report neutral while down | **0 actuated of 624 / 625 / 626** |
| controls released after the drop | **1.00, 1.01, 1.01 s** (`INPUT_NEUTRAL_S` = 1.0) |
| link back | **5.06 s of a 5.0 s hold** → 0.06 s to notice and reopen |
| bare reopen latency (`--hold 0`) | **1.03 s** |
| live again | **250 reports/s**, battery read intact |
| the game's HID handle | **never died** — 0 read errors, 0 empty polls |
| a blip shorter than the threshold | correctly does **not** neutralise |

**The policy, and why.** The virtual device **stays attached** and reports a
neutral controller — sticks centred, buttons released, gyro zeroed, touch
lifted — while battery/headphone/mic status pass through untouched. The
alternative, a clean detach, tears the device out from under a running game and
most titles drop to a "reconnect your controller" screen they do not always
recover from. Repeating the last report forever is worse still: a controller
switched off mid-sprint leaves the stick pinned.

Two mechanisms, because there are two ways to lose a link:

* `hid.read()` raises → 5 consecutive raises → reconnect (already existed).
* the link goes **quiet** — every read times out cleanly and returns `b""`
  forever, so nothing above ever trips. `LINK_DEAD_S` (4 s without a control
  payload) is the watchdog that was missing.

**What is NOT covered.** `force_disconnect()` closes the HID handle underneath
the reader; the device never leaves Windows' enumeration, so the reopen always
succeeds on the first try. A genuinely switched-off controller makes
`_pick_device()` raise until it returns — the same 2 s-backoff loop, more laps.
`force_disconnect(hold_s=)` simulates that half. **The manual test is in
`app/tools/reconnect_test.py`'s docstring and has not been run**: bridge, start
a game, hold PS ~10 s, confirm the game keeps running with a neutral
controller, press PS, confirm it responds again.

### 30-minute soak (§17.6(3): "the longest continuous run was 120 s")

`app/tools/soak.py --minutes 30`, running `BridgeService` — the object the
product actually launches — with a 250 Hz reader on the virtual device standing
in for a game, one sample a minute to a JSONL.

| over 30.0 minutes | |
|---|---|
| reports delivered | **449 990 = 249.99/s** (nominal 250) |
| per-minute rate | min **249.96**, median **250.00**, max **250.02** |
| first 15 min vs last 15 min | **249.993 vs 249.993/s** — no drift at all |
| gap median | **4.019 – 4.033 ms** across all 30 samples |
| gap p99 | **7.21 – 7.70 ms** |
| worst single gap | **15.76 ms** (one sample; every other max ≤ 12.0) |
| **sequence discontinuities** | **0** |
| **empty polls** | **0** |
| **read errors** | **0** |
| Bluetooth read errors | **0** |
| disconnects / reconnects / watchdog trips | **0 / 0 / 0** |
| neutral reports | **0** — the link never dropped |
| Bluetooth link rate | 485.3/s first minute, 472.2/s last |
| input repeat fraction | 30.2 % (matches §16.2's ~32 % on a healthy link) |

**Nothing degraded.** The 30th minute is statistically indistinguishable from
the first, which is the question a soak exists to answer.

**Why no audio.** The Phase 3c full-duplex soak cost 10 % of battery in about
four minutes (`e2e-results.md` §2(e)) — roughly **2.5 %/min** with both
actuators driven. Thirty minutes of that is not something a battery can do; the
run would end as a dead-controller test, and §16.2 records exactly what a dead
controller looks like. HID-only is what a long session looks like between
cutscenes and is the regime where drift and thermal effects are measurable.
`soak.py --audio` exists for a short full-duplex run on a charged unit.

### The battery gauge is coarse and it lags under load — do not trust it live

Worth knowing before anyone reads a battery number as a fuel gauge:

```
03:20 -> 04:24   reads 70 % continuously, through an hour of bridging
04:24            soak ends
04:26            reads 30 %, stable across four consecutive re-reads
```

The DualSense's battery nibble held at level 7 for the entire 30-minute soak and
then dropped four levels within two minutes of the load coming off. Actual drain
over the whole session was roughly **0.7 %/min** for input-only bridging
(80 % → 30 % across ~75 min), so the *number* was wrong long before it moved.

Treat the in-run reading as a **floor that updates lazily**, not a gauge. It is
still the right thing to surface — a controller reading 15 % really is about to
misbehave — but "it said 70 % the whole time" is not evidence that it was.

### Battery surfacing

Three places, because the failure it prevents is a *diagnosis* failure:

* **At claim.** `ds5bridge` reads the level before it attaches anything and
  prints it. Below 20 % it prints a warning instead of a note.
* **Every 60 s while running** (`controller.BatteryWatcher`), decoded on demand
  from the report the backend already holds — never on the 250 Hz request path.
* **In the tray**, as a bar across the bottom of the icon, red below 20 %, and
  as a balloon notification the first time it crosses a threshold downwards.

The message itself is the point:

```
battery 12% -- CRITICAL. A DualSense this low produces dropouts and timeouts
that look exactly like software faults. Charge it.
```

It warns **once per level, not once per minute** (an alert every 60 s for an
hour is an alert people learn to ignore) and **never while charging**, however
low. 11 hardware-free tests cover exactly those properties.

## 18.5 Packaging

Both variants built and run end to end on this machine, `PyInstaller 6.22.2`:

| | exe | total on disk | cold start (3 runs) |
|---|---|---|---|
| **one-dir** (default) | 5.1 MB | **134.3 MB**, 192 files | **0.23 / 0.17 / 0.18 s** |
| one-file | **53.5 MB** | 53.5 MB | 2.32 / 2.03 / 2.17 s |

**One-dir is the default.** A one-file build is a self-extracting archive: it
unpacks ~90 MB of numpy, PyAV and libopus into `%TEMP%\_MEIxxxxx` on *every*
launch, which is both the 12x startup cost and the heuristic shape antivirus
scores badly — a packed executable that writes DLLs to a temp path and then
executes them. One-dir's DLLs sit where they were installed. The one-file build
is kept working (`build.ps1 -OneFile`) because it is genuinely nicer to hand
somebody as one attachment.

**Antivirus, honestly.** Windows Defender with real-time protection **on**
scanned both builds and found nothing (`MpCmdRun -Scan -ScanType 3`). That is
one engine on one machine, and it is not the same question as reputation:
**neither build is code-signed, so both raise SmartScreen's "Windows protected
your PC" on a machine that has not seen them before.** That is a signing
problem, not a packaging one, and it is unsolved here. The user guide says so
plainly rather than telling people to click through a security warning.

**No driver is bundled and none is installed.** usbip-win2 stays the user's
decision, made once. When it is absent, `usbip.MISSING_MESSAGE` names the
release (0.9.7.7), the URL, the "do not use 0.9.7.8" warning and the guide.
`--usbip` / `DS5_USBIP_EXE` is an *exclusive* override — which is also the only
way to exercise that message on a machine that has the driver installed.

Two entry points from one spec: `ds5bridge.exe` (console) and
`ds5bridge-tray.exe` (windowed). The windowed one replaces `sys.stdout`/`stderr`
with a sink first: under `console=False` they are `None`, and a bare `print()`
then raises `AttributeError` deep inside a callback.

## 18.6 The tray

`pystray` + `Pillow`, a thin layer over the same `BridgeService`. Icon colour is
the state (grey/blue/green/**amber = controller offline**/red), with a battery
bar; hover gives serial, battery, reports/s and uptime; right-click gives
Start / Stop / Quit. Balloon notifications come from pystray's own
`icon.notify()` — no third dependency (`win10toast` is unmaintained, `plyer`
pulls a stack in for one call). The icon is *drawn* at runtime, so no image file
has to ship or be located.

Chosen over a tkinter status window because a background utility should be an
icon, not another window to minimise — and tkinter would have added ~10 MB to
the bundle for a worse result.

## 18.7 How to run everything

```powershell
# unit tests -- no hardware, no driver
cd D:\Codes\dualSense\ds5-virtual-usb\emulator
..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .   # 185
cd ..\app
..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .   # 23

# the product, from source
..\prototype\.venv\Scripts\python.exe -m ds5app --serial d42f4ba1485d
..\prototype\.venv\Scripts\python.exe -m ds5app doctor
..\prototype\.venv\Scripts\python.exe -m ds5app cleanup

# hardware tests (each prints RESULT: PASS/FAIL and exits accordingly)
..\prototype\.venv\Scripts\python.exe tools\launcher_test.py  --serial d42f4ba1485d
..\prototype\.venv\Scripts\python.exe tools\reconnect_test.py --serial d42f4ba1485d --cycles 3
..\prototype\.venv\Scripts\python.exe tools\soak.py --serial d42f4ba1485d --minutes 30 `
    --jsonl C:\Temp\ds5-soak.jsonl

# build the exe
powershell -File app\packaging\build.ps1            # one-dir  (default)
powershell -File app\packaging\build.ps1 -OneFile   # one-file (comparison)
```

The Phase-3 two-terminal procedure in §17.7 still works and is what you want
when developing `emulator/` itself.

## 18.8 Gaps remaining

In the order the next agent should care about them.

1. **Code signing.** The single biggest thing between this and "an average
   gamer can install it". SmartScreen warns on first run and there is nothing
   in the software that can fix it. Needs a certificate.
2. **Audio over a long run is unmeasured.** The 30-minute soak is HID-only,
   and deliberately so: the Phase 3c full-duplex soak cost ~2.5 % of battery a
   minute, so 30 minutes of audio is not something a battery can do. A
   mains-powered equivalent (controller on a cable is impossible — the radio
   goes off) does not exist. **Audio drift over an hour is still unknown**, and
   the C→U governor is exactly what a long run would stress.
3. **The manual reconnect test has not been run.** Everything scripted passes;
   a real long PS-press power-off, which also removes the device from Windows'
   enumeration, has not been done. Procedure is in
   `app/tools/reconnect_test.py`'s docstring.
4. **One machine, one Windows build, one game.** Nothing here has been run on
   another PC. In particular, `find_usbip`'s registry probe has only ever seen
   one install layout.
5. **No installer.** The user unzips a folder. A real installer would place the
   Start-menu shortcut, offer the Startup-folder tray option and chain the
   usbip-win2 install (with consent).
6. **Multiple controllers simultaneously.** One bridge, one controller. The
   architecture allows a second instance on another port — the mutex is
   per-port — but nothing has been tested that way beyond two bridges
   co-existing during these tests.
7. Carried over from §17.6 and still open: audio *fidelity* as opposed to
   presence; the real UAC1 volume ranges (risk R7); the feature-report prefetch
   list; headphone routing, which `--audio-target headphone` exposes and nobody
   has ever plugged anything into.

## 18.9 State left behind

- **Nothing is attached**; `usbip port` prints nothing. TCP 3241 free.
- **No emulator, bridge or tray process is running.**
- **`usbipd` Running / Automatic** — never stopped or reconfigured.
- **No system configuration was changed by this run.** No driver installed, no
  service reconfigured, no registry write, no reboot. The only installs were
  `pystray`, `pillow` and `pyinstaller` into `prototype/.venv`.
- `dist/` and `dist-onefile/` hold the built exes and are gitignored.
- Controllers: `d42f4ba1485d` on Bluetooth; `a0fa9c0dd8bb` did not enumerate at
  all this session. **Re-enumerate before trusting either** (§17.5).

<!-- ====================== END PHASE 4a SECTION ====================== -->
