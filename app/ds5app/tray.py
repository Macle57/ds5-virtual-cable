"""System-tray front end. A thin layer over `BridgeService` -- nothing else.

Every behaviour here (which controller, when to clean up, what the teardown
order is) belongs to `service.py` and is shared byte for byte with the CLI. The
tray adds exactly three things: an icon whose colour is the state, a menu with
Start / Stop / Quit, and a balloon notification when something needs attention.

Why pystray and not tkinter: a tray icon is what a background utility should
be, a tkinter window is one more thing to minimise, and pystray's Windows
backend already exposes native balloon notifications through `icon.notify()` --
so there is no third notification dependency (win10toast is unmaintained; plyer
pulls in a stack for one call). Pillow draws the icon in memory, so no image
file has to ship or be found at runtime, which also keeps the frozen exe a
single file.

    ds5bridge tray [--autostart] [--serial BDADDR]
"""

from __future__ import annotations

import os
import sys
import threading
import time

from . import _bootstrap  # noqa: F401  (sys.path side effect)
from . import controller as C
from . import service as S
from .usbip import UsbipNotFound

REFRESH_S = 2.0

# Grey stopped, green running, amber degraded (controller off), red error.
COLORS = {
    S.STOPPED:  (110, 110, 118),
    S.STARTING: (70, 130, 200),
    S.RUNNING:  (60, 170, 90),
    S.DEGRADED: (215, 155, 40),
    S.STOPPING: (110, 110, 118),
    S.ERROR:    (200, 60, 60),
}


