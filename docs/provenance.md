# Provenance and licence audit

*Written for the Phase 4b open-source release. This is an engineering audit, not
legal advice.*

The question this document answers is narrow and specific:

> **Was any code in this repository copied from the two reference
> implementations, or is it independent work that used them as protocol
> documentation?**

Short answer: **mostly independent, with one clearly identified exception that is
fully licence-compatible.** `prototype/ds5bridge/protocol.py` contains
hand-written ports of four functions from `dualsense-tester` and one from
`DS5Dongle`. Both projects are **MIT-licensed**, so the entire obligation is to
keep their copyright notices — which `NOTICE` and `ATTRIBUTIONS.md` now do.
Nothing here is licence-incompatible and nothing has to be removed or rewritten.

---

## 1. Licences of everything this project touches

| project | role here | licence | verified how |
|---|---|---|---|
| [daidr/dualsense-tester](https://github.com/daidr/dualsense-tester) | protocol reference (Bluetooth reports, CRC seeds, input offsets, audio/mic reports) | **MIT**, © 2023 Xuezhou Dai (daidr) | read `LICENSE` in the local clone |
| [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle) | protocol reference (report `0x39`, Opus/haptic parameters, the wired-USB identity concept) | **MIT**, © 2026 awalol | read `LICENSE` in the local clone |
| [vadimgrn/usbip-win2](https://github.com/vadimgrn/usbip-win2) | the signed UDE driver users install; its headers documented the USB/IP wire format we implement | **BSD-2-Clause** since release 0.9.7.0 (GPL-3.0 before that) | its own `Readme.md`, shipped in the installed 0.9.7.7 package, states "The 2-Clause BSD License since release 0.9.7.0"; confirmed against the GitHub project page |
| CPython standard library | the emulator's only runtime dependency | PSF-2.0 | — |

**The pre-0.9.7.0 GPL-3.0 detail matters and is worth stating explicitly**: this
project targets **0.9.7.7**, which is BSD-2-Clause. Nothing in this repository is
derived from a GPL-era usbip-win2 source file, and no usbip-win2 code or header
is part of this repository. Since the installer work (2026-09), the *bundled*
build of the setup exe does redistribute usbip-win2's official installer as a
binary, unmodified; BSD-2-Clause's notice condition therefore applies to that
build and is discharged by `app/packaging/bundle/THIRD-PARTY-NOTICES.txt`,
which the bundled installer ships next to the uninstaller (`NOTICE` describes
both builds). The download build and `scripts/install.ps1` still fetch it
from its own releases page and redistribute nothing.

## 2. Method

1. `git log` reviewed commit by commit (29 commits, single author, no history
   rewrite, no vendored third-party trees at any point).
2. Every source file read in full, and every module docstring's provenance claim
   checked against the referenced file in the sibling clones.
3. Grep sweeps for the fingerprints of copy-paste:
   - **CJK characters** — `dualsense-tester` is commented almost entirely in
     Chinese. **Zero hits** across every `.py`, `.md` and `.ps1` in this
     repository. A copy-paste from that project would almost certainly have
     dragged a comment with it.
   - `port of` / `copied from` / `taken from` / `adapted from` — 8 hits, all
     self-declared, all in `prototype/ds5bridge/` (listed in §4).
   - references to the sibling repos by name — 40 hits, all citations in
     docstrings and docs, none accompanied by pasted code.
4. **Byte-level comparison of the descriptor tables**, which is the one place a
   copy would be invisible to a text diff (see §3).

## 3. The USB descriptors — the case that had to be proved, not asserted

`emulator/ds5emu/descriptors.py` claims its bytes were read off the developer's
own physical controller rather than lifted from `DS5Dongle/src/fake_ds5.h`. That
claim was tested, not taken on trust.

**Result: the two are demonstrably different artefacts.**

| | this project | DS5Dongle |
|---|---|---|
| HID report descriptor | **289 bytes**, `docs/usb-ground-truth.md` | `desc_hid_report_ds`, **321 bytes** |
| relationship | identical for the first 288 bytes, then this project emits `0xC0` (END_COLLECTION) | continues with four more feature reports (`0x85 F6`, `F7`, `F8`, `F9`) before its own `0xC0` |

The shared prefix is exactly what should be expected: **both are dumps of the
same Sony hardware**, and the difference is a firmware revision that declares four
extra vendor feature reports. Two independent dumps of the same device agreeing is
evidence of accurate measurement, not of copying — and if this project had copied
DS5Dongle's array, it would be 321 bytes long, not 289.

Independently corroborating:

- `docs/usb-ground-truth.md` records the **capture method** (USB hub IOCTLs,
  the USBView technique) and ships the tool that reproduces it,
  `prototype/tools/usb_descriptors.py`. Anyone with a wired DualSense can
  regenerate the file.
- `emulator/tests/test_descriptors.py` re-parses the hexdumps out of that
  markdown file and asserts the constants match byte for byte, so the tables
  cannot drift away from the recorded measurement.
- `fake_ds5.h` is not a descriptor file at all — it is a canned 63-byte feature
  report `0x20` payload. This project never uses it, and its own `0x20` response
  is read live from the attached controller.

**The remaining consideration is a different one, and it is worth being open
about**: those descriptor bytes describe *Sony's* device, and the emulator
deliberately presents Sony's VID/PID and its `iManufacturer` / `iProduct`
strings, because a game only enables wired-DualSense features when it sees
exactly that. This is the same interoperability position every controller-support
project in this space occupies — ViGEm, DS4Windows, SDL's controller database,
the Linux `hid-playstation` driver — and no such project has, to our knowledge,
been challenged over it. It is nonetheless a *user-facing* fact rather than a
hidden one, which is why the README says plainly what the software presents to
Windows. This project makes no claim of Sony endorsement, ships no Sony code or
firmware, and requires the user to own the hardware it speaks to.

## 4. Per-file verdict

Legend: **O** = original work · **P** = hand-written port of MIT-licensed code ·
**F** = protocol facts transcribed from a reference (constants, offsets, sizes) ·
**H** = derived from this project's own hardware measurements.

### `emulator/ds5emu/` — original throughout

| file | verdict | notes |
|---|---|---|
| `wire.py` | **O**, structure **F** | USB/IP struct layouts transcribed from usbip-win2's public headers (`include/usbip/proto.h`, `proto_op.h`, `consts.h`) and cross-checked against the [Linux kernel's USB/IP protocol document](https://www.kernel.org/doc/html/latest/usb/usbip_protocol.html). Wire-format field order is an interoperability fact; the Python implementation (`struct.Struct` formats, dataclasses, parse/serialise functions) is entirely this project's. No usbip-win2 code is reproduced. |
| `descriptors.py` | **H** | See §3. Bytes are hardware measurements. |
| `uac.py` | **O** | UAC1 request handling written against the USB Audio Class 1.0 spec. The volume MIN/MAX/RES values are this project's own assumed defaults and are documented as such (risk R7). |
| `device.py` | **O** | The control/interrupt/isochronous state machine. No counterpart exists in either reference — neither is a USB/IP device. |
| `timing.py` | **O** | `FrameClock`, `UrbMeter`. Written in response to a measurement made on this machine (a stream running 3.63× real time). Nothing comparable exists upstream. |
| `server.py` | **O** | asyncio TCP shell. |
| `backend.py` | **O** | Backend ABC + `SyntheticBackend`. |
| `bridge.py` | **O** | `BridgeBackend`: threads, ring buffers, the clock-domain governor. The three-clock analysis and both buffer policies are this project's. |
| `translate.py` | **O** | BT↔USB report translation, expressed as the single-slice identity `usb_body[0:63] == bt_payload[1:64]` rather than as a copied offset table. |
| `__main__.py`, `_bootstrap.py`, `__init__.py` | **O** | |
| `tests/`, `tools/` | **O** | |

### `prototype/ds5bridge/` — where the ports are

| file | verdict | notes |
|---|---|---|
| `crc.py` | **F** | Declares itself a port of `crc32.util.ts`, but is not one in any meaningful sense: the reference hand-rolls a CRC-32 table and loop, while this uses `zlib.crc32`. What is genuinely taken is three **facts** — the seed bytes `0xA2` (output), `0x53` (set-feature), `0xA3` (get-feature) and the little-endian 4-byte tail placement. There is only one way to express "CRC-32 of seed ‖ id ‖ payload" in Python. |
| **`protocol.py`** | **P + F** | **The one file with real ported expression.** Detailed below. |
| `device.py` | **O** | hidapi wrapper, BT/USB classification, extended-mode handshake. |
| `pacing.py` | **O** | `Pacer`/`RateMeter`/`TimerResolution`. A docstring notes the catch-up approach is "like the tester's player" — that is an acknowledged idea, not shared code; the reference schedules in a browser event loop, this uses `perf_counter` and `timeBeginPeriod`. |
| `audio.py` | **O**, params **F** | PyAV pipeline, written here. The Opus parameters it configures (CBR 160 kbit/s, 10 ms frames, `application=lowdelay`, the 45 kHz resample) are protocol facts, cited to both references and then **independently re-measured on hardware** — `tools/rate_trick_test.py` measured the 45 kHz consumption rate at a ratio of 0.9375, confirming the number rather than trusting it. |
| `cli.py` | **O**, `TRIGGER_MODES` **F** | The adaptive-trigger parameter byte strings are widely-circulated community values; `docs/STATUS.md` §6 already flags the unvalidated ones. See §5. |

### `prototype/tools/` — original

All six harnesses are this project's own, and four of them (`loopback_test`,
`rate_trick_test`, `haptic_test`, `cross_mic_test`) implement a measurement
technique — closed-loop FFT through the controller's own microphone — that has no
counterpart in either reference. `usb_descriptors.py` reimplements the *technique*
of Microsoft's USBView sample (SetupAPI enumeration + hub IOCTLs) in ctypes
against the public Windows SDK API; no sample code was copied.

### `app/ds5app/` dashboard (Phase 5) — original, with acknowledged inspiration

*Added with the Phase 5 web dashboard; the audit method of §2 applies.*

| file | verdict | notes |
|---|---|---|
| `dashboard.py`, `telemetry.py` | **O** | Server, SSE stream, config API and the UDP telemetry channel are this project's design; stdlib only. |
| `dashboard.html` | **O**, ideas **F** | The page is written from scratch — every SVG path, style and script line is this project's. What it deliberately takes from **daidr/dualsense-tester** (MIT) is *presentation vocabulary, not code or assets*: the idea of a live SVG controller whose buttons light and sticks travel (their `ModelPanel.vue` + `DSCover.vue`); the touchpad's 1920×1080 device coordinate space and its scale-into-a-rectangle mapping (`DSCover.vue`, constants `TOUCHPAD_RANGE_X/Y` — itself a hardware fact this project's `protocol.py` already ports); and rendering stick deflection as normalised-axis × a fixed travel radius. Their three-layer SVG artwork (`DSBack/DSBody/DSCover`, ~26 KB of paths) was **not** copied — this page draws its own simplified controller from geometric primitives. Input-report byte offsets come from this project's own `ds5bridge.protocol` (whose `Offsets` port is already recorded above), never re-derived in JS. |

### `prototype/ds5bridge/protocol.py` in detail

Five functions are hand-written ports. This is stated in the file's own docstring
and is confirmed by comparison against the sources:

| function here | ported from | how close |
|---|---|---|
| `build_report_36()` | `btAudioStream.ts:buildReportSix` (MIT, daidr) | **Closest match in the repository.** Same parameter list in the same order, and the same sequence of byte assignments. Adds this project's `mic_active` parameter, which **fixes a bug in the reference**: the original hardcodes `p[68] = 0xFE`, which silently tears down microphone streaming the moment playback starts (measured here: mic payloads drop from ~105/s to exactly 0). |
| `Offsets.__init__` | `offset.util.ts:createInputReportOffset` (MIT, daidr) | A field-for-field transliteration of an offset table — same names (snake-cased), same order, same `+ n` construction. The **content** is pure protocol fact: byte offsets into Sony's report. |
| `build_bt_mic_state()` / `build_bt_mic_control()` | `microphoneProtocol.ts` (MIT, daidr) | Ported constant-for-constant. |
| `build_bt_setstate()` | `ds.util.ts:sendOutputReportFactory` (MIT, daidr) | Three lines: seq nibble, `0x10`, body, CRC. |
| `build_report_39()` | `audio.cpp:audio_bt_task` (MIT, awalol) | Ported packet layout. This project adds the `[16,128]` clamp on `audio_buffer_length` after discovering out-of-range values make the controller discard reports invisibly. |
| the `SetState` body field offsets and valid-flag bit names | `outputStruct.ts` / `OutputPanel.vue` (MIT, daidr) | Names and offsets follow the reference's vocabulary. |

**How to read this**: the *information* (which byte holds what) is a fact about
Sony's hardware and is not anyone's to license. The *expression* — the specific
ordering and shape of the code that writes those bytes — visibly follows the
reference in `build_report_36` in particular. Rather than argue about where the
idea/expression line falls, this project takes the simple route: both references
are MIT, MIT is satisfied by preserving the copyright and permission notice, and
`NOTICE` does exactly that. **No further action is required, and no rewrite is
warranted.**

## 5. Community reverse-engineering — credited honestly

Three things in this codebase come from general community knowledge rather than
from either reference clone, and the honest thing to record is that this project
**did not consult a specific named source** for them:

- The **CRC-32 seed `0xA3`** for get-feature reports and the guessed `0xA1` for
  input reports (`crc.py` says "per community docs"). `verify_input_checksum()`
  has never been validated and is flagged for deletion in `docs/STATUS.md` §6.
- The **adaptive-trigger effect names and parameter bytes** in
  `cli.TRIGGER_MODES` (`weapon`, `bow`, `machine`, …), described in the code as
  "community naming". This vocabulary is most commonly traced to
  **Nielk1's DualSense trigger-effect research** (the `TriggerEffectGenerator`
  work), and it is credited on that basis in `ATTRIBUTIONS.md` — but no code or
  table was taken from it directly, and only `0x21`, `0x26` and `0x05` have been
  validated here against the controller's own `at_status` bytes.
- Battery-state and payload-type nibble meanings, which are common to every
  DualSense project in existence.

**A note on what is *not* credited**: earlier planning material for this release
suggested crediting `pydualsense` and the DS4Windows lineage. Neither appears
anywhere in this repository's code, comments, docs or history, and neither was
used. Crediting them would be inventing a provenance that does not exist, so
`ATTRIBUTIONS.md` names them only in the sense of acknowledging the wider
ecosystem, never as a source.

Prior art that *was* studied — and is credited as such — during the
virtualization option study (`docs/virtualization-options.md`): usbip-win2's
issue #35 and the analysis in it by **nefarius** (Benjamin Höglinger-Stelzer,
also the author of ViGEm and of `nssudeaudio`), **jiegec/usbip**,
**lcgamboa/USBIP-Virtual-USB-Device** and its lineage, and
**dorssel/usbipd-win**. No code was taken from any of them; they were read to
establish that a USB/IP device emulator with isochronous endpoints had not been
published before.

