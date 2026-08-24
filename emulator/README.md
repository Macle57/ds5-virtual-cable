# emulator/ — user-mode USB/IP device emulator for a wired DualSense

Phase 2 deliverable. Serves the **real** wired controller's descriptors (read
off the physical device in Phase 0, `docs/usb-ground-truth.md`) over the USB/IP
wire protocol, so that vadimgrn/usbip-win2's Microsoft-signed UDE driver can
attach it as a local USB device.

**Nothing here installs, loads or requires a driver.** The full test suite runs
on a bare machine. Attaching for real is Phase 3, after the user approves the
changes listed in `docs/virtualization-options.md` §7.

## Quick start

```powershell
cd D:\Codes\dualSense\ds5-virtual-usb\emulator

# 91 protocol tests, no driver, no hardware
..\prototype\.venv\Scripts\python.exe -m unittest discover -s tests -t .

# dump the descriptors we would serve
..\prototype\.venv\Scripts\python.exe -m ds5emu descr

# run the server on loopback (binds 127.0.0.1:3240; nothing else happens
# until a USB/IP client attaches, which needs the driver from Phase 3)
..\prototype\.venv\Scripts\python.exe -m ds5emu serve
```

No third-party packages are needed — stdlib only. The existing
`prototype/.venv` is reused purely out of convenience.

## Layout

```
ds5emu/
  wire.py         USB/IP wire protocol. Pure bytes in / bytes out, no I/O.
  descriptors.py  Ground-truth descriptor tables + derived constants.
  uac.py          UAC1 mixer control state (mute/volume on the two feature units).
  device.py       The emulated device: CMD_SUBMIT -> RET_SUBMIT. Pure, no threads.
  backend.py      Backend ABC; SyntheticBackend (hardware-free); BridgeBackend stub.
  server.py       asyncio TCP shell. Thin on purpose.
  __main__.py     CLI: serve / descr / selftest
tests/
  test_wire.py         24 tests: byte-exact wire format
  test_descriptors.py  14 tests: descriptors vs docs/usb-ground-truth.md
  test_device.py       41 tests: control / interrupt / isochronous behaviour
  test_server.py       12 tests: full client-server round trips over loopback TCP
```

The layering is deliberate: **everything protocol-shaped is a pure function**,
so the wire format and the device behaviour are testable without a socket, and
the socket layer is testable without a driver.

## Where the Phase-1 bridge plugs in

`backend.Backend` is the whole seam:

| USB traffic | Backend call |
|---|---|
| interrupt IN `0x84` | `read_input_report(max_len)` |
| interrupt OUT `0x03` | `write_output_report(data)` |
| control HID GET_REPORT (feature) | `get_feature_report(id, len)` |
| control HID SET_REPORT | `set_feature_report(id, data)` / `write_output_report` |
| isochronous OUT `0x01` | `write_audio_out(pcm)` — 4ch s16le @48 k |
| isochronous IN `0x82` | `read_audio_in(nbytes)` — 2ch s16le @48 k |

`SyntheticBackend` implements all of it with no hardware: a canned idle input
report and a generated sine on the mic. It also timestamps every isochronous
OUT packet, which is the measurement Phase-3 experiment E1 needs.

`BridgeBackend` is a **documented stub that raises NotImplementedError**. It
lists precisely what Phase 3 has to wire up against `prototype/ds5bridge`
(rate conversion 48 k↔45 k, Opus framing at 10.667 ms, the haptic channel
split, the mic jitter buffer, and the `mic_active` gotcha from STATUS.md §8.1).
It has never been run. Do not treat it as working.

## Language choice: Python, with an explicit escape hatch

Load the emulator will have to carry, worst case (both audio directions
streaming plus HID at full rate):

| stream | URBs/s | bytes/s |
|---|---|---|
| iso OUT `0x01` (usbaudio batches ~10 packets/URB) | ~100 | 392 000 |
| iso IN `0x82` | ~100 | 196 000 |
| interrupt IN `0x84` @ 4 ms | 250 | 16 000 |
| interrupt OUT `0x03` | ≤250 | ≤16 000 |
| **total** | **~700 req/resp pairs/s** | **~620 KB/s** |

CPython handles that comfortably; the concern is jitter, not throughput. Two
things make Python the right first choice here:

1. Phase 1 already demonstrated Python holding a hard real-time-ish cadence on
   this exact machine — `pacing.Pacer` sustained 93.75 fps with **0 dropped
   frames** over 6 s using `timeBeginPeriod(1)` (`docs/STATUS.md` §5.9).
2. The Bluetooth backend runs in the same process, so the audio hand-off is a
   queue append rather than an IPC hop. Rewriting the transport in C++ would
   force the bridge across a process boundary and *add* latency.

The escape hatch is real and cheap: `wire.py` and `device.py` are pure
functions over `bytes` with no asyncio in them, so the hot loop can move to
C++ (or the whole thing to a C++ server) while the descriptors, control
dispatch, HID translation and audio framing port unchanged.

Two Phase-1 gotchas apply verbatim to any timing loop added here
(`docs/STATUS.md` §8.3): never spin on a bare `pass` — it starves other Python
threads and looks exactly like a hardware failure — and never trust a write's
return value as a byte count.

## Protocol decisions that are load-bearing

All of these came from reading usbip-win2's sources, not from prose. Full
citations in `docs/virtualization-options.md` §2.8.

1. **`speed` must be `USB_SPEED_HIGH` (3)** in `usbip_usb_device`. usbip-win2's
   `patch_config()` rewrites isochronous `bInterval` to `min(bInterval + 3,
   16)` for anything below high speed, which would turn our 1 ms audio
   endpoints into 8 ms ones. Guarded by `if (dev.speed() < USB_SPEED_HIGH)` in
   `drivers/ude/wsk_receive.cpp`. Asserted by `test_device.IdentityTests`.
2. **Echo the busid** in the `OP_REP_IMPORT` reply. The driver compares it and
   aborts the attach on mismatch.
3. **`OP_REQ_DEVLIST` has no request body** — 8 bytes of `op_common` and
   nothing else.
4. **CMD_SUBMIT payload** = `[transfer buffer, OUT only][iso descriptors, iso
   only]`. **RET_SUBMIT payload** = `[transfer buffer, IN only][iso
   descriptors, iso only]`. An OUT reply carries no data no matter what
   `actual_length` says.
5. **Isochronous IN replies are compacted**: packets concatenated with no
   padding, `actual_length` = sum of per-packet actual lengths — but each
   returned descriptor's `offset` **must be the offset the client sent**,
   unchanged. usbip-win2's `validate()` hard-fails on a mismatch.
6. **`start_frame` matters.** usbip-win2 always sets
   `USBD_START_ISO_TRANSFER_ASAP` (it does not implement
   `URB_GET_CURRENT_FRAME_NUMBER`) and writes our reply's `start_frame`
   straight into `URB.StartFrame`, so we return a monotonic 1 kHz counter.
7. **`number_of_packets = -1`** means non-isochronous on the wire; replies use
   `0`, which the driver accepts either way.
8. Everything in the op phase and every header field is **big endian**;
   descriptor contents and USB setup packets stay **little endian**.

## What is NOT done

- `BridgeBackend` — stub only, never executed.
- No driver has been attached, so **nothing here has been validated against a
  real USB/IP client.** The tests validate the format against usbip-win2's
  *source*, which is strong but is not the same as a live attach.
- UAC1 volume MIN/MAX/RES are **assumed** values, not captured from the
  physical controller (risk R7).
- No timing measurement. That is experiment E1 and needs the driver.
