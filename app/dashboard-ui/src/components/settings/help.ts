import type { GestureRow } from "../../lib/types";

/* Help text, written from config.py / intercept.py's docstrings. */
export const HELP: Record<string, string> = {
  /* bridging & tray (top-level keys) */
  master_enabled: "The tray's master switch. Off, every bridge stops and the pads go back to being plain Bluetooth controllers; on, enabled pads are bridged again as they are found.",
  auto_bridge_new: "A DualSense this app has never seen is bridged the moment it pairs. Off, a new pad waits until you enable it here or in the tray.",
  hide_bluetooth_default: "What a NEW controller's 'hide' switch starts as: hide its Bluetooth pad from games and Steam while it is bridged, so only the virtual wired pad is seen (no doubled controller). Existing pads keep their own switch below.",
  ctrl_enabled: "Bridge this controller: run a virtual wired pad for it whenever it is connected. Off, the pad stays a plain Bluetooth controller.",
  ctrl_hide: "Hide this controller's Bluetooth pad (HidHide) while it is bridged, so games and Steam see only the virtual wired one. The pill beside it says whether the hide is actually in effect right now.",
  autostart_on_login: "Start the tray with Windows. Installed as a service it starts before anyone signs in (the pads work on the lock screen too); installed as a scheduled task it starts at sign-in. The installer chooses which; this switch turns whichever is installed on or off.",
  update_check: "Look for a newer release on GitHub once a day and when the tray starts. Off, nothing is ever fetched.",
  update_auto_install: "Install a newer release on its own: a short while after one is found, the silent installer runs and the tray (or service) restarts itself, with a toast under the 'update' notification. Off, a new release is only announced and installed by hand -- the Install button here or the tray's 'Install update' row.",
  tray_actions: "The same commands as the tray menu. Rescan looks for pads again right now; Unhide all lifts every HidHide cloak (a rescue when a pad stays invisible after a crash); Open logs opens the log folder.",
  /* notifications */
  notifications_enabled: "Master switch for every toast this app shows. Off, nothing pops up -- including the battery toast shortcut.",
  n_battery_low: "Warn when a pad's battery runs low (the lightbar flashes too, see Battery).",
  n_remote_mode: "'Remote mode on / off (<pad>)' whenever a pad switches between driving the game and driving the OS.",
  n_keyboard: "'On-screen keyboard opened / closed' when the pad-driven keyboard comes and goes. Off by default: it is visible on screen anyway.",
  n_connection: "'Controller connected / bridged / disconnected' as pads come and go.",
  n_hide: "When a pad's Bluetooth device is hidden from other apps or made visible again.",
  n_update: "When a newer release is found (and, with auto-install on, when it is about to be installed).",
  show_battery: "The 'Battery toast' action (show_battery) is a shortcut you can bind to any chord or remote-mode row: pressing it shows '<pad>: NN % (discharging | charging | full)' for the pad that pressed it. It obeys only the master switch above. By default it sits on R3 while chording.",
  /* macros */
  macro_repeat: "Once per press fires the macro each time the chord lands. While held fires it again every interval for as long as the button stays down. Toggle starts repeating on one press and stops on the next (leaving remote mode or turning the engine off stops it too). Max runs caps a hold/toggle run; 0 is unlimited.",
  /* shortcut engine */
  enabled: "Master switch for the whole shortcut engine. Off, the pad is a plain bridged controller: no chords, no gestures, no remote mode, no idle off-timer, no battery flashes.",
  chord_button: "Hold this button to arm shortcuts: while it is held, other buttons fire their bound actions instead of reaching the game (sticks and triggers still pass -- your aim is never frozen). A quick plain tap is replayed to the game afterwards, so a normal PS press still opens the game's own menu.",
  double_press_ms: "Two presses of the chord button within this window toggle remote mode instead of replaying a tap to the game.",
  tap_replay_ms: "When the chord button is tapped on its own, the press is replayed to the game and held down for this long. The game gets a slightly late press; menus do not care.",
  repeat_ms: "While a chord stays held, repeatable actions (volume, brightness) fire again this often.",
  haptic_ack: "A short rumble pulse whenever a chord is accepted, so you know it landed without looking at the screen.",
  haptic_strength: "How strong the acknowledgement rumble is. 0 is silent, 100 is full motor.",
  stick_mouse_in_chord: "While the chord button is held, the left stick and a one-finger touchpad drag move the mouse pointer (a short one-finger tap clicks, the right stick scrolls) -- a quick nudge-and-click without switching into remote mode. The game sees the sticks centred for the hold.",
  scroll_sensitivity: "How far a two-finger slide scrolls, as a multiplier. 1× is the engine default (about one wheel notch per 100 touchpad points). Only the touchpad: the stick and trigger scroll have their own speed under Remote mode.",
  scroll_reverse: "Flip the scroll direction of a two-finger slide. Off, fingers moving up scroll the content up, the Windows touchpad default.",
  zoom_sensitivity: "How much a pinch zooms, as a multiplier, for both the touch-screen pinch and the Ctrl + wheel zoom.",
  zoom_reverse: "Flip the pinch: fingers moving apart zoom out instead of in.",
  off_timer_minutes: "Minutes without any pad activity before the pad powers itself off to save its battery (the same mechanism as the PS+Triangle chord). 0 disables the timer.",
  chords: "Fires while the chord button is held; the game sees none of these presses. Diagonal d-pad counts as both directions.",
  touch_tap_2f: "Two fingers, a short touch, no click. Right click by default in remote mode.",
  touch_click_2f: "A physical click of the touchpad while two fingers are resting on it. A two-finger contact that ends as a click never also counts as a tap.",
  touch_slide_up: "Slide two fingers up the touchpad without pressing it. Paired with slide down: pick Scroll up here and the down row scrolls down with it, the wheel following the fingers like a precision touchpad. Pick anything else and it fires once after a short swipe.",
  touch_slide_down: "Slide two fingers down the touchpad without pressing it. Paired with slide up (see that row).",
  touch_slide_left: "Slide two fingers left without pressing the pad. Paired with slide right: Scroll left / right scroll sideways as one gesture; Alt+Tab takes both rows and steps through the switcher as you slide, either way.",
  touch_slide_right: "Slide two fingers right without pressing the pad. Paired with slide left (see that row).",
  touch_pinch: "Two fingers moving apart or together, pad not pressed. Pinch zoom by default: a touch-injected pinch that zooms the page the way a precision touchpad does (the visual viewport, not the browser's zoom level). Sensitivity and direction are in the card below.",
  touch_slide_up_pressed: "Click the touchpad and, keeping it pressed, slide two fingers up. Task View by default. The click takes over at once: whatever the fingers were doing unpressed (a scroll, a pinch) ends, and this is measured from the click.",
  touch_slide_down_pressed: "Click the touchpad and, keeping it pressed, slide two fingers down. Minimize all by default.",
  touch_slide_left_pressed: "Click the touchpad and, keeping it pressed, slide two fingers left. Alt+Tab by default, which takes the right row too: the switcher opens and steps as you slide either way; lift to switch.",
  touch_slide_right_pressed: "Click the touchpad and, keeping it pressed, slide two fingers right. Paired with slide left while clicked.",
  touch_pinch_pressed: "Click the touchpad and, keeping it pressed, pinch or spread two fingers. Unbound by default; Ctrl + wheel zoom (the app's own zoom level) is the natural pick.",
  same_gestures: "On, remote mode reuses the gesture table above: the same touchpad gesture fires the same action, chord button or not. Off, remote mode gets its own gesture table (below), so a slide can scroll the desktop while it still alt-tabs in a game. Buttons have their own switch under Chords.",
  remote_enabled: "Double-press the chord button to toggle remote mode: the game is handed a neutral pad while the pad drives the OS. Touchpad drags move the pointer (tap = click, two-finger tap = right click, two-finger horizontal slide = alt-tab), Cross clicks and holds to drag, the left stick moves the pointer, the right stick and triggers scroll, the d-pad is arrow keys, Circle is Esc, Options is Enter. The lightbar holds the colour below while it is on.",
  mouse_speed: "Pointer speed multiplier for touchpad drags and the left stick in remote mode.",
  scroll_speed: "Scroll speed multiplier for the right stick and the triggers (remote mode, and the right stick while chording). The touchpad's two-finger scroll has its own sensitivity under Gestures.",
  remote_lightbar: "Lightbar colour while remote mode is on -- the visible cue that input is going to the OS, not the game. The dashboard's REMOTE MODE badge wears the same colour.",
  same_bindings: "On, remote mode reuses the chord table above: the same button or gesture fires the same action, just without the chord button held. Off, remote mode gets its own table (below), so a button can mean one thing while chording in a game and another while driving the desktop.",
  remote_chords: "In remote mode nothing reaches the game, so there is no chord button to hold: a bound button or gesture fires its action directly, on its own. The defaults spell out the classic remote map (Cross clicks, d-pad arrows, Circle is Esc, Options is Enter); a row set to none makes that button do nothing in remote mode. Only the touchpad and stick pointer controls are fixed.",
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
  touch_tap_2f: "Two-finger tap",
  touch_click_2f: "Two-finger click",
  touch_slide_up: "Two-finger slide up",
  touch_slide_down: "Two-finger slide down",
  touch_slide_left: "Two-finger slide left",
  touch_slide_right: "Two-finger slide right",
  touch_pinch: "Pinch / spread",
  touch_slide_up_pressed: "Slide up, pad clicked",
  touch_slide_down_pressed: "Slide down, pad clicked",
  touch_slide_left_pressed: "Slide left, pad clicked",
  touch_slide_right_pressed: "Slide right, pad clicked",
  touch_pinch_pressed: "Pinch / spread, pad clicked",
};
export const keyLabel = (k: string) => KEY_LABELS[k] || k.replace(/_/g, " ");