## 6. Dependencies — what ships and what does not

**The emulator ships nothing beyond CPython.**

| package | licence | needed for | ships? |
|---|---|---|---|
| *(stdlib only)* | PSF-2.0 | `emulator/ds5emu` — descriptors, wire protocol, device state machine, server, timing | **the whole USB/IP path** |
| `hidapi` 0.15.0 (cython-hidapi) | tri-licensed: GPL-3.0 **or** BSD-3-Clause **or** the [original HIDAPI licence](https://github.com/libusb/hidapi) — the installed wheel declares both the BSD and GPLv3 classifiers | talking to the Bluetooth controller — `prototype/ds5bridge/device.py`, and therefore `BridgeBackend` | **yes, at runtime** |
| `av` 18.1.0 (PyAV) | BSD-3-Clause; the Windows wheels bundle FFmpeg (LGPL-2.1+) and libopus (BSD-3-Clause) | Opus encode/decode and resampling | **yes, at runtime** |
| `numpy` 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | audio buffer maths | **yes, at runtime** |
| `sounddevice` 0.5.6 (PortAudio) | MIT (PortAudio: MIT) | **dev/test only** — `prototype/tools/cross_mic_test.py` and the `emulator/tools/e2e_*` harnesses | no |

(Licence fields read from the installed distributions' own metadata on the
development machine.)

Two things a maintainer should know:

1. **`hidapi`'s licence is a user choice, and the GPL-3.0 option is one of
   them.** This project neither vendors nor redistributes it — it is a `pip
   install` on the user's machine, and importing a permissively-available library
   at runtime creates no obligation on this repository's own MIT licence. If a
   future release ever *bundles* Python and its wheels into a single distributed
   executable, revisit this: pick the BSD-3-Clause option explicitly and record
   that choice.
2. **PyAV's Windows wheels bundle FFmpeg**, which is LGPL-2.1+ (and would be
   GPL if a GPL-only component were enabled). Same reasoning: not redistributed
   here. A bundled-executable release must ship FFmpeg's and libopus' licence
   texts alongside.

