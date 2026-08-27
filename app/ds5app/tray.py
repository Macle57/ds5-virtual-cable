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

**3. The menu must not move under the cursor.** The old version reassigned
`icon.menu` whenever Start had to become Stop, and had to guard against doing
it more often than that -- rebuilding a menu somebody has open is a genuinely
unpleasant bug. pystray evaluates `text`, `checked`, `visible` and `enabled` as
CALLABLES every time the menu is drawn, so the menu here is built exactly once,
in `_menu()`, and every dynamic thing in it is a lambda reading the latest
snapshot. Controllers come and go by flipping `visible` on a fixed set of
slots, which is why `MAX_SLOTS` exists.

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

REFRESH_S = 2.0

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
    def __init__(self, args):
        self.args = args
        self.icon = None
        self._stop = threading.Event()
        self._busy: dict[str, bool] = {}
        self._lock = threading.Lock()

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

    def _work(self, key: str, fn) -> None:
        """Run a menu action off the UI thread, once at a time per key.

        `start()` takes seconds -- it spawns a child, waits for a socket and
        runs `usbip attach` -- and pystray calls menu handlers on the thread
        pumping the Win32 message loop. Doing this inline freezes the menu and
        the icon; doing it without the guard lets an impatient double-click
        start two bridges for one controller, which the manager's own in-flight
        guard would then have to refuse.
        """
        with self._lock:
            if self._busy.get(key):
                return
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

    def _toggle_master(self, *_):
        want = not self.mgr.master_enabled
        self._work("master", lambda: self.mgr.set_master_enabled(want))

    def _toggle_controller(self, serial: str):
        def act(*_):
            want = not self.mgr.is_enabled(serial)
            self._work(serial, lambda: self.mgr.set_enabled(serial, want))
        return act

    def _toggle_hide(self, serial: str):
        def act(*_):
            want = not self.mgr.is_hiding(serial)

            def go():
                self.mgr.set_hide_bluetooth(serial, want)
                if not self._has_hidhide:
                    self._notify("Hide Bluetooth pad",
                                 "HidHide is not installed, so nothing is "
                                 "hidden. See the user guide.")
                elif want:
                    # HidHide gates IRP_MJ_CREATE, so an already-open handle is
                    # untouched: a game that is running right now keeps seeing
                    # the pad. Saying so here is the difference between a
                    # feature that looks broken and one that looks honest.
                    self._notify("Hide Bluetooth pad",
                                 f"{short(serial)} is hidden. Games started "
                                 f"from now on will not see the Bluetooth pad.")
                else:
                    self._notify("Hide Bluetooth pad",
                                 f"{short(serial)} is visible to everything "
                                 f"again.")
            self._work("hide-" + serial, go)
        return act

    def _unhide_all(self, *_):
        """The panic button. Stop everything first, then force the sweep.

        Stopping first is what makes the state coherent afterwards: every
        bridge's own `stop()` unhides its controller through the normal path, so
        the forced sweep only has to deal with what a crash left behind.
        """
        def go():
            from . import hidhide as HH

            self.mgr.stop_all()
            n = HH.sweep(force=True)
            left = HH.journal_count()
            if left:
                self._notify("ds5bridge",
                             f"{left} controller(s) could not be unhidden. Use "
                             f"HidHideClient.exe, or run `ds5bridge unhide`.")
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
            cs = self._controllers()
            return bool(cs[i].get("enabled")) if i < len(cs) else False

        def action(*_):
            cs = self._controllers()
            if i < len(cs):
                self._toggle_controller(cs[i]["serial"])()

        return visible, text, checked, action

    def _hide_slot(self, i: int):
        """One row of the hide submenu -- the same fixed-slot trick as `_slot`.

        A second bank of slots rather than turning each controller row into a
        submenu: the controller rows are click-to-toggle-bridging, which is the
        common case, and making that two clicks to gain a rarely used checkbox
        is a bad trade.
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
            cs = self._controllers()
            return bool(cs[i].get("hide_bluetooth")) if i < len(cs) else False

        def action(*_):
            cs = self._controllers()
            if i < len(cs):
                self._toggle_hide(cs[i]["serial"])()

        return visible, text, checked, action

    def _hide_menu(self):
        """The `Hide Bluetooth pad while bridged >` submenu. Built once.

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
        agg = self._snap.get("aggregate", {})
        if not self._snap.get("master_enabled", True):
            return "ds5bridge -- off"
        running, present = agg.get("running", 0), agg.get("present", 0)
        if not present:
            return "ds5bridge -- no controller connected"
        bits = [f"ds5bridge -- {running} of {present} bridged"]
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
        # The menu is NEVER reassigned. Everything in it is a callable that
        # pystray re-evaluates when the menu is opened, so there is nothing to
        # rebuild and nothing that can change under an open menu.

    def _menu(self):
        """Built once. Every dynamic value below is a lambda -- see the docstring."""
        import pystray

        items = [pystray.MenuItem(lambda _i: self._title().splitlines()[0],
                                  self._copy_status, default=True),
                 pystray.Menu.SEPARATOR,
                 pystray.MenuItem(
                     "Bridging enabled", self._toggle_master,
                     checked=lambda _i: self._snap.get("master_enabled", True)),
                 pystray.Menu.SEPARATOR]

        for i in range(MAX_SLOTS):
            visible, text, checked, action = self._slot(i)
            items.append(pystray.MenuItem(text, action, checked=checked,
                                          visible=visible,
                                          # Greyed out, not hidden, when the
                                          # master switch is off: the row still
                                          # says what it would do.
                                          enabled=lambda _i: self._snap.get(
                                              "master_enabled", True)))
        items += [
            pystray.MenuItem(
                lambda _i: ("No controller connected"
                            if not self._controllers() else ""),
                lambda *_: None, enabled=lambda _i: False,
                visible=lambda _i: not self._controllers()),
            pystray.Menu.SEPARATOR,
            # Visible when HidHide is installed, OR when we have something
            # hidden and it is not -- the second case is the escape hatch.
            pystray.MenuItem("Hide Bluetooth pad while bridged",
                             self._hide_menu(),
                             visible=lambda _i: (self._has_hidhide
                                                 or self._journal_count() > 0)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Rescan for controllers", self._rescan),
            pystray.MenuItem("Start at login", self._toggle_autostart,
                             checked=lambda _i: A.is_enabled(),
                             visible=lambda _i: A.available()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit)]
        return pystray.Menu(*items)

    def _poll(self) -> None:
        while not self._stop.wait(REFRESH_S):
            try:
                self._refresh_now()
            except Exception:  # noqa: BLE001
                pass

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        import pystray

        S.install_crash_handlers()
        # Ctrl+Break, SIGTERM, logoff and shutdown all reach `_teardown_all`,
        # which stops every bridge -- but the `raise KeyboardInterrupt` that
        # ends the CLI does not escape pystray's Win32 GetMessage loop, so
        # without this the tray tears down and then lives on as a process with
        # dead bridges and a stale icon. Measured 2026-08-25: CTRL_BREAK
        # detached the device and the process never exited.
        S.ON_TEARDOWN.append(self._shutdown_icon)
        S.ON_TEARDOWN.append(self.mgr.close)

        self.icon = pystray.Icon("ds5bridge", _icon_image(COLORS[S.STOPPED], None),
                                 "ds5bridge -- starting", menu=self._menu())
        threading.Thread(target=self._poll, name="tray-poll", daemon=True).start()

        # This is the answer to "do I have to do anything after setup?".
        # Nothing is clicked: the watcher bridges every enabled controller that
        # is already on, and every one that appears later.
        if not getattr(self.args, "no_hotplug", False):
            self.mgr.start_hotplug()
        else:
            self._work("startall", self.mgr.start_all)

        try:
            self.icon.run()
        finally:
            self._stop.set()
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
