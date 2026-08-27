# The wired gap: what the emulated DualSense still did differently

**Date:** 2026-08-27
**Method:** a real wired DualSense and this emulator, attached to the same
machine at the same time, probed through the same `HidUsb` + `hidclass` stack,
byte for byte.
**Result:** two independent defects, both now fixed, plus six deviations that
are documented here and deliberately left alone.

The descriptors were never the problem — `docs/identity-comparison.md` was
right, and this run reconfirmed it: **the HID report descriptor Windows parsed
off the emulated device is byte-identical, all 467 bytes, to the one it parsed
off the physical wired unit.** The difference was entirely in *which control
requests get an answer*.

---

## The measurement

Both controllers were present: one real DualSense on a USB cable, one on
Bluetooth driving the bridge. `emulator/tools/hid_diff_probe.py` walks report
ids `0x00`–`0xFF` and issues, read-only:

| what the probe calls | what goes on the wire |
|---|---|
| `hid_get_feature_report(id)` | control `GET_REPORT(Feature, id)` |
| `hid_get_input_report(id)` | control `GET_REPORT(Input, id)` |
| `hid_get_report_descriptor()` | the parsed HID report descriptor |

Because both devices are bound to Microsoft's own HID drivers, a request that
succeeds on one and fails on the other is a difference **in the device**, not in
the stack. That is the whole value of running them side by side.

### What answered, before the fix

| feature report | real wired | emulator (before) | emulator (after) | Bluetooth pad |
|---|---|---|---|---|
| `0x05` calibration | 41 B | 41 B | 41 B | 41 B |
| `0x09` **MAC / pairing** | **20 B** | **STALL** | **20 B** | 20 B |
| `0x0b` pairing info | **42 B** | **STALL** | **42 B** | 42 B |
| `0x20` firmware info | 64 B | 64 B | 64 B | 64 B |
| `0x22` BT patch info | 64 B | 64 B | 64 B | 64 B |
| `0x81` **test result** | **64 B** | **STALL** | **64 B** | 64 B |
| `0x83` | 64 B | STALL | STALL | — |
| `0x85` | 4 B | STALL | STALL | — |
| `0xe0` | 64 B | STALL | STALL | — |
| `0xf1` | 64 B | STALL | STALL | 64 B |
| `0xf2` | 16 B | STALL | STALL | 16 B |
| `0xf5` | 4 B | STALL | STALL | 8 B |
| `GET_REPORT(Input, 0x01)` | **STALL** | **64 B** | 64 B | STALL |

And the parts that were already right, so that the ranking below is honest
about what was *not* wrong:

- report descriptor: identical, 467 bytes;
- interrupt IN: 250.1 reports/s on the real unit, 250.5 on the emulator, all
  64 bytes, same field layout;
- `GET_REPORT(Feature, 0x80)` STALLs on both, correctly;
- `GET_REPORT(Input)` for every id other than `0x01` STALLs on both.

Raw captures: `real-wired.json`, `real-bt.json`, `virtual*.json` produced by the
probe; regenerate with the commands under "Reproducing this" below.

---

## Symptom 2 — "play sound" in dualsense-tester does nothing

### The mechanism

The button is **not** an audio-streaming feature, which is why the working
speaker path was a red herring. It is
`src/router/DualSense/views/AudioControlWidget.vue` →
`controlWaveOut()` in `src/utils/dualsense/ds.util.ts:673`, and it asks the
controller's *own firmware* to generate a 1 kHz tone. Two HID feature writes,
no audio stream at all:

```
SET_REPORT(Feature, 0x80)  body = [06, 04, <20-byte path select>]   AUDIO / BUILTIN_MIC_CALIB_DATA_VERIFY
SET_REPORT(Feature, 0x80)  body = [06, 02, 01, 01, 00]              AUDIO / WAVEOUT_CTRL  (start)
   ... on release ...
SET_REPORT(Feature, 0x80)  body = [06, 02, 00, 01, 00]              AUDIO / WAVEOUT_CTRL  (stop)
```

They go through `setTestCommandWithParams` (`ds.util.ts:389`), which is
**fire-and-forget** — it sends the 0x80 and returns without ever reading 0x81
back. So whether the button makes a sound is decided by exactly one thing:
does that `SET_REPORT` reach the controller?

It did not. `TEST_COMMAND_ALLOWLIST` in `emulator/ds5emu/bridge.py` admits only
read-only factory-test subcommands, and neither `(0x06, 0x02)` nor
`(0x06, 0x04)` was on it. Replaying the tester's exact bytes against the
emulated pad (`emulator/tools/waveout_probe.py`) produced, in the emulator log:

```
BLOCKED factory-test command device=0x06 action=0x04: not on the read-only allowlist.
BLOCKED factory-test command device=0x06 action=0x02: not on the read-only allowlist.
```

Over plain Bluetooth and over a real cable the tester talks to the controller
directly, nothing filters it, and the tone plays. That is the whole difference.

### The second, independent defect on the same button

`GET_REPORT(Feature, 0x81)` STALLed whenever no allowlisted command was armed.
A real wired DualSense **always answers** — measured, a cold `0x81` returns 64
bytes of `81 00 00 …`. This matters because the tester's result poll is

```ts
while (report = await receiveFeatureReport(item, 0x81)) { ... }
```

A STALL throws `receiveFeatureReport`, which exits the loop through the `catch`
and fails the command outright, where a real pad returns an idle header and lets
the caller poll out its own 1000 ms budget. Every panel in the tester that uses
the factory channel — Factory Info, Diagnostics — was one STALL away from a
spurious failure.

### The fix

`emulator/ds5emu/bridge.py`:

1. `(0x06, 0x02)` `WAVEOUT_CTRL` and `(0x06, 0x04)`
   `BUILTIN_MIC_CALIB_DATA_VERIFY` added to `TEST_COMMAND_ALLOWLIST`.
2. `_read_test_result()` returns `FEATURE_TEST_IDLE` — `81` and 63 zeros, the
   measured hardware answer — instead of `None`, whenever nothing is armed, the
   link is down, the Bluetooth read times out, or it raises.

**On the safety rule this bends.** The rest of the allowlist is read-only on
purpose, and the reason is to protect *persistent* controller state: pairing,
flash, calibration, settings. Neither of these two touches any of it.
`WAVEOUT_CTRL` toggles a tone that stops on the next `[0,1,0]` and does not
survive a power cycle; action 4 is the `VERIFY` member of the
`BUILTIN_MIC_CALIB_DATA` family, not `_SEND` (2) or `_STORE` (3) — the two that
would actually write, and which remain blocked, as does everything else.

Note that `README.md` still says feature writes are "deliberately blocked and
never forwarded, even if a host asks". That is now one sentence too absolute and
should be amended to name this exception. README edits were out of scope for
this pass.

### Verified so far, and what needs you

Confirmed by machine: with the fix the emulator logs
`forwarding factory-test command device=0x06 action=0x02 (WAVEOUT_CTRL …)`, the
CRC-signed 0x80 is accepted by the controller's HID stack (no `rc = -1`
rejection), and an allowlisted command through the identical path
(`SYSTEM / READ_SERIAL_NUMBER`) returns real data from the physical pad.

**Not confirmed by machine: that a tone is audible.** That needs an ear.

> **Verification, symptom 2**
> 1. Start the emulator and attach it (see "Reproducing this").
> 2. Open <https://dualsense-tester.daidr.me/> (or the local dev server) in
>    Chrome/Edge, click "Connect", and pick the **DualSense Wireless
>    Controller** — if two appear, the emulated one is the entry whose devnode
>    instance id is *not* the physical pad's.
> 3. Go to the **Audio** panel and press and hold **Speaker 1 kHz sine wave**.
> 4. Expected: a tone from the controller's speaker for as long as you hold,
>    silence on release. Try **Headphone 1 kHz sine wave** with headphones in.
> 5. While you are there, open **Factory Info** and **Diagnostics** — both drive
>    the same 0x80/0x81 channel and both should populate.

---

## Symptom 1 — The Last of Us Part 1 gets no input at all

### Ranked, with the evidence for each

**#1 — `GET_REPORT(Feature, 0x09)` STALLed. Very likely the whole answer.**

Report `0x09` is the DualSense's MAC/pairing report, and it is the **first thing
libScePad does to a DualSense** — libScePad being the Sony controller library
every Sony PC port links against. In the open-source reimplementation
(`WujekFoliarz/duaLib`) the entire registration of a controller sits inside a
single `if`:

```c
// src/source/duaLib.cpp:145
if (duaLibUtils::getMacAddress(handle, newMac, g_deviceList.devices[j].Device, info->bus_type)) {
        ... this is the only path that ever sets controller.valid = true ...
}
```

and `getMacAddress` for a DualSense is one feature read:

```c
// src/source/duaLibUtils.cpp:185-198
unsigned char buffer[20] = {};
buffer[0] = 0x09;                       // Report ID
int res = hid_get_feature_report(handle, buffer, sizeof(buffer));
if (res > 0) { ...parse ClientMac[0..5]...; return true; }
return false;                           // <-- a STALL lands here
```

A device that never becomes `valid` makes every later `scePadReadState` return
`SCE_PAD_ERROR_DEVICE_NOT_CONNECTED` (`duaLib.cpp:419`). That is precisely the
reported symptom: the pad enumerates, Windows is happy, the game sees nothing.
It also explains the split cleanly — **Miles Morales is a Nixxes port that does
its own DualSense HID handling and never asks for 0x09**, which is why every
feature in it worked while a libScePad title got zero input.

This is a hypothesis about TLOU specifically, since duaLib is a clean-room
reimplementation rather than Sony's binary. What is *not* a hypothesis is the
gap itself: a real wired DualSense answers 0x09 with 20 bytes and this emulator
STALLed it.

**#2 — `GET_REPORT(Feature, 0x0b)` STALLed.** Same family, 42 bytes on the real
unit. Nothing observed reads it, but it was a gap and it was free to close.

**#3 — six reports the physical wired unit answers and the emulator still
STALLs.** `0x83`, `0x85`, `0xe0`, `0xf1`, `0xf2`, `0xf5`. Not fixed — see
"Deliberately not fixed" below, which includes the captured bytes so a follow-up
can serve them.

**#4 — `GET_REPORT(Input, 0x01)` on the control pipe: the emulator answers where
the real device STALLs.** The one deviation in the *permissive* direction. Left
alone; it is very hard to construct a story where a device answering a request
causes a game to see less input, and the behaviour is deliberate (see the
comment in `device.py::_hid_class`). Recorded because "byte-identical" is the
project's claim and this is a fingerprintable difference.

**Ruled out by measurement, so nobody re-investigates them:**

- the HID report descriptor (identical, 467 bytes);
- input report rate and shape (250.1 vs 250.5 Hz, 64 bytes, same fields);
- `GET_IDLE` / `SET_IDLE` / `GET_PROTOCOL` (present and answered);
- `GET_REPORT(Feature, 0x80)` (STALLs on both — correct);
- `GET_REPORT(Input)` for any id but `0x01` (STALLs on both).

### The fix

`emulator/ds5emu/bridge.py`: `PREFETCH_FEATURES` now reads `0x09` and `0x0b` off
the Bluetooth controller at open, alongside `0x20` and `0x22`, and serves them
from the same cache. The MAC in the served `0x09` is the bridged controller's
own Bluetooth address, so a game that keys on it gets a genuine, stable,
per-controller identity.

### Making a Bluetooth report look like a wired one

Forwarding the Bluetooth bytes verbatim would have been wrong in two ways, both
now handled in `_feature_bytes`:

- **The CRC trailer.** Measured on both transports: the wired unit ends `0x05`,
  `0x09`, `0x0b`, `0x20` and `0x22` in `00 00 00 00`, the Bluetooth unit ends
  them in a CRC-32. `FEATURE_CRC_TRAILER` zeroes those four bytes. This also
  silently corrected `0x05`, `0x20` and `0x22`, which had been shipping four
  bytes of Bluetooth checksum inside a USB report since the bridge was written.
- **`0x0b`'s link key.** Over Bluetooth `0x0b` carries, after the host MAC, a
  pairing-slot count and link-key material; the wired unit publishes zeros
  there. Serving the Bluetooth bytes would both differ from ground truth and
  hand the controller's pairing material to anything that can open the HID
  device. `FEATURE_0B_WIRED_PREFIX = 17` keeps id + controller MAC + the three
  fixed bytes + host MAC and zeroes the rest.

Confirmed on hardware after the change — emulated on the left, physical wired
pad on the right, same layout, each with its own MAC:

```
0x09  virt  09 bbd80d9cfaa0 082500 1c0eed949ef8 00000000
      wired 09 5d48a14b2fd4 082500 1c0eed949ef8 00000000
0x0b  virt  0b bbd80d9cfaa0 08250000 1c0eed949ef8 00 (x25)
      wired 0b 5d48a14b2fd4 08250000 1c0eed949ef8 00 (x25)
```

### Verification, symptom 1

