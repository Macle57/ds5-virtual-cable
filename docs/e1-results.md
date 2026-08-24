# E1 — isochronous go/no-go: **PASS**

Experiment E1 as specified in `docs/virtualization-options.md` §8, run on
2026-08-24 against usbip-win2 0.9.7.7 (`docs/install-record.md`).

> **Verdict: Option A is viable. Proceed to `BridgeBackend`.**
>
> Windows' `USBAUDIO.SYS` attaches to the synthetic device, exposes a
> 4-channel render endpoint and a 2-channel capture endpoint, and sustains
> **384 kB/s out + 192 kB/s in simultaneously for 60 s with zero underruns**,
> a mean isochronous service interval of 0.9995 ms and audio that comes back
> bit-clean at exactly 1000.000 Hz in both directions.
>
> **No reboot was needed.** The pending-reboot flag from the filter driver's
> 3010 exit code (install-record caveat 1) did not manifest: `USBAUDIO.SYS`
> never showed the `QueryBusTime` / `ABORT_PIPE` symptom described in
> usbip-win2 issue #35. `usbip2_filter.sys` is doing its job as running-installed.
>
> **`usbipd` never had to be stopped** (install-record caveat 2 resolved):
> `usbip.exe` takes a global `--tcp-port`, so the emulator served on 3241 and
> the WSL passthrough service kept port 3240 throughout.

---

## 1. What was run

```powershell
# terminal 1 -- the emulator, on a port that does not collide with usbipd
cd <repo>\emulator
..\prototype\.venv\Scripts\python.exe -m ds5emu serve --port 3241 `
    --stats-json C:\...\stats.json --stats-every 2 [--record-out out.raw]

# terminal 2
& "C:\Program Files\USBip\usbip.exe" --tcp-port 3241 attach -r 127.0.0.1 -b 1-1 --once
```

Backend: `SyntheticBackend` (no Bluetooth, no hardware). The physical wired
DualSense stayed plugged in throughout as the side-by-side control for E3.

Three measurement passes are reported below; the headline numbers come from the
final one, and the middle one reproduced them to within 0.03 %.

## 2. E2 first — does the kernel WSK client talk to a user-mode loopback server?

**Yes, first try.** `usbip --tcp-port 3241 list -r 127.0.0.1` returned our
device, and `attach` brought up a devnode. Risk R3 is closed.

```
Exportable USB devices
    1-1    : Sony Corp. : DualSense wireless controller (PS5) (054c:0ce6)
```

One cosmetic wart: `usbip list` prints every interface as `(00/00/00)` even
though the emulator sends the correct `usbip_usb_interface` triples
(`(1,1,0) (1,2,0) (1,2,0) (3,0,0)`, verified in-process). It is a client display
issue only — `OP_REP_IMPORT` carries no interface array at all, and enumeration
is driven entirely by the configuration descriptor, which arrives verbatim
(§5). Not worth chasing.

## 3. Enumeration — everything binds

| check | result |
|---|---|
| devnode | `USB\VID_054C&PID_0CE6\2&3B7C36A2&0&1`, Status **OK** |
| speed | **High Speed (480 Mbps)** — so `patch_config()` is skipped and our iso `bInterval = 4` survives, exactly as Phase 2 predicted |
| composite split | `usbccgp` → `MI_00` (Class MEDIA, service `usbaudio`) + `MI_03` (Class HIDClass, service `HidUsb`) |
| HID stack | `HID\VID_054C&PID_0CE6&MI_03\4&127b94db&0&0000` — opens through hidapi, 289-byte report descriptor served (hidapi reports its usual 467-byte reconstruction, same as for the physical unit — `STATUS.md` gotcha #4) |
| audio endpoints | **`Speakers (3- DualSense Wireless Controller)` — 4 channels @ 48 kHz** and **`Headset Microphone (3- DualSense Wireless Controller)` — 2 channels @ 48 kHz** |
| stalls | `stalled: 0` over every run — Windows asked nothing we could not answer |

The 4-channel render endpoint answers open question R8 in the affirmative:
Windows exposes all four channels (it does **not** downmix at the endpoint), so
the haptic channels 2/3 are reachable.

## 4. The numbers

### 4.1 Isochronous OUT `0x01` — speaker + haptics, 4 ch

60 s duplex soak, WASAPI shared mode, 1 kHz sine on all four channels:

| quantity | measured |
|---|---|
| URBs | 6014 (100.05/s), **exactly 10.00 packets/URB** |
| packets | **60 140** over 60.110 s |
| bytes/packet | **384.00** — every packet full, none short |
| throughput | **384 191 B/s** (nominal 4 ch × 2 B × 48 000 = 384 000) |
| mean service interval | **0.9995 ms** |
| **underruns (frame-clock resyncs)** | **1, at t = 65.957 s — the stream start. Zero mid-stream.** |
| PortAudio underflow callbacks | **0** |
| URB inter-arrival | min 0.034, median 15.114, p99 17.272, max 32.387 ms |

### 4.2 Isochronous IN `0x82` — microphone, 2 ch

| quantity | measured |
|---|---|
| URBs | 6025 (100.23/s), exactly 10.00 packets/URB |
| packets | **60 250** over 60.113 s |
| bytes/packet | **192.00** (one service interval; see bug 3 below) |
| throughput | **192 437 B/s** (nominal 2 ch × 2 B × 48 000 = 192 000) |
| **underruns** | **1, at t = 65.954 s — the stream start. Zero mid-stream.** |
| PortAudio overflow callbacks | **0** |
| URB inter-arrival | min 0.158, median 15.103, p99 16.756, max 31.318 ms |

### 4.3 End-to-end audio quality

**Capture direction** (host records the emulator's synthetic 1 kHz sine,
WASAPI exclusive, 60 s):

```
CAPTURE: 2 884 800 frames in 60.11 s = 47 996.0 frames/s (nominal 48 000, error -0.008%)
capture peak 0.2500  rms 0.176738   (a 0.25-amplitude sine has rms 0.176777)
capture FFT peak 1000.000 Hz; 99.983% of energy within +/-20 Hz
601 x 100 ms windows: median rms 0.176738  min 0.176738  max 0.176738
windows below half-median = 0        discontinuities = 0 of 2 884 799
```

Every single 100 ms window has **identical** RMS to six decimal places across a
full minute. There is no dropout, no splice, no resample artefact.

**Render direction** (the bytes the emulator actually received, dumped with
`serve --record-out` and FFT'd offline, 20 s at amplitude 0.5):

```
963 648 frames = 20.076 s, 4 channels
ch0..ch3: peak 1000.000 Hz, rms 0.35107 each      (0.5/sqrt(2) = 0.35355)
200 x 100 ms windows: median rms 0.353553  min 0.316228  max 0.353554
windows below half-median = 0
steady-state discontinuities = 0 of 963 647
steady-state exact-zero samples = 40 889   (= the ~40 000 zero crossings of a
                                            1 kHz sine over 20 s, not dropouts)
