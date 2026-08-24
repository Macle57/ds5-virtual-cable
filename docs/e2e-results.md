# E2E — the first full system test: a virtual wired DualSense backed by a live Bluetooth one

Phase 3c. Merges Phase 3a (`usbip-win2` transport validated with
`SyntheticBackend`) and Phase 3b (`BridgeBackend`, live Bluetooth) and runs the
combined system against Windows for the first time.

> **Verdict: the concept is proven. Input and output both work end to end
> through the real Windows stack.**
>
> A game reading the virtual device gets the physical Bluetooth controller's
> live state at **249.90 Hz with 100.00 % field parity** against a simultaneous
> direct Bluetooth read, and a game writing to the virtual device drives the
> physical controller's adaptive triggers with **byte-exact** readback.
>
> The audio stages (c), (d) and (e) were **not run**: the only
> Bluetooth-reachable controller was at 10 % battery, below the abort
> threshold, and the fully-charged unit was on the USB cable. This is a
> hardware-availability blocker, not a technical failure — see §5.
>
> The merge introduced **two real regressions, both found by this run, both
> fixed**. Neither could have been caught without attaching to Windows.

---

## 1. What was run, and against what

| | |
|---|---|
| date | 2026-08-24 / 25 |
| emulator | `python -m ds5emu serve --backend bridge --port 3241` |
| client | `usbip.exe --tcp-port 3241 attach -r 127.0.0.1 -b 1-1` |
| virtual devnode | `USB\VID_054C&PID_0CE6\2&3b7c36a2&0&1`, host controller `ROOT\USB\0000`, service `usbip2_ude` |
| virtual HID | `HID\VID_054C&PID_0CE6&MI_03\4&127b94db&0&0000` |
| virtual endpoints | `Speakers (3- DualSense Wireless Controller)`, `Headset Microphone (3- DualSense Wireless Controller)` |
| Bluetooth controller | `a0fa9c0dd8bb`, firmware `Jul  4 2025 10:38:40` |
| battery | **10 % / discharging at claim, 10 % / discharging at the end** |
| `usbipd` | Running / Automatic throughout, **never touched** (it owns 3240, we own 3241) |

### The controllers swapped again — re-enumerate, always

STATUS §15.6 warns about this and it happened again overnight. At the start of
Phase 3c the mapping was the one §15.6 records; by the time the live stages ran
it had inverted:

| | 2026-08-24 (§15.6) | 2026-08-25 (this run) |
|---|---|---|
| `d42f4ba1485d` (fw `Sep 18 2025`) | Bluetooth, 90 % | **USB cable, 100 % / charging** |
| `a0fa9c0dd8bb` (fw `Jul  4 2025`) | stale paired entry | **Bluetooth, 10 % / discharging** |

Never cache a serial, a HID path or a battery reading across sessions. Read the
battery out of the input report at claim, log it, and re-read it when behaviour
turns strange.

---

## 2. Results by stage

| stage | what it proves | result |
|---|---|---|
| **(a) INPUT** | the virtual device carries the physical controller's live state | **PASS** |
| **(b) OUTPUT** | a write to the virtual device reaches the physical controller | **PASS** |
| (c) AUDIO OUT | speaker + haptic bands, verified acoustically | **NOT RUN** — battery |
| (d) MIC IN | controller mic reaches the virtual capture endpoint | **NOT RUN** — battery |
| (e) SOAK | 120 s of (a)+(c) concurrently | **NOT RUN** — battery |

### (a) INPUT — PASS

`tools/e2e_input_parity.py --seconds 30`, reading the virtual device through
hidapi exactly as a game would, while a second thread reads the physical
controller directly over Bluetooth and compares field by field.

| measure | virtual device | reference |
|---|---|---|
| polls | 7499 = 249.97/s | — |
| **reports delivered** | **7497 = 249.90/s** | physical wired unit 250.13 Hz; E1 synthetic 249.15 Hz |
| empty polls | **2 of 7499 (0.027 %)** | — |
| gap median | **3.999 ms** | physical 4.000 ms |
| gap p99 | **4.103 ms** | physical 4.108 ms |
| gap max | 8.014 ms | physical 4.279 ms |
| **device sequence discontinuities** | **0** | — |
| **field parity vs a live direct BT read** | **7495 / 7495 = 100.00 %** on all 9 fields | — |
| direct BT rate during the run | 322.5 Hz | (see note) |

Emulator-side counters over a 12 s steady-state window, which agree exactly:

```
hid_in           3000  = 249.97/s
input_delivered  3000  = 249.97/s
input_none       0
input_repeated   1509  (50.3 %)
bt_control       3841  = 320.0/s
bt_read_errors   0        reconnects 0        stalled 0
```

