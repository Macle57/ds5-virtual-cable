# E2E — the first full system test: a virtual wired DualSense backed by a live Bluetooth one

Phase 3c. Merges Phase 3a (`usbip-win2` transport validated with
`SyntheticBackend`) and Phase 3b (`BridgeBackend`, live Bluetooth) and runs the
combined system against Windows for the first time.

> ## Verdict: **ALL FIVE STAGES PASS.** The thing works.
>
> Windows enumerates a **wired** DualSense that does not exist. Everything
> behind it is a **Bluetooth** controller on the other side of the room.
>
> * A game reading the virtual device gets the physical controller's live state
>   at **249.90 Hz with 100.00 % field parity** against a simultaneous direct
>   Bluetooth read, with **zero** sequence discontinuities.
> * A game writing to the virtual device drives the physical controller's
>   adaptive triggers, **byte-exact** on all four cases.
> * A tone played to the **virtual Windows render endpoint** comes out of the
>   physical controller's **speaker** and **haptic voice coils**, and is heard
>   back — acoustically, through the air — by the controller's **own
>   microphone**, arriving at the **virtual Windows capture endpoint**:
>   **+64.4 dB** in the speaker band and **+27.8 dB** in the haptic band.
>   Channel mapping is *proven*, not assumed: driving ch2/3 alone puts the
>   loudest bin at **249.7 Hz** while the Opus stream carries digital silence.
> * **120 s concurrent soak**, all of the above at once: HID at **250.33/s**
>   with 0 sequence breaks, both isochronous endpoints at their nominal
>   **1000 packets/s with every packet full**, and **exactly one frame-clock
>   resync on each, both at stream start** — which is E1's pass criterion, now
>   met by the fully joined system. Speaker band held **+85.6 dB median across
>   twelve 10 s windows** with a 1.8 dB spread and no degradation. **0**
>   underrun frames, **0** queue drops, **0** ring overflows, **0** `0x39`
>   write errors, **0** Opus decode errors, **0** capture overflows. Battery
>   90 % → 80 %.
>
> Three real defects were found, all by attaching to Windows, all fixed. Two
> were merge regressions in the HID clock. The third is the most interesting
> thing in this document: **the instrumentation was manufacturing the exact
> fault it existed to report** (§6).

---

## 1. What was run, and against what

| | |
|---|---|
| date | 2026-08-24 / 25 |
| emulator | `python -m ds5emu serve --backend bridge --bt-serial 0011223344bb --port 3241` |
| client | `usbip.exe --tcp-port 3241 attach -r 127.0.0.1 -b 1-1` |
| virtual devnode | `USB\VID_054C&PID_0CE6\2&3b7c36a2&0&1`, host controller `ROOT\USB\0000`, service `usbip2_ude` |
| virtual HID | `HID\VID_054C&PID_0CE6&MI_03\4&127b94db&0&0000` |
| virtual endpoints | `Speakers (3- DualSense Wireless Controller)`, `Headset Microphone (3- DualSense Wireless Controller)` |
| Bluetooth controller, stages (a)(b) | `0011223344aa`, firmware `Jul  4 2025 10:38:40`, **10 %** throughout |
| Bluetooth controller, stages (c)(d)(e) | `0011223344bb`, firmware `Sep 18 2025 13:15:28` |
| battery, (c)(d)(e) | **90 % at claim → 80 % after the soak** |
| `usbipd` | Running / Automatic throughout, **never touched** (it owns 3240, we own 3241) |

### The controllers swapped again — re-enumerate, always

STATUS §15.6 warns about this and it happened again overnight. At the start of
Phase 3c the mapping was the one §15.6 records; by the time the live stages ran
it had inverted:

| | 2026-08-24 (§15.6) | 2026-08-25, stages (a)(b) | 2026-08-25, stages (c)(d)(e) |
|---|---|---|---|
| `0011223344bb` (fw `Sep 18 2025`) | Bluetooth, 90 % | USB cable, 100 % charging | **Bluetooth, 90 %** |
| `0011223344aa` (fw `Jul  4 2025`) | stale paired entry | **Bluetooth, 10 %** | USB cable, charging |

