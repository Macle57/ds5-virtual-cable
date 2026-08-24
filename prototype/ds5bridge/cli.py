"""ds5bridge CLI -- proves every BT subsystem end to end on real hardware.

    python -m ds5bridge list
    python -m ds5bridge inputs [--seconds N] [--transport BT|USB] [--raw]
    python -m ds5bridge setstate --lightbar FF0000 --player 0b01110 --rumble 80,80 \
                                 --trigger-left feedback --mute-led on
    python -m ds5bridge gen-wav out.wav [--seconds 6]
    python -m ds5bridge play file.wav [--target speaker|headphone] [--volume 0..255]
                                      [--report 36|39] [--haptic-gain G] [--no-audio]
    python -m ds5bridge haptics-tone 60 90 [--seconds 3]
    python -m ds5bridge mic out.wav --seconds 5
"""

from __future__ import annotations

import argparse
import sys
import time

from . import audio as A
from . import device as D
from . import protocol as P
from .pacing import Pacer, RateMeter, TimerResolution


def _open(transport: str | None, index: int = 0, require: str | None = None) -> D.DualSense:
    """`require` names the transport a command cannot work without; it becomes the
    default when the user did not pass --transport."""
    info = D.pick(transport or require, index)
    print(f"[open] {D.describe(info)}", file=sys.stderr)
    d = D.DualSense(info).open()
    if d.is_bt:
        cal = d.flip_to_extended()
        print(f"[open] feature 0x05 read ({len(cal)} B) -> extended 0x31 mode",
              file=sys.stderr)
    return d


# ---------------------------------------------------------------------------


def cmd_list(args) -> int:
    devs = D.enumerate_devices()
    if not devs:
        print("no DualSense found")
        return 1
    for i, info in enumerate(devs):
        print(f"[{i}] {D.describe(info)}")
        try:
            d = D.DualSense(info).open(flip_extended=False)
        except Exception as e:  # noqa: BLE001
            print(f"     open failed: {e}")
            continue
        try:
            fw = d.firmware_info()
            build = bytes(fw[1:20]).decode("ascii", errors="replace")
            print(f"     feature 0x20 firmware build: {build!r}")
            cal = d.get_feature(0x05, 64)
            print(f"     feature 0x05 calibration: {len(cal)} bytes")
        except Exception as e:  # noqa: BLE001
            print(f"     feature read failed: {e}")
        finally:
            d.close()
    return 0


def cmd_inputs(args) -> int:
    d = _open(args.transport)
    meter = RateMeter(window=2.0)
    t_end = time.perf_counter() + args.seconds
    last_print = 0.0
    skipped_types: dict[int, int] = {}
    n_decoded = 0
    try:
        while time.perf_counter() < t_end:
            raw = d.read_raw(500)
            if not raw:
                continue
            meter.tick()
            payload = raw[1:]
            if d.is_bt:
                ptype = payload[0] & P.PAYLOAD_TYPE_MASK
                if ptype != P.PAYLOAD_TYPE_CONTROL:
                    skipped_types[ptype] = skipped_types.get(ptype, 0) + 1
                    continue
            st = P.decode_input(payload, usb=not d.is_bt)
            if st is None:
                continue
            n_decoded += 1
            now = time.perf_counter()
            if args.raw:
                print(raw.hex(" "))
            elif now - last_print >= (1.0 / args.print_hz):
                last_print = now
                print(f"{meter.hz:6.1f}Hz {st.one_line()}")
    except KeyboardInterrupt:
        pass
    finally:
        d.close()
    print(f"\n--- {n_decoded} reports decoded, mean rate {meter.mean_hz:.2f} Hz "
          f"over {args.seconds:.1f}s ---")
    if skipped_types:
        print(f"    non-control payload types seen: {skipped_types}")
    return 0


def _parse_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


TRIGGER_MODES = {
    "off": (0x05, b""),
    "feedback": (0x21, bytes([0xFF, 0xFF])),
    "weapon": (0x25, bytes([0x02, 0x07, 0xFF])),
    "vibration": (0x26, bytes([0xFF, 0xFF, 0xFF])),
    "bow": (0x22, bytes([0x00, 0x08, 0xFF, 0xFF])),
    "machine": (0x27, bytes([0x00, 0x09, 0x03, 0x03, 0x08, 0x08])),
}


