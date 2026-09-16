"""`ds5bridge` -- one command that turns a Bluetooth DualSense into a wired one.

    ds5bridge                 do the whole thing; Ctrl+C to stop
    ds5bridge --all           every connected controller at once, one each
    ds5bridge devices         which controllers can I see, and how charged?
    ds5bridge cleanup         rescue: undo a run that crashed without tearing down
    ds5bridge unhide          rescue: give back a pad left hidden by a crash
    ds5bridge doctor          is this machine set up correctly?
    ds5bridge tray            the same thing with a system-tray icon
    ds5bridge service ...     the Windows service that starts the tray at boot,
                              before anyone signs in (install/uninstall/start/
                              stop/status; bare `service` is what the SCM runs)

Everything the two-terminal Phase-3 procedure did by hand, in order, with the
teardown that used to be easy to get wrong. See `docs/USER-GUIDE.md`.

One controller or several
-------------------------
`--serial` bridges exactly one and runs it in this process, which is the
simplest thing that works and is what a single-controller machine should do.
`--all` hands the job to `BridgeManager`, which gives each controller its own
port and its own child process -- measured as the only arrangement that holds
250 Hz on two pads at once (see `manager.py` for the numbers). The tray always
uses the manager.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import controller as C
from . import service as S
from .usbip import DEFAULT_PORT, REQUIRED_RELEASE, Usbip, UsbipNotFound

# The one version number lives in ds5app/__init__.py; everything else -- this
# banner, --version, the release workflow's tag check and the update checker's
# "is that newer than me?" -- reads it from there. Two copies of a version
# string always drift, and a drifted one here would make a build report itself
# as the release it is not.
from . import __version__ as VERSION

BANNER = r"""
  ds5bridge -- a Bluetooth DualSense, presented to Windows as a wired one
