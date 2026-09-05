"""Phase 4c test: the factory-test channel through `BridgeBackend`, on real hardware.

`factory_probe.py` proves what the controller says. This proves that the bridge
says the same thing -- that the forwarding path added in Phase 4c
(`set_feature_report(0x80)` -> writer thread -> Bluetooth, and
`get_feature_report(0x81)` -> writer thread -> Bluetooth, uncached) really is
transparent, and that its allowlist really does stop everything else.

It instantiates the backend the way `__main__.py` does and then calls it exactly
as `device.DualSenseDevice` would from a HID class request -- no USB/IP socket,
no vhci attach, no drivers. The host side is emulated faithfully in one respect
that matters: the payload handed to `set_feature_report` is the *unsigned*
63-byte body a USB host would send, because our virtual device presents the
wired descriptor. Adding the Bluetooth CRC is the bridge's job, and if it stops
doing it every check below goes quiet.

What it asserts:

    prefetch      features 0x20 and 0x22 are cached at connect
    single page   serial number, BD MAC and battery voltage come back and parse
    multi page    the 0x70/1 telemetry block returns FOUR DISTINCT pages, not
                  one page replayed four times -- the specific risk in caching
                  a 0x81 answer
    blocked       a non-allowlisted (deviceId, actionId) is counted, logged and
                  never written to the controller, and 0x81 then stalls
    stats         feature_test_forwarded / feature_test_reads move

    cd emulator
    ..\\prototype\\.venv\\Scripts\\python.exe tools\\bridge_factory_test.py
"""

from __future__ import annotations

import argparse
import logging
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ds5emu.bridge import (            # noqa: E402
    FEATURE_TEST_CMD,
    FEATURE_TEST_PAYLOAD_LEN,
    FEATURE_TEST_RESULT,
    PREFETCH_FEATURES,
    BridgeBackend,
)

from factory_probe import (            # noqa: E402
    PAGE_SIZE,
    ST_COMPLETE,
    ST_COMPLETE_2,
    TELEMETRY_PAGES,
    mac,
    parse_telemetry,
    sjis,
)

#: The GET(0x81) poll cadence the tester uses, and the wall-clock cap per
#: command. The bridge's own `FEATURE_BT_TIMEOUT` is much shorter than this on
#: purpose: a bridge-side timeout costs the host one poll, not the command.
POLL_INTERVAL = 0.010
COMMAND_TIMEOUT = 1.000

#: A deliberately NON-allowlisted pair. deviceId 1 action 3 is WRITE_PCBAID in
#: `DualSenseTestActionId` -- chosen because it is exactly the class of command
#: the allowlist exists to stop. It must never reach the controller; the whole
#: point of the check is that `feature_test_blocked` moves and nothing is sent.
BLOCKED_DEVICE, BLOCKED_ACTION = 0x01, 0x03

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"   {detail}" if detail else ""))
    return ok


def host_payload(device_id: int, action_id: int, params: bytes = b"") -> bytes:
    """What a USB host puts in the data stage of SET_REPORT(feature 0x80).

    63 bytes, no report id, no CRC -- our descriptor is the wired one, so the
    host has no reason to sign anything.
    """
    buf = bytearray(FEATURE_TEST_PAYLOAD_LEN)
    buf[0] = device_id
    buf[1] = action_id
    buf[2:2 + len(params)] = params
    return bytes(buf)


