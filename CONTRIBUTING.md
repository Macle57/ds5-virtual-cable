# Contributing

Thanks for looking. This project is small, unusual, and unusually well
documented — the fastest way to be useful is to read `docs/STATUS.md` first.
It is the handoff document, written so that someone with no context but this
repository can pick the work up.

## The one thing that would help most right now

**Try a game and report what happened.** The pipeline is proven end to end, but
exactly one title has ever been tested. A one-line issue saying "game X, worked /
did not work, here is what I saw" is worth more than most patches.

## Repository layout

```
emulator/          the USB/IP device emulator -- STDLIB ONLY, on purpose
  ds5emu/
    wire.py         USB/IP wire protocol. Pure bytes in / bytes out.
    descriptors.py  the real controller's descriptors, from docs/usb-ground-truth.md
    uac.py          UAC1 mixer control state
    timing.py       the frame clock. READ THIS BEFORE CHANGING PACING.
    device.py       the emulated device: CMD_SUBMIT -> RET_SUBMIT, pure
    backend.py      Backend ABC + SyntheticBackend (no hardware)
    bridge.py       BridgeBackend: the live Bluetooth seam
    translate.py    BT <-> USB report translation, pure
    server.py       asyncio TCP shell, thin on purpose
  tests/            173 unit tests
  tools/            live end-to-end harnesses (need hardware + the driver)
prototype/         the Bluetooth-side core
  ds5bridge/        protocol, CRC, hidapi wrapper, pacing, audio
  tools/            descriptor dumpers, closed-loop audio/haptic proofs
docs/              see the table in README.md
```

Two structural rules that are load-bearing, not stylistic:

1. **`emulator/ds5emu/` imports nothing outside the standard library**, except
   `bridge.py`, which is imported lazily through a module `__getattr__`. This is
   what lets 132 of the tests run on a bare Python with no packages and no
   driver. If you add a `numpy` import to `wire.py`, CI will tell you, but the
   real cost is that the protocol stops being testable on any machine.
2. **Everything protocol-shaped is a pure function.** `wire.py` and `device.py`
   have no sockets and no threads. That is why the whole USB behaviour can be
   exercised without a driver installed — and it is also the escape hatch if the
   hot loop ever has to move to another language.

## Development setup

```powershell
git clone <this repo>
cd <repo>
python -m venv prototype\.venv
prototype\.venv\Scripts\python.exe -m pip install hidapi numpy av sounddevice
```

- `hidapi` (cython-hidapi) — the `hid` module; only the Bluetooth side needs it.
- `av` (PyAV) — Opus **and** swresample, bundled in the Windows wheel, so there
  is no libopus to source separately. `opuslib` was tried and rejected: it ships
  no DLL on Windows.
- `numpy` — audio buffer maths.
- `sounddevice` — only `prototype/tools/cross_mic_test.py` and the
  `emulator/tools/e2e_*` harnesses.

## Running the tests

```powershell
cd emulator
python -m unittest discover -s tests -t .
```

Expect **173 tests in about 4 seconds**. On a bare Python with none of the
packages above, **132 run and 41 skip** — the 41 are `test_bridge.py`, which
needs numpy/PyAV/hidapi but still needs **no hardware and no driver**. CI runs
both configurations, and both must be green.

There is no hardware in CI and there never will be. If you add a test that needs
a controller, put it in `emulator/tools/` as a harness, not in `tests/`.

## Testing against real hardware

The live harnesses each print `RESULT: PASS` or `FAIL` and set an exit code, so
they chain. They are documented in `docs/STATUS.md` §16.9 and §17.7.

**Read this before you run any of them. Both items have cost real debugging
hours, more than once.**

### 1. Check the battery first, and check it again afterwards

**A dying DualSense produces failure modes that are indistinguishable from
protocol bugs**: dropped reports, timeouts, audio that stops. One controller
died at 10 % mid-phase and the tail of that session looked exactly like a
translation regression.

- Read the battery level out of the input report the moment you claim the
  device, and log it.
- Anything that drives the speaker or the haptic actuators continuously — the
  audio and soak stages — needs a healthy controller. Audio playback drains it
  fast.
- Re-read the battery whenever behaviour turns strange, before you start
  bisecting.

### 2. Select the controller by serial. Never by index, never from memory.