Two things worth reading carefully:

* **The parity check is the load-bearing part.** Rate alone would pass with a
  canned report. Every report handed to Windows was compared against the set of
  states the controller itself emitted within ±60 ms, on all nine fields. A
  wrong offset, a stale buffer or a dropped byte cannot survive that.
* **50.3 % of reports are repeats**, against Phase 3b's 32 %. That is the
  Bluetooth link running at 320 Hz rather than the 483 Hz §16.2 measured — a
  weaker link on a 10 % battery — so more of the 250 Hz polls land inside a
  burst gap. `repeat_stale_input` is doing exactly its job: a real wired
  DualSense emits a report every 4 ms whether or not anything moved, and the
  virtual one now does too.

### (b) OUTPUT — PASS

`tools/e2e_setstate.py`. A HID output report `0x02` written to the **virtual**
device with hidapi, the effect read back out of the **virtual** device's input
stream. The full path is `hidapi → HidUsb → usbip2_ude → TCP → ds5emu →
BridgeBackend → CRC32 → Bluetooth → controller → back`.

| sent as USB report `0x02` | `(at_status0, at_status1, at_status2)` | Phase 1 expected |
|---|---|---|
| triggers off (`0x05`) | `(9, 9, 0)` | `(9, 9, 0)` |
| `0x21` feedback | `(16, 16, 17)` | `(16, 16, 17)` |
| `0x26` vibration | `(1, 1, 51)` | `(1, 1, 51)` |
| `0x05` off again | `(9, 9, 0)` | `(9, 9, 0)` |

**Four of four byte-exact.** Battery 10 % before and 10 % after. The emulator
recorded `hid_out 5`, `setstate in/sent/coalesced 5/4/1` — the coalesced one is
the teardown write that repeated the state already set, which is precisely the
airtime saving §16.7 describes.

The adaptive-trigger status bytes are the only output effect on a DualSense
observable without a human watching it, which is why Phase 1 chose them as the
oracle and why they remain the regression test for the output path.

### Whole-run health

```
uptime 264 s   connections 1   stalled 0
control 51   hid_in 64036   hid_out 5   iso_out 0   iso_in 0
bt_reports 83550   bt_read_errors 0   reconnects 0
control_jobs 0 (dropped 0)
```

Zero stalls, zero Bluetooth read errors and zero reconnects across 264 s — on a
10 % battery. `iso_out`/`iso_in` are zero because no audio stage ran.

---

## 3. Two merge regressions, both found only by attaching to Windows

Both are the same shape: **two independently correct pieces that are wrong
together.** This is exactly the failure mode a merge of two parallel agents
should be expected to produce, and neither unit tests nor either agent's own
harness could have caught them.

### 3.1 The interrupt IN endpoint had no clock — 13 415 reports/s

Phase 3a found that interrupt IN free-runs on a UDE bus (no SOF, so the URB
completes the instant we answer and Windows resubmits at once) and fixed it by
pacing inside `SyntheticBackend.read_input_report`.

Phase 3b's `BridgeBackend` was written in parallel against the same `Backend`
contract and never inherited that fix — and it *cannot*, because
`repeat_stale_input` is correct: a wired DualSense really does emit a report
every 4 ms whether or not anything moved, so the backend answers every poll by
design.

First measurement through the live driver:

```
13 415 reports/s   (real wired unit: 250.13 Hz — 54x too fast)
gap median 0.071 ms   p99 0.196 ms
```

The data was already perfect — 988/988 = 100.00 % field parity in that same
run. The bridge worked; the endpoint had no clock.

**Fix:** the 4 ms gate moved into `DualSenseDevice`, where `bInterval = 6`
actually comes from, so no backend can opt out of physics by accident. The
backend is not even *asked* while the interval is open, which also keeps its own
bookkeeping honest — `BridgeBackend` renumbers the device sequence byte per
delivered report, and it had been advancing ~50 counts between two reports the
host kept.

### 3.2 The gate then under-delivered — 220.2 reports/s

With the gate in place the endpoint ran at **220.2/s against a nominal 250**, a
12 % shortfall, with **10 % of inter-report gaps at exactly 8 ms** — one whole
service interval skipped each time. Two independent causes, both about
scheduling latency being converted into rate error:

1. **The schedule absorbed its own lateness.** The commit advanced with
   `max(now, next) + period`. Delivery is always slightly late — the URB
   crosses a socket and an event loop — so that form re-adds the lateness every
   cycle and the period becomes 4 ms + latency, permanently. It advances on the
   grid now (`next += period`), which is what `FrameClock.reserve()` already
   does with integer frames, and resynchronises only when a whole interval has
   genuinely lapsed.