Never cache a serial, a HID path or a battery reading across sessions. Read the
battery out of the input report at claim, log it, and re-read it when behaviour
turns strange.

### And SELECT BY SERIAL, not by index

A controller charging over USB **still enumerates over Bluetooth**, as a stale
entry whose feature reads fail, and `enumerate_devices()` orders by path — so
the dead one can sort first:

```
[1] BT 0011223344aa   feature read failed: read error      <- charging on USB
[2] BT 0011223344bb   fw 'Sep 18 2025 13:15:28'            <- the live one
```

`BridgeBackend(serial=...)` / `serve --bt-serial 0011223344bb` exists for
exactly this. "First BT match" would have claimed the dying unit.

---

## 2. Results by stage

| stage | what it proves | result |
|---|---|---|
| **(a) INPUT** | the virtual device carries the physical controller's live state | **PASS** |
| **(b) OUTPUT** | a write to the virtual device reaches the physical controller | **PASS** |
| **(c) AUDIO OUT** | the virtual render endpoint drives the real speaker *and* the haptic voice coils | **PASS** |
| **(d) MIC IN** | the controller's own mic reaches the virtual capture endpoint | **PASS** |
| **(e) SOAK** | 120 s of (a)+(c)+(d) concurrently, without degradation | **PASS** |

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

### (c) AUDIO OUT and (d) MIC IN — PASS, and they are one experiment

`tools/e2e_audio.py`. These two stages share a loop, and that is a feature: the
strongest available witness for "did the controller really make that sound?" is
the controller's own microphone, and the only way to hear that microphone is
the virtual capture endpoint. So one run proves both directions at once:

```
tone -> VIRTUAL render endpoint (WASAPI, 4 ch)                      <- (c)
  -> usbip2_ude -> TCP -> ds5emu iso OUT 0x01
  -> BridgeBackend: 48k->45k resample, Opus encode, 48k->3k haptic decimate
  -> BT report 0x39 -> controller
  -> ch0/1 out of the SPEAKER, ch2/3 into the HAPTIC voice coils
  -> ((( through the air / the controller's own body )))
  -> the controller's MICROPHONE                                    <- (d)
  -> BT 0x31 audio payload -> Opus decode -> mic ring
  -> ds5emu iso IN 0x82 -> usbip2_ude
  -> VIRTUAL capture endpoint (WASAPI EXCLUSIVE) -> FFT
```

Nothing in that chain is simulated. Judged against a silent baseline recorded
seconds earlier through the same path, 3 s each, band energy within ±25 Hz.

| mode | ch0/1 band @1500 Hz | ch2/3 band @250 Hz | loudest bin | verdict |
|---|---|---|---|---|
| **both** | **+64.4 dB** | **+27.8 dB** | 1499.2 Hz | both actuators driven |
| **speaker only** | **+61.4 dB** | +4.7 dB | 1499.7 Hz | haptic band quiet |
| **haptics only** | +12.1 dB | **+42.4 dB** | **249.7 Hz** | speaker band quiet |

Capture overflows: **0** in every run.

**The two single-channel rows are the channel-mapping proof.** In `--mode
haptics` the Opus stream carries literal digital silence, so a 250 Hz peak — and
a *loudest bin of 249.7 Hz across the whole spectrum* — can only be the voice
coils. In `--mode speaker` the haptic band sits at +4.7 dB, i.e. the noise
floor. USB ch0/1 → speaker and ch2/3 → haptics is **confirmed, not assumed**.

The test is frequency-selective — it only passes if energy appears at the exact
frequency commanded — so room noise cannot fake it.

These match Phase 3b's backend-only numbers (§16.5(c)) closely: +65.4/+32.7 both,
+68.9/+1.6 speaker, +11.6/+38.5 haptics. **Going through the whole Windows
stack costs essentially nothing** compared with driving `BridgeBackend`
directly, which is the useful result.