def poll_pages(be: BridgeBackend, device_id: int, action_id: int,
               max_pages: int = 8) -> list[bytes]:
    """Repeated GET_REPORT(feature 0x81) exactly as the USB device layer does it.

    A None answer is a STALL, which a real host retries -- so it is a reason to
    poll again, not a reason to stop.
    """
    pages: list[bytes] = []
    deadline = time.perf_counter() + COMMAND_TIMEOUT * max(1, max_pages // 2)
    while time.perf_counter() < deadline and len(pages) < max_pages:
        raw = be.get_feature_report(FEATURE_TEST_RESULT, 64)
        if raw and len(raw) >= 4 and raw[0] == FEATURE_TEST_RESULT \
                and raw[1] == device_id and raw[2] == action_id:
            status = raw[3]
            if status in (ST_COMPLETE, ST_COMPLETE_2):
                pages.append(raw[4:4 + PAGE_SIZE])
                if status == ST_COMPLETE:
                    break
                time.sleep(POLL_INTERVAL)
                continue
        elif pages:
            # The header stopped echoing us: a paged block has finished.
            break
        time.sleep(POLL_INTERVAL)
    return pages


def call(be: BridgeBackend, device_id: int, action_id: int,
         params: bytes = b"") -> bytes | None:
    be.set_feature_report(FEATURE_TEST_CMD, host_payload(device_id, action_id, params))
    pages = poll_pages(be, device_id, action_id)
    return b"".join(pages) if pages else None


def run(be: BridgeBackend) -> None:
    # FIRST, while nothing is armed: a cold GET of 0x81 must STALL rather than
    # poll the controller, and a blocked 0x80 must not arm it either. Run before
    # anything else because `_test_armed` is sticky by design -- an allowlisted
    # command leaves it set, and a later blocked command does not disturb it.
    print("\n-- cold start: nothing armed --")
    check("GET 0x81 with no command armed stalls",
          be.get_feature_report(FEATURE_TEST_RESULT, 64) is None)
    be.set_feature_report(FEATURE_TEST_CMD,
                          host_payload(BLOCKED_DEVICE, BLOCKED_ACTION))
    check("GET 0x81 still stalls after a BLOCKED command (it never armed)",
          be.get_feature_report(FEATURE_TEST_RESULT, 64) is None,
          f"feature_test_blocked={be.stats['feature_test_blocked']}")

    print("\n-- prefetched plain features --")
    for rid in PREFETCH_FEATURES:
        body = be.get_feature_report(rid, 64)
        check(f"feature 0x{rid:02x} cached at connect", body is not None,
              f"{len(body)} bytes" if body else "MISSING")
        if body:
            print(f"        0x{rid:02x} = {body.hex(' ')}")
    cal = be.get_feature_report(0x05, 64)
    check("feature 0x05 cached at connect", cal is not None,
          f"{len(cal)} bytes" if cal else "MISSING")

    print("\n-- single-page factory reads through the bridge --")
    serial = call(be, 0x01, 0x13)
    check("serial number (device 1 action 0x13)", bool(serial),
          sjis(serial[:32]) if serial else "no answer")

    bd = call(be, 0x09, 0x02)
    check("BD MAC address (device 9 action 2)", bool(bd),
          mac(bd[:6]) if bd else "no answer")

    volt = call(be, 0x04, 0x03)
    mv = struct.unpack_from("<H", volt, 0)[0] if volt else 0
    check("battery voltage (device 4 action 3)", 2000 < mv < 5000,
          f"{mv} mV" if volt else "no answer")

    print("\n-- multi-page telemetry through the bridge --")
    be.set_feature_report(FEATURE_TEST_CMD, host_payload(0x70, 0x01))
    pages = poll_pages(be, 0x70, 0x01, TELEMETRY_PAGES)
    check(f"telemetry returned {TELEMETRY_PAGES} pages", len(pages) == TELEMETRY_PAGES,
          f"{len(pages)} pages")
    distinct = len({bytes(p) for p in pages})
    check("every telemetry page is distinct (no cached 0x81)",
          distinct == len(pages) and distinct > 1,
          f"{distinct} distinct of {len(pages)}")
    if pages:
        common, buttons = parse_telemetry(b"".join(pages))
        print(f"        peripheral serial   {common['peripheral serial']}")
        print(f"        total active time   {common['total active time']}")
        print(f"        USB / BT connects   {common['USB connect count']}"
              f" / {common['Bluetooth connect count']}")
        top = ", ".join(f"{n}={c}" for n, c in buttons[4:8])
        print(f"        face buttons        {top}")
        check("page 4 carries button counters (not a repeat of page 1)",
              any(c for _, c in buttons), "counters non-zero")

    print("\n-- a non-allowlisted command must be blocked --")
    before_blocked = be.stats["feature_test_blocked"]
    before_fwd = be.stats["feature_test_forwarded"]
    writes_before = len(be.feature_writes)
    be.set_feature_report(FEATURE_TEST_CMD,
                          host_payload(BLOCKED_DEVICE, BLOCKED_ACTION))
    check(f"device {BLOCKED_DEVICE} action {BLOCKED_ACTION} counted as blocked",
          be.stats["feature_test_blocked"] == before_blocked + 1,
          f"feature_test_blocked {before_blocked} -> {be.stats['feature_test_blocked']}")
    check("blocked command was NOT forwarded",
          be.stats["feature_test_forwarded"] == before_fwd,
          f"feature_test_forwarded stayed {before_fwd}")
    check("blocked command was still recorded for inspection",
          len(be.feature_writes) == writes_before + 1)
    answered = poll_pages(be, BLOCKED_DEVICE, BLOCKED_ACTION, max_pages=1)
    check("0x81 never echoes the blocked command", not answered,
          "stalled / returned the previous command's header")

    print("\n-- stats --")
    s = be.stats
    for k in ("feature_test_forwarded", "feature_test_blocked",
              "feature_test_reads", "feature_test_errors", "feature_bt_timeouts"):
        print(f"        {k:<24} {s[k]}")
    # Four allowlisted commands go out above: serial, BD MAC, battery, telemetry.
    check("feature_test_forwarded moved", s["feature_test_forwarded"] >= 4,
          str(s["feature_test_forwarded"]))
    check("feature_test_reads moved", s["feature_test_reads"] >= 5,
          str(s["feature_test_reads"]))
    if be.feature_misses:
        print(f"        feature_misses           {be.feature_misses}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--transport", default="BT", choices=["BT", "USB"])
    ap.add_argument("--serial", default=None,
                    help="BD address of the controller to bridge")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--verbose", action="store_true",
                    help="show the bridge's own log lines, including the BLOCKED warning")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="  %(levelname)-7s %(message)s")

    be = BridgeBackend(transport=args.transport, device_index=args.index,
                       serial=args.serial, target="speaker", auto_arm=False)
    be.start()
    try:
        if not be.connected.wait(6.0):
            print("controller did not connect")
            return 2
        print(f"connected: {args.transport} "
              f"serial={be._dev.info.serial or '(none)'}")  # noqa: SLF001
        run(be)
    finally:
        be.stop()

    failed = [r for r in _results if r[0] == FAIL]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    for _, name, detail in failed:
        print(f"  FAILED: {name}   {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