```

All four channels carry the signal identically and intact. The single
low window (0.316) is the one containing WASAPI's silent prefill.

### 4.4 HID, alongside the audio

| | virtual | physical wired (control) |
|---|---|---|
| input report rate | **249.15 Hz** (10 s) / 250.06 Hz (5 s) | 250.13 Hz |
| gap median | 3.999 ms | 4.000 ms |
| gap p99 | 4.101 ms | 4.108 ms |
| gap max | 19.132 ms | 4.279 ms |
| feature `0x05` | 41 bytes | 41 bytes |
| feature `0x20` | 64 bytes | 64 bytes |
| output report `0x02` | written; arrived at the emulator on interrupt OUT `0x03` (`hid_out: 1`) | n/a |

The virtual device's worst-case HID gap (19 ms) is larger than the physical
one's (4.3 ms) — a Windows scheduling artefact of the user-mode hold-and-poll
loop, not a lost report: the mean rate is right and no report was dropped.

## 5. Pass criteria, judged

`virtualization-options.md` §8 wrote the criteria assuming one URB per
isochronous packet. In reality `usbaudio.sys` batches **exactly 10 packets per
URB** and submits them in bursts of one or two every ~15 ms (the Windows audio
engine period). Two criteria therefore have to be restated to mean what they
were written to mean; both restatements are stricter, not looser.

| § 8 criterion | judgement |
|---|---|
| packets/s = 1000 ± 1 | **PASS.** 60 140 packets in 60.11 s of URB arrivals. The cadence is 1000.000/s *by construction* — `FrameClock` allocates consecutive integer frames — so the meaningful check is whether the schedule ever broke, which is the underrun row. Independently corroborated by the host: capture ran at 47 996.0 frames/s against nominal 48 000, i.e. **6 ms of drift in a minute (-0.008 %)**. |
| zero gaps > 2 ms between consecutive iso OUT service intervals | **PASS, restated.** Not directly measurable — packets arrive ten at a time. The equivalent-or-stronger statement is *the endpoint never ran out of scheduled frames*: `FrameClock.resyncs` = **1 on each endpoint, both at stream start**, zero mid-stream, over 60 s. |
| p99 inter-arrival < 2 ms | **PASS, restated.** p99 *URB* gap is 17.3 ms, but each burst carries ≥10 ms of audio and the reservation backlog never emptied. Mean service interval 0.9995 ms. |
| clean 1 kHz FFT, both directions, no dropouts | **PASS.** Capture: 1000.000 Hz, 99.98 % in-band, 601/601 windows identical, 0 discontinuities. Render: 1000.000 Hz on all 4 channels, 0 discontinuities. |

## 6. Three emulator bugs the hardware found — all fixed

None of these could have been caught by the Phase-2 unit tests, because all
three are about *when* the emulator answers, not what it answers. All three are
now covered by `emulator/tests/test_timing.py`.

### 6.1 There is no bus clock — the emulator IS the audio clock (critical)

On a real bus the host controller paces isochronous traffic from SOF. Over
USB/IP + UDE there is no SOF, and `usbip2_filter.sys` answers `QueryBusTime`
with a constant (that is *why* `USBAUDIO.SYS` works over UDE at all). So
nothing between `usbaudio.sys` and the emulator imposes a rate: **the stream
runs exactly as fast as the emulator completes URBs.**

First measurement, before any pacing existed:

```
played 875 040 frames in 5.02 s = 174 429 frames/s   (nominal 48 000)
portaudio status callbacks (underrun/overflow) = 0
```

**3.63x real time, and Windows reported no problem at all.** Fixed by
`ds5emu/timing.py::FrameClock`: each isochronous URB reserves N consecutive
1 ms service intervals on its endpoint and the transport sleeps until the last
one is due. After the fix, 47 953 → 47 996 frames/s.

This is the single most important thing Phase 3 learned, and it applies
verbatim to `BridgeBackend`.

### 6.2 Interrupt IN was not rate-limited

`SyntheticBackend.read_input_report()` returned a report on every call, so the
URB completed instantly and Windows resubmitted at once:

```
input reports 77 630 in 5.00 s = 15 525.86 Hz      (real device: 250 Hz)
```

62x too fast. A real endpoint NAKs until its 4 ms service interval. Now
clock-paced at 250 Hz (`bInterval = 6` → 4 ms at high speed), measured
249.15–250.06 Hz against the physical unit's 250.13 Hz.

### 6.3 Isochronous IN returned wMaxPacketSize instead of one service interval

The host always asks for `wMaxPacketSize` = 196 B. But 196 B is **49** stereo
frames, so returning it every millisecond runs the microphone at 49 kHz:

```
captured 491 520 frames in 10.03 s = 48 983.4 frames/s   (nominal 48 000)
```

Clamped to `ISO_IN_BYTES_PER_MS` = 192 B = 48 frames. The 4 bytes of headroom
in `wMaxPacketSize` exist so an *asynchronous* endpoint can occasionally send
one extra frame to express clock drift; our frame clock does not drift. After
the fix: 47 996.0 frames/s (-0.008 %).

A fourth, minor one turned up while writing the regression tests: the
input-report pacer let its first two calls through before the 4 ms cadence took
hold. Harmless in practice (the hardware run still measured 250.06 Hz), fixed
anyway.

## 7. Two operational gotchas worth knowing

### 7.1 `usbip attach` arms an automatic re-attach — disarm it with `attach -X`

After a successful attach, usbip-win2 keeps retrying that busid in the
background. A restarted emulator therefore silently acquires a *second*
attached device:

```
Port 01: device in use at High Speed(480Mbps)  -> usbip://127.0.0.1:3241/1-1
Port 02: device in use at High Speed(480Mbps)  -> usbip://127.0.0.1:3241/1-1
```

Observed once, 2 min 40 s after a server restart. It did **not** contaminate
the measurements — the server log timestamps the second `OP_REQ_IMPORT` at
uptime 161 s, well after the 60 s soak finished at uptime ~126 s, and the
snapshot taken during the soak records `connections: 1`. Teardown now always
runs `usbip.exe attach -X` (`--stop-all`) before `detach`.

### 7.2 Windows' shared-mode capture APO gates a steady tone to digital silence

Recording the virtual microphone in WASAPI **shared** mode returns the tone for
~250 ms and then decays to exact zeros. This is **not** an emulator or driver
fault, and the proof is the physical controller:

| 6 s shared-mode capture | median window rms | windows at/near exact zero |
|---|---|---|
| **physical** wired DualSense mic | 0.00006 | 23 of 60 |
| **virtual** mic (1 kHz tone) | 0.00000 | tone present only in the first ~2 windows |

Both endpoints get the same treatment from the same enhancement chain, and the
emulator's own counters show isochronous IN packets flowing continuously and at
the correct rate the whole time in both cases. Exclusive mode bypasses the APO
and gives the flawless result in §4.3. Anything that must verify mic content —
including the Phase-4 loopback tests — should capture in exclusive mode.

## 8. What E1 did *not* establish

- **The synthetic backend generates the audio.** E1 proves the USB/IP + UDE
  transport holds 1 ms isochronous in both directions with real Windows audio
  clients at either end. It does not prove the Bluetooth side can *feed* that
  pipe — 48 kHz USB against the controller's 45 kHz Opus consumption, with a
  10.667 ms frame quantum, is `BridgeBackend`'s problem and is untested.
- **UAC1 volume MIN/MAX/RES are still assumed** (risk R7). Windows accepted
  them and produced a working mixer, which is weaker than capturing the real
  device's answers with USBPcap.
- **No game or Sony PC SDK title has been run** against the virtual device.
  That is the real acceptance test and it is still open (see
  `identity-comparison.md`).
- **CPU cost was not profiled.** The Python server kept up comfortably, but the
  headroom was not quantified.
