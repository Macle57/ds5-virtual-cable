"""System-tray front end -- N controllers, a switch for each, and one for all.

A thin layer over `BridgeManager`, the way it used to be a thin layer over
`BridgeService`. Everything that is genuinely hard (which controller, what port,
what order to tear down in, how to survive a hard kill) lives below this file
and is shared byte for byte with the CLI. The tray adds four things: an icon
whose colour is the aggregate state, a menu of toggles, a balloon notification
when something needs attention, and the settings that make all of it survive a
reboot.

Three things this file exists to get right
------------------------------------------
**1. A way to turn it off.** The point of a virtual wired controller is that
some games only light up for one. That is not always what you want: a title
with real DualSense Bluetooth support, or a launcher that gets confused by two
pads, is better served by the native stack. So there is a master switch and a
per-controller switch, both persisted -- and "off" means the bridge stops and
the controller is left alone on Bluetooth for whatever else wants it. Nothing
is uninstalled and no driver is touched; the controller simply goes back to
being an ordinary Bluetooth pad.

**2. It should already be running.** Before this, using the tray meant
launching it and clicking Start. Now `config.autostart_on_login` puts it in the
Run key and the hotplug watcher bridges each enabled controller as it appears,
so the honest answer to "do I have to do anything after setup?" is no: turn the
controller on, and a wired DualSense shows up in Windows a few seconds later.

**3. The menu must be current, and must not move under the cursor.** These
pull in opposite directions, and the first version got the balance wrong in a
way that is worth spelling out because the pystray documentation invites it.

`text`, `checked`, `visible` and `enabled` may all be CALLABLES, so the menu is
built exactly once in `_menu()` and every dynamic thing in it is a lambda over
the latest snapshot; controllers come and go by flipping `visible` on a fixed
bank of slots, which is why `MAX_SLOTS` exists. What is NOT true -- and what
this file assumed for a while -- is that those callables are re-evaluated when
the menu is opened. On Windows they are not. `pystray._win32` evaluates every
one of them in `_update_menu()` and bakes the answers into a native HMENU;
right-clicking the icon only calls `TrackPopupMenuEx` on that cached handle. So
the menu froze at the moment `_mark_ready()` built it -- before any controller
had been discovered -- and stayed frozen. The bug the user sees is: plug in a
second controller, watch the console bridge it, open the menu, and be told
there is one. Then click any row, and the menu is suddenly right -- because
pystray wraps every item's action in `_handler`, which calls `update_menu()`
afterwards. "Rescan for controllers" appeared to be what fixed it. It was not:
the click was.

So `_refresh_now()` fingerprints what the menu RENDERS (`_menu_key`) and calls
`icon.update_menu()` when that changes -- which is the documented contract for
dynamic menus, and rare, because the fingerprint deliberately ignores the
report rate ticking between 249/s and 251/s. The other half is `_menu_open`:
`update_menu()` destroys the HMENU it replaces, `TrackPopupMenuEx` is modal on
the message-loop thread, and this refresh runs on another thread -- so a
rebuild that lands while the menu is on screen would destroy the menu somebody
is reading. `_watch_menu_visibility()` wraps pystray's own notify handler to
know when that is, and a rebuild that arrives during it is deferred to the
moment the menu closes.

**4. A click is an intent, not an order to move hardware right now.** Starting
a bridge takes seconds and cloaking a pad talks to a kernel driver, and a user
working a menu of checkboxes will click three of them in four seconds. So every
toggle updates the config file and the checkbox INSTANTLY, but the side effects
are debounced: each click (re)arms one `APPLY_DELAY_S` timer, and when it
expires the DIFF between what the user asked for (`_pending`) and what the
manager is actually doing is applied -- so a box ticked and unticked inside the
window is a no-op, and three clicks cost one apply pass. See `_poke_apply` /
`_apply_now` for the mechanics and `_quit` for why quitting CANCELS rather than
flushes.

A note on the menu's shape, because it was tried the other way first: pystray
cannot make one item both a checkbox you can click and a submenu you can open.
`MenuItem` takes a single `action` that is either a callable or a `Menu`, and
`MenuItem.__call__` refuses to invoke a `Menu` action; on Windows the backend
would even RENDER `checked` on a submenu item (`_win32._create_menu_item` sets
MFS_CHECKED and hSubMenu independently), but an MF_POPUP item never sends
WM_COMMAND, so the checkmark would be a picture of a control rather than a
control. Hence the fallback shape: a checkable "Bridging enabled" row with a
"Controllers" submenu next to it, and the same pair for hiding.

Why pystray and not tkinter: a tray icon is what a background utility should
be, a tkinter window is one more thing to minimise, and pystray's Windows
backend already exposes native balloon notifications through `icon.notify()` --
so there is no third notification dependency (win10toast is unmaintained; plyer
pulls in a stack for one call). Pillow draws the icon in memory, so no image
file has to ship or be found at runtime, which also keeps the frozen exe a
single file.

    ds5bridge tray [--serial BDADDR] [--no-hotplug]
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import autostart as A
from . import config as K
from . import controller as C
from . import manager as M
from . import service as S
from . import update as U

log = logging.getLogger("ds5app.tray")

REFRESH_S = 2.0

#: How long a toggled checkbox waits for the user's next click before its side
#: effects run. Two seconds is long enough to tick three boxes in one visit to
#: the menu and short enough that "I clicked it and nothing happened" never has
#: time to form as a thought. Config and checkbox state change at click time;
#: only the bridge starts/stops and HidHide cloaks wait for this.
APPLY_DELAY_S = 2.0

#: Fixed per-controller menu slots. A slot is a menu item whose `visible` is a
#: lambda, so the menu object never has to be rebuilt (see the module docstring).
#: Eight is well past anything real -- Windows tops out at four DualSenses for
#: most games -- and an invisible slot costs nothing.
MAX_SLOTS = 8

#: Grey stopped, blue starting, green running, amber degraded, red error.
COLORS = {
    S.STOPPED:  (110, 110, 118),
    S.STARTING: (70, 130, 200),
    S.RUNNING:  (60, 170, 90),
    S.DEGRADED: (215, 155, 40),
    S.STOPPING: (110, 110, 118),
    S.ERROR:    (200, 60, 60),
}
DISABLED_COLOR = (86, 86, 92)


def _icon_image(rgb, battery: int | None, count: int = 0, size: int = 64):
    """A DualSense-ish silhouette in the state colour, with a battery bar.

    Drawn rather than loaded: a bundled .ico is one more thing for a one-file
    exe to unpack and find, and at 64 px the shape only has to read as "a
    gamepad" at 16 px.

    `count` is how many controllers are bridged. Two pads at 16 px is an
    unreadable smudge, so a second one is a pip in the corner instead.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0
    # body: two grips joined by a bar
    d.rounded_rectangle([6 * s, 20 * s, 58 * s, 46 * s], radius=12 * s, fill=rgb + (255,))
    d.ellipse([2 * s, 22 * s, 26 * s, 50 * s], fill=rgb + (255,))
    d.ellipse([38 * s, 22 * s, 62 * s, 50 * s], fill=rgb + (255,))
    # sticks
    for cx in (24, 40):
        d.ellipse([(cx - 5) * s, 30 * s, (cx + 5) * s, 40 * s], fill=(20, 20, 24, 255))
    if count > 1:
        # A filled pip, outlined so it stays visible against the body colour.
        d.ellipse([44 * s, 2 * s, 62 * s, 20 * s], fill=(30, 30, 34, 255))
        d.ellipse([46 * s, 4 * s, 60 * s, 18 * s], fill=rgb + (255,))
    if battery is not None:
        w = max(2, int(48 * s * min(100, max(0, battery)) / 100))
        low = battery <= C.BATTERY_WARN_PERCENT
        d.rectangle([8 * s, 52 * s, 56 * s, 58 * s], fill=(0, 0, 0, 90))
        d.rectangle([8 * s, 52 * s, 8 * s + w, 58 * s],
                    fill=(215, 60, 60, 255) if low else (60, 190, 100, 255))
    return img