> **WASAPI exclusive mode on capture is not optional.** Shared mode runs an
> enhancement chain that gates a steady tone to digital silence within ~250 ms.
> The *physical* controller shows the same behaviour, so it is Windows, not us
> (`e1-results.md` §7.2). `--shared-capture` is provided to reproduce the trap
> deliberately, and for nothing else.

### (e) SOAK — PASS

`tools/e2e_soak.py --seconds 120`. Stage (a) and stages (c)+(d) running *at the
same time* — HID at 250 Hz, isochronous OUT at 1000 packets/s and isochronous IN
at 1000 packets/s, through one emulator, one TCP socket and one Bluetooth link.
Neither Phase 3a nor Phase 3b ever did this.

| measure | result | reference |
|---|---|---|
| HID reports | **30 040 = 250.33/s** — empty 0, errors 0 | physical wired 250.13 Hz |
| HID gap | median **4.000 ms**, p99 **5.777 ms**, max 12.434 ms | physical 4.000 / 4.108 |
| **HID sequence discontinuities** | **0** | |
| **iso OUT `0x01`** | **1000.3 packets/s**, 384.00 B/packet | nominal 1000, E1 384.00 |
| **iso IN `0x82`** | **1001.1 packets/s**, 192.00 B/packet | nominal 1000, E1 192.00 |
| **frame-clock resyncs** | **1 on each endpoint, both at stream start** | E1's criterion exactly |
| `0x39` reports | 5584, **0 write errors** | |
| mic payloads | 12 184, **0 decode errors** | |
| **U→B underrun frames** | **0** | |
| **U→B queue drops** | **0** | |
| out-ring peak | 50.0 ms of a 120 ms cap, **0 B overflow** | |
| mic ring overflow | **0 B** | |
| capture overflows | **0** | |
| **battery** | **90 % → 80 %** across ~4 min of full-duplex audio + haptics | |

Every packet full, none short, on both isochronous endpoints, for two minutes,
while HID ran at the correct rate and the audio stayed clean. This is the E1
result reproduced by the *joined* system with a live Bluetooth controller
behind it instead of a synthetic tone generator.

Band energy per 10 s window, twelve windows — the "does it degrade?" question:

| | min | median | max | spread |
|---|---|---|---|---|
| speaker band @1500 Hz | +85.0 dB | **+85.6 dB** | +86.8 dB | **1.8 dB** |
| haptic band @250 Hz | +39.9 dB | **+40.3 dB** | +40.6 dB | **0.7 dB** |

The last window is indistinguishable from the first. The haptic band varies by
0.7 dB over two minutes.

#### The drift governor, doing its job

This is the first time the C→U seam has run on hardware, and it is the only
place in the system where two independent crystals meet:

```
mic depth      mean 56.5 ms   range [0.0 .. 100.0]   target 25   band [10, 90]
C->U skew      pad 634 frames    drop 208 frames
mic underruns  18    reprimes 18    ring overflow 0 B
```

842 single-frame corrections over 120 s of 48 kHz stereo is **0.015 % of
5 760 000 samples** — each one frame, none audible, none a jump, and the ring
neither overflowed nor grew without bound. Depth sat at 56 ms, comfortably
inside [10, 90], so the governor spent most of its time doing nothing, which is
the design intent: it steers only at the edges.

The brief excursion to 100 ms is expected — the governor removes at most one
frame per call, so a Bluetooth burst pushes the depth above the high-water mark
and it slews back rather than jumping. The 18 underrun/reprime pairs are the
capture-stream start and the boundaries between measurement windows; the flat
band-energy table above is the check that matters.

### Whole-run health

The stages (a)+(b) session, on the 10 % unit:

```
uptime 264 s   connections 1   stalled 0
control 51   hid_in 64036   hid_out 5   iso_out 0   iso_in 0
bt_reports 83550   bt_read_errors 0   reconnects 0
control_jobs 0 (dropped 0)
```