def cmd_setstate(args) -> int:
    d = _open(args.transport)
    st = P.SetState()
    did = []
    if args.lightbar:
        r, g, b = _parse_rgb(args.lightbar)
        st.lightbar(r, g, b)
        did.append(f"lightbar #{args.lightbar}")
    if args.player is not None:
        st.player_leds(int(args.player, 0), brightness=args.brightness)
        did.append(f"playerLEDs {int(args.player,0):05b}")
    if args.rumble:
        left, right = (int(x) for x in args.rumble.split(","))
        st.rumble(left, right)
        did.append(f"rumble L={left} R={right}")
    if args.mute_led:
        st.mute_led({"off": 0, "on": 1, "blink": 2}[args.mute_led])
        did.append(f"muteLED {args.mute_led}")
    if args.trigger_left:
        m, p = TRIGGER_MODES[args.trigger_left]
        st.trigger_left(m, p)
        did.append(f"L2 {args.trigger_left}")
    if args.trigger_right:
        m, p = TRIGGER_MODES[args.trigger_right]
        st.trigger_right(m, p)
        did.append(f"R2 {args.trigger_right}")
    if args.speaker_volume is not None:
        st.speaker_volume(args.speaker_volume)
        did.append(f"speakerVol {args.speaker_volume}")
    if args.headphone_volume is not None:
        st.headphone_volume(args.headphone_volume)
        did.append(f"headphoneVol {args.headphone_volume}")
    if not did:
        print("nothing to do; pass at least one option")
        d.close()
        return 2
    n = d.send_setstate(st)
    print(f"sent SetState: {', '.join(did)}")
    print(f"  validFlag0=0x{st.body[0]:02x} validFlag1=0x{st.body[1]:02x} "
          f"validFlag2=0x{st.body[38]:02x}")
    print(f"  body={bytes(st.body).hex(' ')}")
    print(f"  hid write() returned {n}")
    if args.hold:
        print(f"holding {args.hold}s (effects persist while the process runs)...")
        try:
            time.sleep(args.hold)
        except KeyboardInterrupt:
            pass
        d.send_setstate(P.SetState().rumble(0, 0).trigger_left(0x05).trigger_right(0x05))
    d.close()
    return 0


def cmd_gen_wav(args) -> int:
    A.generate_test_wav(args.out, args.seconds)
    w = A.load_wav(args.out)
    print(f"wrote {args.out}: {w.duration:.2f}s {w.rate}Hz {w.channels}ch")
    return 0