def short(serial: str) -> str:
    """A bdaddr shortened for a menu. Keeps both ends -- the middle is noise."""
    s = (serial or "").strip()
    return s if len(s) <= 8 else f"{s[:4]}..{s[-4:]}"


def describe(c: dict) -> str:
    """One controller as a single menu line.

    Reads left to right as identity, then what it is doing, then why you might
    care: 'a0fa..d8bb  running 250/s  20%'.
    """
    label = (c.get("label") or "").strip()
    name = f"{label} ({short(c['serial'])})" if label else short(c["serial"])
    bits = [name]
    if not c.get("enabled"):
        bits.append("off")
    elif not c.get("present"):
        bits.append("not connected")
    elif c["state"] == S.RUNNING:
        bits.append(f"{c.get('reports_per_s') or 0:.0f}/s")
    elif c["state"] == S.STOPPED and c.get("error"):
        bits.append("failed")
    else:
        bits.append(str(c["state"]))
    bat = c.get("battery_percent")
    if bat is not None:
        bits.append(f"{bat}%")
    return "  ".join(bits)


class TrayApp:
    #: Class-level default so an instance whose __init__ was bypassed (the
    #: test rig builds them with __new__) still renders a menu: every menu
    #: lambda goes through U.* helpers that accept None.
    updater = None

    def __init__(self, args):
        self.args = args
        self.icon = None
        self._stop = threading.Event()
        self._busy: dict[str, bool] = {}
        self._lock = threading.Lock()
        #: What the menu looked like the last time it was actually rebuilt, and
        #: whether a rebuild is waiting for an open menu to close. See point 3
        #: of the module docstring.
        self._menu_key_built: tuple | None = None
        self._menu_open = False
        self._menu_dirty = False
        #: The debounced-intent ledger -- see point 4 of the module docstring.
        #: Keys are "master", ("enabled", serial) and ("hide", serial); the
        #: value is what the user last asked for. An entry exists only between
        #: a click and the apply pass that honoured (or no-op'd) it, so the
        #: rule for reading state anywhere in this file is: pending first,
        #: then the snapshot. Guarded by `_lock`, like everything else the
        #: menu handlers and the worker threads both touch.
        self._pending: dict = {}
        self._apply_timer = None
        #: `threading.Timer` in production; tests inject a hand-fired fake so
        #: that "wait two seconds" never appears in a test run.
        self._timer_factory = threading.Timer

        self.cfg = K.load()
        self._reconcile_autostart()
        # A --serial on the command line is a one-run override, not a settings
        # change: it restricts this session to one controller without editing
        # what the user has chosen for every other run.
        self.only = K.norm_serial(getattr(args, "serial", None)) or None

        #: Whether HidHide is installed, decided ONCE. The menu's `visible`
        #: lambdas are evaluated on every draw and must stay cheap, and
        #: installing HidHide requires a reboot anyway, so there is nothing to
        #: notice mid-session.
        self.hidhide_cli = getattr(args, "hidhide_cli", None) or self.cfg.hidhide_cli
        self._has_hidhide = self._detect_hidhide()

        #: Background update check (None when `update_check: false`). The whole
        #: integration is four U.* calls -- this one, two lambdas in `_menu()`,
        #: one entry in `_menu_key()` -- and everything else lives in update.py,
        #: deliberately: see its module docstring, "The tray touchpoint".
        self.updater = U.start_if_enabled(self.cfg, notify=self._notify)

        self.mgr = M.BridgeManager(
            base_port=self.cfg.port_base,
            ports=self._ports(),
            enabled=self._enabled(),
            hide_bluetooth={s: cc.hide_bluetooth
                            for s, cc in self.cfg.controllers.items()},
            hide_default=self.cfg.hide_bluetooth_default,
            hidhide_cli=self.hidhide_cli,
            master_enabled=self.cfg.enabled,
            default_enabled=self.cfg.auto_bridge_new,
            usbip_exe=getattr(args, "usbip", None),
            audio_target=getattr(args, "audio_target", "speaker"),
            hotplug_interval=float(getattr(args, "hotplug_interval", 5.0)),
            on_event=self._on_event,
            on_enabled_changed=self._persist_enabled,
            on_master_changed=self._persist_master,
            on_port_assigned=self._persist_port,
            on_hide_changed=self._persist_hide)
        self._snap = self.mgr.snapshot()

    @staticmethod
    def _detect_hidhide() -> bool:
        try:
            from . import hidhide as HH

            return HH.HidHide.detect() is not None
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _journal_count() -> int:
        """How many controllers WE have hidden. 0 on any failure.

        Drives the visibility of "Unhide everything now", so it must answer even
        when HidHide has been uninstalled underneath us -- that is precisely the
        state a user needs to escape from.
        """
        try:
            from . import hidhide as HH

            return HH.journal_count()
        except Exception:  # noqa: BLE001
            return 0

    # -- settings ----------------------------------------------------------

    def _reconcile_autostart(self) -> None:
        """THE REGISTRY WINS. `config.autostart_on_login` only mirrors it.

        Two places can say whether this program starts at login, and they can
        disagree for reasons that have nothing to do with each other: the Run
        key is edited by Task Manager's Startup tab, by `autostart.enable()`
        called directly, by a profile migration, or by a security product
        clearing it; the config file is edited by hand and copied between
        machines. Observed straight away in practice -- the Run key was set
        from outside the tray and `autostart_on_login` stayed false.

        Only one can be the answer, and it has to be the mechanism Windows
        actually obeys. So the checkbox reads `autostart.is_enabled()` and this
        drags the config field into line at startup, which keeps the field
        honest for anything that reads it later without making it load-bearing.
        """
        if not A.available():
            return
        live = A.is_enabled()
        if live != self.cfg.autostart_on_login:
            self.cfg.autostart_on_login = live
            K.try_save(self.cfg)

    def _ports(self) -> dict:
        return {s: cc.port for s, cc in self.cfg.controllers.items() if cc.port}

    def _enabled(self) -> dict:
        """The per-controller switches, with `--serial` applied on top.

        With `--serial`, everything else is off for this run only -- nothing is
        written, so the next plain launch is back to the user's real settings.
        """
        out = {s: cc.enabled for s, cc in self.cfg.controllers.items()}
        if self.only:
            out = {s: (s == self.only) for s in set(out) | {self.only}}
        return out

    def _save(self) -> None:
        # try_save, never save: an exception raised inside a pystray menu
        # handler unwinds through the Win32 message loop and takes the icon
        # with it, and a tray utility that vanishes when you click a checkbox
        # is worse than one that quietly fails to remember a setting.
        if not K.try_save(self.cfg):
            self._notify("ds5bridge", "Could not save settings to "
                                      f"{K.config_path()}")

    def _persist_enabled(self, serial: str, value: bool) -> None:
        if self.only:
            return                      # a one-run override must not be written
        self.cfg.set_enabled(serial, value)
        self._save()

    def _persist_master(self, value: bool) -> None:
        self.cfg.set_master_enabled(value)
        self._save()

    def _persist_hide(self, serial: str, value: bool) -> None:
        self.cfg.set_hide_bluetooth(serial, value)
        self._save()

    def _persist_port(self, serial: str, port: int) -> None:
        # Trap 5 from the manager's hardware testing: a controller that comes
        # back on a different port is a NEW devnode to Windows, so it loses its
        # per-device settings (button mapping, per-game bindings). The map has
        # to outlive the process for the port to be stable.
        self.cfg.get(serial).port = port
        self._save()

    # -- events from the manager -------------------------------------------

    def _on_event(self, serial: str, kind: str, text: str) -> None:
        print(f"  {short(serial)} {kind}: {text}", flush=True)
        if kind in ("warn", "error") and self.icon is not None:
            self._notify(f"ds5bridge -- {short(serial)}", text)

    def _notify(self, title: str, text: str) -> None:
        try:
            self.icon.notify(text[:250], title)
        except Exception:  # noqa: BLE001  (no notification support is not fatal)
            pass

    # -- menu actions ------------------------------------------------------

    def _work(self, key: str, fn) -> bool:
        """Run a menu action off the UI thread, once at a time per key.

        `start()` takes seconds -- it spawns a child, waits for a socket and
        runs `usbip attach` -- and pystray calls menu handlers on the thread
        pumping the Win32 message loop. Doing this inline freezes the menu and
        the icon; doing it without the guard lets an impatient double-click
        start two bridges for one controller, which the manager's own in-flight
        guard would then have to refuse.

        Returns whether the work was actually started: False means an earlier
        run under the same key is still going, which `_apply_pending` uses to
        try again later rather than silently dropping the user's intent.
        """
        with self._lock:
            if self._busy.get(key):
                return False
            self._busy[key] = True

        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self._notify("ds5bridge", str(e))
            finally:
                with self._lock:
                    self._busy[key] = False
                self._refresh_now()
        threading.Thread(target=run, name=f"tray-{key}", daemon=True).start()
        return True

    # -- desired state: what the user has asked for, applied or not ---------

    def _desired_master(self) -> bool:
        with self._lock:
            want = self._pending.get("master")
        if want is not None:
            return bool(want)
        return bool(self._snap.get("master_enabled", True))

    def _desired_enabled(self, serial: str) -> bool:
        with self._lock:
            want = self._pending.get(("enabled", serial))
        if want is not None:
            return bool(want)
        c = self._snap.get("controllers", {}).get(serial) or {}
        return bool(c.get("enabled"))

    def _desired_hide(self, serial: str) -> bool:
        with self._lock:
            want = self._pending.get(("hide", serial))
        if want is not None:
            return bool(want)
        c = self._snap.get("controllers", {}).get(serial) or {}
        return bool(c.get("hide_bluetooth"))

    # -- the debounce: click now, move hardware in APPLY_DELAY_S ------------

    def _poke_apply(self) -> None:
        """(Re)arm the one apply timer. Called at the end of every toggle.

        One timer for the whole menu, not one per switch: the point is that a
        burst of clicks -- master off, two pads re-enabled, one hide flipped --
        settles into a single apply pass over the net difference, and separate
        timers would turn that back into four staggered ones.
        """
        with self._lock:
            if self._apply_timer is not None:
                self._apply_timer.cancel()
            t = self._timer_factory(APPLY_DELAY_S, self._apply_pending)
            t.daemon = True
            self._apply_timer = t
        t.start()

    def _apply_pending(self) -> None:
        """The timer expired with no further clicks -- run the apply pass.

        Runs on the timer thread, so it goes through `_work` like every other
        slow action. A refusal means the PREVIOUS apply pass is still running
        (a bridge start mid-flight); re-arming the timer instead of dropping is
        what keeps a click made during that window from being lost -- the diff
        is computed at execution time, so the retry always applies the newest
        intent.
        """
        if not self._work("apply", self._apply_now):
            self._poke_apply()

    def _apply_now(self) -> None:
        """Diff the intent ledger against the manager and apply the NET change.

        A switch that was flipped on and back off inside the window compares
        equal to what the manager is already doing and costs nothing -- that is
        the entire contract of the debounce, and it falls out of diffing here
        rather than remembering what each click said.

        The order is deliberate: master-off first, so the per-controller flags
        that follow are bookkeeping against an already-stopped fleet instead of
        a stop apiece; then the per-controller switches; then master-on, whose
        `start_all()` starts exactly the set the flags now describe; then the
        hides, which want the bridges in their final state so a cloak lands on
        a pad that is actually being bridged.

        Entries are cleared only if the user has not touched them again while
        this ran -- a click mid-apply re-armed the timer, and its entry stays
        pending for that next pass.
        """
        with self._lock:
            todo = dict(self._pending)
        if not todo:
            return
        want_master = todo.get("master")
        if want_master is not None and not want_master \
                and self.mgr.master_enabled:
            self.mgr.set_master_enabled(False)
        for key, want in todo.items():
            if isinstance(key, tuple) and key[0] == "enabled" \
                    and bool(want) != self.mgr.is_enabled(key[1]):
                self.mgr.set_enabled(key[1], bool(want))
        if want_master is not None and want_master \
                and not self.mgr.master_enabled:
            self.mgr.set_master_enabled(True)
        hid, shown = [], []
        for key, want in todo.items():
            if isinstance(key, tuple) and key[0] == "hide" \
                    and bool(want) != self.mgr.is_hiding(key[1]):
                self.mgr.set_hide_bluetooth(key[1], bool(want))
                (hid if want else shown).append(key[1])
        with self._lock:
            for key, want in todo.items():
                if self._pending.get(key) == want:
                    del self._pending[key]
        self._notify_hide_applied(hid, shown)

    def _notify_hide_applied(self, hid: list, shown: list) -> None:
        """ONE balloon for what an apply pass hid or unhid, however it got here.

        The per-controller path has to name the pad it is talking about; a
        coalesced click on the all-switch is about all of them, and N balloons
        for one gesture is how a helpful message becomes something a user turns
        off. Without HidHide nothing was cloaked and the CLICK already said so,
        so this stays quiet rather than repeating it two seconds later.
        """
        if not self._has_hidhide or not (hid or shown):
            return
        if len(hid) == 1 and not shown:
            # HidHide gates IRP_MJ_CREATE, so an already-open handle is
            # untouched: a game that is running right now keeps seeing the
            # pad. Saying so here is the difference between a feature that
            # looks broken and one that looks honest.
            self._notify("Hide Bluetooth pad",
                         f"{short(hid[0])} is hidden. Games started "
                         f"from now on will not see the Bluetooth pad.")
        elif len(shown) == 1 and not hid:
            self._notify("Hide Bluetooth pad",
                         f"{short(shown[0])} is visible to everything again.")
        else:
            bits = []
            if hid:
                bits.append(f"{len(hid)} controller(s) hidden. Games started "
                            f"from now on will not see the Bluetooth pads.")
            if shown:
                bits.append(f"{len(shown)} controller(s) visible to "
                            f"everything again.")
            self._notify("Hide Bluetooth pads", " ".join(bits))

    def _cancel_pending(self) -> None:
        """Drop every unapplied intent and disarm the timer. Used at teardown.

        Cancelling is safe precisely because the config was written at click
        time: nothing the user chose is lost, the next launch reads the file
        and converges on it.
        """
        with self._lock:
            t, self._apply_timer = self._apply_timer, None
            self._pending.clear()
        if t is not None:
            try:
                t.cancel()
            except Exception:  # noqa: BLE001
                pass

    # -- menu toggles: instant config + checkbox, debounced hardware --------

    def _toggle_master(self, *_):
        want = not self._desired_master()
        with self._lock:
            self._pending["master"] = want
        self.cfg.set_master_enabled(want)
        self._save()
        self._poke_apply()

    def _toggle_controller(self, serial: str):
        def act(*_):
            want = not self._desired_enabled(serial)
            with self._lock:
                self._pending[("enabled", serial)] = want
            self._persist_enabled(serial, want)
            self._poke_apply()
        return act

    def _toggle_hide(self, serial: str):
        def act(*_):
            want = not self._desired_hide(serial)
            with self._lock:
                self._pending[("hide", serial)] = want
            self._persist_hide(serial, want)
            if not self._has_hidhide:
                # Said at click time, not two seconds later: with HidHide
                # absent there is no hardware side to wait for, and delaying
                # "this will do nothing" makes it read like it did something.
                self._notify("Hide Bluetooth pad",
                             "HidHide is not installed, so nothing is "
                             "hidden. See the user guide.")
            self._poke_apply()
        return act

    def _toggle_hide_all(self, *_):
        """One click for every controller, and for the ones not here yet.

        Hiding two pads used to be two clicks in two different rows, and there
        was no way at all to say "and anything else I plug in". This says both:
        every present controller's flag goes into the intent ledger (the
        debounced apply pushes them through the manager, which applies each to
        a running bridge), and `hide_bluetooth_default` records the same answer
        for a controller seen for the first time.

        The seed is written to BOTH halves immediately, and forgetting either
        one makes the all-switch look like it did not stick:
        `hide_bluetooth_default` seeds `hide_bluetooth` on a first sighting, so
        it is what a pad plugged in tomorrow inherits; the manager took a COPY
        of it at construction (`hide_default`), so it is also what a pad
        plugged in five minutes from now inherits, without a restart. The copy
        is assigned directly because the manager has no setter for it -- a bare
        bool store is atomic, and the manager only ever reads it as a fallback.
        It is not debounced: seeding is a pure settings write with no hardware
        behind it, exactly like the per-controller config flags.
        """
        want = not self._hide_all_checked()
        serials = [c["serial"] for c in self._controllers()]
        with self._lock:
            for serial in serials:
                self._pending[("hide", serial)] = want
        self.cfg.set_hide_bluetooth_default(want)
        for serial in serials:
            self.cfg.set_hide_bluetooth(serial, want)
        self._save()
        self.mgr.hide_default = bool(want)
        if not self._has_hidhide:
            self._notify("Hide Bluetooth pads",
                         "HidHide is not installed, so nothing is hidden. "
                         "See the user guide.")
        elif not serials:
            self._notify("Hide Bluetooth pads",
                         "No controller is connected. Pads that show up "
                         "from now on will be hidden while bridged."
                         if want else
                         "No controller is connected. Pads that show up "
                         "from now on will stay visible.")
        self._poke_apply()

    def _open_dashboard(self, *_):
        """Open the local status dashboard in whatever browser is the default.

        `webbrowser.open` can block for however long the OS takes to find and
        launch a browser, and this is a pystray menu handler, so it goes
        through `_work` like everything else slow. The port comes from the
        config so the menu item and the server (which lives elsewhere) can
        never disagree about the address.
        """
        url = f"http://127.0.0.1:{getattr(self.cfg, 'dashboard_port', K.DEFAULT_DASHBOARD_PORT)}"

        def go():
            import webbrowser

            if not webbrowser.open(url):
                self._notify("Open dashboard",
                             f"Could not open a browser. The dashboard is at "
                             f"{url}.")
        self._work("dashboard", go)

    def _unhide_all(self, *_):
        """The panic button. Stop everything first, then force the sweep.

        Stopping first is what makes the state coherent afterwards: every
        bridge's own `stop()` unhides its controller through the normal path, so
        the forced sweep only has to deal with what a crash left behind.

        What it must never do is say "every pad is visible again" on the
        strength of an empty journal. The journal is a list of debts we know
        about, and the failure this button exists for -- 2026-08-27, "unhiding
        does not unhide" -- produces a blacklist entry with no debt recorded
        against it. On that machine, this notification was the all-clear that
        sent the user away from the one control that could still have helped.
        So the sweep's own count is reported, and then HidHide is asked.
        """
        def go():
            from . import hidhide as HH

            self.mgr.stop_all()
            # Whose pads this is answerable for, taken BEFORE the sweep -- a
            # cleared record is a deleted record, and the serials are what the
            # visibility check below needs.
            owed = [r.get("serial") for r in HH.read_records() if r.get("serial")]
            n = HH.sweep(force=True, log_fn=lambda t: print("  " + t, flush=True))
            left = HH.journal_count()
            stray = HH.unrecorded_hidden()
            stuck = [s for s in owed if HH.pad_visible(s) is False]
            if left:
                # A record that survives the sweep is the worse failure of the
                # two and names the real culprit, so it is reported first even
                # though its pad is necessarily in `stuck` as well.
                self._notify("ds5bridge",
                             f"{left} controller(s) could not be unhidden. Use "
                             f"HidHideClient.exe, or run `ds5bridge unhide`.")
            elif stuck:
                # The blacklist can be empty and the controller still gone: the
                # HID devnode under it goes phantom, and only a re-enumeration
                # brings it back. `sweep()` has already tried, including the
                # elevated route if this process happens to have it.
                self._notify("ds5bridge", HH.power_cycle_hint(short(stuck[0]))
                             if len(stuck) == 1 else
                             f"{len(stuck)} controllers are unhidden but still "
                             f"not showing up. Switch each one off and on "
                             f"again.")
            elif stray is None:
                self._notify("ds5bridge",
                             "HidHide would not say what it is hiding, so this "
                             "cannot confirm your pads are visible. Check "
                             "HidHideClient.exe.")
            elif stray:
                self._notify("ds5bridge",
                             f"HidHide is still hiding {len(stray)} device(s) "
                             f"that ds5bridge has no record of. Run `ds5bridge "
                             f"unhide --all-hidhide`, or untick them in "
                             f"HidHideClient.exe.")
            else:
                self._notify("ds5bridge",
                             f"Unhid {n} controller(s). Every pad is visible "
                             f"to Windows again.")
        self._work("unhide-all", go)

    def _toggle_autostart(self, *_):
        def act():
            want = not A.is_enabled()
            try:
                A.set_enabled(want)
            except A.AutostartError as e:
                self._notify("Start at login", str(e))
                return
            self.cfg.autostart_on_login = want
            self._save()
            self._notify("ds5bridge",
                         "ds5bridge will start when you log in."
                         if want else "ds5bridge will no longer start at login.")
        self._work("autostart", act)

    def _install_update(self, *_):
        """The "Install update" row. All of it happens in update.py; the tray
        only supplies its own notifier and the clean way to exit -- the swap
        helper is waiting on this PID, so `_quit` (teardown, then exit) is
        exactly the right hand-off."""
        U.install(self.updater, notify=self._notify, quit_cb=self._quit)

    def _rescan(self, *_):
        self._work("rescan", self.mgr.poll_once)

    def _copy_status(self, *_):
        # No clipboard dependency: printing to the console the tray was
        # launched from is enough, and a hidden console is the user's choice.
        for line in self.mgr.statuses() or ["no controllers"]:
            print("  " + line, flush=True)

    def _shutdown_icon(self) -> None:
        """Break the message loop. Safe from any thread and safe twice.

        Followed by a hard-exit watchdog, which is not paranoia: measured
        2026-08-25 on the FROZEN tray, a Ctrl+Break tore the bridge down
        correctly (the virtual device really did detach) and the process then
        stayed alive with a dead bridge and a stale icon. A windowed build has
        no console, the signal never becomes a `KeyboardInterrupt` the message
        loop can see, and something in the GUI stack keeps a thread alive. By
        the time this fires the teardown has already completed and been
        verified, so there is nothing left to lose by leaving abruptly -- and a
        tray icon that will not go away is a genuinely bad user experience.
        """
        self._stop.set()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass

        def _hard_exit():
            time.sleep(3.0)
            os._exit(0)

        threading.Thread(target=_hard_exit, name="tray-exit", daemon=True).start()

    def _quit(self, *_):
        """Cancel unapplied intents, then tear down. CANCEL, not flush.

        The choice matters and was made on one question: which behaviour can
        strand a pad hidden? Cancelling cannot. A pending "hide" was never
        applied, so there is no cloak to strand; a pending "unhide" names a pad
        that IS cloaked, and `mgr.close()` unhides everything this process hid
        regardless of any intent -- each bridge's stop path uncloaks its own
        pad and the final sweep clears the journal. Flushing, by contrast, is
        the dangerous one: it could start a bridge or lay a fresh cloak
        milliseconds before `close()` tears it down, racing the three-second
        `os._exit` watchdog that `_shutdown_icon` arms -- and a cloak applied
        just before a hard exit is exactly how a pad gets stranded invisible.
        Nothing the user chose is lost either way, because every click already
        wrote the config; the next launch converges on it.
        """
        self._cancel_pending()
        try:
            self.mgr.close()
        finally:
            self._shutdown_icon()

    # -- rendering ---------------------------------------------------------

    def _controllers(self) -> list[dict]:
        """The snapshot's controllers, in a STABLE order.

        Sorted by serial, deliberately. Enumeration order is not stable
        (`controller.py`), and a menu whose rows swap places between two
        openings is a menu you cannot click reliably.
        """
        cs = list(self._snap.get("controllers", {}).values())
        for c in cs:
            c.setdefault("label", self.cfg.get(c["serial"]).label)
        return sorted(cs, key=lambda c: c["serial"])

    def _slot(self, i: int):
        def visible(_item=None) -> bool:
            return i < len(self._controllers())

        def text(_item=None) -> str:
            cs = self._controllers()
            return describe(cs[i]) if i < len(cs) else ""

        def checked(_item=None) -> bool:
            # Desired, not actual: the checkbox must move at click time, two
            # seconds before the bridge does. The row TEXT stays actual
            # (`describe`), so a glance still says what is really running.
            cs = self._controllers()
            return self._desired_enabled(cs[i]["serial"]) if i < len(cs) \
                else False

        def action(*_):
            cs = self._controllers()
            if i < len(cs):
                self._toggle_controller(cs[i]["serial"])()

        return visible, text, checked, action

    def _hide_slot(self, i: int):
        """One row of the hide submenu -- the same fixed-slot trick as `_slot`.

        A second bank of slots rather than reusing the bridging rows: the
        bridging rows are click-to-toggle-bridging, which is the common case,
        and overloading them with a second meaning would make the frequent
        action pay for the rare one.
        """
        def visible(_item=None) -> bool:
            return self._has_hidhide and i < len(self._controllers())

        def text(_item=None) -> str:
            cs = self._controllers()
            if i >= len(cs):
                return ""
            label = (cs[i].get("label") or "").strip()
            return f"{label} ({short(cs[i]['serial'])})" if label \
                else short(cs[i]["serial"])

        def checked(_item=None) -> bool:
            # Desired, like the bridging rows: the box moves at click time.
            cs = self._controllers()
            return self._desired_hide(cs[i]["serial"]) if i < len(cs) \
                else False

        def action(*_):
            cs = self._controllers()
            if i < len(cs):
                self._toggle_hide(cs[i]["serial"])()

        return visible, text, checked, action

    def _hide_all_checked(self, _item=None) -> bool:
        """Checked only when EVERY listed controller is set to hide. Mixed
        reads off.

        This is the tri-state rule flattened for a control with two states:
        all-on renders checked, all-off and PARTIAL both render unchecked --
        pystray has no third state, so a mixed selection has to pick one, and
        the choice is made by what the next click should do. An unchecked box
        hides the remainder, which is the reason somebody opens this menu with
        one pad already hidden. Checking it on a mixed state would instead
        unhide the pad that is already hidden -- the opposite of what the row
        is for, and destructive of a setting the user made deliberately.

        Reads DESIRED state (`_desired_hide`), so the box answers for the
        click the user just made rather than the cloak that lands two seconds
        later.

        With nothing connected the row shows the SEED
        (`hide_bluetooth_default`) rather than the vacuous `all([]) is True`.
        A checkbox that is ticked when there is nothing to tick would claim a
        state that does not exist, and clicking it would then unhide-by-default
        every controller that ever appears.
        """
        cs = self._controllers()
        if not cs:
            return bool(self.cfg.hide_bluetooth_default)
        return all(self._desired_hide(c["serial"]) for c in cs)

    def _hide_all_row(self):
        """(visible, checked, action) for the top-level hide switch.

        This is the OUTER checkbox now -- "Hide Bluetooth pads while bridged"
        in the main menu, next to the per-controller submenu -- but the shape
        is unchanged: visible on exactly the rule the per-controller rows use,
        because a switch over rows that are not there explains nothing. Kept as
        a factory so the menu can stay built-exactly-once and so this is
        testable without pystray.
        """
        return (lambda _item=None: self._has_hidhide,
                self._hide_all_checked,
                self._toggle_hide_all)

    def _hide_menu(self):
        """The per-controller half of hiding -- the submenu. Built once.

        The all-switch is NOT in here any more: it is the checkable
        "Hide Bluetooth pads while bridged" row in the main menu, one level up
        (see the module docstring for why pystray cannot put the checkbox and
        the submenu on one item). This menu is only the per-pad checkboxes and
        the escape hatch.

        Two visibility rules that are deliberate:

        * with HidHide absent, the per-controller rows are HIDDEN and a single
          disabled row explains why. Greying out the checkboxes instead would
          leave an unexplained grey control, which invites a support question
          that the row answers for free.
        * `Unhide everything now` is visible whenever the journal is non-empty
          EVEN IF HidHide is undetected, because "HidHide is gone and my pad is
          still hidden" is exactly the state a user needs to escape.
        """
        import pystray

        items = []
        for i in range(MAX_SLOTS):
            visible, text, checked, action = self._hide_slot(i)
            items.append(pystray.MenuItem(text, action, checked=checked,
                                          visible=visible))
        items += [
            pystray.MenuItem("HidHide is not installed", lambda *_: None,
                             enabled=lambda _i: False,
                             visible=lambda _i: not self._has_hidhide),
            pystray.MenuItem(
                lambda _i: ("No controller connected" if self._has_hidhide
                            else ""),
                lambda *_: None, enabled=lambda _i: False,
                visible=lambda _i: (self._has_hidhide
                                    and not self._controllers())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Unhide everything now", self._unhide_all,
                             visible=lambda _i: self._journal_count() > 0),
        ]
        return pystray.Menu(*items)

    def _title(self) -> str:
        """The tooltip, and the first row of the menu.

        An offline controller is counted apart from the bridged ones rather
        than among them. Both numbers are true -- a bridge whose Bluetooth link
        has dropped keeps its virtual device attached and feeds the game
        neutral input on purpose, which is what `running` counts -- but "2 of 2
        bridged" is not what a person wants to read about a controller they can
        see is switched off, and it is the line that made the program look
        wrong when it was merely being slow (`offline_grace`).
        """
        agg = self._snap.get("aggregate", {})
        if not self._snap.get("master_enabled", True):
            return "ds5bridge -- off"
        running, present = agg.get("running", 0), agg.get("present", 0)
        offline = agg.get("degraded", 0)
        if not present:
            return "ds5bridge -- no controller connected"
        head = f"ds5bridge -- {running - offline} of {present} bridged"
        if offline:
            head += f", {offline} offline"
        bits = [head]
        for c in self._controllers():
            bits.append("  " + describe(c))
        return "\n".join(bits)[:127]      # Win32 tooltips are capped at 128

    def _art(self) -> tuple:
        """(colour, battery, count) -- also the redraw key.

        The icon is a PNG encode on every change, and this is evaluated every
        couple of seconds forever, so the battery is bucketed to 10 % and the
        redraw only happens when this tuple actually moves.
        """
        agg = self._snap.get("aggregate", {})
        cs = [c for c in self._controllers() if c.get("enabled")]
        if not self._snap.get("master_enabled", True):
            return DISABLED_COLOR, None, 0
        if agg.get("errors"):
            state = S.ERROR
        elif agg.get("running"):
            state = (S.RUNNING if agg["running"] == len(cs) or agg["running"] > 1
                     else S.DEGRADED)
        elif any(c["state"] in (S.STARTING, S.STOPPING) for c in cs):
            state = S.STARTING
        else:
            state = S.STOPPED
        bats = [c["battery_percent"] for c in cs if c.get("battery_percent") is not None]
        return COLORS.get(state, (128, 128, 128)), (min(bats) if bats else None), \
            agg.get("running", 0)

    def _menu_key(self) -> tuple:
        """Everything the MENU shows, as one comparable value. In memory only.

        The redraw key for the menu, exactly as `_art()` is the redraw key for
        the icon, and bucketed for the same reason: the report rate moves every
        single tick, and rebuilding a native menu twice a second forever to
        keep "250/s" honest would be all cost and no benefit. Anything a person
        would actually notice -- a controller appearing or going away, a state,
        a checkbox, the battery, the count in the first row -- is in here.

        Deliberately absent: `A.is_enabled()` and `_journal_count()`, which are
        a registry read and a directory listing. Both are evaluated when the
        menu is rebuilt, and neither may be put on a two-second timer just to
        notice a change that only a click can cause -- and a click rebuilds the
        menu anyway, through pystray's own `_handler`.
        """
        return (self._title().splitlines()[0],
                # DESIRED values for everything a checkbox renders, so the
                # menu that pystray rebuilds right after a click (its own
                # `_handler` calls `update_menu`) is recognised here as the
                # current one rather than rebuilt again two seconds later.
                self._desired_master(),
                self._hide_all_checked(),
                # In-memory read; this is how the "Install update" row appears
                # when the daily background check lands mid-session.
                U.menu_visible(self.updater),
                tuple((c["serial"], (c.get("label") or ""), c.get("state"),
                       self._desired_enabled(c["serial"]),
                       bool(c.get("present")),
                       self._desired_hide(c["serial"]), bool(c.get("error")),
                       c.get("battery_percent"),
                       round((c.get("reports_per_s") or 0.0) / 10.0))
                      for c in self._controllers()))

    def _sync_menu(self) -> None:
        """Rebuild the native menu if what it renders has moved. Never mid-open.

        `update_menu()` destroys the HMENU it replaces (`pystray._win32`), and
        `TrackPopupMenuEx` is modal on the message-loop thread while this runs
        on the poll thread -- so a rebuild landing while the menu is on screen
        would pull it out from under the cursor, or worse. When that is the
        case the rebuild is remembered instead, and `_watch_menu_visibility`
        runs it the moment the menu closes.

        `_menu_key_built` is only advanced by a rebuild that actually happened,
        so a deferred one is still pending on the next tick if the menu is
        somehow still open.
        """
        if self.icon is None:
            return
        key = self._menu_key()
        if key == self._menu_key_built:
            return
        with self._lock:
            if self._menu_open:
                self._menu_dirty = True
                return
            self._menu_dirty = False
        try:
            self.icon.update_menu()
        except Exception:  # noqa: BLE001  (a stale menu is not worth a crash)
            return
        self._menu_key_built = key

    def _watch_menu_visibility(self) -> None:
        """Know when the menu is on screen, by wrapping pystray's own handler.

        There is no public way to ask. `_on_notify` is what displays the menu
        and it does not return until the user has dismissed it, so wrapping it
        brackets exactly the window in which a rebuild is unsafe -- and gives
        the natural moment to run one that was deferred.

        Reaching into `_message_handlers` is reaching into a private API, so it
        is done by identity rather than by hardcoding `WM_NOTIFY`, and every
        failure is survivable: without the wrapper the menu still refreshes,
        it just refreshes without knowing whether anyone is looking at it.
        """
        handlers = getattr(self.icon, "_message_handlers", None)
        inner = getattr(self.icon, "_on_notify", None)
        if not isinstance(handlers, dict) or inner is None:
            return
        codes = [code for code, fn in handlers.items() if fn == inner]
        if not codes:
            return

        def on_notify(wparam, lparam):
            with self._lock:
                self._menu_open = True
            try:
                return inner(wparam, lparam)
            finally:
                with self._lock:
                    self._menu_open = False
                    dirty = self._menu_dirty
                if dirty:
                    self._sync_menu()

        for code in codes:
            handlers[code] = on_notify

    def _refresh_now(self) -> None:
        if self.icon is None:
            return
        self._snap = self.mgr.snapshot()
        title = self._title()
        if title != getattr(self, "_last_title", None):
            self._last_title = title
            self.icon.title = title
        rgb, bat, count = self._art()
        key = (rgb, None if bat is None else bat // 10, count)
        if key != getattr(self, "_last_art", None):
            self._last_art = key
            self.icon.icon = _icon_image(rgb, bat, count)
        # The menu OBJECT is never reassigned -- every item in it is a lambda
        # over `self._snap`. What has to be asked for is the native rebuild
        # that re-evaluates those lambdas; see point 3 of the module docstring.
        self._sync_menu()

    def _controllers_menu(self):
        """The per-controller bridging rows -- the `Controllers >` submenu.

        These used to sit at the top level; a machine with two pads made the
        main menu mostly serials, and the switch a user actually reaches for
        nine times out of ten is the master one. The rows themselves are
        unchanged -- `describe()` text, checkbox is the enable switch -- and
        the explanatory row keeps the submenu non-empty (pystray hides a
        submenu item whose menu has no visible rows), so "Controllers" never
        blinks out of existence just because the last pad powered off.
        """
        import pystray

        items = []
        for i in range(MAX_SLOTS):
            visible, text, checked, action = self._slot(i)
            items.append(pystray.MenuItem(text, action, checked=checked,
                                          visible=visible,
                                          # Greyed out, not hidden, when the
                                          # master switch is off: the row still
                                          # says what it would do.
                                          enabled=lambda _i:
                                              self._desired_master()))
        items.append(pystray.MenuItem(
            "No controller connected", lambda *_: None,
            enabled=lambda _i: False,
            visible=lambda _i: not self._controllers()))
        return pystray.Menu(*items)

    def _menu(self):
        """Built once. Every dynamic value below is a lambda -- see the docstring.

        The shape is pairs: a checkable row that answers for ALL controllers,
        with the per-controller submenu directly under it. One item doing both
        jobs was the first design and pystray cannot express it -- see the
        module docstring -- so the checkbox and the submenu are adjacent rows
        instead.
        """
        import pystray

        all_visible, all_checked, all_action = self._hide_all_row()
        items = [pystray.MenuItem(lambda _i: self._title().splitlines()[0],
                                  self._copy_status, default=True),
                 pystray.Menu.SEPARATOR,
                 pystray.MenuItem("Open dashboard", self._open_dashboard),
                 pystray.Menu.SEPARATOR,
                 pystray.MenuItem(
                     "Bridging enabled", self._toggle_master,
                     checked=lambda _i: self._desired_master()),
                 pystray.MenuItem("Controllers", self._controllers_menu()),
                 pystray.Menu.SEPARATOR,
                 # The pair for hiding: the checkbox is the all-switch (checked
                 # only when EVERY pad is set to hide -- `_hide_all_checked`),
                 # the submenu is the per-pad boxes. The submenu stays visible
                 # on the wider rule: when HidHide is gone but the journal says
                 # we still owe a pad its visibility, the escape hatch in there
                 # is exactly what the user needs.
                 pystray.MenuItem("Hide Bluetooth pads while bridged",
                                  all_action, checked=all_checked,
                                  visible=all_visible),
                 pystray.MenuItem("Hide per controller",
                                  self._hide_menu(),
                                  visible=lambda _i: (self._has_hidhide
                                                      or self._journal_count()
                                                      > 0)),
                 pystray.Menu.SEPARATOR,
                 pystray.MenuItem("Rescan for controllers", self._rescan),
                 pystray.MenuItem("Start at login", self._toggle_autostart,
                                  checked=lambda _i: A.is_enabled(),
                                  visible=lambda _i: A.available()),
                 # Hidden until the background check finds a newer release; the
                 # action stages the download, hands off to the swap helper and
                 # quits so the helper can replace the locked exe. See update.py.
                 pystray.MenuItem(lambda _i: U.menu_text(self.updater),
                                  self._install_update,
                                  visible=lambda _i:
                                      U.menu_visible(self.updater)),
                 pystray.Menu.SEPARATOR,
                 pystray.MenuItem("Quit", self._quit)]
        return pystray.Menu(*items)

    def _poll(self) -> None:
        while not self._stop.wait(REFRESH_S):
            try:
                self._refresh_now()
            except Exception:  # noqa: BLE001
                pass

    def _announce_switches(self) -> None:
        """Say out loud when a switch, not a fault, is why nothing is bridging.

        Measured 2026-08-27 on this project's own dev launcher: the master
        switch was off in the settings, so `poll_once` returned at its first
        line, so no controller was ever started -- and the console printed
        NOTHING AT ALL. Every symptom of a switch being off is identical to the
        program being broken: no bridge, no virtual pad, no error. The tray icon
        does say "off", but a developer running the console form is reading the
        console, and an empty one is indistinguishable from a hang.

        A switch is a setting somebody chose; being quiet about a fault is a
        bug, and being quiet about a choice that stops the entire program from
        doing its job is the same bug wearing better clothes.
        """
        if not self.mgr.master_enabled:
            print("  bridging is OFF -- the \"Bridging enabled\" switch in the "
                  "tray menu is unticked,\n  so no controller will be bridged "
                  "until it is turned back on.", flush=True)
            return
        off = sorted(s for s in self.mgr.known() if not self.mgr.is_enabled(s))
        for serial in off:
            print(f"  {short(serial)} is switched off in the tray menu and will "
                  f"not be bridged.", flush=True)

    # -- startup and shutdown ----------------------------------------------

    def _install_teardown_hooks(self) -> None:
        """Ctrl+Break, SIGTERM, logoff and shutdown all reach `_teardown_all`,
        which stops every bridge -- but the `raise KeyboardInterrupt` that ends
        the CLI does not escape pystray's Win32 `GetMessage` loop, so without
        the second hook the tray tears down and then lives on as a process with
        dead bridges and a stale icon. Measured 2026-08-25: CTRL_BREAK detached
        the device and the process never exited.

        THE ORDER IS LOAD-BEARING, and it is not the order these were first
        written in. `_shutdown_icon` arms a three-second `os._exit(0)`
        watchdog, so everything that must actually finish has to be registered
        BEFORE it: `mgr.close()` stops every child, detaches every virtual
        device and unhides every cloaked pad, which on a two-controller machine
        is comfortably more than three seconds of work. The other way round, a
        Ctrl+C exits the process mid-detach and leaves behind precisely the
        zombie device and the invisible controller this path exists to prevent.

        `_cancel_pending` goes first for the same reason `_quit` cancels
        rather than flushes: a debounce timer that fires mid-close would be
        applying toggles to a manager that is tearing the bridges down under
        it, and the one thing it could add is a cloak for `close()` to miss.
        """
        S.ON_TEARDOWN.append(self._cancel_pending)
        S.ON_TEARDOWN.append(self.mgr.close)
        S.ON_TEARDOWN.append(self._shutdown_icon)

    def _startup_reconcile(self) -> None:
        """Clear what a previous run's death left on the machine.

        An armed auto-re-attach and any virtual controller still attached with
        no server behind it -- neither of which is visible in this program's own
        memory, because both live in the usbip driver and outlive every process
        on this side. Synchronous and before the hotplug watcher, deliberately:
        it is two fast usbip calls, and a pass racing it would be starting
        bridges into a machine still holding the last run's wreckage.

        HidHide's half of the same job has already run, from
        `install_crash_handlers()` -> `hidhide_sweep_once()`.
        """
        try:
            self.mgr.reconcile_stale(log_fn=lambda t: print("  " + t, flush=True))
        except Exception:  # noqa: BLE001  (housekeeping must not stop the tray)
            log.exception("the startup reconciliation failed")

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        import pystray

        S.install_crash_handlers()
        self._install_teardown_hooks()

        self.icon = pystray.Icon("ds5bridge", _icon_image(COLORS[S.STOPPED], None),
                                 "ds5bridge -- starting", menu=self._menu())
        self._watch_menu_visibility()
        threading.Thread(target=self._poll, name="tray-poll", daemon=True).start()

        self._startup_reconcile()

        # This is the answer to "do I have to do anything after setup?".
        # Nothing is clicked: the watcher bridges every enabled controller that
        # is already on, and every one that appears later.
        self._announce_switches()
        if not getattr(self.args, "no_hotplug", False):
            self.mgr.start_hotplug()
        else:
            self._work("startall", self.mgr.start_all)

        try:
            self.icon.run()
        finally:
            self._stop.set()
            self._cancel_pending()
            self.mgr.close()
        return 0


def run_tray(args) -> int:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("The tray needs pystray and Pillow:\n"
              "    python -m pip install pystray pillow\n"
              "Or use `ds5bridge` (no tray) instead.", file=sys.stderr)
        return 3
    return TrayApp(args).run()