2. **The transport polled when it could have known.** The server retried every
   1 ms until the gate opened, and `asyncio.sleep(0.001)` overshoots on
   Windows, so the open interval was found late — often late enough that it had
   already lapsed. `SubmitResult` grew a `retry_at` field: when the reply is
   empty *because the service interval has not come round*, the device says
   exactly when it will and the server sleeps to that instant. When the backend
   is simply empty, `retry_at` is `None` and the polling fallback applies,
   because nobody can predict that.

**Result: 220.27 → 249.90 Hz, empty polls 10.3 % → 0.027 %.**

---

## 4. The clock reconciliation, and what the run showed about it

Phase 3a made the emulator the audio clock; Phase 3b paced Bluetooth with its
own 21.3 ms tick. The reconciliation (`ds5emu/bridge.py` module docstring) is
that there are **three** clocks, not two:

| domain | what | drifts against U? |
|---|---|---|
| **U** — USB | `timing.FrameClock`, 1 ms service intervals, `perf_counter` | — |
| **B** — Bluetooth send | `pacing.Pacer`, one tick per `0x39`, `perf_counter` | **no** |
| **C** — controller | the DualSense's own crystal, sets mic arrival | **yes** |

U and B are the same oscillator related by an exact integer ratio —
`46.875 reports/s × 1024 samples/report = 48 000 samples/s` — so the pump is a
1/1024 divider of the frame clock, not a second clock. No rate conversion, no
accumulator, no possible drift; only *phase* varies, and phase is a buffering
problem. C is genuinely independent and is the only place drift accumulates.

Policies implemented and unit-tested (`ClockSeamTests`, 19 tests):

* **U→B**: encoded-frame queue target 4 frames (42.7 ms), ceiling 8 (85.3 ms),
  transmission starts *at* target so steady-state latency is chosen rather than
  inherited from whatever the startup burst happened to leave. Over the ceiling
  the oldest frames are dropped back to target and **counted** — dropping
  encoded audio is audible and must never be silent in the logs. The 120 ms
  out-ring remains the hard latency ceiling behind it.
* **C→U**: the mic ring is *steered*, not merely sized. Outside [10 ms, 90 ms]
  the governor adds or removes exactly **one** 48 kHz frame per
  `read_audio_in()` call — at 1000 calls/s that is ±2 % authority against a
  crystal error of order 0.01 %, so always a slew, never a jump. Skew
  corrections are counted separately from overflow so the two can never be
  confused.
* **No Bluetooth I/O on the URB path.** `set_alt_setting` and `on_uac_control`
  are called from the asyncio thread that owes the isochronous endpoints a 1 ms
  deadline, and both did blocking hidapi I/O. Mic arming is two writes with a
  20 ms settle between them, so `SET_INTERFACE alt 1` stalled *every* endpoint
  for ~40 ms at exactly the moment the host opens the audio stream. Phase 3b
  never saw it: its harnesses called the backend from their own thread, with no
  event loop to block. Now posted to a bounded queue drained by the writer
  thread; the state flag still flips synchronously so the next `0x39` carries
  `mic_enabled`.

`clock_seam()` publishes the whole seam as JSON under `--stats-json`. During
this run every seam counter stayed at zero, which is correct and uninformative:
no audio stage ran. **The U→B and C→U policies are unit-tested but not yet
exercised on hardware.**

---

## 5. Why (c), (d) and (e) did not run

Not a technical failure. Hardware availability:

* The Bluetooth-reachable controller (`a0fa9c0dd8bb`) was at **10 % /
  discharging**, below the ~20 % abort threshold this run was given.
* That is the same unit STATUS §16.2 records as having **died at 10 %** during
  Phase 3b, producing a tail of symptoms that looked exactly like protocol bugs
  and cost real time before the battery was suspected.
* The fully-charged unit (`d42f4ba1485d`, **100 %**) was on the USB cable, and a
  DualSense disables its Bluetooth radio while wired. It was left charging as
  instructed.

Stages (a) and (b) were run anyway because they are input/output only — no
speaker, no haptic voice coils, no microphone, no 120 s soak — so their
marginal drain is negligible, and the battery read **10 % before and 10 %
after**. Stages (c)–(e) drive the speaker and both haptic actuators
continuously and would both flatten the unit and produce data that could not be
trusted.

### To finish the run

1. Unplug `d42f4ba1485d` from USB and let it reconnect over Bluetooth (it is
   already paired; it was the Bluetooth unit for the whole of Phase 3b).