/* A gesture key is every `touch_*` key but the physical one-finger click
   (CONTRACT section 1) -- the rule both binding tables split their rows by. */
export const isGestureKey = (k: string) => k.startsWith("touch_") && k !== "touchpad_click";

/* The Gestures tab's rows when /api/actions has no `gestures` list (a 0.5
   backend): the 1.0 vocabulary, in the engine's order. Labels and help come
   from KEY_LABELS / HELP so the two never drift. */
export const GESTURE_FALLBACK: GestureRow[] = ([
  ["touch_tap_2f", "taps"], ["touch_click_2f", "taps"],
  ["touch_slide_up", "unpressed", "touch_slide_down", "up"], ["touch_slide_down", "unpressed", "touch_slide_up", "down"],
  ["touch_slide_left", "unpressed", "touch_slide_right", "left"], ["touch_slide_right", "unpressed", "touch_slide_left", "right"],
  ["touch_pinch", "unpressed"],
  ["touch_slide_up_pressed", "pressed", "touch_slide_down_pressed", "up"], ["touch_slide_down_pressed", "pressed", "touch_slide_up_pressed", "down"],
  ["touch_slide_left_pressed", "pressed", "touch_slide_right_pressed", "left"], ["touch_slide_right_pressed", "pressed", "touch_slide_left_pressed", "right"],
  ["touch_pinch_pressed", "pressed"],
] as const).map(([key, group, pair, dir]) => ({ key, group, label: KEY_LABELS[key], help: HELP[key], pair, dir }));

