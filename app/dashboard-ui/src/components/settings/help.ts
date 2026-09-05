/* Help text, written from config.py / intercept.py's docstrings. */
export const HELP: Record<string, string> = {
  enabled: "Master switch for the whole shortcut engine. Off, the pad is a plain bridged controller: no chords, no gestures, no remote mode, no idle off-timer, no battery flashes.",
  chord_button: "Hold this button to arm shortcuts: while it is held, other buttons fire their bound actions instead of reaching the game (sticks and triggers still pass -- your aim is never frozen). A quick plain tap is replayed to the game afterwards, so a normal PS press still opens the game's own menu.",
  double_press_ms: "Two presses of the chord button within this window toggle remote mode instead of replaying a tap to the game.",
  tap_replay_ms: "When the chord button is tapped on its own, the press is replayed to the game and held down for this long. The game gets a slightly late press; menus do not care.",
  repeat_ms: "While a chord stays held, repeatable actions (volume, brightness) fire again this often.",
  haptic_ack: "A short rumble pulse whenever a chord is accepted, so you know it landed without looking at the screen.",
  haptic_strength: "How strong the acknowledgement rumble is. 0 is silent, 100 is full motor.",
  stick_mouse_in_chord: "While the chord button is held, the left stick moves the mouse pointer -- a quick nudge-and-click without switching into remote mode.",
  off_timer_minutes: "Minutes without any pad activity before the pad powers itself off to save its battery (the same mechanism as the PS+Triangle chord). 0 disables the timer.",
  chords: "Fires while the chord button is held; the game sees none of these presses. Diagonal d-pad counts as both directions.",
  touch_slide_horizontal: "Slide two fingers left or right across the touchpad while the chord button is held. Bound to alt_tab, it opens the window switcher and steps through it as you slide; lift to switch.",
  touch_swipe_up: "Swipe two fingers up the touchpad while the chord button is held.",
  touch_swipe_down: "Swipe two fingers down the touchpad while the chord button is held.",
  remote_enabled: "Double-press the chord button to toggle remote mode: the game is handed a neutral pad while the pad drives the OS. Touchpad drags move the pointer (tap = click, two-finger tap = right click, two-finger horizontal slide = alt-tab), Cross clicks and holds to drag, the left stick moves the pointer, the right stick and triggers scroll, the d-pad is arrow keys, Circle is Esc, Options is Enter. The lightbar holds the colour below while it is on.",
  mouse_speed: "Pointer speed multiplier for touchpad drags and the left stick in remote mode.",
  scroll_speed: "Scroll speed multiplier for the right stick and the triggers in remote mode.",
  remote_lightbar: "Lightbar colour while remote mode is on -- the visible cue that input is going to the OS, not the game. The dashboard's REMOTE MODE badge wears the same colour.",
  same_bindings: "On, remote mode reuses the chord table above: the same button or gesture fires the same action, just without the chord button held. Off, remote mode gets its own table (below), so a button can mean one thing while chording in a game and another while driving the desktop.",
  remote_chords: "In remote mode nothing reaches the game, so there is no chord button to hold: a bound button or gesture fires its action directly, on its own. Buttons the remote map already uses for the pointer (Cross clicks, d-pad arrows, Circle is Esc, Options is Enter) are overridden by a binding here; leave a row unbound to keep the built-in remote behaviour.",
  dim_after_minutes: "Minutes of bridged play before the lightbar dims to save the pad's battery. 0 never dims (the default -- a lightbar going dark unasked reads as a fault). The game's colour is rewritten in flight, so the game's own idea of its lightbar stays untouched.",
  dim_level: "Brightness once dimmed: 0% is fully off, 100% is untouched. Applied as a multiplier on whatever colour the game asked for.",
  battery_enabled: "Flash the lightbar when the pad's battery runs low. The flash overlays the game's colour briefly; nothing the game set is lost.",
  low_percent: "Battery percentage at which the low-battery flashes begin.",
  critical_percent: "Battery percentage at which the more urgent critical flashes take over.",
  low_interval_s: "Seconds between low-battery flashes.",
  critical_interval_s: "Seconds between critical-battery flashes.",
  low_blinks: "How many blinks per low-battery flash.",
  critical_blinks: "How many blinks per critical-battery flash.",
};

/* Human names for the config's button/gesture vocabulary. */
export const KEY_LABELS: Record<string, string> = {
  cross: "Cross", circle: "Circle", square: "Square", triangle: "Triangle",
  dpad_up: "D-pad up", dpad_down: "D-pad down",
  dpad_left: "D-pad left", dpad_right: "D-pad right",
  l1: "L1", r1: "R1", l3: "L3 (left stick click)", r3: "R3 (right stick click)",
  create: "Create", options: "Options", ps: "PS",
  touchpad_click: "Touchpad click", mute: "Mute",
  touch_slide_horizontal: "Two-finger slide (left/right)",
  touch_swipe_up: "Two-finger swipe up",
  touch_swipe_down: "Two-finger swipe down",
};
export const keyLabel = (k: string) => KEY_LABELS[k] || k.replace(/_/g, " ");

/* Keys each level of the config the page owns; anything else (a newer
   build's settings) still renders generically inside the right card. */
export const GLOBAL_FIELDS: [string, string, "bool" | "number" | "string"][] = [
  ["enabled", "Bridging enabled (master switch)", "bool"],
  ["autostart_on_login", "Start at login", "bool"],
  ["auto_bridge_new", "Bridge new controllers automatically", "bool"],
  ["hide_bluetooth_default", "Hide new controllers' Bluetooth pad", "bool"],
  ["port_base", "First usbip TCP port", "number"],
  ["dashboard_port", "Dashboard port (this page)", "number"],
  ["hidhide_cli", "HidHideCLI.exe path (blank = auto)", "string"],
];
export const CTRL_FIELDS: [string, string, "bool" | "number" | "string" | "choice", string[]?][] = [
  ["enabled", "Bridge this controller", "bool"],
  ["label", "Label", "string"],
  ["audio_target", "Audio target", "choice", ["speaker", "headphone"]],
  ["hide_bluetooth", "Hide Bluetooth pad while bridged", "bool"],
  ["port", "usbip TCP port (blank = automatic)", "number"],
];
export const KNOWN_GLOBAL = new Set([...GLOBAL_FIELDS.map((f) => f[0]), "controllers", "input"]);
export const KNOWN_CTRL = new Set(CTRL_FIELDS.map((f) => f[0]));
export const KNOWN_INPUT = new Set(["enabled", "chord_button", "chords", "actions", "macros",
  "double_press_ms", "tap_replay_ms", "repeat_ms", "haptic_ack", "haptic_strength",
  "stick_mouse_in_chord", "off_timer_minutes", "battery", "lightbar", "remote"]);
export const KNOWN_REMOTE = new Set(["enabled", "mouse_speed", "scroll_speed", "lightbar_color", "same_bindings", "chords"]);
export const KNOWN_LIGHTBAR = new Set(["dim_after_minutes", "dim_level"]);
export const KNOWN_BATTERY = new Set(["enabled", "low_percent", "critical_percent",
  "low_interval_s", "critical_interval_s", "low_color", "critical_color", "low_blinks", "critical_blinks"]);

/* [r,g,b] <-> #rrggbb for <input type=color>. */
export const rgbToHex = (c: unknown) =>
  "#" + (Array.isArray(c) && c.length === 3 ? c : [0, 0, 0])
    .map((v) => Math.max(0, Math.min(255, Math.round(Number(v)) || 0)).toString(16).padStart(2, "0")).join("");
export const hexToRgb = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) || 0);
