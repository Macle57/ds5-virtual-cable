"""A Steam-style on-screen keyboard, driven from the pad, typed with SendInput.

What it is
----------
The Steam Deck / Big Picture keyboard, as a Windows desktop window: five
rows of dark keys with one white highlight, the left stick or dpad moves the
highlight, Cross presses it, and the face buttons carry the shortcuts Steam
put on them (Square = Backspace, Triangle = Space, L2 = Shift, L3 = Caps,
R2 = Enter, L1/R1 = cursor left/right). It is toggled by the `keyboard`
engine action -- bindable to any chord or gesture -- and closed from the pad
with Circle or Options. While it is open the game sees a neutral pad, exactly
as in remote mode, so a text box can be filled in without the character
behind it wandering off.

Three parts, deliberately separated:

  `KeyboardModel`   the layout, the highlight position, Shift/Caps state and
                    what each key TYPES. Pure, and where the unit tests live.
  `PadDriver`       pad frames -> model operations, with auto-repeat and the
                    button map above. Pure as well (an injected clock).
  `Win32Renderer`   the window. Raw ctypes over user32/gdi32 on its own
                    thread with its own message loop; it receives immutable
                    snapshots and paints them. tkinter is excluded from the
                    packaged build on purpose (app/packaging/ds5bridge.spec)
                    and a Tk window can steal focus; this one cannot.

Why the window can never steal focus
------------------------------------
WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW, shown with
SW_SHOWNOACTIVATE, positioned with SWP_NOACTIVATE, answering
WM_MOUSEACTIVATE with MA_NOACTIVATE. The characters go to whatever window
HAD the focus, via `SendInput` with KEYEVENTF_UNICODE (layout-independent) --
so the text lands in the game's chat box, the browser's address bar, the
Run dialog: wherever the caret is.

Typing is `actions.OsActions.inject` -- the same seam every chord uses, so a
test sees `("unicode", 0x61, True)` and never a real keystroke.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

from . import actions as ACT

log = logging.getLogger("ds5app.osk")

# ---------------------------------------------------------------------------
# the layout
# ---------------------------------------------------------------------------

#: Pad glyphs drawn in a key's corner: the button that is a shortcut for it.
GLYPH_SQUARE, GLYPH_TRIANGLE = "□", "△"       # □ △
GLYPH_L2, GLYPH_L3, GLYPH_R2 = "L2", "L3", "R2"


@dataclass(frozen=True)
class Key:
    """One key on the layout.

    kind:  "char"   types `text` (or `shifted` with Shift/Caps)
           "keys"   taps the VK combination in `vks` (Backspace, Enter, Tab,
                    arrows, Win+. for the emoji panel, Ctrl+V)
           "shift"  latches Shift for the next character
           "caps"   toggles Caps Lock (the model's own, not the OS key)
           "move"   toggles move mode: the left stick / dpad drags the window
    """

    label: str
    kind: str = "char"
    text: str = ""
    shifted: str = ""
    vks: tuple = ()
    width: float = 1.0
    hint: str = ""

    def __post_init__(self):
        if self.kind == "char" and not self.text:
            object.__setattr__(self, "text", self.label)


def _chars(plain: str, shifted: str) -> list:
    return [Key(c, "char", c, s) for c, s in zip(plain, shifted)]


LAYOUT: tuple = (
    tuple(_chars("`1234567890-=", "~!@#$%^&*()_+")
          + [Key("Backspace", "keys", vks=(ACT.VK_BACK,), width=2.0,
                 hint=GLYPH_SQUARE)]),
    tuple([Key("Tab", "keys", vks=(ACT.VK_TAB,), width=1.5)]
          + _chars("qwertyuiop[]\\", "QWERTYUIOP{}|")),
    tuple([Key("Caps", "caps", width=1.75, hint=GLYPH_L3)]
          + _chars("asdfghjkl;'", 'ASDFGHJKL:"')
          + [Key("Enter", "keys", vks=(ACT.VK_RETURN,), width=2.25,
                 hint=GLYPH_R2)]),
    tuple([Key("Shift", "shift", width=2.25, hint=GLYPH_L2)]
          + _chars("zxcvbnm,./", "ZXCVBNM<>?")
          + [Key("Shift", "shift", width=2.25, hint=GLYPH_L2)]),
    (Key("☺", "keys", vks=(ACT.VK_LWIN, ACT.VK_OEM_PERIOD), width=1.5),
     Key("Space", "char", text=" ", shifted=" ", width=6.0,
         hint=GLYPH_TRIANGLE),
     Key("←", "keys", vks=(ACT.VK_LEFT,)),
     Key("↑", "keys", vks=(ACT.VK_UP,)),
     Key("↓", "keys", vks=(ACT.VK_DOWN,)),
     Key("→", "keys", vks=(ACT.VK_RIGHT,)),
     Key("Paste", "keys", vks=(ACT.VK_CONTROL, ACT.VK_V), width=1.5),
     Key("Move", "move", width=1.5)),
)

#: Where the highlight starts: the "g" key -- the middle of the home row.
HOME = (2, 5)

#: Geometry in key units. `UNIT` is one plain key's pitch in pixels.
UNIT = 52
GAP = 5
MARGIN = 10
ROW_UNITS = max(sum(k.width for k in row) for row in LAYOUT)


def _row_offsets(row) -> list:
    """Left edge of each key in units, cumulative widths."""
    out, x = [], 0.0
    for k in row:
        out.append(x)
        x += k.width
    return out


class KeyboardModel:
    """The keyboard's state, minus the pixels. Everything here is pure.

    Shift has two sources: the L2 trigger HELD (`shift_held`) and the Shift
    keys on the layout, which LATCH for one character the way a phone's do
    (`shift_latch`). Caps toggles from L3 or the Caps key and affects letters
    only -- Caps Lock on a real keyboard leaves "1" as "1", and so does this.
    """

    def __init__(self, layout=LAYOUT, home=HOME):
        self.layout = layout
        self.row, self.col = home
        self.shift_held = False
        self.shift_latch = False
        self.caps = False
        self.move_mode = False
        #: Bumped on every visible change; the renderer redraws when it moves.
        self.version = 0

    # -- state -------------------------------------------------------------

    @property
    def current(self) -> Key:
        return self.layout[self.row][self.col]

    @property
    def shift(self) -> bool:
        return self.shift_held or self.shift_latch

    def _bump(self) -> None:
        self.version += 1

    def set_shift_held(self, held: bool) -> None:
        if held != self.shift_held:
            self.shift_held = held
            self._bump()

    def toggle_caps(self) -> None:
        self.caps = not self.caps
        self._bump()

    def toggle_move_mode(self) -> None:
        self.move_mode = not self.move_mode
        self._bump()

    # -- navigation --------------------------------------------------------

    def _center(self, row: int, col: int) -> float:
        offs = _row_offsets(self.layout[row])
        return offs[col] + self.layout[row][col].width / 2.0

    def move(self, dx: int, dy: int) -> bool:
        """Move the highlight one key. -> did it move.

        Sideways clamps at the row's ends. Up/down lands on the key whose
        centre is nearest the current one's, so going up from "g" reaches
        "t"/"y" rather than "column 6 of a row with wider keys".
        """
        row, col = self.row, self.col
        if dy:
            new_row = max(0, min(len(self.layout) - 1, row + dy))
            if new_row != row:
                cx = self._center(row, col)
                offs = _row_offsets(self.layout[new_row])
                col = min(range(len(offs)),
                          key=lambda i: abs(offs[i] + self.layout[new_row][i].width
                                            / 2.0 - cx))
                row = new_row
        if dx:
            col = max(0, min(len(self.layout[row]) - 1, col + dx))
        if (row, col) == (self.row, self.col):
            return False
        self.row, self.col = row, col
        self._bump()
        return True

    # -- what a key does ---------------------------------------------------

    def key_text(self, key: Key) -> str:
        """The character a "char" key types right now."""
        if key.kind != "char":
            return ""
        if key.text.isalpha():
            upper = self.shift != self.caps          # XOR: Shift undoes Caps
        else:
            upper = self.shift
        return key.shifted if upper and key.shifted else key.text

    def key_label(self, key: Key) -> str:
        """What to DRAW on the key: the character it would type."""
        return self.key_text(key) if key.kind == "char" and key.text != " " \
            else key.label

    def activate(self, key: Key | None = None) -> list:
        """Press `key` (default: the highlighted one). -> emits.

        Emits are `("text", str)` or `("keys", (vk, ...))`; the state keys
        (Shift, Caps, Move) change the model and emit nothing. A typed
        character consumes the Shift latch.
        """
        key = key or self.current
        if key.kind == "char":
            text = self.key_text(key)
            if self.shift_latch:
                self.shift_latch = False
                self._bump()
            return [("text", text)]
        if key.kind == "keys":
            return [("keys", tuple(key.vks))]
        if key.kind == "shift":
            self.shift_latch = not self.shift_latch
            self._bump()
        elif key.kind == "caps":
            self.toggle_caps()
        elif key.kind == "move":
            self.toggle_move_mode()
        return []

    # -- geometry for the renderer ------------------------------------------

    def snapshot(self, unit: int = UNIT, gap: int = GAP,
                 margin: int = MARGIN) -> dict:
        """Everything the renderer paints, as plain tuples. Immutable by
        construction, so the paint thread never reads a half-updated model."""
        keys = []
        for r, row in enumerate(self.layout):
            offs = _row_offsets(row)
            for c, key in enumerate(row):
                on = ((key.kind == "shift" and self.shift)
                      or (key.kind == "caps" and self.caps)
                      or (key.kind == "move" and self.move_mode))
                keys.append((
                    int(margin + offs[c] * unit),
                    int(margin + r * unit),
                    int(key.width * unit - gap),
                    int(unit - gap),
                    self.key_label(key),
                    key.hint,
                    (r, c) == (self.row, self.col),
                    on,
                ))
        return {
            "version": self.version,
            "width": int(2 * margin + ROW_UNITS * unit - gap),
            "height": int(2 * margin + len(self.layout) * unit - gap),
            "keys": tuple(keys),
        }


# ---------------------------------------------------------------------------
# the pad side
# ---------------------------------------------------------------------------

#: Stick deflection (0..1 after the deadzone) that counts as a nudge.
NAV_STICK = 0.55
#: Trigger byte above which L2 is "held" (Shift) / R2 "pressed" (Enter).
TRIGGER_ON = 96
#: Auto-repeat for a held direction / Backspace / L1 / R1.
REPEAT_DELAY_S = 0.35
REPEAT_S = 0.08
#: Window drag: pixels per second at full right-stick deflection.
DRAG_PX_S = 900.0
#: Move mode with the dpad/left stick: pixels per nudge.
MOVE_STEP_PX = 24

_NAV_DPAD = {"dpad_up": (0, -1), "dpad_down": (0, 1),
             "dpad_left": (-1, 0), "dpad_right": (1, 0)}


def _norm(v: int, deadzone: int = 12) -> float:
    d = v - 0x80
    if abs(d) <= deadzone:
        return 0.0
    span = 127 - deadzone
    return max(-1.0, min(1.0, (d - deadzone * (1 if d > 0 else -1)) / span))


class PadDriver:
    """Frames in, model operations and emits out. One per keyboard.

    `frame(buttons, lx, ly, rx, ry, l2, r2, now)` returns a `Step`:
    `emits` to type, `close` when Circle/Options asked for it, `drag` in
    pixels for the window, `changed` when the picture needs repainting.
    """

    def __init__(self, model: KeyboardModel):
        self.model = model
        self._prev: set = set()
        self._armed = False
        self._nav: tuple | None = None
        self._nav_next = 0.0
        self._repeat: dict = {}        # button -> next repeat due
        self._last_at: float | None = None
        self._drag_frac = [0.0, 0.0]
        self._r2_down = False
        self._l2_down = False

    def reset(self) -> None:
        """Forget every held button. The next frame ARMS rather than acts:
        the button that opened the keyboard is usually still down on it, and
        must not type the highlighted key on the way in."""
        self._prev = set()
        self._armed = False
        self._nav = None
        self._repeat.clear()
        self._last_at = None
        self._r2_down = self._l2_down = False
        self.model.set_shift_held(False)

    def frame(self, buttons: set, lx: int, ly: int, rx: int, ry: int,
              l2: int, r2: int, now: float) -> "Step":
        m = self.model
        before = m.version
        emits: list = []
        close = False
        drag = (0, 0)
        if not self._armed:
            self._armed = True
            self._prev = set(buttons)
            self._l2_down = l2 >= TRIGGER_ON
            self._r2_down = r2 >= TRIGGER_ON
            self._last_at = now
            return Step([], False, (0, 0), False)
        new = buttons - self._prev
        dt = 0.0
        if self._last_at is not None:
            dt = min(0.05, max(0.0, now - self._last_at))
        self._last_at = now

        # -- Shift (L2 held), Enter (R2 edge) --------------------------------
        l2_down = l2 >= TRIGGER_ON
        if l2_down != self._l2_down:
            self._l2_down = l2_down
            m.set_shift_held(l2_down)
        r2_down = r2 >= TRIGGER_ON
        if r2_down and not self._r2_down:
            emits.append(("keys", (ACT.VK_RETURN,)))
        self._r2_down = r2_down

        # -- face buttons ------------------------------------------------------
        if "cross" in new:
            emits += m.activate()
        if "circle" in new or "options" in new:
            close = True
        if "triangle" in new:
            emits.append(("text", " "))
        if "l3" in new:
            m.toggle_caps()
        for btn, vks in (("square", (ACT.VK_BACK,)),
                         ("l1", (ACT.VK_LEFT,)), ("r1", (ACT.VK_RIGHT,))):
            if btn in new:
                emits.append(("keys", vks))
                self._repeat[btn] = now + REPEAT_DELAY_S
            elif btn in buttons and btn in self._repeat:
                if now >= self._repeat[btn]:
                    self._repeat[btn] = now + REPEAT_S
                    emits.append(("keys", vks))
            else:
                self._repeat.pop(btn, None)

        # -- navigation: dpad or left stick, with auto-repeat -----------------
        nav = None
        for name, d in _NAV_DPAD.items():
            if name in buttons:
                nav = d
                break
        if nav is None:
            sx, sy = _norm(lx), _norm(ly)
            if abs(sx) >= NAV_STICK or abs(sy) >= NAV_STICK:
                nav = ((1 if sx > 0 else -1) if abs(sx) >= abs(sy) else 0,
                       (1 if sy > 0 else -1) if abs(sy) > abs(sx) else 0)
        step = None
        if nav != self._nav:
            self._nav = nav
            if nav is not None:
                step = nav
                self._nav_next = now + REPEAT_DELAY_S
        elif nav is not None and now >= self._nav_next:
            self._nav_next = now + REPEAT_S
            step = nav
        if step is not None:
            if m.move_mode:
                drag = (step[0] * MOVE_STEP_PX, step[1] * MOVE_STEP_PX)
            else:
                m.move(step[0], step[1])

        # -- right stick drags the window --------------------------------------
        if dt > 0:
            fx, fy = self._drag_frac
            fx += _norm(rx) * DRAG_PX_S * dt
            fy += _norm(ry) * DRAG_PX_S * dt
            ix, iy = int(fx), int(fy)
            self._drag_frac = [fx - ix, fy - iy]
            if ix or iy:
                drag = (drag[0] + ix, drag[1] + iy)

        self._prev = set(buttons)
        return Step(emits, close, drag, m.version != before)


@dataclass(frozen=True)
class Step:
    emits: list
    close: bool
    drag: tuple
    changed: bool


# ---------------------------------------------------------------------------
# the keyboard object the engine holds
# ---------------------------------------------------------------------------


class OnScreenKeyboard:
    """Model + driver + renderer, behind the four calls the engine makes:
    `toggle()`, `on_frame()`, `close()` and `shutdown()`.

    `renderer_factory` builds the window object on first open (a
    `Win32Renderer` in production, a recorder in tests); it is created
    lazily so an engine that never opens the keyboard never starts a thread.
    """

    def __init__(self, actions: ACT.OsActions, renderer_factory=None):
        self.actions = actions
        self.model = KeyboardModel()
        self.driver = PadDriver(self.model)
        self._factory = renderer_factory or Win32Renderer
        self._renderer = None
        self.is_open = False
        #: Window position (top-left) once the user has moved it; None means
        #: "where the renderer puts it by default" (bottom centre).
        self.pos: tuple | None = None

    # Every method below flips state at once and returns a THUNK (or None)
    # for the window work, because the engine calls them under its lock and
    # nothing foreign runs there. The engine runs the thunks right after.

    def open(self):
        if self.is_open:
            return None
        self.driver.reset()
        self.model.shift_latch = False
        self.model.move_mode = False
        self.is_open = True
        log.info("on-screen keyboard open")
        snap = self._snapshot()

        def show():
            try:
                if self._renderer is None:
                    self._renderer = self._factory()
                self._renderer.show(snap)
            except Exception:  # noqa: BLE001
                log.exception("the on-screen keyboard window could not be shown")
        return show

    def close(self):
        if not self.is_open:
            return None
        self.is_open = False
        log.info("on-screen keyboard closed")

        def hide():
            r = self._renderer
            if r is not None:
                try:
                    r.hide()
                except Exception:  # noqa: BLE001
                    log.exception("hiding the on-screen keyboard failed")
        return hide

    def toggle(self):
        return self.close() if self.is_open else self.open()

    def shutdown(self) -> None:
        """End the renderer thread. The engine's `close()` -- not under the
        lock, so this one blocks until the window thread is gone."""
        self.is_open = False
        r, self._renderer = self._renderer, None
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                log.exception("closing the on-screen keyboard window failed")

    def on_frame(self, frame, now: float) -> list:
        """One pad report while open -> thunks to run outside the engine lock.

        `frame` is anything with `buttons`, `lx`, `ly`, `rx`, `ry`, `l2`,
        `r2` (the engine's `_Frame`). Typing goes through `actions.inject`;
        the renderer gets a fresh snapshot when the picture changed. All of
        it in the thunks.
        """
        if not self.is_open:
            return []
        step = self.driver.frame(frame.buttons, frame.lx, frame.ly, frame.rx,
                                 frame.ry, frame.l2, frame.r2, now)
        thunks: list = []
        for kind, payload in step.emits:
            if kind == "text":
                events = ACT.unicode_events(payload)
                if events:
                    thunks.append(lambda ev=events: self.actions.inject(ev))
            elif kind == "keys":
                thunks.append(lambda vks=payload: self.actions.tap(*vks))
        moved = False
        if step.drag != (0, 0):
            base = self.pos
            if base is None and self._renderer is not None:
                try:
                    base = self._renderer.position()
                except Exception:  # noqa: BLE001
                    base = None
            base = base or (0, 0)
            self.pos = (base[0] + step.drag[0], base[1] + step.drag[1])
            moved = True
        if step.close:
            fn = self.close()
            if fn is not None:
                thunks.append(fn)
        elif step.changed or moved:
            snap = self._snapshot()

            def update():
                r = self._renderer
                if r is not None:
                    try:
                        r.update(snap)
                    except Exception:  # noqa: BLE001
                        log.exception("updating the on-screen keyboard failed")
            thunks.append(update)
        return thunks

    def _snapshot(self) -> dict:
        snap = self.model.snapshot()
        snap["pos"] = self.pos
        return snap


# ---------------------------------------------------------------------------
# the window: raw Win32, own thread, own message loop
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _u32 = ctypes.windll.user32
    _g32 = ctypes.windll.gdi32
    _k32 = ctypes.windll.kernel32

    LRESULT = ctypes.c_ssize_t
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t
    HANDLE = ctypes.c_void_p
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HANDLE, ctypes.c_uint, WPARAM, LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = (("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", HANDLE), ("hIcon", HANDLE),
                    ("hCursor", HANDLE), ("hbrBackground", HANDLE),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR))

    class MSG(ctypes.Structure):
        _fields_ = (("hwnd", HANDLE), ("message", ctypes.c_uint),
                    ("wParam", WPARAM), ("lParam", LPARAM),
                    ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                    ("lPrivate", wintypes.DWORD))

    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = (("hdc", HANDLE), ("fErase", wintypes.BOOL),
                    ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                    ("fIncUpdate", wintypes.BOOL),
                    ("rgbReserved", ctypes.c_ubyte * 32))

    for fn, res, args in (
        (_u32.RegisterClassW, wintypes.ATOM, (ctypes.POINTER(WNDCLASSW),)),
        (_u32.UnregisterClassW, wintypes.BOOL, (wintypes.LPCWSTR, HANDLE)),
        (_u32.CreateWindowExW, HANDLE,
         (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          HANDLE, HANDLE, HANDLE, ctypes.c_void_p)),
        (_u32.DefWindowProcW, LRESULT, (HANDLE, ctypes.c_uint, WPARAM, LPARAM)),
        (_u32.GetMessageW, wintypes.BOOL,
         (ctypes.POINTER(MSG), HANDLE, ctypes.c_uint, ctypes.c_uint)),
        (_u32.TranslateMessage, wintypes.BOOL, (ctypes.POINTER(MSG),)),
        (_u32.DispatchMessageW, LRESULT, (ctypes.POINTER(MSG),)),
        (_u32.PostMessageW, wintypes.BOOL, (HANDLE, ctypes.c_uint, WPARAM, LPARAM)),
        (_u32.PostQuitMessage, None, (ctypes.c_int,)),
        (_u32.DestroyWindow, wintypes.BOOL, (HANDLE,)),
        (_u32.ShowWindow, wintypes.BOOL, (HANDLE, ctypes.c_int)),
        (_u32.SetWindowPos, wintypes.BOOL,
         (HANDLE, HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          ctypes.c_uint)),
        (_u32.SetLayeredWindowAttributes, wintypes.BOOL,
         (HANDLE, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD)),
        (_u32.InvalidateRect, wintypes.BOOL,
         (HANDLE, ctypes.POINTER(wintypes.RECT), wintypes.BOOL)),
        (_u32.BeginPaint, HANDLE, (HANDLE, ctypes.POINTER(PAINTSTRUCT))),
        (_u32.EndPaint, wintypes.BOOL, (HANDLE, ctypes.POINTER(PAINTSTRUCT))),
        (_u32.GetClientRect, wintypes.BOOL, (HANDLE, ctypes.POINTER(wintypes.RECT))),
        (_u32.GetWindowRect, wintypes.BOOL, (HANDLE, ctypes.POINTER(wintypes.RECT))),
        (_u32.FillRect, ctypes.c_int, (HANDLE, ctypes.POINTER(wintypes.RECT), HANDLE)),
        (_u32.DrawTextW, ctypes.c_int,
         (HANDLE, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT),
          ctypes.c_uint)),
        (_u32.GetSystemMetrics, ctypes.c_int, (ctypes.c_int,)),
        (_g32.CreateSolidBrush, HANDLE, (wintypes.COLORREF,)),
        (_g32.DeleteObject, wintypes.BOOL, (HANDLE,)),
        (_g32.SelectObject, HANDLE, (HANDLE, HANDLE)),
        (_g32.CreateCompatibleDC, HANDLE, (HANDLE,)),
        (_g32.CreateCompatibleBitmap, HANDLE, (HANDLE, ctypes.c_int, ctypes.c_int)),
        (_g32.BitBlt, wintypes.BOOL,
         (HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          HANDLE, ctypes.c_int, ctypes.c_int, wintypes.DWORD)),
        (_g32.DeleteDC, wintypes.BOOL, (HANDLE,)),
        (_g32.RoundRect, wintypes.BOOL,
         (HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          ctypes.c_int, ctypes.c_int)),
        (_g32.GetStockObject, HANDLE, (ctypes.c_int,)),
        (_g32.CreateFontW, HANDLE,
         (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
          wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
          wintypes.LPCWSTR)),
        (_g32.SetBkMode, ctypes.c_int, (HANDLE, ctypes.c_int)),
        (_g32.SetTextColor, wintypes.COLORREF, (HANDLE, wintypes.COLORREF)),
        (_k32.GetModuleHandleW, HANDLE, (wintypes.LPCWSTR,)),
    ):
        fn.restype = res
        fn.argtypes = args

    _WS_POPUP = 0x80000000
    _WS_EX_TOPMOST = 0x00000008
    _WS_EX_TOOLWINDOW = 0x00000080
    _WS_EX_NOACTIVATE = 0x08000000
    _WS_EX_LAYERED = 0x00080000
    _LWA_ALPHA = 0x2
    _SW_HIDE, _SW_SHOWNOACTIVATE = 0, 4
    _SWP_NOACTIVATE, _SWP_NOZORDER, _SWP_SHOWWINDOW = 0x0010, 0x0004, 0x0040
    _HWND_TOPMOST = ctypes.c_void_p(-1)
    _WM_DESTROY, _WM_CLOSE, _WM_PAINT = 0x0002, 0x0010, 0x000F
    _WM_ERASEBKGND, _WM_MOUSEACTIVATE = 0x0014, 0x0021
    _WM_APP = 0x8000
    _MSG_UPDATE, _MSG_SHOW, _MSG_HIDE = _WM_APP + 1, _WM_APP + 2, _WM_APP + 3
    _MA_NOACTIVATE = 3
    _NULL_PEN = 8
    _TRANSPARENT = 1
    _DT_CENTER, _DT_VCENTER, _DT_SINGLELINE = 0x1, 0x4, 0x20
    _DT_RIGHT, _DT_TOP = 0x2, 0x0
    _SRCCOPY = 0x00CC0020
    _SM_CXSCREEN, _SM_CYSCREEN = 0, 1
    _CLEARTYPE_QUALITY = 5
    _FW_SEMIBOLD = 600

    def _rgb(r, g, b) -> int:
        return r | (g << 8) | (b << 16)

    _C_BG = _rgb(28, 29, 34)
    _C_KEY = _rgb(58, 60, 68)
    _C_KEY_ON = _rgb(70, 100, 170)
    _C_HILITE = _rgb(245, 245, 247)
    _C_TEXT = _rgb(235, 235, 240)
    _C_TEXT_DARK = _rgb(20, 20, 24)
    _C_HINT = _rgb(150, 155, 170)
    _C_HINT_DARK = _rgb(90, 90, 100)
    _ALPHA = 236

    class Win32Renderer:
        """The keyboard window. `show`/`update`/`hide`/`close`/`position`
        are called from the engine's threads; everything Win32 happens on the
        renderer's own thread, reached by `PostMessageW`."""

        CLASS_NAME = "ds5bridge.osk"

        def __init__(self):
            self._lock = threading.Lock()
            self._snap: dict | None = None
            self._hwnd = None
            self._thread: threading.Thread | None = None
            self._ready = threading.Event()
            self._proc = None                       # keep the callback alive
            self._font = self._hint_font = None
            self._pos: tuple | None = None
            self._size = (0, 0)

        # -- the engine's side ------------------------------------------

        def show(self, snap: dict) -> None:
            with self._lock:
                self._snap = snap
            if self._thread is None or not self._thread.is_alive():
                # The thread shows the window itself from the snapshot just
                # stored; nobody waits for it (`show` is called from the
                # engine's thunks, which must not block on a window).
                self._ready.clear()
                self._thread = threading.Thread(target=self._run,
                                                name="ds5-osk", daemon=True)
                self._thread.start()
                return
            self._post(_MSG_SHOW)

        def update(self, snap: dict) -> None:
            with self._lock:
                self._snap = snap
            self._post(_MSG_UPDATE)

        def hide(self) -> None:
            self._post(_MSG_HIDE)

        def position(self):
            with self._lock:
                return self._pos

        def close(self) -> None:
            self._post(_WM_CLOSE)
            t = self._thread
            if t is not None:
                t.join(timeout=3.0)
            self._thread = None

        def _post(self, msg: int) -> None:
            hwnd = self._hwnd
            if hwnd:
                _u32.PostMessageW(hwnd, msg, 0, 0)

        # -- the window's thread ---------------------------------------

        def _run(self) -> None:
            hinst = _k32.GetModuleHandleW(None)
            self._proc = WNDPROC(self._wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = self.CLASS_NAME
            wc.hbrBackground = None
            atom = _u32.RegisterClassW(ctypes.byref(wc))
            if not atom and ctypes.get_last_error() not in (0, 1410):
                # 1410 = ERROR_CLASS_ALREADY_EXISTS: a previous renderer in
                # this process registered it; reuse is fine.
                log.warning("RegisterClassW failed: %d", ctypes.get_last_error())
            try:
                snap = self._snap or {"width": 100, "height": 50, "keys": (),
                                      "pos": None}
                x, y = self._place(snap)
                self._hwnd = _u32.CreateWindowExW(
                    _WS_EX_NOACTIVATE | _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW
                    | _WS_EX_LAYERED,
                    self.CLASS_NAME, "ds5bridge keyboard", _WS_POPUP,
                    x, y, snap["width"], snap["height"], None, None, hinst, None)
                if not self._hwnd:
                    log.error("CreateWindowExW failed: %d", ctypes.get_last_error())
                    return
                _u32.SetLayeredWindowAttributes(self._hwnd, 0, _ALPHA, _LWA_ALPHA)
                self._font = _g32.CreateFontW(
                    -int(UNIT * 0.40), 0, 0, 0, _FW_SEMIBOLD, 0, 0, 0, 1, 0, 0,
                    _CLEARTYPE_QUALITY, 0, "Segoe UI")
                self._hint_font = _g32.CreateFontW(
                    -int(UNIT * 0.24), 0, 0, 0, _FW_SEMIBOLD, 0, 0, 0, 1, 0, 0,
                    _CLEARTYPE_QUALITY, 0, "Segoe UI Symbol")
                self._apply_geometry(snap, show=True)
                self._ready.set()
                msg = MSG()
                while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    _u32.TranslateMessage(ctypes.byref(msg))
                    _u32.DispatchMessageW(ctypes.byref(msg))
            except Exception:  # noqa: BLE001
                log.exception("the keyboard window thread died")
            finally:
                for f in (self._font, self._hint_font):
                    if f:
                        _g32.DeleteObject(f)
                self._font = self._hint_font = None
                self._hwnd = None
                _u32.UnregisterClassW(self.CLASS_NAME, hinst)
                self._ready.set()

        def _place(self, snap: dict) -> tuple:
            """Where the window goes: the user's drag position, clamped to the
            primary screen, or bottom-centre with a 48 px lift."""
            sw, sh = _u32.GetSystemMetrics(_SM_CXSCREEN), _u32.GetSystemMetrics(_SM_CYSCREEN)
            w, h = snap["width"], snap["height"]
            pos = snap.get("pos")
            if pos is None:
                x, y = (sw - w) // 2, sh - h - 48
            else:
                x, y = int(pos[0]), int(pos[1])
            x = max(0, min(max(0, sw - w), x))
            y = max(0, min(max(0, sh - h), y))
            with self._lock:
                self._pos = (x, y)
            return x, y

        def _apply_geometry(self, snap: dict, show: bool) -> None:
            x, y = self._place(snap)
            flags = _SWP_NOACTIVATE | (_SWP_SHOWWINDOW if show else 0)
            _u32.SetWindowPos(self._hwnd, _HWND_TOPMOST, x, y,
                              snap["width"], snap["height"], flags)
            self._size = (snap["width"], snap["height"])
            _u32.InvalidateRect(self._hwnd, None, False)

        def _wndproc(self, hwnd, msg, wparam, lparam):
            try:
                if msg == _WM_PAINT:
                    self._paint(hwnd)
                    return 0
                if msg == _WM_ERASEBKGND:
                    return 1
                if msg == _WM_MOUSEACTIVATE:
                    return _MA_NOACTIVATE
                if msg == _MSG_UPDATE:
                    with self._lock:
                        snap = self._snap
                    if snap is not None:
                        self._apply_geometry(snap, show=False)
                    return 0
                if msg == _MSG_SHOW:
                    with self._lock:
                        snap = self._snap
                    if snap is not None:
                        self._apply_geometry(snap, show=True)
                    _u32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
                    return 0
                if msg == _MSG_HIDE:
                    _u32.ShowWindow(hwnd, _SW_HIDE)
                    return 0
                if msg == _WM_DESTROY:
                    _u32.PostQuitMessage(0)
                    return 0
            except Exception:  # noqa: BLE001
                log.exception("keyboard window message %#x failed", msg)
                return 0
            return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _paint(self, hwnd) -> None:
            with self._lock:
                snap = self._snap
            ps = PAINTSTRUCT()
            hdc = _u32.BeginPaint(hwnd, ctypes.byref(ps))
            try:
                rc = wintypes.RECT()
                _u32.GetClientRect(hwnd, ctypes.byref(rc))
                w, h = rc.right - rc.left, rc.bottom - rc.top
                # Double-buffer: draw everything into a memory bitmap, blit
                # once. Direct painting of ~70 rounded rectangles flickers.
                mem = _g32.CreateCompatibleDC(hdc)
                bmp = _g32.CreateCompatibleBitmap(hdc, max(1, w), max(1, h))
                old_bmp = _g32.SelectObject(mem, bmp)
                try:
                    self._draw(mem, rc, snap)
                    _g32.BitBlt(hdc, 0, 0, w, h, mem, 0, 0, _SRCCOPY)
                finally:
                    _g32.SelectObject(mem, old_bmp)
                    _g32.DeleteObject(bmp)
                    _g32.DeleteDC(mem)
            finally:
                _u32.EndPaint(hwnd, ctypes.byref(ps))

        def _draw(self, dc, rc, snap) -> None:
            bg = _g32.CreateSolidBrush(_C_BG)
            _u32.FillRect(dc, ctypes.byref(rc), bg)
            _g32.DeleteObject(bg)
            if not snap:
                return
            _g32.SetBkMode(dc, _TRANSPARENT)
            old_pen = _g32.SelectObject(dc, _g32.GetStockObject(_NULL_PEN))
            brushes = {c: _g32.CreateSolidBrush(c)
                       for c in (_C_KEY, _C_KEY_ON, _C_HILITE)}
            try:
                for (x, y, w, h, label, hint, hilite, on) in snap["keys"]:
                    color = _C_HILITE if hilite else (_C_KEY_ON if on else _C_KEY)
                    old_brush = _g32.SelectObject(dc, brushes[color])
                    _g32.RoundRect(dc, x, y, x + w, y + h, 10, 10)
                    _g32.SelectObject(dc, old_brush)
                    # label, centred
                    r = wintypes.RECT(x, y, x + w, y + h)
                    old_font = _g32.SelectObject(dc, self._font)
                    _g32.SetTextColor(dc, _C_TEXT_DARK if hilite else _C_TEXT)
                    _u32.DrawTextW(dc, label, -1, ctypes.byref(r),
                                   _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE)
                    if hint:
                        # the pad button that is a shortcut for this key
                        _g32.SelectObject(dc, self._hint_font)
                        _g32.SetTextColor(dc, _C_HINT_DARK if hilite else _C_HINT)
                        hr = wintypes.RECT(x, y + 3, x + w - 6, y + h)
                        _u32.DrawTextW(dc, hint, -1, ctypes.byref(hr),
                                       _DT_RIGHT | _DT_TOP | _DT_SINGLELINE)
                    _g32.SelectObject(dc, old_font)
            finally:
                _g32.SelectObject(dc, old_pen)
                for b in brushes.values():
                    _g32.DeleteObject(b)

else:  # pragma: no cover - the app targets Windows; keep imports safe elsewhere
    class Win32Renderer:
        """No window anywhere but Windows: the keyboard state still works,
        it just has nothing to draw on."""

        def show(self, snap: dict) -> None:
            log.debug("no on-screen keyboard window on %s", sys.platform)

        def update(self, snap: dict) -> None:
            pass

        def hide(self) -> None:
            pass

        def position(self):
            return None

        def close(self) -> None:
            pass