export const GESTURE_GROUPS: { id: string; title: string; hint: string }[] = [
  { id: "taps", title: "Taps & clicks", hint: "two fingers on the pad" },
  { id: "unpressed", title: "Slides & pinch", hint: "pad not pressed" },
  { id: "pressed", title: "While the pad is clicked", hint: "click, hold, then move two fingers — the click always wins over an unpressed gesture in progress" },
];

/* The feel knobs the Gestures tab draws itself; the thresholds render
   generically under them. */
export const KNOWN_GESTURES = new Set(["scroll_sensitivity", "scroll_reverse", "zoom_sensitivity", "zoom_reverse"]);

/* Keys each level of the config the page owns; anything else (a newer
   build's settings) still renders generically inside the right card. */
export const GLOBAL_FIELDS: [string, string, "bool" | "number" | "string"][] = [
  ["enabled", "Bridging enabled (master switch)", "bool"],
  ["autostart_on_login", "Start at login", "bool"],
  ["auto_bridge_new", "Bridge new controllers automatically", "bool"],
  ["hide_bluetooth_default", "Hide new controllers' Bluetooth pad", "bool"],
  ["update_check", "Check for updates", "bool"],
  ["update_auto_install", "Install updates automatically", "bool"],
  ["port_base", "First usbip TCP port", "number"],
  ["dashboard_port", "Dashboard port (this page)", "number"],
  ["hidhide_cli", "HidHideCLI.exe path (blank = auto)", "string"],
];
/* The rows the Bridging & tray card does NOT cover: the Advanced card. */
export const ADVANCED_FIELDS = GLOBAL_FIELDS.filter(([k]) => ["port_base", "dashboard_port", "hidhide_cli"].includes(k));
export const NOTIFICATION_FIELDS: [string, string, boolean, string][] = [
  ["enabled", "Show notifications", true, "notifications_enabled"],
  ["battery_low", "Low battery", true, "n_battery_low"],
  ["remote_mode", "Remote mode on / off", true, "n_remote_mode"],
  ["keyboard", "On-screen keyboard opened / closed", false, "n_keyboard"],
  ["connection", "Controller connected / disconnected", true, "n_connection"],
  ["hide", "Bluetooth pad hidden / unhidden", true, "n_hide"],
  ["update", "Update available", true, "n_update"],
];
export const KNOWN_NOTIFICATIONS = new Set(NOTIFICATION_FIELDS.map((f) => f[0]));
export const CTRL_FIELDS: [string, string, "bool" | "number" | "string" | "choice", string[]?][] = [
  ["enabled", "Bridge this controller", "bool"],
  ["label", "Label", "string"],
  ["audio_target", "Audio target", "choice", ["speaker", "headphone"]],
  ["hide_bluetooth", "Hide Bluetooth pad while bridged", "bool"],
  ["port", "usbip TCP port (blank = automatic)", "number"],
];
export const KNOWN_GLOBAL = new Set([...GLOBAL_FIELDS.map((f) => f[0]), "controllers", "input", "notifications"]);
export const KNOWN_CTRL = new Set(CTRL_FIELDS.map((f) => f[0]));
export const KNOWN_INPUT = new Set(["enabled", "chord_button", "chords", "actions", "macros",
  "double_press_ms", "tap_replay_ms", "repeat_ms", "haptic_ack", "haptic_strength",
  "stick_mouse_in_chord", "off_timer_minutes", "battery", "lightbar", "remote", "gestures"]);
export const KNOWN_REMOTE = new Set(["enabled", "mouse_speed", "scroll_speed", "lightbar_color", "same_bindings", "same_gestures", "chords"]);
export const KNOWN_LIGHTBAR = new Set(["dim_after_minutes", "dim_level"]);
export const KNOWN_BATTERY = new Set(["enabled", "low_percent", "critical_percent",
  "low_interval_s", "critical_interval_s", "low_color", "critical_color", "low_blinks", "critical_blinks"]);

/* [r,g,b] <-> #rrggbb for <input type=color>. */
export const rgbToHex = (c: unknown) =>
  "#" + (Array.isArray(c) && c.length === 3 ? c : [0, 0, 0])
    .map((v) => Math.max(0, Math.min(255, Math.round(Number(v)) || 0)).toString(16).padStart(2, "0")).join("");
export const hexToRgb = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) || 0);
