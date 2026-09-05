"""OS-side actions the chord engine fires -- stdlib only, ctypes `SendInput`.

`intercept.py` decides *when* (a chord, a gesture, a remote-mode button); this
module is *what*: synthetic keyboard and mouse input, monitor brightness, and
the small registry that maps the action names a user writes in `config.json`
onto callables.

Design constraints, in order:

1. **Stdlib only.** CI runs the app suite on a bare Python; a pip dependency
   here would be the first one the app layer ever took. Everything is ctypes
   (`user32.SendInput`) plus one PowerShell call for brightness -- WMI has no
   stdlib binding, and shelling out is the documented fallback.
2. **One injectable seam.** Every synthetic event funnels through the `inject`
   callable given to `OsActions` -- a list of `("key", vk, down)` /
   `("move", dx, dy)` / `("button", which, down)` / `("wheel", delta)` /
   `("hwheel", delta)` tuples per call. The default sends them as ONE
   `SendInput` array, which matters: Win+D injected as two separate calls can
   interleave with the user's real typing; one array is atomic. Tests replace
   `inject` with a recorder and never touch the desktop.
3. **Hold semantics are explicit.** Alt-Tab is not a tap: the switcher stays
   open exactly as long as Alt is down, so the gesture engine needs
   `alt_tab_start` / `alt_tab_step` / `alt_tab_commit` / `alt_tab_cancel` as
   separate calls with the Alt key held across them. A stuck Alt key is the
   failure mode, which is why commit/cancel are idempotent and `close()`
   releases anything still held.

Nothing here knows about controllers, reports or Bluetooth.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
from dataclasses import dataclass

log = logging.getLogger("ds5app.actions")

# --- virtual-key codes (winuser.h) ------------------------------------------

VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_MENU = 0x12          # Alt
VK_ESCAPE = 0x1B
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_LWIN = 0x5B
VK_D, VK_M, VK_P = 0x44, 0x4D, 0x50
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

#: One notch of a physical mouse wheel, per winuser.h.
WHEEL_DELTA = 120

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# the default injector: ctypes SendInput
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_EXTENDEDKEY = 0x0001
    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
    _MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
    _MOUSEEVENTF_WHEEL = 0x0800
    _MOUSEEVENTF_HWHEEL = 0x1000
    _INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1

    #: Arrow keys, Home/End-cluster keys etc. are "extended" keys; without the
    #: flag some applications see the numpad variants instead.
    _EXTENDED_VKS = frozenset({VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_LWIN,
                               0x5C, 0x5D,              # RWin, the menu key
                               0x21, 0x22, 0x23, 0x24,  # PgUp PgDn End Home
                               0x2C, 0x2D, 0x2E,        # PrtSc Ins Del
                               0x90, 0x6F})             # NumLock, numpad /

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)))

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)))

    class _INPUTUNION(ctypes.Union):
        _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))

    class _INPUT(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))

    _BUTTON_FLAGS = {
        ("left", True): _MOUSEEVENTF_LEFTDOWN,
        ("left", False): _MOUSEEVENTF_LEFTUP,
        ("right", True): _MOUSEEVENTF_RIGHTDOWN,
        ("right", False): _MOUSEEVENTF_RIGHTUP,
        ("middle", True): _MOUSEEVENTF_MIDDLEDOWN,
        ("middle", False): _MOUSEEVENTF_MIDDLEUP,
    }

    def send_input_events(events: list) -> None:
        """The real thing: one `SendInput` call for the whole event list."""
        arr = (_INPUT * len(events))()
        for slot, ev in zip(arr, events):
            kind = ev[0]
            if kind == "key":
                _, vk, down = ev
                slot.type = _INPUT_KEYBOARD
                flags = 0 if down else _KEYEVENTF_KEYUP
                if vk in _EXTENDED_VKS:
                    flags |= _KEYEVENTF_EXTENDEDKEY
                slot.u.ki = _KEYBDINPUT(vk, 0, flags, 0, None)
            elif kind == "move":
                _, dx, dy = ev
                slot.type = _INPUT_MOUSE
                slot.u.mi = _MOUSEINPUT(int(dx), int(dy), 0,
                                        _MOUSEEVENTF_MOVE, 0, None)
            elif kind == "button":
                _, which, down = ev
                slot.type = _INPUT_MOUSE
                slot.u.mi = _MOUSEINPUT(0, 0, 0,
                                        _BUTTON_FLAGS[(which, down)], 0, None)
            elif kind == "wheel":
                slot.type = _INPUT_MOUSE
                slot.u.mi = _MOUSEINPUT(0, 0, int(ev[1]) & 0xFFFFFFFF,
                                        _MOUSEEVENTF_WHEEL, 0, None)
            elif kind == "hwheel":
                slot.type = _INPUT_MOUSE
                slot.u.mi = _MOUSEINPUT(0, 0, int(ev[1]) & 0xFFFFFFFF,
                                        _MOUSEEVENTF_HWHEEL, 0, None)
            else:  # pragma: no cover - programming error, not user input
                raise ValueError(f"unknown event {ev!r}")
        n = ctypes.windll.user32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))
        if n != len(arr):
            log.warning("SendInput delivered %d of %d events", n, len(arr))
else:  # pragma: no cover - the app targets Windows; keep imports safe elsewhere
    def send_input_events(events: list) -> None:
        log.debug("SendInput unavailable on %s; dropped %d events",
                  sys.platform, len(events))


def run_powershell(script: str, timeout: float = 10.0) -> bool:
    """Run one PowerShell command, windowless. True when it exited 0.

    Used only for monitor brightness: WMI (`WmiMonitorBrightnessMethods`) has
    no stdlib binding, and this is the documented fallback. It runs on the
    engine's dispatch thread, never on the Bluetooth reader -- a PowerShell
    cold start is hundreds of milliseconds.
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW)
        if r.returncode != 0:
            log.warning("powershell failed (%d): %s", r.returncode,
                        r.stderr.decode(errors="replace").strip()[:200])
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        log.warning("powershell did not run: %s", e)
        return False


