/* The wire contract with ds5app/dashboard.py + telemetry.py. Everything is
   optional on purpose: a controller known only to the manager (no telemetry
   port) or only to the hub (--fake) renders whatever half it has. */

export type ButtonName =
  | "sq" | "x" | "o" | "tri" | "L1" | "R1" | "L2" | "R2"
  | "create" | "options" | "L3" | "R3" | "PS" | "touchpad" | "mute";

export interface TouchPoint { active: boolean; id: number; x: number; y: number }

export interface Decoded {
  lx: number; ly: number; rx: number; ry: number;
  l2: number; r2: number;
  seq: number;
  dpad: string;                              // "N", "NE", ..., "-"
  buttons: Partial<Record<ButtonName, boolean>>;
  touch: TouchPoint[];
  gyro: [number, number, number];
  accel: [number, number, number];
  battery_percent: number;
  battery_state: string;
  headphone: boolean;
  mic: boolean;
  mic_muted: boolean;
}

export interface Telemetry {
  decoded: Decoded | null;
  connected: boolean;
  stale_s: number | null;
  rps: number;
  t: number | null;
  age_s: number;
  remote_mode?: boolean;                     // mirrored from the interceptor
  keyboard_open?: boolean;
}

export interface Controller {
  serial: string;
  label?: string;
  state?: string;                            // running | controller offline | ...
  enabled?: boolean;
  present?: boolean;
  port?: number | null;
  pid?: number | null;
  battery_percent?: number | null;
  reports_per_s?: number | null;
  uptime_s?: number | null;
  attached?: boolean;
  error?: string | null;
  hide_bluetooth?: boolean;                  // what the user ASKED for
  hide_effective?: boolean | null;           // HidHide's filter verified attached (null = unknown)
  hide_note?: string;                        // why hide_effective disagrees, when it does
  remote_mode?: boolean | null;              // the pad is driving the OS, not the game
  keyboard_open?: boolean | null;            // the on-screen keyboard is up
  last_event?: string;
  telemetry?: Telemetry;
}

export interface Aggregate {
  present?: number; bridges?: number; running?: number; degraded?: number;
  attached?: number; reports_per_s?: number; errors?: number;
  master_enabled?: boolean;
}

/* /api/state.update -- the tray's updater (1.0 backend; absent before, and
   the page then hides the row and disables the buttons). */
export interface UpdateInfo {
  available?: string | null;        // "1.0.1" when a newer release is known
  url?: string | null;
  checked_at?: number | null;       // unix seconds of the last check
  installing?: boolean;
  error?: string | null;
}
/* /api/state.autostart -- how "start with Windows" is installed. */
export interface AutostartInfo { enabled?: boolean; mode?: "task" | "service" | "none" | string }

export interface LiveState {
  ts: number;
  controllers: Record<string, Controller>;
  aggregate: Aggregate;
  update?: UpdateInfo;
  autostart?: AutostartInfo;
}

/* /api/actions */
export interface ActionSpec { name: string; doc: string; repeatable: boolean }
/* One row of the Gestures tab, in the engine's order. `group`: taps (2-finger
   tap / click), unpressed (slides, swipes, pinch with the pad NOT clicked),
   pressed (the same while the pad is held clicked). */
export type GestureGroup = "taps" | "unpressed" | "pressed";
export interface GestureRow { key: string; label: string; help: string; group: GestureGroup | string }
export interface ActionsMeta {
  actions: ActionSpec[];
  chord_buttons: string[];
  chord_keys: string[];
  gesture_keys: string[];
  macro_keys?: string[];          // the key vocabulary a user macro may use
  engine_actions?: { name: string; doc: string }[];   // pad power / lightbar
  remote_keys?: string[];         // keys input.remote.chords accepts (falls back to chord_keys + gesture_keys)
  gestures?: GestureRow[];        // the Gestures tab's rows (1.0 backend; a hard-coded table stands in)
  defaults: { chords: Record<string, string>; remote_chords?: Record<string, string> };
}

/* /api/config -- the document is deliberately loose: known keys get
   purpose-built controls, everything else renders generically by type. */
export type Json = string | number | boolean | null | Json[] | { [k: string]: Json };
export type ConfigDoc = { [k: string]: Json };
export interface ConfigResponse { path: string; config: ConfigDoc; error?: string }

/* POST /api/action {"op"} -> {"ok","text"} (1.0 backend; 404 before). */
export type ActionOp = "rescan" | "unhide_all" | "update_check" | "update_install" | "open_logs";
export interface ActionResponse { ok: boolean; text?: string }

/* input.macros.<name>.repeat: a bool in 0.5 (true = hold), an object in 1.0. */
export type RepeatMode = "once" | "hold" | "toggle";
export interface MacroRepeat { mode: RepeatMode; interval_ms: number; max_runs: number }
