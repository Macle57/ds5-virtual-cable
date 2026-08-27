"""Replay dualsense-tester's "1 kHz sine wave" play-sound button over hidapi.

This is `controlWaveOut()` from daidr/dualsense-tester
(src/utils/dualsense/ds.util.ts, and the widget in
src/router/DualSense/views/AudioControlWidget.vue) reproduced exactly:

    setTestCommandWithParams(AUDIO=6, BUILTIN_MIC_CALIB_DATA_VERIFY=4, params[20])
    setTestCommandWithParams(AUDIO=6, WAVEOUT_CTRL=2, [1, 1, 0])
    ... hold ...
    setTestCommandWithParams(AUDIO=6, WAVEOUT_CTRL=2, [0, 1, 0])

Each of those is one SET_REPORT(Feature, 0x80) with a 63-byte body.

NOTE ON THE POLLING BELOW. The tester itself does NOT poll: `controlWaveOut`
goes through `setTestCommandWithParams`, which sends the 0x80 and returns
(ds.util.ts:389-406). Whether the button makes a sound is decided entirely by
whether that one SET_REPORT reaches the controller. The GET_REPORT(0x81) polls
here are extra instrumentation -- they show what a *real* pad answers with while
a command is running, and they were what first showed that this emulator
STALLed 0x81 where the hardware returns an idle header.

BY DEFAULT IT ONLY TALKS TO THE VIRTUAL DEVICE. `--pick` must name a device the
`hid_diff_probe.py --list` output tags, and the tool refuses anything that is
not the emulated pad unless `--i-know-this-is-real` is passed: writing 0x80 to a
physical controller is the one thing this repo deliberately never does.
"""

from __future__ import annotations

import argparse
import binascii
import sys
import time

import hid

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from hid_diff_probe import enumerate_pads  # noqa: E402

TEST_CMD = 0x80
TEST_RESULT = 0x81
PAYLOAD_LEN = 63

DEV_AUDIO = 6
ACT_WAVEOUT_CTRL = 2
ACT_MIC_CALIB_VERIFY = 4


def build(device_id: int, action_id: int, params: bytes) -> bytes:
    body = bytearray(PAYLOAD_LEN)
    body[0] = device_id
    body[1] = action_id
    body[2:2 + len(params)] = params
    return bytes([TEST_CMD]) + bytes(body)


def poll_result(h, device_id: int, action_id: int, budget_ms: int = 1000) -> None:
    """The tester's getTestResult loop: poll 0x81 until it echoes the command."""
    t0 = time.perf_counter()
    n = 0
    while (time.perf_counter() - t0) * 1000 < budget_ms:
        n += 1
        try:
            r = bytes(h.get_feature_report(TEST_RESULT, 64))
        except Exception as e:  # noqa: BLE001
            print(f"    poll {n}: GET 0x81 STALLED ({e})  "
                  f"<- the tester's while-loop throws here and the command FAILS")
            return
        if not r:
            print(f"    poll {n}: GET 0x81 returned nothing")
            return
        head = binascii.hexlify(r[:6]).decode()
        if r[0] == TEST_RESULT and r[1] == device_id and r[2] == action_id:
            print(f"    poll {n}: MATCH  {head}  status=0x{r[3]:02x}")
            if r[3] in (0x02, 0x03):
                return
        else:
            print(f"    poll {n}: header {head} (not our command yet)")
        time.sleep(0.01)
    print("    timed out after 1000 ms -> TEST_RESULT_FAIL")


def run(h, enable: bool, target: str) -> None:
    if enable:
        params = bytearray(20)
        if target == "headphone":
            params[4] = 4
            params[6] = 6
        else:
            params[2] = 8
        print(f"  SET 0x80 AUDIO/BUILTIN_MIC_CALIB_DATA_VERIFY params={params.hex()}")
        rc = h.send_feature_report(build(DEV_AUDIO, ACT_MIC_CALIB_VERIFY, bytes(params)))
        print(f"    send_feature_report -> {rc}")
        poll_result(h, DEV_AUDIO, ACT_MIC_CALIB_VERIFY)
    ctrl = bytes([1 if enable else 0, 1, 0])
    print(f"  SET 0x80 AUDIO/WAVEOUT_CTRL params={ctrl.hex()}")
    rc = h.send_feature_report(build(DEV_AUDIO, ACT_WAVEOUT_CTRL, ctrl))
    print(f"    send_feature_report -> {rc}")
    poll_result(h, DEV_AUDIO, ACT_WAVEOUT_CTRL)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", required=True)
    ap.add_argument("--target", choices=("speaker", "headphone"), default="speaker")
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--i-know-this-is-real", action="store_true",
                    help="permit writing report 0x80 to a PHYSICAL controller")
    args = ap.parse_args()

    pads = {p["tag"]: p for p in enumerate_pads()}
    p = pads.get(args.pick)
    if p is None:
        print(f"no device tagged {args.pick!r}", file=sys.stderr)
        return 2
    if not args.i_know_this_is_real:
        print("Refusing to write report 0x80 without --i-know-this-is-real.\n"
              "Point --pick at the EMULATED pad and pass the flag; the emulator's\n"
              "own allowlist is what then decides whether it reaches hardware.",
              file=sys.stderr)
        return 3

    h = hid.device()
    h.open_path(p["raw"])
    try:
        print(f"start wave-out on {args.target} ({args.pick})")
        run(h, True, args.target)
        time.sleep(args.hold)
        print("stop wave-out")
        run(h, False, args.target)
    finally:
        h.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