Neither caveat affects source distribution, which is what this repository is.

## 7. Data-scrub policy for the public repository

Applied in the commit "Phase 4b: scrub machine-specific identifiers".

**Redacted everywhere** (code, docs, help text):

- The **Bluetooth BD addresses** of the two development controllers, replaced
  with the fixed placeholders `0011223344aa` and `0011223344bb`. They are unique
  identifiers of hardware the developer owns; publishing them alongside a named
  GitHub account is a small, pointless privacy leak. The replacements are the
  same 12 characters wide, so every markdown table stays aligned and the
  historical narrative is untouched.
- The developer's **Windows user name** in documented paths → `<you>`.
- The **absolute repository path** `D:\Codes\dualSense\ds5-virtual-usb` →
  `<repo>` in every documented command.

**Deliberately kept**, with reasons:

- Windows **devnode instance fragments** (`4&127b94db`) and **audio endpoint
  friendly names** (`Speakers (3- DualSense Wireless Controller)`) in `docs/`.
  These are enumeration artefacts, machine-local, not identifying, and they are
  the *evidence* in the results documents — `docs/identity-comparison.md` is
  precisely a table of virtual-vs-physical devnode properties, and redacting it
  would destroy the record. Where they appeared as **command-line defaults** in
  the `emulator/tools/e2e_*` harnesses they have been annotated as
  machine-specific, pointing at `tools/e2e_endpoints.ps1`, which prints the
  correct values for any machine.