def cmd_play(args) -> int:
    w = A.load_wav(args.wav)
    print(f"[wav] {args.wav}: {w.duration:.2f}s {w.rate}Hz {w.channels}ch")

    if args.no_audio:
        opus_frames: list[bytes] = []
    else:
        pcm45 = A.resample(w, A.AUDIO_SOURCE_RATE, 2)
        nfr = pcm45.shape[0] / A.OPUS_SAMPLES_PER_FRAME
        print(f"[audio] resampled to {A.AUDIO_SOURCE_RATE}Hz (45k trick) -> "
              f"{pcm45.shape[0]} samples = {nfr:.1f} frames x {A.FRAME_MS:.4f}ms "
              f"= {nfr*A.FRAME_MS/1000:.2f}s wall clock")
        t0 = time.perf_counter()
        opus_frames = A.encode_opus_frames(pcm45)
        sizes = {len(f) for f in opus_frames}
        print(f"[audio] opus: {len(opus_frames)} frames, sizes={sizes}, "
              f"encode took {time.perf_counter()-t0:.2f}s")

    hap_frames = A.extract_haptic_frames(w, args.haptic_gain)
    print(f"[haptics] {len(hap_frames)} frames of {A.HAPTIC_FRAME_BYTES}B "
          f"@ {A.HAPTIC_RATE}Hz int8 stereo (gain {args.haptic_gain})")

    n_frames = max(len(opus_frames), len(hap_frames))
    silent_op = A.silent_opus_frame()
    silent_hp = A.silent_haptic_frame()

    def opus_at(i: int) -> bytes:
        return opus_frames[i] if i < len(opus_frames) else silent_op

    def hap_at(i: int) -> bytes:
        return hap_frames[i] if i < len(hap_frames) else silent_hp

    d = _open(args.transport, require="BT")
    if not d.is_bt:
        print("play requires the Bluetooth controller (0x36/0x39 are BT reports)")
        d.close()
        return 2

    # Prime the audio path: set the routing + volume once via SetState.
    st = P.SetState()
    if args.target == "headphone":
        st.headphone_volume(args.volume)
    else:
        st.speaker_volume(args.volume)
    st.haptic_volume(args.haptic_volume)
    d.send_setstate(st)
    time.sleep(0.05)

    per_report = 1 if args.report == 36 else 2
    frame_ms = A.FRAME_MS * per_report
    print(f"[send] report 0x{args.report}, {per_report} frame(s)/report, "
          f"{frame_ms:.4f} ms period ({1000/frame_ms:.2f} reports/s), "
          f"target={args.target} volume={args.volume}")

    pacer = Pacer(frame_ms=frame_ms, max_backlog=8, max_burst=4)
    frame_counter = 0
    sent = 0
    errors = 0
    with TimerResolution(1):
        pacer.reset()
        try:
            while True:
                idx = pacer.emitted * per_report
                if idx >= n_frames:
                    break
                due = pacer.frames_due()
                if due == 0:
                    pacer.sleep_until_next()
                    continue
                for _ in range(due):
                    i = pacer.emitted * per_report
                    if i >= n_frames:
                        break
                    try:
                        if args.report == 36:
                            d.send_report_36(opus_at(i), hap_at(i), frame_counter,
                                             args.target, args.volume)
                            frame_counter = (frame_counter + 1) & 0xFF
                        else:
                            d.send_report_39(
                                (opus_at(i), opus_at(i + 1)),
                                (hap_at(i), hap_at(i + 1)),
                                frame_counter, args.target,
                            )
                            frame_counter = (frame_counter + 2) & 0xFF
                        sent += 1
                    except Exception as e:  # noqa: BLE001
                        errors += 1
                        if errors < 4:
                            print(f"  write error: {e}", file=sys.stderr)
                    pacer.commit(1)
        except KeyboardInterrupt:
            print("\ninterrupted")

    print(f"[send] {pacer.stats()}")
    print(f"[send] {sent} reports written, {errors} write errors")

    # stop tone: silence one frame so the controller doesn't repeat the tail
    for _ in range(3):
        if args.report == 36:
            d.send_report_36(silent_op, silent_hp, frame_counter, args.target, args.volume)
        else:
            d.send_report_39((silent_op, silent_op), (silent_hp, silent_hp),
                             frame_counter, args.target)
        time.sleep(frame_ms / 1000.0)
    d.close()
    return 0


def cmd_haptics_tone(args) -> int:
    frames = A.sine_haptic_frames(args.freq_l, args.freq_r, args.seconds, args.amp)
    print(f"[haptics] {args.freq_l}Hz L / {args.freq_r}Hz R, {len(frames)} frames "
          f"@ {A.HAPTIC_RATE}Hz int8 (Nyquist is {A.HAPTIC_RATE//2}Hz -- above that it folds)")
    d = _open(args.transport, require="BT")
    if not d.is_bt:
        print("haptics-tone requires the Bluetooth controller")
        d.close()
        return 2
    d.send_setstate(P.SetState().haptic_volume(args.haptic_volume))
    silent_op = A.silent_opus_frame()
    pacer = Pacer(frame_ms=A.FRAME_MS, max_backlog=8, max_burst=4)
    fc = 0
    with TimerResolution(1):
        pacer.reset()
        try:
            while pacer.emitted < len(frames):
                due = pacer.frames_due()
                if due == 0:
                    pacer.sleep_until_next()
                    continue
                for _ in range(due):
                    if pacer.emitted >= len(frames):
                        break
                    d.send_report_36(silent_op, frames[pacer.emitted], fc, "speaker", 0)
                    fc = (fc + 1) & 0xFF
                    pacer.commit(1)
        except KeyboardInterrupt:
            pass
    print(f"[send] {pacer.stats()}")
    for _ in range(3):
        d.send_report_36(silent_op, A.silent_haptic_frame(), fc, "speaker", 0)
        time.sleep(A.FRAME_MS / 1000)
    d.close()
    return 0