Zero stalls, zero Bluetooth read errors and zero reconnects across 264 s on a
10 % battery. `iso_out`/`iso_in` are zero because that session ran no audio.

The stages (c)(d)(e) session, on the 90 % unit, ran ~10 min including three
audio-mode runs and two 120 s soaks: **`stalled` 0 throughout**, iso OUT
384.00 bytes/packet and iso IN 192.00 bytes/packet — *every packet full, none
short*, exactly as E1 measured with the synthetic backend — and URB
inter-arrival median 14.66 ms / p99 19.89 ms, which is the Windows audio-engine
burst period (E1 saw 15.1 / 17.3), not a gap in service.

---

## 3. Three defects, all found only by attaching to Windows

The first two are merge regressions and share a shape: **two independently
correct pieces that are wrong together.** That is exactly the failure mode a
merge of two parallel agents should be expected to produce, and neither unit
tests nor either agent's own harness could have caught them.

The third was not a merge regression at all. It had been latent since Phase 3a
and only a *live, loaded, long* run could expose it.

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

### 3.3 The underrun counter was reporting its own instrumentation

The best find of the run, and the one most worth remembering.

The first 120 s soak passed, but iso OUT logged **39 frame-clock resyncs** —
E1's rule is "one at stream start, more than that mid-stream means the audio
actually glitched". The audio was demonstrably fine (band energy flat to 1.6 dB
across twelve windows), so either the metric or the audio was lying.

The resync timestamps settled it instantly:

```
370.057, 372.056, 374.088, 376.103, 378.107, 380.104, 382.106, 384.108, ...
deltas: 2.00 2.03 2.01 2.00 2.00 2.00 2.00 2.03 2.02 2.03 2.00 2.00 ...
```

**Exactly 2.00 s apart** — the `--stats-every 2` snapshot interval.

`UrbMeter.summary()` sorted every URB gap it had ever recorded, and
`gap_histogram()` walked the whole deque again, for both endpoints, every two
seconds. After a few minutes that is tens of thousands of records and ~15 ms of
work — on the asyncio thread that owes the isochronous endpoints a service
interval every 1 ms. Fifteen consecutive intervals missed, and the frame clock
correctly resynchronised. **The act of measuring produced the fault the
measurement existed to report.**

A control experiment confirmed it before anything was changed — same hardware,
same audio, only the snapshot cadence different:

| snapshot interval | iso OUT resyncs | iso IN resyncs |
|---|---|---|
| `--stats-every 2` | **35–41**, spaced 2.00 s | 1 |
| first snapshot after the audio finished | **1**, at stream start | 1 |

#### `asyncio.to_thread` did not fix it — and that is the lesson

The obvious fix is to move the snapshot off the event loop. It made no
difference at all: the next soak still logged 39 resyncs, still at 2.00 s.

**`sorted()` on a list of floats is a single C call that never releases the
GIL.** CPU-bound work in a worker thread blocks the event loop just as
completely as work done inline. Threads move *blocking I/O* off the loop; they
do nothing for CPU-bound Python.

The real fix is to make the work cheap:

* exact totals (`urbs`, `packets`, `bytes`, first/last timestamp) are now
  running counters updated in `record()`, O(1) per URB, so **no reported count
  is ever approximate**;
* the distribution stats (median, p99, histogram) are computed over the last
  `UrbMeter.SUMMARY_WINDOW = 4000` URBs only — ~40 s of history, ~0.2 ms to
  sort, comfortably inside one service interval;
* `records_snapshot()` takes that tail with `itertools.islice` rather than
  copying the whole deque, and retries on the `RuntimeError` that iterating a
  deque during concurrent append can raise.

Re-measured on hardware, same `--stats-every 2`, same 120 s concurrent soak:

| | before | after |
|---|---|---|
| iso OUT resyncs | 39 | **1** (stream start) |
| iso OUT packets/s | 514.3 | **1000.3** |
| iso IN packets/s | 568.7 | **1001.1** |
| U→B underrun frames | 32 | **0** |
| HID rate | 248.49/s | **250.33/s** |
| HID gap max | 30.8 ms | **12.4 ms** |
| C→U skew corrections | 1339 | **842** |

