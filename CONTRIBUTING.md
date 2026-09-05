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
  tests/            185 unit tests
  tools/            live end-to-end harnesses (need hardware + the driver)
prototype/         the Bluetooth-side core
  ds5bridge/        protocol, CRC, hidapi wrapper, pacing, audio
  tools/            descriptor dumpers, closed-loop audio/haptic proofs
app/               the product layer: what a person actually runs
  ds5app/
    usbip.py        finding and driving usbip.exe
    controller.py   which Bluetooth DualSense, and is it alive
    service.py      ONE bridge: bring-up and tear-down order. Read the docstring.
    manager.py      N bridges, one child process each. Read the docstring for
                    the measurements that chose processes over threads.
    config.py       %APPDATA% settings. load() must never raise -- it runs at
                    login, before there is anywhere to show an error.
    autostart.py    the HKCU Run key
    cli.py          ds5bridge
    tray.py         the tray icon, built once over callables
  tests/            158 unit tests, all against fakes
  tools/            multi_soak.py and the other live harnesses
  packaging/        PyInstaller spec and build.ps1
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

## Running the app while you work on it

Nothing has to be built or installed to get the real tray icon. `app/ds5app` is
plain Python, and `app\tools\dev_tray.ps1` starts it the way the shipped exe
starts it:

```powershell
powershell -File app\tools\dev_tray.ps1              # console window + live logs
powershell -File app\tools\dev_tray.ps1 -Windowed    # no console, like ds5bridge-tray.exe
powershell -File app\tools\dev_tray.ps1 -Isolated    # scratch DS5_CONFIG, not your real settings
powershell -File app\tools\dev_tray.ps1 -Stop        # put it down again
```

Edit a file, run it again — each start stops the previous one first, because two
bridges contend for the same instance mutex and port range and the resulting
mess looks exactly like a bug in whatever you just changed.

**Stop it through the script, the tray's Quit item, or Ctrl+C — not Task
Manager.** A hard kill skips the teardown, and the teardown is what detaches the
device and gives back a Bluetooth pad that HidHide is hiding. `-Stop` sends
CTRL_BREAK where it can and runs `ds5bridge cleanup` where it cannot, so either
way the debt is repaid; killing the process yourself repays neither.

The underlying commands, if you would rather type them, are in `app/README.md`.
Quit the installed build from its tray menu before starting a dev one.

## Running the tests

```powershell
cd emulator
python -m unittest discover -s tests -t .

cd ..\app
python -m unittest discover -s tests -t .
```

Expect **185 tests in about 4 seconds** from `emulator`, and **158** from `app`.
On a bare Python with none of the packages above the emulator suite runs 137 and
skips 48 — the 48 are `test_bridge.py`, which needs numpy/PyAV/hidapi but still
needs **no hardware and no driver**. CI runs both configurations of the emulator
suite and both must be green.

The `app` suite needs `hidapi` importable (the `ds5app` package imports it on
the way in) but no controller, no driver and no tray: the settings, manager and
tray tests all run against fakes. It runs in the with-deps CI job.

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

### If you work on the HidHide feature

`app/ds5app/hidhide.py` hides the real Bluetooth pad while a controller is
bridged. Three things to know before you touch it:

- **Running from source whitelists your venv's `python.exe`.** `our_images()`
  registers `prototype\.venv\Scripts\python.exe` with HidHide so the bridge and
  the tray can still open a pad they have hidden. Say it plainly: **any script
  run by that interpreter can then open hidden HID devices**, not just this one.
  It is your own venv, so it is a defensible grant -- but it is a real one, and
  it stays until HidHide's whitelist is cleaned.
- **Hiding is a debt in the registry.** It survives your crash, `taskkill /F`, a
  bugcheck and a reboot; nothing expires on its own. That is why the journal is
  written *before* the hide and deleted *after* the unhide, and why `sweep()`
  runs on every process start. If you change that ordering, you can strand
  somebody with a controller they cannot use. The tests in
  `app/tests/test_hidhide.py` exist to stop that and should not be relaxed.
- **Uninstall HidHide only with its own uninstaller.** Removing the driver in
  Device Manager leaves its `UpperFilters` entries behind and every HID device
  on the machine then fails to start -- no keyboard, no mouse, recoverable only
  from the Windows Recovery Environment.

`docs/hidhide-scoping.md` has the design and the H0 hardware findings, including
the two that contradict the obvious implementation: HidHide's `Parameters`
registry key is unreadable even elevated, and its control device can stop
answering while `HidHideCLI.exe` keeps working.

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

The name lives only in prose — no module, package or import uses it, so it stays
cheap to change. To change it:

```powershell
$old = 'DS5 Virtual Dongle'; $new = 'NewName'
git ls-files | ForEach-Object {
    $text = Get-Content -Raw -LiteralPath $_
    if ($text -and $text.Contains($old)) {
        Set-Content -NoNewline -LiteralPath $_ -Value $text.Replace($old, $new)
        "renamed in $_"
    }
}
```

Driving it off `git ls-files` means nothing untracked or ignored is touched,
and only files that actually contain the name get rewritten (`build*/` and
`dist*/` are ignored, so no PyInstaller binary is read). Then check
`git grep -i <old name>` comes back empty, and rename the GitHub repository to
match.

## Code of conduct

Be decent. This is a gift to people who want their controller to work properly;
keep it feeling like one.