> 1. Start the emulator and attach it.
> 2. Confirm the report is being served, without launching anything:
>    ```powershell
>    prototype\.venv\Scripts\python.exe emulator\tools\hid_diff_probe.py --list
>    prototype\.venv\Scripts\python.exe emulator\tools\hid_diff_probe.py --match <instance-id-of-the-virtual-pad> --first 0x09 --last 0x0b --no-input
>    ```
>    Expect `feature: 2 answered -> 0x09(20), 0x0b(42)`.
> 3. In Steam, open **The Last of Us Part I** → Properties → Controller and set
>    **Disable Steam Input**. Steam Input re-presents the pad as an Xbox device
>    and would mask what is being tested here.
> 4. Launch the game and check the pause menu / button prompts.
> 5. **If input now works:** please say so — it confirms #1 and closes this out.
> 6. **If it still does not:** the next suspects are the six reports under
>    "Deliberately not fixed", in the order `0xe0`, `0xf2`, `0xf1`, `0x83`,
>    `0x85`, `0xf5`. Their exact bytes are recorded below, so serving them is a
>    small change. Capture `--stats-json` from the emulator during the failed
>    launch as well: `feature_misses` names every report id the game asked for
>    and did not get, which turns the next round from guessing into reading.

---

## Deliberately not fixed, with the ground truth to fix them later

Six feature reports the physical wired unit answers, the Bluetooth unit does
**not**, and the emulator therefore cannot forward. Serving them means serving a
constant, and a wrong constant can be worse than a STALL — `0xf1`/`0xf2` are the
authentication challenge/response pair, and a caller that writes `0xf0` and then
polls a canned "idle" status could wait forever where a STALL would make it give
up at once. Captured from the physical wired controller on 2026-08-27, one unit,
so treat them as a sample and not as gospel:

| id | len | bytes |
|---|---|---|
| `0x83` | 64 | `83 ffffffff` then zeros |
| `0x85` | 4 | `85 00 ff 00` |
| `0xe0` | 64 | `e0 04 00 2f 00 00 00 00 00 00 00 00 06 00` then zeros |
| `0xf1` | 64 | `f1` then zeros |
| `0xf2` | 16 | `f2 00 00 10` then zeros |
| `0xf5` | 4 | `f5 00 00 00` |

Also unchanged: `GET_REPORT(Input, 0x01)` still answers where the hardware
STALLs (deviation #4 above).

---

## Reproducing this

```powershell
# one terminal: the emulator, pointed at the Bluetooth pad
cd emulator
..\prototype\.venv\Scripts\python.exe -m ds5emu serve --backend bridge --port 3241 --bt-serial <bdaddr> -v

# another: attach
& "C:\Program Files\USBip\usbip.exe" --tcp-port 3241 attach -r 127.0.0.1 -b 1-1

# the probe. --list first: with a real pad attached too there are two `usb`
# entries, and the tags are ordered by path, so they MOVE when the virtual
# device comes and goes. Select by --match <instance id> instead.
prototype\.venv\Scripts\python.exe emulator\tools\hid_diff_probe.py --list
prototype\.venv\Scripts\python.exe emulator\tools\hid_diff_probe.py --match 7&4b40afc --out real.json
prototype\.venv\Scripts\python.exe emulator\tools\hid_diff_probe.py --match 4&127b94db --out virt.json
prototype\.venv\Scripts\python.exe emulator\tools\hid_diff_probe.py --diff real.json virt.json

# replay the tester's play-sound button (writes only to the pad you name; the
# tool refuses to run without the explicit flag)
prototype\.venv\Scripts\python.exe emulator\tools\waveout_probe.py --match ... --i-know-this-is-real

# teardown. -X FIRST, it stops the auto-reattach.
& "C:\Program Files\USBip\usbip.exe" attach -X
& "C:\Program Files\USBip\usbip.exe" detach -p 1
```

`hid_diff_probe.py` is read-only and safe to point at a physical controller.
`waveout_probe.py` writes report `0x80` and refuses to run without
`--i-know-this-is-real`.

## Files touched

| file | change |
|---|---|
| `emulator/ds5emu/bridge.py` | prefetch `0x09`/`0x0b`; zero the BT CRC trailer; trim `0x0b`'s link key; `0x81` answers idle instead of STALLing; wave-out pair added to the allowlist |
| `emulator/tests/test_bridge.py` | tests for all of the above; the three tests that asserted the old STALL behaviour updated |
| `emulator/tools/hid_diff_probe.py` | new — the read-only side-by-side prober |
| `emulator/tools/waveout_probe.py` | new — replays the tester's play-sound button |

Test suite: `226 tests, OK`
(`cd emulator && ..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .`).