2. Confirm with `python -m ds5bridge list` that the BT entry is the
   `Sep 18 2025 13:15:28` firmware, and read the battery.
3. Bring the system up exactly as §1 and run stages (c)–(e). The techniques are
   in `tools/bridge_audio_loopback.py` (speaker/haptic band FFT, and the
   controller's own mic as the acoustic witness) and `tools/bridge_e2e.py`.
4. **Capture the mic in WASAPI exclusive mode.** Shared mode gates a steady
   tone to digital silence in ~250 ms, and the *physical* controller shows the
   same behaviour — it is Windows, not us (e1-results §7.2).

---

## 6. Operational traps found by this run

Continuing STATUS §15.5's numbering.

7. **A stale emulator is invisible to a command-line-based kill, and the new
   one starts anyway.** `Win32_Process.CommandLine` comes back **empty** for
   some python processes — the venv `python.exe` launcher spawns a child whose
   command line the query cannot read — so a teardown that matches on
   `CommandLine -match 'ds5emu'` silently misses the process actually holding
   the socket. The stale server keeps port 3241, a new one appears to start
   normally, and you end up **measuring the old emulator while reading the new
   one's stats file**. The give-away was `connections: 0` in a stats file
   written by a process that could not possibly have been serving the device
   that was plainly working. `tools/e2e_teardown.ps1` now kills by
   `Get-NetTCPConnection -LocalPort 3241` **first**; the command-line match is
   the fallback, not the primary.

8. **A process started from a `nohup ... &` background shell can be
   un-killable** from a later shell in the same session (`Stop-Process`:
   *Access is denied*, and `taskkill /T /F` too). Start the emulator with
   `Start-Process` from PowerShell instead and it stays under your control.

9. **Do not tell the virtual device from the physical one by the hub.** Both
   report `USB\ROOT_HUB30\...` two levels up, because usbip-win2's UDE emulates
   a root hub too, and both report `USB\VID_054C&PID_0CE6\...` one level up.
   The distinguishing fact is **four** levels up, at the host controller:

   | | host controller | service |
   |---|---|---|
   | virtual | `ROOT\USB\0000` | **`usbip2_ude`** |
   | physical | `PCI\VEN_8086&DEV_43ED&…` | `USBXHCI` |

   `tools/e2e_endpoints.ps1` does this walk and prints `RENDER=`, `CAPTURE=`
   and `HIDNODE=`. Combined with §15.5 trap 2 (never trust the `2-`/`3-` name
   prefix), this is the only reliable way to know what you are measuring.

---

## 7. What this run establishes, and what it does not

**Established.**

* A virtual **wired** DualSense, enumerated by Windows over USB/IP, backed
  live by a **Bluetooth** controller, is real and works.
* Input: 249.90 Hz, 100.00 % field parity, 0 sequence discontinuities, 0 empty
  polls of consequence — indistinguishable from the physical wired unit on
  every measure taken.
* Output: byte-exact adaptive-trigger control from a hidapi write to the
  virtual device.
* Stability: 264 s, 0 stalls, 0 Bluetooth read errors, 0 reconnects, on a 10 %
  battery.
* The merged emulator is correct on the HID path under the real Windows driver,
  after two regressions that only a live attach could have exposed.

**Not established.**

* **Audio in either direction through the virtual device.** Phase 3a proved the
  transport carries 1 ms isochronous with `SyntheticBackend`; Phase 3b proved
  `BridgeBackend` drives the speaker, the haptics and the microphone through
  its own API. **Nobody has yet joined the two.** This is the single biggest
  remaining gap and stages (c)–(e) are exactly it.
* The U→B and C→U buffering policies, on hardware. Unit-tested only.
* Anything beyond 264 s. No soak was run.
* Headphone routing (nothing plugged into the 3.5 mm jack).
* The UAC volume heuristic against a real host mixer (risk R7 — `uac.py`'s
  MIN/MAX/RES are still assumed values).
* The feature-report prefetch list. Only `0x05` and `0x20` are cached;
  everything else STALLs and is counted in `feature_misses`. This run recorded
  no misses, so Windows asked for nothing else — but no game has been run.
* Reconnect. Implemented, never force-tested; `reconnects` stayed 0.
* No game or Sony PC SDK title. Still the real acceptance test.

---

## 8. End state

```
usbip port                     EMPTY
python processes               none
TCP 3241                       free
usbipd                         Running / Automatic, never stopped or reconfigured
present VID_054C&PID_0CE6      the physically-plugged unit and its two children only
system configuration           unchanged — no driver installed, no service
                               reconfigured, no registry write, no reboot
```