"""


def _log(kind: str, text: str) -> None:
    # Each prefix is a 5-character tag `manager._EVENT_PREFIX` parses back
    # into the kind -- "mode" carries the chord engine's remote/keyboard
    # state, which the tray needs even with telemetry off.
    prefix = {"warn": "  !  ", "error": " !!! ", "ready": "  *  ",
              "battery": "  ~  ", "mode": "  #  ",
              "toast": "  >  "}.get(kind, "  -  ")
    print(prefix + text, flush=True)


# ---------------------------------------------------------------------------


def cmd_devices(args) -> int:
    cands = C.probe_all("BT", want_battery=True)
    if not cands:
        print("No DualSense is paired over Bluetooth.")
        print("Pair it: hold CREATE + PS until the light bar flashes, then add")
        print("it in Windows Settings > Bluetooth & devices.")
        return 1
    print(f"{len(cands)} controller(s) enumerated over Bluetooth:\n")
    for c in cands:
        mark = "  " if c.alive else "x "
        print(f" {mark}{c.describe()}")
    dead = [c for c in cands if not c.alive]
    if dead:
        print("\n'x' means it enumerates but does not answer. The usual cause is")
        print("that it is charging on a USB cable -- a DualSense on a cable turns")
        print("its Bluetooth radio off but leaves a stale pairing entry behind.")
    live = [c for c in cands if c.alive]
    if len(live) > 1:
        print(f"\n{len(live)} are live. Bridge one, or bridge all of them:")
        print(f"  ds5bridge --serial {live[0].serial}")
        print(f"  ds5bridge --all")
    return 0


def cmd_cleanup(args) -> int:
    # The documented rescue verb, and a user who has lost a controller to a
    # crashed run will type it -- so it repays the hide debt too, not just the
    # usbip one. `install_crash_handlers()` has already run the sweep by the
    # time most paths get here; this makes it true for `cleanup` specifically,
    # which does not start a service.
    S.hidhide_sweep_once(log_fn=print)
    ok = S.cleanup(port=args.port, usbip_exe=args.usbip, log_fn=print)
    return 0 if ok else 1


def cmd_unhide(args) -> int:
    """Give me my controller back. The escape hatch, from a terminal.

    Deliberately usable when the tray will not start, when HidHide has been
    uninstalled underneath us, and when nothing is running at all. It skips the
    liveness check that `sweep()` normally applies -- this is the "I do not care
    what is running" path -- so a bridge that is live when this runs simply
    finds its pad visible again, which is the outcome the user asked for.
    """
    from . import hidhide as HH

    records = HH.read_records()
    if not records and not args.all_hidhide:
        print("Nothing is hidden by ds5bridge.")
        print(f"(checked {HH.journal_dir()})")
        # An empty journal is not the same claim as "your pad is visible", and
        # the 2026-08-27 report is what that difference costs: a lying unhide
        # deletes the record and leaves the blacklist entry, after which this
        # command used to print the line above and stop -- the escape hatch
        # reporting all-clear on the exact machine that needed it. So say what
        # HidHide itself is holding, and name the verb that clears it.
        #
        # And then the other half of "give me my controller back", which has
        # nothing to do with HidHide's lists at all: a HID child devnode that
        # has gone phantom. Nothing above this line can see that, and nothing
        # else in this program will look until the next hide.
        rc = _report_foreign_entries(HH, args)
        return _revive_known(HH) or rc

    if args.all_hidhide:
        return _unhide_everything(HH, args)

    owed = [r.get("serial") for r in records if r.get("serial")]
    n = HH.sweep(force=True, log_fn=lambda t: print("  " + t))
    left = HH.read_records()
    if left:
        print(f"\n{len(left)} record(s) could NOT be cleared:")
        for r in left:
            print(f"  {r.get('serial')}  {r.get('instance_ids')}")
        print("\nHidHide's own client can clear these: run HidHideCLI.exe "
              "--cloak-off, or untick the device in HidHideClient.exe.")
        return 1
    print(f"unhid {n} controller(s); nothing is hidden by ds5bridge any more")
    # An empty blacklist is not the same claim as a working controller: on the
    # 2026-08-27 machine the list was clear and the pad's HID devnode was still
    # a phantom. `sweep()` has already tried to bring each one back; this says
    # what to do about the ones it could not.
    stuck = [s for s in owed if HH.pad_visible(s) is False]
    for serial in stuck:
        print(f"\n  {HH.power_cycle_hint(serial)}")
    return 1 if stuck else 0


def _revive_known(HH) -> int:
    """Restart the devnode of any known controller that is connected but gone.

    -> 1 if one of them is still not visible afterwards, else 0.

    The escape hatch for the failure that has nothing to do with a blacklist:
    HidHide is holding nothing, the journal is empty, and the pad is still
    invisible because its HID child devnode is a phantom. `revive_serial` does
    the work, decides for itself whether there is anything to do, and says
    "power-cycle the controller" when there is nothing left to try.

    Run as an administrator, this can also reach `pnputil /restart-device`,
    which is what fixed the machine this was written for.
    """
    from . import config as K

    stuck = 0
    for serial in sorted(K.load().controllers):
        r = HH.revive_serial(serial, log_fn=lambda t: print(f"  {t}"))
        if r.get("visible") is False and r.get("action") not in ("disconnected",):
            stuck += 1
    return 1 if stuck else 0


def _report_foreign_entries(HH, args) -> int:
    """Print what HidHide is hiding that we have no record of. Never removes it.

    Read-only on purpose. Some of these entries legitimately belong to somebody
    else -- DS4Windows hides pads through the same driver -- and this module's
    standing rule is that we never take out an entry we did not put in without
    being asked in words. `--all-hidhide` is that asking.
    """
    try:
        hh = HH.HidHide.detect(getattr(args, "hidhide_cli", None))
    except Exception:  # noqa: BLE001
        return 0
    if hh is None:
        return 0
    entries = hh.hidden_raw()
    if entries is None:
        print("\nHidHide is installed but would not tell us what it is hiding, "
              "so this cannot confirm your controller is visible.")
        print("Check it in HidHideClient.exe.")
        return 0
    if not entries:
        return 0
    print(f"\nHidHide itself is still hiding {len(entries)} device(s), none of "
          f"which ds5bridge has a record of:\n")
    for e in entries:
        print(f"    {e}")
    print("\nIf one of those is your controller, `ds5bridge unhide "
          "--all-hidhide` clears them all\n(it also clears any that another "
          "program such as DS4Windows put there), or untick\nthe device in "
          "HidHideClient.exe to remove just the one.")
    return 0


def _unhide_everything(HH, args) -> int:
    """`--all-hidhide`: clear EVERY entry HidHide holds, ours or not.

    Loud and confirmed, because it is a breach of this module's own rule that we
    never remove an entry we did not add. Somebody hiding pads for DS4Windows
    would lose that configuration, so they are shown exactly what is about to go
    and have to type the word.
    """
    hh = HH.HidHide.detect(getattr(args, "hidhide_cli", None))
    if hh is None:
        print("HidHide is not installed, so nothing is hidden by it.")
        return 0
    entries = hh.hidden()
    if not entries:
        print("HidHide's blacklist is already empty.")
        return 0
    print("This clears EVERY device HidHide is hiding, including any that")
    print("another program (DS4Windows, DS4WindowsEx, ...) put there:\n")
    for e in entries:
        print(f"    {e}")
    try:
        if input("\nType 'yes' to clear all of them: ").strip().lower() != "yes":
            print("nothing was changed")
            return 0
    except (EOFError, KeyboardInterrupt):
        print("\nnothing was changed")
        return 0
    ok = hh.unhide(entries)
    for r in HH.read_records():
        HH.remove_record(r.get("_path") or r.get("serial") or "")
    print("cleared" if ok else "HidHide would not accept the change")
    return 0 if ok else 1


def cmd_doctor(args) -> int:
    problems = 0
    print(BANNER.strip() + f"   v{VERSION}\n")

    try:
        u = Usbip(args.usbip, port=args.port)
        ver = u.version()
        print(f"{'[ok]':7}usbip.exe  {u.exe}")
        print(f"{'':7}version    {ver}")
        if REQUIRED_RELEASE not in ver:
            print(f"{'[warn]':7}this project is validated against {REQUIRED_RELEASE}. "
                  f"Do NOT use 0.9.7.8 -- its maintainer warns it can corrupt memory.")
    except UsbipNotFound as e:
        print("[FAIL] usbip.exe not found\n")
        print(str(e))
        return 1

    ports = u.our_ports()
    if ports:
        print(f"{'[warn]':7}something is already attached on port(s) {ports} -- "
              f"run `ds5bridge cleanup`")
        problems += 1
    else:
        print(f"{'[ok]':7}nothing attached")

    if S.port_free(args.port):
        print(f"{'[ok]':7}TCP {args.port} free")
    else:
        print(f"{'[warn]':7}TCP {args.port} held by PID(s) {S.port_owner_pids(args.port)}"
              f" -- run `ds5bridge cleanup`")
        problems += 1

    cands = C.probe_all("BT", want_battery=True)
    live = [c for c in cands if c.alive]
    if not live:
        print(f"{'[FAIL]':7}no live Bluetooth DualSense")
        problems += 1
    else:
        for c in live:
            note = C.battery_note(c.battery_percent, c.battery_state)
            flag = "warn" if (c.battery_percent is not None
                              and c.battery_percent <= C.BATTERY_WARN_PERCENT
                              and not c.battery_state.startswith("charging")) else "ok"
            print(f"{'[' + flag + ']':7}controller {c.serial}  {note}")
    for c in cands:
        if not c.alive:
            print(f"{'[note]':7}{c.serial} enumerates but does not answer "
                  f"(charging on a cable?)")

    # Settings, because "it is not bridging my controller" is far more often a
    # switch somebody turned off than anything wrong with the machine, and
    # there is no other way to see the file's effect without opening it.
    from . import autostart as A
    from . import config as K

    cfg = K.load()
    print(f"{'[ok]':7}settings   {K.config_path()}"
          f"{'' if os.path.exists(K.config_path()) else '  (defaults; no file yet)'}")
    if not cfg.enabled:
        print(f"{'[warn]':7}bridging is turned OFF in the tray menu -- nothing "
              f"will be bridged until it is turned back on")
        problems += 1
    for c in live:
        cc = cfg.get(c.serial)
        if not cc.enabled:
            print(f"{'[note]':7}{c.serial} is switched off in settings "
                  f"(the tray menu turns it back on)")
    if A.available():
        print(f"{'[ok]':7}autostart  {A.describe()}")

    problems += _doctor_hidhide(cfg, live)

    print()
    print("no problems found" if not problems else f"{problems} thing(s) to fix")
    return 0 if not problems else 1


def _doctor_hidhide(cfg, live) -> int:
    """The HidHide block. Reports even when HidHide is absent, on purpose.

    `doctor` is what the user guide tells people to paste into an issue, and
    "my controller vanished from Windows" is the single worst thing this feature
    can do to somebody. Every fact needed to diagnose that is here: whether the
    driver is installed, whether we can talk to it at all, what WE have hidden,
    and -- loudly -- any record whose owning process is dead, which is the
    signature of a crash that did not get to unhide.
    """
    try:
        return _doctor_hidhide_inner(cfg, live)
    except Exception as e:  # noqa: BLE001
        # Non-raising like everything else in this feature, and for a sharper
        # reason here: a diagnostic command that dies with a traceback has
        # failed at the one job it has. Report the failure as a finding.
        print(f"{'[warn]':7}HidHide    could not be checked ({e})")
        return 1


def _doctor_verify_marker() -> None:
    """Report what `app/tools/hidhide_verify.py` last measured, if anything.

    The whitelist read-back above proves the driver HOLDS our path; only the
    verify tool proves the pad actually becomes invisible to others and stays
    open to us, because that needs a real controller and a real hidden window.
    Surfacing its last result keeps the distinction honest instead of implying
    the cheap check is the whole story.
    """
    from . import hidhide as HH

    rec = HH.read_verify_marker()
    if not rec:
        print(f"{'[note]':7}verified   never -- run "
              f"`python app\\tools\\hidhide_verify.py` to prove hiding works "
              f"end to end on this machine")
        return
    when = rec.get("at") or "?"
    blocking = rec.get("blocking")
    granting = rec.get("granting")
    if blocking and granting:
        print(f"{'[ok]':7}verified   {when}: blocking and granting both passed")
    else:
        print(f"{'[warn]':7}verified   {when}: blocking={blocking} "
              f"granting={granting} -- re-run app\\tools\\hidhide_verify.py")


def _doctor_hidhide_inner(cfg, live) -> int:
    from . import hidhide as HH

    problems = 0
    wanted = [c.serial for c in live if cfg.get(c.serial).hide_bluetooth]
    records = HH.read_records()

    hh = HH.HidHide.detect(cfg.hidhide_cli)
    if hh is None:
        flag = "note" if not wanted else "warn"
        print(f"{'[' + flag + ']':7}HidHide    not installed"
              + ("  -- hide_bluetooth is ON for "
                 f"{', '.join(wanted)} and will do nothing" if wanted else
                 "  (optional; hides the Bluetooth pad while bridged)"))
        problems += 1 if wanted else 0
    else:
        ver = hh.version() or "unknown"
        print(f"{'[ok]':7}HidHide    {ver}"
              + (f"  {hh.cli.exe}" if hh.cli else "  (driver only, no CLI)"))
        if hh.control_ok():
            n_all = len(hh.hidden())
            state = "on" if hh.active() else "off"
            print(f"{'[ok]':7}control    reachable; cloak {state}, "
                  f"{n_all} device(s) hidden in total")
            # "Granting": can WE still open a pad we hide? Blocking and granting
            # fail independently, and blocking-without-granting is the one
            # combination that is worse than doing nothing -- it blinds the
            # bridge. Report it by name so it is never mistaken for "HidHide is
            # broken".
            covered = hh.whitelist_covers_us()
            me = HH.current_image_path() or "unknown"
            if covered is True:
                print(f"{'[ok]':7}granting   this program is whitelisted")
                print(f"{'':7}           {me}")
            elif covered is False:
                # Not being whitelisted is the NORMAL idle state -- we register
                # only when we are about to hide something, and the entry is
                # removed with the app. So this is only a finding when a
                # controller has actually asked for hiding; otherwise saying
                # "problem" here would make `doctor` cry wolf on every healthy
                # machine that simply is not using the feature.
                flag = "warn" if wanted else "note"
                print(f"{'[' + flag + ']':7}granting   not whitelisted yet"
                      + ("  -- registered at the next bridge start"
                         if not wanted else
                         "  -- hiding will be REFUSED until this works"))
                print(f"{'':7}           {me}")
                problems += 1 if wanted else 0
            else:
                print(f"{'[note]':7}granting   not checked (the whitelist could "
                      f"not be read)")
            _doctor_verify_marker()
        else:
            print(f"{'[warn]':7}control    HidHide is installed but will not "
                  f"answer. A reboot completes its install; until then the "
                  f"hide feature does nothing.")
            problems += 1

    if not records:
        print(f"{'[ok]':7}hidden by  us: nothing")
    else:
        stale = [r for r in records if not HH.owner_alive(r)]
        flag = "warn" if stale else "ok"
        print(f"{'[' + flag + ']':7}hidden by  us: {len(records)} record(s) in "
              f"{HH.journal_dir()}")
        for r in records:
            dead = "" if HH.owner_alive(r) else "  -- OWNER IS GONE"
            print(f"{'':7}           {r.get('serial')}  pid {r.get('pid')}"
                  f"  {len(r.get('instance_ids') or [])} interface(s){dead}")
        if stale:
            print(f"{'[warn]':7}           run `ds5bridge unhide` to get "
                  f"{'them' if len(stale) > 1 else 'it'} back")
            problems += 1

    problems += _doctor_filter(HH, hh, wanted, records)
    return problems + _doctor_visibility(HH, cfg)


def _doctor_filter(HH, hh, wanted, records) -> int:
    """Is HidHide's FILTER actually in the stack of each pad we hide?

    The check that answers the 2026-09-05 report ("hidden" in the UI, visible
    to every game). HidHide's class filter joins a HID device's stack only
    when that stack is built, so a pad that was already paired when HidHide
    was installed carries a correct hide-list entry that does nothing until
    the device is restarted or the PC rebooted. Everything above this line
    reads the LIST; this reads the STACK (`DEVPKEY_Device_Stack`), for every
    pad we currently hide (the journal) and every connected pad that is set
    to be hidden. Silent about pads that are switched off.
    """
    if hh is None:
        return 0
    problems = 0
    seen = set()
    rows = []
    for r in records:
        serial = (r.get("serial") or "").lower()
        ids = [i for i in (r.get("instance_ids") or []) if i]
        if not serial or not ids:
            continue
        seen.add(serial)
        rows.append((serial, ids, (r.get("parent_id") or "").strip(), True))
    for serial in wanted:
        if serial in seen:
            continue
        ids = HH.resolve_serial(serial, cli=hh.cli)
        if not ids:
            continue                        # off, or not enumerable: not ours to judge here
        rows.append((serial, ids, HH.bt_parent_for_serial(serial), False))
    for serial, ids, parent, hidden_now in rows:
        got = HH.filter_covers(ids, parent)
        what = "hidden now" if hidden_now else "set to hide"
        if got is True:
            print(f"{'[ok]':7}filter     {serial} ({what}): HidHide's filter is "
                  f"in the device stack -- the hide is effective")
        elif got is False:
            print(f"{'[warn]':7}filter     {serial} ({what}): HidHide's filter "
                  f"is NOT in the device stack, so hiding it does nothing yet")
            print(f"{'':7}           (the pad was enumerated before HidHide "
                  f"was installed). The tray restarts the device when it")
            print(f"{'':7}           bridges the pad; or switch the controller "
                  f"off and on, or reboot.")
            problems += 1
        else:
            print(f"{'[note]':7}filter     {serial} ({what}): the device stack "
                  f"could not be read; whether the hide is effective is unknown")
    return problems


def _doctor_visibility(HH, cfg) -> int:
    """Is every controller this machine knows about actually VISIBLE?

    The check that would have answered the 2026-08-27 report in one command.
    Everything above this line can be green -- blacklist empty, cloak off, no
    records -- while a pad's HID child devnode sits there as a phantom and no
    application on the machine can open it. Only an enumeration can tell.

    Silent about controllers that are simply switched off: `revive_serial`
    makes that distinction from the Bluetooth parent devnode, and a diagnostic
    that reported every pad in a drawer as a fault would be worth nothing.
    """
    stuck = []
    for serial in sorted(cfg.controllers):
        if HH.pad_visible(serial) is not False:
            continue                       # visible, or hidapi cannot say
        parent = HH.bt_parent_for_serial(serial)
        if parent and HH.pad_connected(serial, parent):
            stuck.append(serial)
    if not stuck:
        return 0
    print(f"{'[warn]':7}visible    {len(stuck)} connected controller(s) cannot "
          f"be enumerated:")
    for serial in stuck:
        print(f"{'':7}           {serial}")
    print(f"{'':7}           HidHide is not hiding {'them' if len(stuck) > 1 else 'it'}"
          f" -- the HID device itself is missing.")
    print(f"{'':7}           Switch the controller off (hold PS ~10 s) and on "
          f"again, or run")
    print(f"{'':7}           `ds5bridge unhide` from an ADMINISTRATOR prompt to "
          f"restart the device.")
    return len(stuck)


def cmd_run_all(args) -> int:
    """Every connected controller, one child process and one port each.

    Deliberately a different code path from the single-controller case rather
    than the same one with N=1: `BridgeService` in this process is simpler,
    needs no child, and is what the overwhelmingly common one-pad machine
    should get. `manager.py` documents why more than one bridge cannot share an
    interpreter.
    """
    from . import manager as MG

    def ev(serial: str, kind: str, text: str) -> None:
        _log(kind, f"{serial[:4]}..{serial[-4:]}  {text}")

    from . import config as K

    cfg = K.load()
    # The dashboard's telemetry hub binds an ephemeral UDP port; every child
    # is told where it is via `--telemetry-port`. Best-effort throughout: a
    # dashboard problem must never cost anybody their controllers.
    hub = dash = None
    try:
        from . import dashboard as DB
        from . import telemetry as TM

        hub = TM.TelemetryHub()
        hub.start()
    except Exception:  # noqa: BLE001
        logging.getLogger("ds5app.cli").exception(
            "could not start the telemetry hub")
    mgr = MG.BridgeManager(base_port=args.port, usbip_exe=args.usbip,
                           audio_target=args.audio_target,
                           hide_bluetooth={s: cc.hide_bluetooth
                                           for s, cc in cfg.controllers.items()},
                           hide_default=(getattr(args, "hide_bluetooth", False)
                                         or cfg.hide_bluetooth_default),
                           hidhide_cli=(getattr(args, "hidhide_cli", None)
                                        or cfg.hidhide_cli),
                           telemetry_port=(hub.port if hub else None),
                           on_event=ev)
    if hub is not None:
        try:
            dash = DB.DashboardServer(hub=hub, snapshot_fn=mgr.snapshot,
                                      port=cfg.dashboard_port)
            dash.start()
            _log("info", f"dashboard at {dash.url}")
        except Exception as e:  # noqa: BLE001
            dash = None
            _log("warn", f"the dashboard could not start ({e}); bridging "
                         f"continues without it")
    S.install_crash_handlers()
    S.ON_TEARDOWN.append(mgr.close)
    # Before anything of ours goes near a port: disarm any auto-re-attach a
    # previous run left armed, and detach virtual devices whose server is gone.
    # `install_crash_handlers()` has already done the same for HidHide.
    mgr.reconcile_stale(log_fn=lambda t: _log("info", t))
    try:
        started = mgr.start_all()
    except C.NoControllerError as e:
        print(f"\n  {e}")
        _close_quietly(dash, hub)
        return 4
    if not started:
        print("\n  No controller could be bridged. `ds5bridge doctor` checks "
              "the whole setup.")
        mgr.close()
        _close_quietly(dash, hub)
        return 4

    print(f"\n  Windows now sees {len(started)} wired DualSense(s). Start your game.")
    print("  New controllers are picked up as they connect.")
    print("  Press Ctrl+C here to stop and put everything back.\n")
    mgr.start_hotplug()
    try:
        last = 0.0
        while True:
            time.sleep(0.5)
            if args.status_every and time.time() - last >= args.status_every:
                last = time.time()
                for line in mgr.statuses():
                    print("  " + line, flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        mgr.close()
        _close_quietly(dash, hub)
    return 0


def _close_quietly(*closers) -> None:
    """Stop dashboard pieces without letting them complicate a teardown."""
    for closer in closers:
        if closer is not None:
            try:
                closer.stop()
            except Exception:  # noqa: BLE001
                pass


def cmd_run(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    print(BANNER)

    if getattr(args, "all", False):
        if args.serial:
            print("--all and --serial contradict each other. Pick one.")
            return 2
        return cmd_run_all(args)

    # The flag wins; otherwise the user's own setting for this controller does.
    # A child spawned by the manager always gets the flag explicitly, so the
    # config lookup here only ever serves a bare `ds5bridge run`.
    from . import config as K

    cfg = K.load()
    hide = bool(getattr(args, "hide_bluetooth", False))
    if not hide and args.serial:
        hide = cfg.should_hide(args.serial)

    svc = S.BridgeService(serial=args.serial, port=args.port, usbip_exe=args.usbip,
                          audio_target=args.audio_target,
                          auto_cleanup=not args.no_auto_cleanup,
                          hide_bluetooth=hide,
                          hidhide_cli=(getattr(args, "hidhide_cli", None)
                                       or cfg.hidhide_cli),
                          telemetry_port=getattr(args, "telemetry_port", None),
                          input_config=cfg.input,
                          on_event=_log)
    try:
        svc.start()
    except C.AmbiguousControllerError as e:
        print("\nMore than one Bluetooth DualSense is connected. Pick one:\n")
        for c in e.candidates:
            print(f"    ds5bridge --serial {c.serial}      ({c.describe()})")
        print("\n... or bridge every one of them:\n")
        print("    ds5bridge --all")
        return 2
    # The service already emitted every one of these through `_log`, so these
    # handlers add the *next step* and nothing else. Printing `str(e)` again
    # here showed the whole 12-line "install usbip-win2" message twice.
    except UsbipNotFound:
        return 3
    except S.AlreadyRunningError:
        return 5
    except (C.NoControllerError, RuntimeError):
        print("\nNothing was changed on this machine. `ds5bridge doctor` checks "
              "the whole setup.")
        return 4
    except KeyboardInterrupt:
        return 130

    print("\n  Windows now sees a wired DualSense. Start your game.")
    print("  Press Ctrl+C here to stop and put everything back.\n")

    try:
        run_loop(svc, args.status_every)
    except KeyboardInterrupt:
        print()
    finally:
        svc.stop()
    return 0


def run_loop(svc, status_every: float, tick: float = 0.5) -> None:
    """Report status until the bridge fails. Returns on ERROR, raises on Ctrl+C.

    A STATE CHANGE IS PRINTED THE MOMENT IT HAPPENS, not at the next tick.

    This loop is also the status channel of every child `BridgeManager` spawns
    (`manager.default_child_command`), and the state in these lines is the ONLY
    way a parent learns that a controller's Bluetooth link has dropped -- it is
    the one signal that still works while the pad is cloaked and therefore
    cannot be enumerated at all. On a status timer alone, a disconnect waited
    out most of `--status-every` before the parent even heard about it, and
    every one of those seconds is a second in which a game holds a wired
    DualSense that is switched off.

    Half a second of polling costs nothing: `BridgeService.snapshot()` is O(1)
    counters by construction (it says so, and why), and a line is only printed
    when it says something new.
    """
    last = 0.0
    last_state = svc.state
    while True:
        time.sleep(tick)
        snap = svc.snapshot()
        state = snap["state"]
        due = status_every and time.time() - last >= status_every
        if state != last_state or due:
            last_state = state
            last = time.time()
            # The snapshot we already took: `snapshot()` differences the input
            # counter against the previous call to get the report rate, so
            # taking a second one in the same instant reports a stale number.
            print("  " + svc.status_line(snap), flush=True)
        if svc.state == S.ERROR:
            return


def cmd_tray(args) -> int:
    if getattr(args, "service_child", False):
        # Under the service there is no console and no user profile: the
        # narration and the log records go to ProgramData\ds5bridge\logs.
        from . import winsvc as W

        path = W.setup_file_logging("tray", logging.DEBUG if args.verbose
                                    else logging.INFO)
        stream = W.LogStream(logging.getLogger("ds5app.tray.stdout"))
        sys.stdout = sys.stderr = stream
        if path:
            logging.getLogger("ds5app.cli").info("service child starting; "
                                                 "log at %s", path)
    from .tray import run_tray
    return run_tray(args)


def cmd_service(args) -> int:
    """`ds5bridge service [install|uninstall|start|stop|status]`.

    With no action this IS the service: `winsvc.run_service()` hands the
    process to the Service Control Manager and returns when the service
    stops (or at once, with a hint, when a person ran it from a terminal).
    """
    from . import winsvc as W

    action = getattr(args, "action", None) or "run"
    if action == "run":
        return W.run_service()
    if action == "status":
        print(f"service '{W.SERVICE_NAME}': {W.status_text()}")
        q = W.query()
        if q and q.get("binpath"):
            print(f"  {q['binpath']}")
        print(f"  logs: {W.log_dir()}")
        return 0 if q else 1
    try:
        if action == "install":
            exe = getattr(args, "exe", None)
            W.install(exe, start_at_boot=not getattr(args, "manual", False))
            print(f"service '{W.SERVICE_NAME}' registered: {W.bin_path(exe)}")
            if not getattr(args, "no_migrate", False):
                moved = W.migrate_user_config(getattr(args, "migrate_from", None))
                for f in moved:
                    print(f"  settings migrated: {f}")
                if not moved:
                    print(f"  settings: {W.program_data_dir()} (nothing to migrate)")
            if getattr(args, "start", False):
                W.start()
                print("  started")
        elif action == "uninstall":
            W.uninstall()
            print(f"service '{W.SERVICE_NAME}' removed")
        elif action == "start":
            W.start()
            print(f"service '{W.SERVICE_NAME}' started ({W.status_text()})")
        elif action == "stop":
            W.stop()
            print(f"service '{W.SERVICE_NAME}' stopped")
        else:
            print(f"unknown service action {action!r}")
            return 2
    except W.ServiceError as e:
        print(f"[FAIL] {e}")
        return 1
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ds5bridge", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"ds5bridge {VERSION}")

    def common(sp):
        sp.add_argument("--serial", default=None, metavar="BDADDR",
                        help="which controller, by Bluetooth address (see "
                             "`ds5bridge devices`). Required when more than one "
                             "is connected -- index order is NOT stable")
        sp.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"TCP port for the USB/IP server (default "
                             f"{DEFAULT_PORT}; 3240 belongs to usbipd-win and is "
                             f"never touched)")
        sp.add_argument("--usbip", default=None, metavar="PATH",
                        help="path to usbip.exe, if it is somewhere unusual")
        return sp

    sub = p.add_subparsers(dest="cmd")

    r = common(sub.add_parser("run", help="start the bridge (the default)"))
    r.add_argument("--all", action="store_true",
                   help="bridge EVERY connected controller, each on its own "
                        "port and in its own process, and pick up new ones as "
                        "they connect")
    r.add_argument("--audio-target", choices=("speaker", "headphone"),
                   default="speaker")
    r.add_argument("--no-auto-cleanup", action="store_true",
                   help="fail instead of clearing leftovers from a crashed run")
    r.add_argument("--status-every", type=float, default=30.0, metavar="SECONDS",
                   help="print a status line this often (0 to stay quiet)")
    r.add_argument("--hide-bluetooth", action="store_true",
                   help="hide the real Bluetooth pad from everything else while "
                        "it is bridged, so games see ONE controller. Needs "
                        "HidHide installed. Always undone on stop; if a crash "
                        "leaves it hidden, `ds5bridge unhide` gets it back")
    r.add_argument("--hidhide-cli", default=None, metavar="PATH",
                   help="path to HidHideCLI.exe, if it is somewhere unusual")
    r.add_argument("--telemetry-port", type=int, default=None, metavar="PORT",
                   help="publish live input state (buttons, sticks, touch) as "
                        "UDP datagrams to 127.0.0.1:PORT at up to 60 Hz, for "
                        "the web dashboard. The tray passes this to every "
                        "bridge it spawns; a bare run rarely needs it")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("devices", help="list controllers and their battery level")
    d.set_defaults(func=cmd_devices)

    c = common(sub.add_parser("cleanup", help="undo a run that crashed"))
    c.set_defaults(func=cmd_cleanup)

    dr = common(sub.add_parser("doctor", help="check this machine's setup"))
    dr.set_defaults(func=cmd_doctor)

    uh = sub.add_parser("unhide",
                        help="give back a Bluetooth pad left hidden by a crash")
    uh.add_argument("--all-hidhide", action="store_true",
                    help="LAST RESORT: clear every device HidHide is hiding, "
                         "including ones another program hid. Asks first")
    uh.add_argument("--hidhide-cli", default=None, metavar="PATH")
    uh.set_defaults(func=cmd_unhide)

    t = common(sub.add_parser("tray", help="run with a system-tray icon"))
    t.add_argument("--audio-target", choices=("speaker", "headphone"),
                   default="speaker")
    t.add_argument("--hidhide-cli", default=None, metavar="PATH")
    t.add_argument("--no-auto-cleanup", action="store_true")
    t.add_argument("--no-hotplug", action="store_true",
                   help="bridge what is connected now and then stop watching, "
                        "instead of picking up controllers as they connect")
    t.add_argument("--hotplug-interval", type=float, default=5.0,
                   metavar="SECONDS",
                   help="how often to look for controllers (default 5)")
    # Accepted and ignored. It used to mean "start bridging without waiting for
    # a click", which is now simply what the tray does -- but silently failing
    # to parse somebody's existing shortcut or Run-key entry is a worse
    # outcome than carrying a dead flag.
    t.add_argument("--autostart", action="store_true",
                   help=argparse.SUPPRESS)
    # Set by the Windows service (winsvc.py), never by a person: this tray is
    # the service's child. Quit stops the service; the stop event ends it.
    t.add_argument("--service-child", dest="service_child", action="store_true",
                   help=argparse.SUPPRESS)
    t.add_argument("-v", "--verbose", action="store_true")
    t.set_defaults(func=cmd_tray)

    sv = sub.add_parser("service",
                        help="the Windows service that starts ds5bridge at "
                             "boot, before anyone signs in")
    sv.add_argument("action", nargs="?", default="run",
                    choices=("run", "install", "uninstall", "start", "stop",
                             "status"),
                    help="install/uninstall need an administrator prompt; "
                         "bare `service` is what the Service Control Manager "
                         "runs")
    sv.add_argument("--exe", default=None, metavar="PATH",
                    help="install: the ds5bridge.exe to register (default: "
                         "the one next to this program)")
    sv.add_argument("--manual", action="store_true",
                    help="install: register but do not start with Windows")
    sv.add_argument("--start", action="store_true",
                    help="install: start it right away")
    sv.add_argument("--no-migrate", action="store_true",
                    help="install: do not copy this user's settings into "
                         "%%ProgramData%%\\ds5bridge")
    sv.add_argument("--migrate-from", default=None, metavar="DIR",
                    help="install: the settings directory to migrate (default "
                         "%%APPDATA%%\\ds5bridge)")
    sv.set_defaults(func=cmd_service)

    return p


def _owns_console() -> bool:
    """True when this process is the only one on its console.

    Which is the same as "somebody double-clicked me": launched from an existing
    terminal, that terminal's shell is on the console too. It matters because a
    double-clicked program that exits with an error closes its window
    instantly, taking the error message with it -- and the error message is the
    entire point of `doctor` and of every failure path here.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        buf = (ctypes.c_uint * 8)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
        if n != 1:
            return False
        # Alone on a console nobody can see (CREATE_NO_WINDOW: the installer's
        # helper, the service, a scheduled task) is not "double-clicked": there
        # is no window to keep open, and a pause here waits for a keypress
        # that never comes -- 2026-09-16, the uninstaller's `unhide` step.
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        return bool(hwnd) and bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _pause_if_double_clicked() -> None:
    if not _owns_console():
        return
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return
    except Exception:  # noqa: BLE001
        return
    try:
        input("\nPress Enter to close this window ...")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