Note the packets/s columns: before the fix the *reported throughput* was also
wrong, because the snapshot's own stalls were inside the span it measured. The
fix improved the real system too — the HID endpoint gained 2 Hz and its worst
gap more than halved — because those 15 ms stalls were hurting every endpoint,
not only the one whose counter noticed.

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

`clock_seam()` publishes the whole seam as JSON under `--stats-json`, and the
soak is the first time it has ever run on hardware. **Both policies behaved as
designed:**

* **U→B**, the seam that cannot drift: `audio_underrun_frames` **0**,
  `audio_q_drop_frames` **0**, out-ring peak 50 ms against a 120 ms cap,
  overflow 0 B, across 5584 `0x39` reports with 0 write errors. The queue was
  never starved and never had to be trimmed — which is what "same oscillator,
  exact integer ratio" predicts, now confirmed rather than argued.
* **C→U**, the seam that does drift: 634 pad + 208 drop single-frame
  corrections over 120 s = **0.015 %** of samples, depth steady at 56 ms inside
  a [10, 90] ms band, ring overflow 0 B. The governor sat idle in mid-band and
  slewed only at the edges, exactly as intended.

The one design note the hardware added: depth settled at ~56 ms rather than the
25 ms target, because the governor deliberately does nothing inside the band.
That is a latency the design accepts in exchange for never correcting when it
does not have to. Narrowing the band would trade audio latency for more
frequent correction; nothing observed here justifies it.

---

## 5. The stage that ran in two sittings

Stages (a) and (b) ran on `0011223344aa` at **10 % battery** — acceptable
because they drive no speaker, no haptic actuator and no microphone, and the
battery read 10 % before and 10 % after. Stages (c)(d)(e) waited for the
healthy unit: they drive both actuators continuously for minutes at a time, and
`0011223344aa` is the same unit STATUS §16.2 records as having **died at 10 %**
during Phase 3b, producing a tail of symptoms that looked exactly like protocol
bugs.

Once `0011223344bb` was moved back to Bluetooth at **90 %**, (c)(d)(e) ran and
passed. The split is recorded here because the two halves of §2 were measured
on different controllers, over a different-quality link — which is visible in
the data and worth reading as signal rather than noise:

| | (a)(b) on `0011223344aa` @10 % | (c)(d)(e) on `0011223344bb` @90 % |
|---|---|---|
| BT input rate | 320–322 Hz | 400–480 Hz |
| input repeats | 50.3 % | ~32 % (matches §16.2) |
| HID delivered | 249.90/s | 248.49/s under full audio load |

**The repeat fraction is a link-quality readout, not a fault.** A weaker link
delivers fewer Bluetooth reports per second, so more of the fixed 250 Hz polls
land inside a burst gap and repeat the current state — which is exactly what a
real wired DualSense does anyway. The delivered rate is unaffected, and that is
the point of `repeat_stale_input`.

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
* Input: 249.90 Hz, 100.00 % field parity, 0 sequence discontinuities —
  indistinguishable from the physical wired unit on every measure taken.
* Output: byte-exact adaptive-trigger control from a hidapi write to the
  virtual device.
* **Audio out**: the virtual Windows render endpoint drives the real speaker
  (+64.4 dB) *and* the real haptic voice coils (+27.8 dB), with the channel
  mapping proven by single-pair runs rather than assumed.
* **Audio in**: the controller's own microphone reaches the virtual Windows
  capture endpoint in exclusive mode, cleanly enough to FFT.
* **Both at once, for two minutes**: 1000.3 / 1001.1 packets/s on the two
  isochronous endpoints, every packet full, **1 frame-clock resync each and
  both at stream start** — E1's pass criterion, met by the joined system.
  0 underrun frames, 0 queue drops, 0 ring overflows, 0 write errors, 0 decode
  errors, 0 capture overflows, 0 HID sequence breaks.