**A controller charging over a USB cable still enumerates over Bluetooth**, as a
stale entry whose feature reads fail — and enumeration is ordered by path, so
the dead one can sort first:

```
[1] BT 0011223344aa   feature read failed: read error      <- charging on USB
[2] BT 0011223344bb   fw 'Sep 18 2025 13:15:28'            <- the live one
```

"First Bluetooth match" would have claimed the wrong device. Pass
`--bt-serial <bdaddr>` (or `BridgeBackend(serial=...)`), and check the
emulator's `opened BT serial=...` log line before you trust a measurement.

**Never cache a serial, a HID path or a battery level across sessions.** Run
`python -m ds5bridge list` every time. Controllers get swapped, re-paired and
plugged in, and every one of those changes the identifiers.

### 3. Tear down properly

```powershell
powershell -File emulator\tools\e2e_teardown.ps1
```

Idempotent and safe to run when nothing is up. Doing it by hand, `usbip attach -X`
comes **before** `detach`, or you acquire a second attached device on the next
run. Verify with `usbip.exe port`, which prints nothing when clean.

### 4. Do not guess which device you are measuring

The virtual and physical controllers look identical two *and* three levels up
the devnode tree; the host controller four levels up is the distinguishing fact
(`ROOT\USB\0000` / `usbip2_ude` vs `PCI\...` / `USBXHCI`). The `2-`/`3-` prefix
in an audio endpoint's friendly name moves between runs and must never be
trusted. Run `emulator\tools\e2e_endpoints.ps1`, which does the walk and prints
`RENDER=`, `CAPTURE=` and `HIDNODE=`.

## Things that will bite you

`docs/STATUS.md` §8, §16.6 and §17.8 are a numbered list of empirical traps,
each with the measurement that found it. The four with the widest blast radius:

- **Never spin on a bare `pass`** — it never releases the GIL and starves every
  other Python thread. Use `time.sleep(0)`. And do not run *that* at 1 ms
  either: releasing the GIL 100 000 times a second is its own denial of service,
  measured here as microphone throughput halving.
- **The emulator is the audio clock.** There is no bus clock over USB/IP. Read
  `emulator/ds5emu/timing.py` before touching anything that paces.
- **Instrumentation can manufacture the fault it measures.** A stats snapshot
  that sorted its whole history took 15 ms on the event loop every 2 seconds and
  produced 39 fake underruns. `asyncio.to_thread` does **not** fix this —
  `sorted()` is one C call that holds the GIL wherever it runs. Anything on the
  event loop must cost well under one 1 ms service interval.
- **Never use a HID write's return value as a byte count** on Windows. It
  returns the padded buffer size — a 78-byte write reports 547.

## Documentation culture

This repository is written for whoever picks it up next, including you in three
months. Two conventions:

- **`docs/STATUS.md` is the handoff.** Substantial work appends a numbered
  section: what was done, what is verified *with the observation attached*, what
  is implemented but unconfirmed, the traps found, and what the next person
  should do. Sections are wrapped in `BEGIN/END` HTML comment markers so that
  parallel work does not collide.
- **Separate "verified" from "assumed", always.** Every claim in `docs/` either
  carries a measurement or is explicitly labelled unverified. `docs/STATUS.md`
  §6 is a standing list of things that are implemented but never confirmed —
  keeping it honest is more valuable than shortening it.

## Pull requests

- Run the tests. Both configurations.
- Say what you verified and how. "Ran the input soak, 15 s, 0 discontinuities,
  battery 85 % throughout" is the house style.
- If you found a trap, add it to the numbered list in `docs/STATUS.md` with the
  measurement that found it. That list is the most valuable thing here.
- Keep `emulator/ds5emu/` stdlib-only.
- Line endings are pinned by `.gitattributes`; you should not have to think
  about them.

## Renaming the project

The name is a placeholder and lives only in prose — no module, package or import
uses it. To change it:

```powershell
Get-ChildItem -Recurse -Include *.md,*.yml,LICENSE,NOTICE -Exclude .git |
  ForEach-Object { (Get-Content -Raw $_) -replace 'PhantomCable','NewName' |
  Set-Content -NoNewline $_ }
```

Then check `git grep -i phantomcable` comes back empty.

## Code of conduct

Be decent. This is a gift to people who want their controller to work properly;
keep it feeling like one.
