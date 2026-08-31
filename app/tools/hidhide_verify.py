"""Prove -- on real hardware -- that hiding actually hides, and still lets US in.

Run this after installing HidHide and REBOOTING. It is the hardware half of
`docs/hidhide-scoping.md` Q3, and it exists because the feature has two halves
that fail independently:

    1. BLOCKING  -- a process that is not whitelisted must stop seeing the pad.
    2. GRANTING  -- a process that IS whitelisted must still be able to open it.

Half 1 without half 2 is worse than doing nothing: it would blind the bridge
itself. On the development machine on 2026-08-25, before a reboot, half 1
worked and half 2 did NOT -- which is why the feature ships OFF and why this
script exists.

    prototype\\.venv\\Scripts\\python.exe app\\tools\\hidhide_verify.py

Safety: it touches only the DualSense's own device instances, keeps the hidden
window to a couple of seconds, unhides in a `finally` (with retries), restores
the cloak flag to whatever it was, and puts the whitelist back exactly as it
found it. If it somehow cannot, it prints the one command that fixes it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
_REPO = os.path.dirname(_APP)
for _p in (_APP, os.path.join(_REPO, "prototype")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ds5app import hidhide as HH          # noqa: E402


def bt_serials() -> list[str]:
    from ds5bridge import device as DEV

    return sorted({(i.serial or "").lower() for i in DEV.enumerate_devices()
                   if i.transport == "BT" and i.serial})


def can_open(serial: str) -> tuple[bool, str]:
    """Actually `CreateFile` the pad. Enumeration alone is not the question."""
    try:
        import hid
        from ds5bridge import device as DEV

        matches = [i for i in DEV.enumerate_devices()
                   if i.transport == "BT" and (i.serial or "").lower() == serial]
        if not matches:
            return False, "not enumerated"
        path = matches[0].path
        d = hid.device()
        d.open_path(path if isinstance(path, bytes) else path.encode())
        d.close()
        return True, "opened"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def unwhitelisted_sees(serial: str) -> bool:
    """Ask a process that is NOT whitelisted.

    `sys.executable` is whitelisted by the time this matters, so asking from
    here would answer the wrong question. `HidHideCLI.exe --dev-gaming` reports
    `present` from SetupDi rather than an open, so that is no good either. A
    fresh child of the same interpreter is not enough on its own -- the grant is
    per image path, not per process -- so this is only meaningful when called
    BEFORE the whitelist step.
    """
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from ds5bridge import device as D;"
        "print([i.serial for i in D.enumerate_devices() if i.transport=='BT'])"
        % os.path.join(_REPO, "prototype"))
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60).stdout
        return serial in out.lower()
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    hh = HH.HidHide.detect()
    if hh is None:
        print("HidHide is not installed. Install it from")
        print("  https://github.com/nefarius/HidHide/releases")
        print("and REBOOT, then run this again.")
        return 2

    print(f"HidHide {hh.version() or 'unknown'}")
    print(f"  CLI          {hh.cli.exe if hh.cli else '(none)'}")
    print(f"  IOCTL works  {hh.ioctl.active() is not None}")
    print(f"  control ok   {hh.control_ok()}")
    if not hh.control_ok():
        print("\nHidHide will not answer at all. A reboot completes its install.")
        return 2

    serials = bt_serials()
    if not serials:
        print("\nNo Bluetooth DualSense is connected. Turn one on and retry.")
        return 2
    serial = serials[0]
    ids = HH.resolve_serial(serial, cli=hh.cli)
    print(f"\ncontroller {serial} -> {len(ids)} HID interface(s)")
    for i in ids:
        print(f"    {i}")
    if not ids:
        print("could not resolve the serial to a device instance")
        return 1

    base_cloak = hh.active()
    base_wl = hh.allowed()
    blocking = granting = False

    try:
        # -- half 1: blocking, from a NON-whitelisted image -------------------
        print("\n[1/2] blocking -- hiding, then asking a non-whitelisted process")
        hh.hide(ids)
        if base_cloak is False:
            hh.set_active(True)
        time.sleep(1.0)
        blocking = not unwhitelisted_sees(serial)
        print(f"      non-whitelisted process sees it: {not blocking}")
        print(f"      BLOCKING: {'PASS' if blocking else 'FAIL'}")

        # -- half 2: granting -------------------------------------------------
        print("\n[2/2] granting -- whitelisting ourselves, then opening it")
        hh.allow(HH.our_images())
        time.sleep(1.0)
        granting, why = can_open(serial)
        print(f"      open result: {why}")
        print(f"      GRANTING: {'PASS' if granting else 'FAIL'}")
    finally:
        print("\n--- restoring ---")
        for attempt in range(3):
            hh.unhide(ids)
            if base_cloak is False:
                hh.set_active(False)
            if not [i for i in hh.hidden() if i in ids]:
                break
            time.sleep(0.5)
        else:
            print("!!! could not unhide. Run this to fix it:")
            print(f"!!!   \"{hh.cli.exe if hh.cli else 'HidHideCLI.exe'}\" --cloak-off")
        if hh.ioctl.allowed() is not None:
            hh.ioctl.set_allowed(base_wl)
        elif hh.cli is not None:
            for p in hh.allowed():
                if p not in base_wl:
                    hh.cli.unallow_one(p)
        print(f"    blacklist {hh.hidden()}")
        print(f"    cloak     {hh.active()}")
        print(f"    whitelist {hh.allowed()}")
        time.sleep(1.0)
        print(f"    controller visible again: {serial in bt_serials()}")

    HH.write_verify_marker(blocking, granting, image=HH.current_image_path(),
                           version=hh.version())

    print("\n" + "=" * 68)
    if blocking and granting:
        print("BOTH HALVES PASS -- the feature is safe to switch on.")
        print("Tray -> Hide per controller -> tick your controller.")
        return 0

    if blocking and not granting:
        # Do NOT guess at a reboot here. Granting has several distinguishable
        # causes and this tool can tell most of them apart, so it says which one
        # it actually observed rather than repeating folklore.
        print("BLOCKING works but GRANTING does not.")
        print("DO NOT switch the feature on: hiding would blind the bridge.")
        print()
        me = HH.current_image_path()
        listed = hh.whitelist_covers_us()
        print(f"  this process's real image : {me or '(could not be read)'}")
        print(f"  sys.executable            : {sys.executable}")
        if me and os.path.normcase(os.path.realpath(sys.executable)) != \
                os.path.normcase(me):
            print()
            print("  Those differ, which means your python.exe is a LAUNCHER")
            print("  SHIM that runs the real interpreter as a child process")
            print("  (a Windows venv, pyenv-win, py.exe or a Store install).")
            print("  HidHide matches the real image and does not inherit the")
            print("  grant, so the shim's path must not be what gets registered.")
            print("  ds5bridge handles this; a third-party tool may not.")
        if listed is False:
            print()
            print("  DIAGNOSIS: the whitelist does not contain that image, so")
            print("  the registration did not land. Check the path above against")
            print("  `HidHideCLI.exe --app-list`.")
        elif listed is True:
            print()
            print("  DIAGNOSIS: the whitelist DOES contain that exact image and")
            print("  the driver still refused the open. That is not something")
            print("  ds5bridge can fix. Known causes, in order of likelihood:")
            print("    * an anti-virus that hooks process creation -- Kaspersky")
            print("      is named in HidHide's own docs as breaking whitelisting;")
            print("    * the path reaches the exe through a directory junction")
            print("      (HidHide issue #79);")
            print("    * HidHide was installed while this process's parent was")
            print("      already running -- log out and back in, then retry;")
            print("    * an upstream bug on this Windows build. Report it at")
            print("      https://github.com/nefarius/HidHide/issues with the")
            print("      output above and your Windows build number.")
        else:
            print()
            print("  DIAGNOSIS: the whitelist could not be read back, so this")
            print("  tool cannot say whether the entry landed. Check that")
            print("  HidHideCLI.exe runs and try again.")
        return 1

    print("BLOCKING does not work -- hiding had no effect at all.")
    print()
    print("  The pad stayed visible to a non-whitelisted process even with the")
    print("  cloak on and the device on the blacklist. The usual cause is that")
    print("  HidHide's filter is not in this device's driver stack: it attaches")
    print("  only to HID devices created AFTER it was installed.")
    print()
    print("  Fix: reboot. Or, to test without one, restart just this device:")
    print(f'    pnputil /restart-device "{ids[0]}"')
    print()
    print("  If it still does not work after a reboot, HidHide is not filtering")
    print("  on this build -- report it upstream rather than working around it.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