def cmd_mic(args) -> int:
    import numpy as np

    d = _open(args.transport, require="BT")
    if not d.is_bt:
        print("mic requires the Bluetooth controller (0x31/0x32 BT mic protocol)")
        d.close()
        return 2

    print("[mic] arming: SetState 0x31 mic-state + 0x32 control (active)")
    d.send_mic_state(True, muted=False, headset_plugged=args.headset)
    time.sleep(0.02)
    d.send_mic_control(True)

    dec = A.MicDecoder()
    chunks: list[np.ndarray] = []
    n_audio = 0
    n_control = 0
    bad = 0
    meter = RateMeter(2.0)
    t_end = time.perf_counter() + args.seconds
    try:
        while time.perf_counter() < t_end:
            raw = d.read_raw(500)
            if not raw or raw[0] != P.BT_INPUT_31:
                continue
            payload = raw[1:]
            ptype = payload[0] & P.PAYLOAD_TYPE_MASK
            if ptype == P.PAYLOAD_TYPE_AUDIO:
                n_audio += 1
                meter.tick()
                try:
                    pcm = dec.decode(P.get_mic_opus(payload))
                    if pcm.size:
                        chunks.append(pcm)
                except Exception:  # noqa: BLE001
                    bad += 1
            elif ptype == P.PAYLOAD_TYPE_CONTROL:
                n_control += 1
    except KeyboardInterrupt:
        pass
    finally:
        print("[mic] disarming")
        try:
            d.send_mic_control(False)
            time.sleep(0.02)
            d.send_mic_state(False)
        except Exception:  # noqa: BLE001
            pass
        d.close()

    print(f"[mic] {n_audio} type-0x02 audio payloads ({meter.mean_hz:.1f}/s), "
          f"{n_control} type-0x01 control payloads, {bad} decode errors")
    if not chunks:
        print("[mic] NO audio payloads captured -- controller did not start streaming")
        return 1
    pcm = np.concatenate(chunks)
    A.save_wav(args.out, pcm.reshape(-1, 1), A.MIC_RATE)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    rms = float(np.sqrt(np.mean(pcm**2))) if pcm.size else 0.0
    print(f"[mic] wrote {args.out}: {pcm.size/A.MIC_RATE:.2f}s @ {A.MIC_RATE}Hz mono, "
          f"peak={peak:.4f} rms={rms:.5f}")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ds5bridge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", choices=["BT", "USB"], default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="enumerate controllers + read feature 0x05/0x20").set_defaults(
        func=cmd_list
    )

    p = sub.add_parser("inputs", help="decode input reports live and measure the rate")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--print-hz", type=float, default=20.0)
    p.add_argument("--raw", action="store_true", help="hexdump every report instead")
    p.set_defaults(func=cmd_inputs)

    p = sub.add_parser("setstate", help="rumble / triggers / LEDs")
    p.add_argument("--lightbar", help="RRGGBB")
    p.add_argument("--player", help="5-bit mask, e.g. 0b01110 or 14")
    p.add_argument("--brightness", type=int, default=None, help="0 high, 1 med, 2 low")
    p.add_argument("--rumble", help="left,right (0-255)")
    p.add_argument("--mute-led", choices=["off", "on", "blink"])
    p.add_argument("--trigger-left", choices=sorted(TRIGGER_MODES))
    p.add_argument("--trigger-right", choices=sorted(TRIGGER_MODES))
    p.add_argument("--speaker-volume", type=int)
    p.add_argument("--headphone-volume", type=int)
    p.add_argument("--hold", type=float, default=0.0, help="keep the process alive N s")
    p.set_defaults(func=cmd_setstate)

    p = sub.add_parser("gen-wav", help="generate the sweep+beats test WAV")
    p.add_argument("out")
    p.add_argument("--seconds", type=float, default=6.0)
    p.set_defaults(func=cmd_gen_wav)

    p = sub.add_parser("play", help="WAV -> speaker/headphone + haptics over BT")
    p.add_argument("wav")
    p.add_argument("--target", choices=["speaker", "headphone"], default="speaker")
    p.add_argument("--volume", type=int, default=0x4B)
    p.add_argument("--haptic-volume", type=int, default=0xFF)
    p.add_argument("--haptic-gain", type=float, default=2.0)
    p.add_argument("--report", type=int, choices=[36, 39], default=36)
    p.add_argument("--no-audio", action="store_true", help="haptics only, silent opus")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("haptics-tone", help="pure sine straight into the actuators")
    p.add_argument("freq_l", type=float)
    p.add_argument("freq_r", type=float)
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--amp", type=float, default=1.0)
    p.add_argument("--haptic-volume", type=int, default=0xFF)
    p.set_defaults(func=cmd_haptics_tone)

    p = sub.add_parser("mic", help="capture the controller mic to a WAV")
    p.add_argument("out")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--headset", action="store_true", help="route the headset mic")
    p.set_defaults(func=cmd_mic)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