class _Sink:
    """A stdout/stderr that swallows. Only ever used when there is no real one."""

    def write(self, _s):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False


def harden_streams() -> None:
    """Give `sys.stdout`/`sys.stderr` something to be under `pythonw.exe`.

    A GUI-subsystem interpreter has no standard handles, so CPython sets both
    streams to None -- and then the first `print()` anywhere below raises
    AttributeError from inside whatever callback happened to reach it, with no
    console for the traceback to land on. The frozen windowed build has guarded
    against this since it existed (`packaging/entry_tray.py`); the SOURCE form
    did not, and the source form is exactly what `autostart.build_command()`
    registers on a checkout -- `pythonw.exe -c "...main(['tray'])"`. Same
    hazard, same fix, at the one door every source entry goes through.

    Idempotent, and a no-op wherever the streams are real.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _Sink())


def main(argv=None) -> int:
    harden_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare `ds5bridge` means `ds5bridge run`, and so does `ds5bridge --serial X`.
    if not argv or argv[0].startswith("-") and argv[0] not in ("--version", "-h", "--help"):
        argv.insert(0, "run")
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["run"])
    try:
        code = args.func(args)
    except KeyboardInterrupt:
        return 130
    if code:
        _pause_if_double_clicked()
    return code


if __name__ == "__main__":
    sys.exit(main())
