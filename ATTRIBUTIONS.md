# Attributions

This project exists because other people published what they learned. The legal
obligations are in [`NOTICE`](NOTICE); the file-by-file audit of what is derived
from what is in [`docs/provenance.md`](docs/provenance.md). This file is the
human version — who to thank, and for what.

## The two implementations this was built from

### [daidr/dualsense-tester](https://github.com/daidr/dualsense-tester) — MIT, © 2023 Xuezhou Dai

A browser WebHID app that does *everything* over Bluetooth: input, rumble, HD
haptics, adaptive triggers, LEDs, speaker and headphone audio, and the
microphone. It is the single most complete public description of the DualSense
Bluetooth protocol, and it is readable — which matters more than it sounds.

Directly used here: the CRC-32 seed bytes, the input-report offset table, the
output report `0x31` wrapper, the `0x36` audio+haptics report layout, and the
`0x31`/`0x32` microphone-arming pair. Several functions in
`prototype/ds5bridge/protocol.py` are hand-written ports of its TypeScript.

### [awalol/DS5Dongle](https://github.com/awalol/DS5Dongle) — MIT, © 2026 awalol

Pico 2 W firmware that bridges a Bluetooth DualSense to a fake *wired* USB one.
It is the proof that this idea works at all, in hardware, and this project is
in a real sense the software answer to the same question.

Directly used here: the report `0x39` packet layout (two Opus frames + two
haptic frames per report), the `audio_buffer_length` range that matters,
the Opus encoder configuration, and the 4-channel USB audio split where
channels 0/1 are the speaker and 2/3 are the haptic voice coils.

A correction worth recording, because it runs the other way: DS5Dongle's USB
descriptors include a Microsoft OS 2.0 selective-suspend descriptor. Reading the
real controller's descriptors off the wire showed that is **DS5Dongle's own
addition** — Sony ships no BOS descriptor and no MS OS string. See
`docs/FINDINGS.md`.

## The driver that makes it possible

### [vadimgrn/usbip-win2](https://github.com/vadimgrn/usbip-win2) — BSD-2-Clause (0.9.7.0 and later)

A Microsoft-signed USB/IP client for Windows, WHLK-certified with the help of
the [Open Source Codesigning Initiative](https://github.com/OSSign). Without a
signed UDE driver this project would need a custom kernel driver, test-signing
mode, and a multi-gigabyte WDK; with it, everything stays in user mode and a bug
is a crashed Python process rather than a bugcheck.

Its `usbip2_filter.sys` companion is the specific reason USB audio works at all
over an emulated host controller — it implements the `QueryBusTime` call that
`USBAUDIO.SYS` requires and that UDECx does not provide.

Nothing from it is redistributed here. Its public headers documented the USB/IP
wire format; `emulator/ds5emu/wire.py` is an independent implementation.

usbip-win2 is itself a fork of [cezanne/usbip-win](https://github.com/cezanne/usbip-win).

## Prior art and analysis that shaped the design

- **[nefarius](https://github.com/nefarius)** (Benjamin Höglinger-Stelzer) —
  author of ViGEm and of
  [`nssudeaudio`](https://git.nefarius.at/nefarius/nssudeaudio), and the person
  who opened and drove
  [usbip-win2 issue #35](https://github.com/vadimgrn/usbip-win2/issues/35). That
  thread is where the three specific reasons isochronous audio fails over
  Windows' UDE stack are written down. Establishing that a real DualSense avoids
  all three is what turned this project from a guess into a plan — it is the
  single most load-bearing piece of research behind the whole architecture.
- **[jiegec/usbip](https://github.com/jiegec/usbip)** (Rust) and the
  **[lcgamboa/USBIP-Virtual-USB-Device](https://github.com/lcgamboa/USBIP-Virtual-USB-Device)**
  lineage (Python/C, descended from a 2014 blog post) — prior USB/IP device
  emulators. Both are HID/control only, which is how we knew the isochronous
  part had not been done before.
- **[dorssel/usbipd-win](https://github.com/dorssel/usbipd-win)** — the USB/IP
  *server* for Windows that shares real devices for WSL. Not usable here, but its
  issue tracker is another data point that isochronous is the weak spot of every
  USB/IP implementation. It is also what already owns TCP port 3240 on many
  machines, which is why this project defaults to a different one.
- **The Linux kernel's
  [USB/IP protocol document](https://www.kernel.org/doc/html/latest/usb/usbip_protocol.html)**
  — the normative description of the wire format.
- **Microsoft's USBView sample** — the SetupAPI + hub-IOCTL technique that
  `prototype/tools/usb_descriptors.py` reimplements in ctypes to read the real
  controller's descriptors without disturbing any driver.

## The wider DualSense community

Some of what this project relies on has no single traceable author — it is
knowledge that has been passed around forums, gists and issue threads for years.
Recorded here so it is not silently absorbed:

- The **adaptive-trigger effect names and parameter bytes** (`weapon`, `bow`,
  `galloping`, `machine`, …) follow the community vocabulary that is most
  commonly traced to **[Nielk1](https://github.com/Nielk1)**'s DualSense
  trigger-effect research. This project did not take code from it, and has only
  validated three effect modes against the controller's own status bytes — the
  rest are carried forward on the community's word and are marked unvalidated in
  `docs/STATUS.md`.
- The CRC-32 seed for *get-feature* reports (`0xA3`) and the guessed input-report
  seed (`0xA1`) are "per community docs" with no specific source recorded.
- Projects like **DS4Windows**, **pydualsense** and the Linux
  **`hid-playstation`** driver have carried much of this knowledge into the open
  over the years. To be precise about it: **none of them was used as a source
  for this project**, and nothing here is derived from them. They are named
  because the ecosystem they built is the reason a project like this can be
  written at all.

## And

The people who built [Opus](https://opus-codec.org/),
[FFmpeg](https://ffmpeg.org/), [PyAV](https://github.com/PyAV-Org/PyAV),
[hidapi](https://github.com/libusb/hidapi), [NumPy](https://numpy.org/) and
[PortAudio](https://www.portaudio.com/) — every one of which this project uses
without ceremony and would be far harder without.

---

*This project is not affiliated with, endorsed by, or sponsored by Sony
Interactive Entertainment. See the trademark notice in
[`README.md`](README.md).*
