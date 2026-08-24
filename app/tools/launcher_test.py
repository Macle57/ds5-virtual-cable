"""Does `ds5bridge` really put everything back? Four scenarios, on real hardware.

Teardown is the part of this product that is easy to get wrong and impossible
to notice going wrong: a run that leaves the auto-re-attach armed looks fine
until the *next* run silently acquires a second device. So each scenario ends
by asserting the machine is clean, and scenario 2 asserts something stronger --
that starting again afterwards produces exactly ONE attached device.

    1. clean run + Ctrl+C           the normal path
    2. hard kill + restart          the rescue path, and the armed-re-attach trap
    3. cleanup after a hard kill    `ds5bridge cleanup` as a person would run it
    4. double start                 a second bridge must refuse, not fight

Run from anywhere:
    prototype\\.venv\\Scripts\\python.exe app\\tools\\launcher_test.py --serial d42f4ba1485d

Every wait is bounded and every subprocess is killed in a finally block, so a
failure leaves the machine no worse than a crash would.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
REPO = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))

from ds5app import service as S           # noqa: E402
from ds5app.usbip import Usbip            # noqa: E402

PY = str(REPO / "prototype" / ".venv" / "Scripts" / "python.exe")
CREATE_NEW_PROCESS_GROUP = 0x00000200

FAILURES: list[str] = []


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"    {'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}",
          flush=True)
    if not cond:
        FAILURES.append(name)
    return cond


class Bridge:
    """`ds5bridge run` as a child process, in its own console process group so
    a real CTRL_BREAK can be delivered to it (there is no other way to send a
    console signal to another process on Windows)."""

    def __init__(self, serial: str, port: int):
        self.args = [PY, "-m", "ds5app", "run", "--port", str(port),
                     "--status-every", "0"]
        if serial:
            self.args += ["--serial", serial]
        self.p: subprocess.Popen | None = None
        self.log: list[str] = []

    def start(self) -> None:
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.p = subprocess.Popen(
            self.args, cwd=str(APP_DIR), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NEW_PROCESS_GROUP)

    def wait_for(self, needle: str, timeout: float) -> bool:
        end = time.monotonic() + timeout
        assert self.p and self.p.stdout
        while time.monotonic() < end:
            line = self.p.stdout.readline()
            if not line:
                if self.p.poll() is not None:
                    return False
                continue
            self.log.append(line.rstrip())
            print("      | " + line.rstrip(), flush=True)
            if needle in line:
                return True
        return False

    def drain(self, seconds: float = 3.0) -> None:
        end = time.monotonic() + seconds
        assert self.p and self.p.stdout
        while time.monotonic() < end and self.p.poll() is None:
            line = self.p.stdout.readline()
            if not line:
                break
            self.log.append(line.rstrip())
            print("      | " + line.rstrip(), flush=True)

    def ctrl_break(self) -> None:
        assert self.p
        os.kill(self.p.pid, signal.CTRL_BREAK_EVENT)

    def hard_kill(self) -> None:
        if self.p and self.p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.p.pid)],
                           capture_output=True)

    def wait_exit(self, timeout: float) -> int | None:
        assert self.p
        try:
            return self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None


def clean_state(u: Usbip, port: int) -> tuple[bool, str]:
    ports = u.attached_ports()
    free = S.port_free(port)
    return (not ports and free,
            f"attached={ports} tcp_{port}_free={free}")


def scenario_1_ctrl_c(serial: str, port: int, u: Usbip) -> None:
    say("SCENARIO 1: clean run, Ctrl+C teardown")
    b = Bridge(serial, port)
    try:
        b.start()
        ok = b.wait_for("virtual wired DualSense attached", 60)
        check("bridge came up", ok)
        if not ok:
            return
        ports = u.attached_ports()
        check("exactly one device attached", len(ports) == 1, f"ports={ports}")
        b.drain(5.0)
        b.ctrl_break()
        code = b.wait_exit(30)
        check("exited on Ctrl+Break", code is not None, f"exit={code}")
        b.drain(1.0)
        time.sleep(1.5)
        clean, detail = clean_state(u, port)
        check("machine is clean after teardown", clean, detail)
    finally:
        b.hard_kill()


def scenario_2_hard_kill_then_restart(serial: str, port: int, u: Usbip) -> None:
    say("SCENARIO 2: hard kill, then start again -- the armed-re-attach trap")
    b = Bridge(serial, port)
    try:
        b.start()
        if not check("bridge came up", b.wait_for("virtual wired DualSense attached", 60)):
            return
        b.drain(3.0)
        b.hard_kill()
        b.wait_exit(15)
        time.sleep(2.0)
        # The driver detaches when the socket dies, so this looks clean -- and
        # is not: the background auto-re-attach is still armed. Measured
        # 2026-08-25: start any server on this port and a device appears with
        # nobody having asked for it.
        say("  after the hard kill: " + clean_state(u, port)[1])
    finally:
        b.hard_kill()

    b2 = Bridge(serial, port)
    try:
        b2.start()
        if not check("bridge came up again", b2.wait_for("virtual wired DualSense attached", 60)):
            return
        ports = u.attached_ports()
        check("still exactly ONE device (no phantom from the armed re-attach)",
              len(ports) == 1, f"ports={ports}")
        b2.ctrl_break()
        b2.wait_exit(30)
        time.sleep(1.5)
        clean, detail = clean_state(u, port)
        check("machine is clean after teardown", clean, detail)
    finally:
        b2.hard_kill()


def scenario_3_cleanup(serial: str, port: int, u: Usbip) -> None:
    say("SCENARIO 3: `ds5bridge cleanup` after a hard kill")
    b = Bridge(serial, port)
    try:
        b.start()
        if not check("bridge came up", b.wait_for("virtual wired DualSense attached", 60)):
            return
        b.drain(2.0)
        # Kill only the parent so the machine is left in the worst realistic
        # state: whatever the OS did not tidy up on its own.
        subprocess.run(["taskkill", "/F", "/PID", str(b.p.pid)], capture_output=True)
        b.wait_exit(15)
        time.sleep(1.0)
        r = subprocess.run([PY, "-m", "ds5app", "cleanup", "--port", str(port)],
                           cwd=str(APP_DIR), capture_output=True, text=True, timeout=90)
        for line in r.stdout.splitlines():
            print("      | " + line, flush=True)
        check("cleanup exits 0", r.returncode == 0, f"rc={r.returncode}")
        clean, detail = clean_state(u, port)
        check("machine is clean after cleanup", clean, detail)
    finally:
        b.hard_kill()


def scenario_4_double_start(serial: str, port: int, u: Usbip) -> None:
    say("SCENARIO 4: a second bridge on the same port must refuse, not fight")
    b = Bridge(serial, port)
    try:
        b.start()
        if not check("first bridge came up", b.wait_for("virtual wired DualSense attached", 60)):
            return
        # Default flags on purpose. With --auto-cleanup (the default) the second
        # instance used to read the healthy first one as "leftovers from a
        # crashed run", kill it and detach its device. The mutex is what stops
        # that, so this scenario must test the DEFAULT path.
        second = Bridge(serial, port)
        second.start()
        code = second.wait_exit(60)
        out = ""
        if second.p and second.p.stdout:
            out = second.p.stdout.read() or ""
        for line in out.splitlines():
            print("      2 | " + line, flush=True)
        check("second bridge refused", code not in (0, None), f"exit={code}")
        check("...and said it is already running", "already running" in out,
              out.strip()[-160:])
        ports = u.attached_ports()
        check("first bridge is untouched, still one device", len(ports) == 1,
              f"ports={ports}")
        check("first bridge is still alive", b.p.poll() is None)
        b.ctrl_break()
        b.wait_exit(30)
        time.sleep(1.5)
        clean, detail = clean_state(u, port)
        check("machine is clean after teardown", clean, detail)
    finally:
        b.hard_kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None)
    ap.add_argument("--port", type=int, default=3241)
    ap.add_argument("--only", type=int, default=0, help="run only scenario N")
    a = ap.parse_args()

    u = Usbip(port=a.port)
    say(f"usbip {u.version()}  at {u.exe}")
    S.cleanup(a.port, u.exe, log_fn=lambda t: print("    " + t, flush=True))

    scenarios = [scenario_1_ctrl_c, scenario_2_hard_kill_then_restart,
                 scenario_3_cleanup, scenario_4_double_start]
    for i, fn in enumerate(scenarios, 1):
        if a.only and i != a.only:
            continue
        fn(a.serial, a.port, u)
        print(flush=True)

    S.cleanup(a.port, u.exe, log_fn=lambda t: print("    " + t, flush=True))
    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