- The **system restore point number** in `docs/install-record.md` and the
  usbip-win2 installer's SHA-256 — the first is meaningless off-machine, the
  second is a useful integrity check anyone can verify.
- **Firmware date strings** and battery percentages — properties of the device
  model and of a moment, not of a person.

No `docs/internal/` split was created: after the scrub there is nothing left in
`docs/` that needs to be withheld, and a two-tier docs layout would only invite
the next contributor to put something private in the wrong tier.

**One thing a maintainer must decide before publishing** — see `README` credits
and §8: the git history's author identity (real name and personal email address
on all 29 commits) becomes public with the repository. That is normal for open
source and is the author's call, but it is a deliberate decision, not an
oversight, and it cannot be undone after a push without rewriting history.

## 8. Verdict

**Clean to publish under MIT.**

- No verbatim copying was found anywhere.
- One file (`prototype/ds5bridge/protocol.py`) contains hand-written ports of
  MIT-licensed functions. Both upstream projects are MIT; their notices are
  reproduced in `NOTICE`. Obligation discharged.
- The USB descriptors are this project's own hardware measurements, proven by a
  byte-level difference from the nearest published table.
- No third-party source is vendored. The only third-party binaries
  redistributed are the two vendors' own installers inside the bundled setup
  exe, unmodified, with their notices (§1).
- The trademark position is handled by naming (§ `README`) rather than by
  pretending the device is not a Sony device.