_DETACHED_PROCESS = 0x00000008 if sys.platform == "win32" else 0
_CREATE_NEW_PROCESS_GROUP = 0x00000200 if sys.platform == "win32" else 0


def launch_command(cmd: str) -> bool:
    """Start a program the way the Run box would, and forget about it.

    Used only by user macros of the `run` kind. The child is detached (its
    own process group, no console inherited from a windowless tray) and its
    handles point at NUL, so a chatty command can neither block the engine's
    dispatch thread nor outlive it as a zombie. True when it started.
    """
    try:
        subprocess.Popen(cmd, shell=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("macro command did not start (%s): %s", cmd[:80], e)
        return False


#: One WMI round trip that clamps and applies a relative brightness change.
#: `{step}` is a signed integer. Laptops and WMI-capable externals only;
#: on a desktop with a dumb monitor it exits non-zero and the action is a no-op.
_BRIGHTNESS_PS = (
    "$m = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
    "-ErrorAction Stop | Select-Object -First 1; "
    "$b = [math]::Max(0, [math]::Min(100, [int]$m.CurrentBrightness + ({step}))); "
    "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods "
    "| Select-Object -First 1) | "
    "Invoke-CimMethod -MethodName WmiSetBrightness "
    "-Arguments @{{Timeout=0; Brightness=$b}} | Out-Null"
)


# ---------------------------------------------------------------------------
# key names: the vocabulary a user macro is written in
# ---------------------------------------------------------------------------

#: name -> VK, the way people write a shortcut ("ctrl", "shift", "f5",
#: "printscreen"); letters and digits are their own names. Insertion order
#: is meaningful: /api/actions serves the list in this order and the settings
#: page shows it as-is, so modifiers come first and the numpad last.
KEY_NAMES: dict[str, int] = {
    "ctrl": 0x11, "shift": VK_SHIFT, "alt": VK_MENU, "win": VK_LWIN,
    "enter": VK_RETURN, "esc": VK_ESCAPE, "tab": VK_TAB, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": VK_UP, "down": VK_DOWN, "left": VK_LEFT, "right": VK_RIGHT,
    "printscreen": 0x2C, "pause": 0x13, "capslock": 0x14, "numlock": 0x90,
    "scrolllock": 0x91, "menu": 0x5D,
    "volume_up": VK_VOLUME_UP, "volume_down": VK_VOLUME_DOWN,
    "volume_mute": VK_VOLUME_MUTE, "media_play_pause": VK_MEDIA_PLAY_PAUSE,
    "media_next": VK_MEDIA_NEXT_TRACK, "media_prev": VK_MEDIA_PREV_TRACK,
    "media_stop": 0xB2,
    "minus": 0xBD, "equals": 0xBB, "comma": 0xBC, "period": 0xBE,
    "slash": 0xBF, "backslash": 0xDC, "semicolon": 0xBA, "quote": 0xDE,
    "lbracket": 0xDB, "rbracket": 0xDD, "grave": 0xC0,
}
KEY_NAMES.update({f"f{n}": 0x70 + n - 1 for n in range(1, 25)})
KEY_NAMES.update({c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"})
KEY_NAMES.update({d: ord(d) for d in "0123456789"})
KEY_NAMES.update({f"numpad{d}": 0x60 + d for d in range(10)})

#: Spellings a hand-written config is likely to use.
_KEY_ALIASES = {
    "control": "ctrl", "escape": "esc", "return": "enter", "windows": "win",
    "super": "win", "meta": "win", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "pgdown": "pagedown",
    "bksp": "backspace", "prtsc": "printscreen", "print": "printscreen",
    "spacebar": "space", "apps": "menu", "option": "alt",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left",
    "arrowright": "right",
}


def key_vk(name: object) -> int | None:
    """A key name (any case, spaces/dashes tolerated) -> VK, or None."""
    if not isinstance(name, str):
        return None
    n = name.strip().lower().replace(" ", "").replace("-", "_")
    n = _KEY_ALIASES.get(n, n)
    return KEY_NAMES.get(n)


#: A macro's name is a config identifier: it is what a chord binds to.
MACRO_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


# ---------------------------------------------------------------------------
# the actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """One nameable action: what to call, and whether holding repeats it."""

    name: str
    run: object                 # callable(params: dict) -> None
    repeatable: bool = False
    doc: str = ""


class OsActions:
    """Every OS action, over two injectable seams (`inject`, `run_ps`).

    Thread expectations: `mouse_*`/`wheel`/`key` are called straight from the
    engine on the Bluetooth reader thread -- `SendInput` is microseconds, that
    is fine. Registry actions are dispatched through the engine's worker
    thread because one of them (brightness) shells out.
    """

    def __init__(self, inject=None, run_ps=None, launch=None):
        self.inject = inject or send_input_events
        self.run_ps = run_ps or run_powershell
        self.launch = launch or launch_command
        self._lock = threading.Lock()
        self._alt_held = False
        self._shift_held = False

    # -- primitives (also used directly by remote mode) --------------------

    def tap(self, *vks: int) -> None:
        """Press the keys in order, release in reverse, in ONE SendInput."""
        events = [("key", vk, True) for vk in vks]
        events += [("key", vk, False) for vk in reversed(vks)]
        self.inject(events)

    def key(self, vk: int, down: bool) -> None:
        self.inject([("key", vk, down)])

    def mouse_move(self, dx: int, dy: int) -> None:
        if dx or dy:
            self.inject([("move", dx, dy)])

    def mouse_button(self, which: str, down: bool) -> None:
        self.inject([("button", which, down)])

    def wheel(self, delta: int, horizontal: bool = False) -> None:
        if delta:
            self.inject([("hwheel" if horizontal else "wheel", delta)])

    # -- Alt-Tab hold -------------------------------------------------------

    def alt_tab_start(self) -> None:
        """Open the switcher and keep it open: Alt goes DOWN and stays down."""
        with self._lock:
            if self._alt_held:
                return
            self._alt_held = True
        self.inject([("key", VK_MENU, True), ("key", VK_TAB, True),
                     ("key", VK_TAB, False)])

    def alt_tab_step(self, forward: bool = True) -> None:
        """One step through the open switcher. Backward = Shift+Tab."""
        with self._lock:
            if not self._alt_held:
                return
        if forward:
            self.inject([("key", VK_TAB, True), ("key", VK_TAB, False)])
        else:
            self.inject([("key", VK_SHIFT, True),
                         ("key", VK_TAB, True), ("key", VK_TAB, False),
                         ("key", VK_SHIFT, False)])

    def alt_tab_commit(self) -> None:
        """Release Alt: the highlighted window is activated. Idempotent."""
        with self._lock:
            if not self._alt_held:
                return
            self._alt_held = False
        self.inject([("key", VK_MENU, False)])

    def alt_tab_cancel(self) -> None:
        """Esc then Alt-up: close the switcher without switching. Idempotent."""
        with self._lock:
            if not self._alt_held:
                return
            self._alt_held = False
        self.inject([("key", VK_ESCAPE, True), ("key", VK_ESCAPE, False),
                     ("key", VK_MENU, False)])

    @property
    def alt_tab_open(self) -> bool:
        return self._alt_held

    def close(self) -> None:
        """Release anything still held -- a stuck Alt key outlives the bridge."""
        self.alt_tab_cancel()

    # -- registry actions ---------------------------------------------------

    def _volume(self, vk: int, params: dict) -> None:
        # Each VK tap moves the system volume by 2 %; `step` is in taps.
        for _ in range(max(1, int(params.get("step", 1)))):
            self.tap(vk)

    def _brightness(self, sign: int, params: dict) -> None:
        step = max(1, min(100, int(params.get("step", 10))))
        self.run_ps(_BRIGHTNESS_PS.format(step=sign * step))

    # -- user macros --------------------------------------------------------

    def macro_spec(self, name: str, definition: object) -> ActionSpec | None:
        """Compile one `input.macros` entry into an ActionSpec, or None.

        Two kinds, told apart by which key is present:

          {"keys": ["ctrl", "shift", "esc"]}   one chord: press in order,
                                               release in reverse, ONE
                                               SendInput (see `tap`)
          {"run": "notepad.exe"}               start a program, detached

        plus optional `label` (what the settings page shows) and `repeat`
        (True: fires again at `repeat_ms` while the chord is held -- only
        sensible for key macros). Anything malformed is logged and skipped;
        a bad macro must never take the engine down, only itself.
        """
        if not isinstance(name, str) or not MACRO_NAME_RE.match(name):
            log.warning("macro %r: bad name (a-z, 0-9, _; max 40)", name)
            return None
        if not isinstance(definition, dict):
            log.warning("macro %r: definition is %s, not an object",
                        name, type(definition).__name__)
            return None
        label = definition.get("label")
        doc = label.strip() if isinstance(label, str) and label.strip() else name
        repeat = bool(definition.get("repeat", False))
        keys = definition.get("keys")
        cmd = definition.get("run")
        if isinstance(keys, list) and keys:
            vks = []
            for k in keys:
                vk = key_vk(k)
                if vk is None:
                    log.warning("macro %r: unknown key %r -- skipped", name, k)
                    return None
                vks.append(vk)
            if len(vks) > 8:
                log.warning("macro %r: more than 8 keys -- skipped", name)
                return None
            return ActionSpec(name, lambda p, vks=tuple(vks): self.tap(*vks),
                              repeat, doc)
        if isinstance(cmd, str) and cmd.strip():
            return ActionSpec(name, lambda p, c=cmd.strip(): self.launch(c),
                              False, doc)
        log.warning("macro %r: needs a non-empty 'keys' list or a 'run' "
                    "command -- skipped", name)
        return None

    def registry(self) -> dict:
        """Action name -> ActionSpec. The names are the config vocabulary.

        `pad_power_off` is absent on purpose: powering the pad off is the
        ENGINE's business (it owns the bridge callback), not an OS action.
        `alt_tab` is registered as a plain forward step so a user may bind it
        to a *button* chord too; the touchpad gesture drives the hold
        variants directly.
        """
        a = self
        return {s.name: s for s in (
            ActionSpec("volume_up",
                       lambda p: a._volume(VK_VOLUME_UP, p), True,
                       "system volume up (step taps of 2%)"),
            ActionSpec("volume_down",
                       lambda p: a._volume(VK_VOLUME_DOWN, p), True,
                       "system volume down"),
            ActionSpec("volume_mute",
                       lambda p: a.tap(VK_VOLUME_MUTE), False,
                       "toggle system mute"),
            ActionSpec("media_play_pause",
                       lambda p: a.tap(VK_MEDIA_PLAY_PAUSE), False,
                       "play/pause the active media session"),
            ActionSpec("media_next",
                       lambda p: a.tap(VK_MEDIA_NEXT_TRACK), False,
                       "next track"),
            ActionSpec("media_prev",
                       lambda p: a.tap(VK_MEDIA_PREV_TRACK), False,
                       "previous track"),
            ActionSpec("brightness_up",
                       lambda p: a._brightness(+1, p), True,
                       "monitor brightness up (WMI via PowerShell)"),
            ActionSpec("brightness_down",
                       lambda p: a._brightness(-1, p), True,
                       "monitor brightness down"),
            ActionSpec("projection_cycle",
                       lambda p: a.tap(VK_LWIN, VK_P), False,
                       "Win+P projection chooser"),
            ActionSpec("show_desktop",
                       lambda p: a.tap(VK_LWIN, VK_D), False,
                       "Win+D show desktop (toggles back)"),
            ActionSpec("minimize_all",
                       lambda p: a.tap(VK_LWIN, VK_M), False,
                       "Win+M minimize all windows"),
            ActionSpec("task_view",
                       lambda p: a.tap(VK_LWIN, VK_TAB), False,
                       "Win+Tab Task View"),
            ActionSpec("alt_tab",
                       lambda p: a.tap(VK_MENU, VK_TAB), False,
                       "one Alt+Tab step (the gesture uses hold semantics)"),
        )}