* The U→B and C→U buffering policies, on hardware.
* The merged emulator is correct on the HID path under the real Windows driver,
  after three defects that only a live attach could have exposed.

**Not established.**

* **Anything beyond ~4 minutes.** The longest continuous run was 120 s. Thermal
  behaviour, long-run drift and battery-sag effects are unmeasured.
* Headphone routing (nothing plugged into the 3.5 mm jack).
* The UAC volume heuristic against a real host mixer (risk R7 — `uac.py`'s
  MIN/MAX/RES are still assumed values).
* The feature-report prefetch list. Only `0x05` and `0x20` are cached;
  everything else STALLs and is counted in `feature_misses`. This run recorded
  no misses, so Windows asked for nothing else — but no game has been run.
* Reconnect. Implemented, never force-tested; `reconnects` stayed 0.
* **Audio *fidelity*, as opposed to audio *presence*.** Every audio result here
  is band energy at a commanded frequency against a silent baseline. That
  proves the path carries the signal and the channel mapping is right; it does
  not measure distortion, latency or the quality of the 48→45 kHz conversion.
  A cleaner measurement would record the emulator's received stream with
  `--record-out` and compare it sample-for-sample against what was played.
* No game or Sony PC SDK title. Still the real acceptance test.

---

## 8. How to re-run all five stages

```powershell
cd <repo>\emulator

# 1. emulator. SELECT THE CONTROLLER BY SERIAL (see §1).
..\prototype\.venv\Scripts\python.exe -m ds5emu serve --backend bridge `
    --bt-serial 0011223344bb --port 3241 `
    --stats-json C:\Temp\ds5c_stats.json --stats-every 2

# 2. attach. usbipd owns 3240 and is never touched; --tcp-port is GLOBAL and
#    must precede the subcommand.
& "C:\Program Files\USBip\usbip.exe" --tcp-port 3241 attach -r 127.0.0.1 -b 1-1

# 3. which endpoints are ours? Do not guess -- see §6, trap 9.
powershell -File tools\e2e_endpoints.ps1

# 4. the stages
..\prototype\.venv\Scripts\python.exe tools\e2e_input_parity.py --seconds 30
..\prototype\.venv\Scripts\python.exe tools\e2e_setstate.py
..\prototype\.venv\Scripts\python.exe tools\e2e_audio.py --mode both
..\prototype\.venv\Scripts\python.exe tools\e2e_audio.py --mode speaker
..\prototype\.venv\Scripts\python.exe tools\e2e_audio.py --mode haptics
..\prototype\.venv\Scripts\python.exe tools\e2e_soak.py --seconds 120

# 5. teardown -- idempotent, safe twice, safe when nothing is up
powershell -File tools\e2e_teardown.ps1
```

Each tool prints `RESULT: PASS` or `FAIL` and exits accordingly, so they chain
in CI style. Read the battery first if anything fails.

---

## 9. End state

```
usbip port                     EMPTY
python processes               none
TCP 3241                       free
usbipd                         Running / Automatic, never stopped or reconfigured
usbip2_ude, usbip2_filter      Running / Manual (as installed)
present VID_054C&PID_0CE6      the physically-plugged unit and its two children only
system configuration           unchanged -- no driver installed, no service
                               reconfigured, no registry write, no reboot
controllers                    0011223344bb on Bluetooth at 80 %;
                               0011223344aa on the USB cable, charging
```

## 6. Game validation — 2026-08-25 (user-run)

**Spider-Man: Miles Morales (Sony PC port): PASS — user-reported "worked flawlessly, all DualSense features worked straight away."** The game detected the virtual device as a wired DualSense and enabled adaptive triggers, HD haptics, speaker and lightbar without any configuration. This settles open question R2 (identity): the missing `LocationPaths` and the `usbip2_ude` parent controller do NOT matter to Sony's PC SDK. The pipeline is validated end-to-end by the target workload.