def _icon_image(rgb, battery: int | None, size: int = 64):
    """A DualSense-ish silhouette in the state colour, with a battery bar.

    Drawn rather than loaded: a bundled .ico is one more thing for a one-file
    exe to unpack and find, and at 64 px the shape only has to read as "a
    gamepad" at 16 px.
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
    if battery is not None:
        w = max(2, int(48 * s * min(100, max(0, battery)) / 100))
        low = battery <= C.BATTERY_WARN_PERCENT
        d.rectangle([8 * s, 52 * s, 56 * s, 58 * s], fill=(0, 0, 0, 90))
        d.rectangle([8 * s, 52 * s, 8 * s + w, 58 * s],
                    fill=(215, 60, 60, 255) if low else (60, 190, 100, 255))
    return img


class TrayApp:
    def __init__(self, args):
        self.args = args
        self.svc = S.BridgeService(
            serial=getattr(args, "serial", None),
            port=args.port,
            usbip_exe=getattr(args, "usbip", None),
            audio_target=getattr(args, "audio_target", "speaker"),
            auto_cleanup=not getattr(args, "no_auto_cleanup", False),
            on_event=self._on_event)
        self.icon = None
        self._stop = threading.Event()
        self._last_title = ""
        self._last_battery: int | None = None
        self._busy = threading.Lock()

    # -- events from the service ------------------------------------------

    def _on_event(self, kind: str, text: str) -> None:
        print(f"  {kind}: {text}", flush=True)
        if kind in ("warn", "error") and self.icon is not None:
            self._notify("ds5bridge", text)

    def _notify(self, title: str, text: str) -> None:
        try:
            self.icon.notify(text[:250], title)
        except Exception:  # noqa: BLE001  (no notification support is not fatal)
            pass

    # -- menu actions ------------------------------------------------------

    def _start(self, *_):
        if not self._busy.acquire(blocking=False):
            return
        def work():
            try:
                self.svc.start()
                self._notify("ds5bridge", "Virtual wired DualSense attached.")
            except C.AmbiguousControllerError as e:
                self._notify("Pick a controller",
                             "More than one DualSense is connected. Start it from "
                             "a terminal with --serial " + e.candidates[0].serial)
            except UsbipNotFound:
                self._notify("usbip-win2 is not installed",
                             "Install USBip 0.9.7.7, then try again. See the "
                             "user guide.")
            except Exception as e:  # noqa: BLE001
                self._notify("Could not start", str(e))
            finally:
                self._busy.release()
                self._refresh_now()
        threading.Thread(target=work, name="tray-start", daemon=True).start()

    def _stop_bridge(self, *_):
        if not self._busy.acquire(blocking=False):
            return
        def work():
            try:
                self.svc.stop()
            finally:
                self._busy.release()
                self._refresh_now()
        threading.Thread(target=work, name="tray-stop", daemon=True).start()

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
            self.svc.stop()
        finally:
            self._shutdown_icon()

    def _copy_status(self, *_):
        # No clipboard dependency: printing to the console the tray was
        # launched from is enough, and a hidden console is the user's choice.
        print("  " + self.svc.status_line(), flush=True)

    # -- rendering ---------------------------------------------------------

    def _title(self, snap: dict) -> str:
        if snap["state"] == S.STOPPED:
            return "ds5bridge -- stopped"
        bits = [f"ds5bridge -- {snap['state']}"]
        if snap["serial"]:
            bits.append(snap["serial"])
        if snap["battery_percent"] is not None:
            bits.append(f"battery {snap['battery_percent']}%")
        if snap["state"] in (S.RUNNING, S.DEGRADED):
            bits.append(f"{snap['reports_per_s']:.0f} reports/s")
            bits.append(f"up {int(snap['uptime_s']) // 60}m")
        return "\n".join(bits)

    def _refresh_now(self) -> None:
        if self.icon is None:
            return
        snap = self.svc.snapshot()
        title = self._title(snap)
        if title != self._last_title:
            self._last_title = title
            self.icon.title = title
        bat = snap["battery_percent"]
        # Redraw only when the state or the battery bucket changes: the icon is
        # a PNG encode and this runs every couple of seconds forever.
        bucket = None if bat is None else bat // 10
        if (snap["state"], bucket) != getattr(self, "_last_art", None):
            self._last_art = (snap["state"], bucket)
            self.icon.icon = _icon_image(COLORS.get(snap["state"], (128, 128, 128)), bat)
        # The menu only changes shape when Start has to become Stop. Reassigning
        # it every two seconds because the uptime ticked would rebuild it under
        # the cursor of somebody who has it open; the live numbers are in the
        # hover title, which is free to change.
        running = snap["state"] not in (S.STOPPED, S.ERROR)
        if running != getattr(self, "_last_menu_running", None):
            self._last_menu_running = running
            self.icon.menu = self._menu(snap)

    def _menu(self, snap: dict):
        import pystray

        running = snap["state"] not in (S.STOPPED, S.ERROR)
        lines = [pystray.MenuItem("ds5bridge", self._copy_status,
                                  default=True, enabled=True),
                 pystray.Menu.SEPARATOR]
        if running:
            lines.append(pystray.MenuItem("Stop bridging", self._stop_bridge))
        else:
            lines.append(pystray.MenuItem("Start bridging", self._start))
        lines += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("Quit", self._quit)]
        return pystray.Menu(*lines)

    def _poll(self) -> None:
        # Battery warnings arrive as service events; this loop only repaints.
        while not self._stop.wait(REFRESH_S):
            try:
                self._refresh_now()
            except Exception:  # noqa: BLE001
                pass

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        import pystray

        S.install_crash_handlers()
        self.icon = pystray.Icon(
            "ds5bridge", _icon_image(COLORS[S.STOPPED], None),
            "ds5bridge -- stopped", menu=self._menu(self.svc.snapshot()))
        # Ctrl+Break, SIGTERM, logoff and shutdown all reach `_teardown_all`,
        # which stops the bridge correctly -- but the `raise KeyboardInterrupt`
        # that ends the CLI does not escape pystray's Win32 GetMessage loop, so
        # without this the tray tears down and then lives on as a process with a
        # dead bridge and a stale icon. Measured 2026-08-25: CTRL_BREAK detached
        # the device and the process never exited.
        S.ON_TEARDOWN.append(self._shutdown_icon)
        threading.Thread(target=self._poll, name="tray-poll", daemon=True).start()
        if getattr(self.args, "autostart", False):
            self._start()
        try:
            self.icon.run()
        finally:
            self._stop.set()
            self.svc.stop()
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
