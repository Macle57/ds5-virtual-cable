"""Phase 3b test (e): all four pipes through the *real* USB request path.

The other three tools call `BridgeBackend` directly. This one goes through
`DualSenseDevice.handle_submit()` with hand-built USBIP_CMD_SUBMIT frames, so
everything the USB/IP server would exercise is exercised: enumeration, the HID
class requests, the interrupt endpoints, and — the part that matters — the
isochronous packing rules (per-packet descriptors, echoed offsets on OUT,
compacted payload on IN).

The only thing between this and a real attach is the TCP socket and
usbip-win2's kernel client. Everything else is identical, which is why this can
be run today, while driver attach is still the sibling experiment.

Cadence: URBs are submitted the way Windows submits them — one iso URB per
millisecond carrying 8 packets (8 ms of audio), and one interrupt IN URB every
4 ms. The 8-packets-per-URB shape is what USBAUDIO.SYS does; it is also what
makes the offset/compaction rules actually get tested.

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\bridge_e2e.py --seconds 6
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "emulator"))
sys.path.insert(0, str(ROOT / "prototype"))
sys.path.insert(0, str(ROOT / "prototype" / "tools"))

from ds5emu import descriptors as D          # noqa: E402
from ds5emu import wire as W                 # noqa: E402
from ds5emu.bridge import BridgeBackend      # noqa: E402
from ds5emu.device import DualSenseDevice    # noqa: E402

from ds5bridge import protocol as P                  # noqa: E402
from ds5bridge.pacing import Pacer, TimerResolution  # noqa: E402
import loopback_test as LB                   # noqa: E402

SAMPLES_PER_MS = D.AUDIO_SAMPLE_RATE // 1000
OUT_PKT = SAMPLES_PER_MS * D.AUDIO_OUT_CHANNELS * D.AUDIO_BYTES_PER_SAMPLE   # 384
IN_PKT = SAMPLES_PER_MS * D.AUDIO_IN_CHANNELS * D.AUDIO_BYTES_PER_SAMPLE     # 192
PKTS_PER_URB = 8

_seq = [0]


def next_seq() -> int:
    _seq[0] += 1
    return _seq[0]


def control(dev, bmRequestType, bRequest, wValue=0, wIndex=0, wLength=0, data=b""):
    cmd = W.CmdSubmit(
        seqnum=next_seq(), devid=0x00010001,
        direction=W.DIR_IN if bmRequestType & 0x80 else W.DIR_OUT,
        ep=0, transfer_flags=0, transfer_buffer_length=wLength or len(data),
        start_frame=0, number_of_packets=-1, interval=0,
        setup=struct.pack("<BBHHH", bmRequestType, bRequest, wValue, wIndex, wLength),
    )
    reply = dev.handle_submit(cmd, data)
    return W.unpack_ret_submit(reply), reply[W.HEADER_SIZE:]


def interrupt_in(dev, ep=D.EP_HID_IN, length=64):
    cmd = W.CmdSubmit(
        seqnum=next_seq(), devid=0x00010001, direction=W.DIR_IN, ep=ep & 0x7F,
        transfer_flags=0, transfer_buffer_length=length, start_frame=0,
        number_of_packets=-1, interval=6, setup=b"\0" * 8,
    )
    reply = dev.handle_submit(cmd, b"")
    return W.unpack_ret_submit(reply), reply[W.HEADER_SIZE:]


def interrupt_out(dev, data):
    cmd = W.CmdSubmit(
        seqnum=next_seq(), devid=0x00010001, direction=W.DIR_OUT, ep=D.EP_HID_OUT,
        transfer_flags=0, transfer_buffer_length=len(data), start_frame=0,
        number_of_packets=-1, interval=6, setup=b"\0" * 8,
    )
    return W.unpack_ret_submit(dev.handle_submit(cmd, data))


def iso_out(dev, pcm: bytes, npkts=PKTS_PER_URB):
    descs = [W.IsoPacket(offset=i * OUT_PKT, length=OUT_PKT) for i in range(npkts)]
    cmd = W.CmdSubmit(
        seqnum=next_seq(), devid=0x00010001, direction=W.DIR_OUT, ep=D.EP_ISO_OUT,
        transfer_flags=0, transfer_buffer_length=len(pcm), start_frame=0,
        number_of_packets=npkts, interval=4, setup=b"\0" * 8,
    )
    reply = dev.handle_submit(cmd, pcm + W.pack_iso_packets(descs))
    info = W.unpack_ret_submit(reply)
    # An OUT reply carries no data buffer, so the descriptor array starts at
    # offset 0 of the body (an IN reply puts it after `actual_length` bytes).
    out = W.unpack_iso_packets(reply[W.HEADER_SIZE:], info["number_of_packets"], 0)
    return info, out


def iso_in(dev, npkts=PKTS_PER_URB):
    descs = [W.IsoPacket(offset=i * IN_PKT, length=IN_PKT) for i in range(npkts)]
    cmd = W.CmdSubmit(
        seqnum=next_seq(), devid=0x00010001, direction=W.DIR_IN, ep=D.EP_ISO_IN | 0x80,
        transfer_flags=0, transfer_buffer_length=npkts * IN_PKT, start_frame=0,
        number_of_packets=npkts, interval=4, setup=b"\0" * 8,
    )
    reply = dev.handle_submit(cmd, W.pack_iso_packets(descs))
    info = W.unpack_ret_submit(reply)
    body = reply[W.HEADER_SIZE:]
    data = body[: info["actual_length"]]
    out = W.unpack_iso_packets(body, info["number_of_packets"], info["actual_length"])
    return info, data, out


def block(n0, count, spk_hz, hap_hz, spk_amp=0.6, hap_amp=1.0) -> bytes:
    t = np.arange(n0, n0 + count, dtype=np.float64) / D.AUDIO_SAMPLE_RATE
    b = np.zeros((count, 4))
    if spk_hz:
        b[:, 0] = b[:, 1] = np.sin(2 * np.pi * spk_hz * t) * spk_amp
    if hap_hz:
        b[:, 2] = b[:, 3] = np.sin(2 * np.pi * hap_hz * t) * hap_amp
    return (np.clip(b, -1, 1) * 32767.0).astype("<i2").tobytes()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--speaker-freq", type=float, default=1500.0)
    ap.add_argument("--haptic-freq", type=float, default=250.0)
    ap.add_argument("--volume", type=int, default=140)
    args = ap.parse_args()

    be = BridgeBackend(speaker_volume=args.volume)
    be.start()
    dev = DualSenseDevice(be)
    fail = 0

    # ---- enumeration, exactly the order Windows uses --------------------
    print("--- enumeration through handle_submit ---")
    info, data = control(dev, 0x80, 0x06, 0x0100, 0, 18)
    print(f"  GET_DESCRIPTOR(device)      {len(data)} B  "
          f"VID:PID {data[8]|data[9]<<8:04X}:{data[10]|data[11]<<8:04X}")
    fail += data != D.DEVICE_DESCRIPTOR
    info, data = control(dev, 0x80, 0x06, 0x0200, 0, 227)
    print(f"  GET_DESCRIPTOR(config)      {len(data)} B")
    fail += data != D.CONFIG_DESCRIPTOR
    info, data = control(dev, 0x81, 0x06, 0x2200, D.IFACE_HID, 289)
    print(f"  GET_DESCRIPTOR(HID report)  {len(data)} B")
    fail += data != D.HID_REPORT_DESCRIPTOR
    control(dev, 0x00, 0x09, 1, 0, 0)

    for rid in (0x05, 0x20):
        info, data = control(dev, 0xA1, 0x01, 0x0300 | rid, D.IFACE_HID, 64)
        ok = info["status"] == 0 and len(data) and data[0] == rid
        print(f"  GET_REPORT(feature 0x{rid:02x})     "
              f"{'STALL' if info['status'] else f'{len(data)} B'}"
              f"  {data[:8].hex(' ') if ok else ''}")
        fail += not ok

    # ---- open both audio streams the way the class driver does ----------
    control(dev, 0x01, 0x0B, 1, D.IFACE_AUDIO_OUT, 0)   # SET_INTERFACE alt 1
    control(dev, 0x01, 0x0B, 1, D.IFACE_AUDIO_IN, 0)
    print(f"  SET_INTERFACE alt 1 on iface {D.IFACE_AUDIO_OUT} and {D.IFACE_AUDIO_IN}"
          f"  -> mic armed={be._mic_armed.is_set()} audio armed={be._audio_armed.is_set()}")
    time.sleep(0.5)

    # ---- baseline: iso IN only -------------------------------------------
    print(f"\n--- {args.seconds:.0f}s baseline: iso IN only ---")
    base = run(dev, args.seconds, None)
    print(f"  captured {base['pcm'].size} samples, iso IN URBs {base['in_urbs']}, "
          f"short packets {base['short']}")

    # ---- full duplex ------------------------------------------------------
    print(f"\n--- {args.seconds:.0f}s full duplex: iso OUT + iso IN + interrupt IN/OUT ---")
    mic0 = be.stats["bt_mic"]
    tone = run(dev, args.seconds, (args.speaker_freq, args.haptic_freq))
    mic_during = be.stats["bt_mic"] - mic0

    print(f"  iso OUT URBs {tone['out_urbs']} ({tone['out_urbs']/args.seconds:.1f}/s, "
          f"{tone['out_urbs']*PKTS_PER_URB/args.seconds:.0f} packets/s), "
          f"offset echo errors {tone['offset_err']}")
    print(f"  iso IN  URBs {tone['in_urbs']}, compaction errors {tone['compact_err']}, "
          f"short packets {tone['short']}")
    print(f"  interrupt IN {tone['hid_in']} ({tone['hid_in']/args.seconds:.1f}/s), "
          f"empty {tone['hid_empty']}, seq discontinuities {tone['seq_gaps']}")
    print(f"  interrupt OUT (SetState) {tone['hid_out']}")
    print(f"  mic payloads during playback {mic_during} "
          f"({mic_during/args.seconds:.1f}/s)")
    print(f"  device stats {dev.stats}")
    print(f"  backend {be.summary()}")

    control(dev, 0x01, 0x0B, 0, D.IFACE_AUDIO_OUT, 0)
    control(dev, 0x01, 0x0B, 0, D.IFACE_AUDIO_IN, 0)
    be.stop()

    # ---- verdict ----------------------------------------------------------
    ok = True
    if base["pcm"].size > 2048 and tone["pcm"].size > 2048:
        bf, bm = LB.spectrum(base["pcm"])
        tf, tm = LB.spectrum(tone["pcm"])
        print("\n--- FFT of the mic stream as served on iso IN ---")
        for label, f in (("speaker", args.speaker_freq), ("haptics", args.haptic_freq)):
            eb = LB.band_energy(bf, bm, f)
            et = LB.band_energy(tf, tm, f)
            db = 20 * np.log10(et / eb) if eb > 0 else float("inf")
            print(f"  {label:<9} @{f:6.0f} Hz  {db:+.1f} dB")
            ok &= db > (12 if label == "speaker" else 10)
        print(f"  loudest bin {tf[int(np.argmax(tm))]:.1f} Hz")
    else:
        print("\nnot enough mic audio captured")
        ok = False

    ok &= fail == 0
    ok &= tone["offset_err"] == 0 and tone["compact_err"] == 0
    ok &= tone["seq_gaps"] == 0
    ok &= mic_during > args.seconds * 50
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def run(dev, seconds: float, tones) -> dict:
    """One pass at the real URB cadence. `tones` None = no iso OUT."""
    be = dev.backend
    r = dict(pcm=None, in_urbs=0, out_urbs=0, hid_in=0, hid_empty=0, hid_out=0,
             short=0, offset_err=0, compact_err=0, seq_gaps=0)
    cap = bytearray()
    n = 0
    prev_seq = None
    setstate_body = None
    # 4 ms tick = the HID endpoint's real bInterval. Pacing at 1 ms instead
    # would spend the whole millisecond inside Pacer's sleep(0) spin, and that
    # starves the Bluetooth reader thread badly enough to be measurable in the
    # microphone payload rate -- the Phase-1 GIL lesson, seen from the other
    # side. Iso URBs go out every second tick carrying 8 packets each, which is
    # both what USBAUDIO.SYS does and still 1000 iso packets/s.
    TICK_MS = 4
    TICKS_PER_ISO_URB = PKTS_PER_URB // TICK_MS   # 2 ticks = 8 ms = 8 packets
    pacer = Pacer(frame_ms=float(TICK_MS), max_backlog=8, max_burst=4)
    t_end = time.perf_counter() + seconds
    with TimerResolution(1):
        pacer.reset()
        while time.perf_counter() < t_end:
            due = pacer.frames_due()
            if due == 0:
                pacer.sleep_until_next()
                continue
            for _ in range(due):
                tick = pacer.emitted
                pacer.commit(1)

                # iso URBs every 8 ms, 8 packets each (USBAUDIO.SYS's shape)
                if tick % TICKS_PER_ISO_URB == 0:
                    if tones is not None:
                        pcm = block(n, SAMPLES_PER_MS * PKTS_PER_URB, tones[0], tones[1])
                        n += SAMPLES_PER_MS * PKTS_PER_URB
                        info, descs = iso_out(dev, pcm)
                        r["out_urbs"] += 1
                        for i, d in enumerate(descs):
                            if d.offset != i * OUT_PKT or d.actual_length != OUT_PKT:
                                r["offset_err"] += 1
                    info, data, descs = iso_in(dev)
                    r["in_urbs"] += 1
                    if len(data) != sum(d.actual_length for d in descs):
                        r["compact_err"] += 1
                    for d in descs:
                        if d.actual_length != IN_PKT:
                            r["short"] += 1
                    cap += data

                # interrupt IN every tick = every 4 ms = the real bInterval
                if True:
                    info, rep = interrupt_in(dev)
                    if info["actual_length"] == 0:
                        r["hid_empty"] += 1
                    else:
                        r["hid_in"] += 1
                        s = rep[1 + P.OFFSETS_USB.sequence_num]
                        if prev_seq is not None and s != ((prev_seq + 1) & 0xFF):
                            r["seq_gaps"] += 1
                        prev_seq = s

                # a SetState now and then, as a real host driver does
                if tick % 125 == 0:  # every 500 ms
                    st = P.SetState().lightbar((tick // 125) * 40 & 0xFF, 0x20, 0x80)
                    setstate_body = P.build_usb_setstate(bytes(st.body))
                    interrupt_out(dev, setstate_body)
                    r["hid_out"] += 1
    pcm = np.frombuffer(bytes(cap), dtype="<i2")
    r["pcm"] = (pcm.reshape(-1, 2)[:, 0].astype(np.float32) / 32768.0
                if pcm.size else np.zeros(0, np.float32))
    return r


if __name__ == "__main__":
    sys.exit(main())
