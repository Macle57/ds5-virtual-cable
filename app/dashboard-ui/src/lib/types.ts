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

export interface LiveState {
  ts: number;
  controllers: Record<string, Controller>;
  aggregate: Aggregate;
}

/* /api/actions */
export interface ActionSpec { name: string; doc: string; repeatable: boolean }
export interface ActionsMeta {
  actions: ActionSpec[];
  chord_buttons: string[];
  chord_keys: string[];
  gesture_keys: string[];
  macro_keys?: string[];          // the key vocabulary a user macro may use
  engine_actions?: { name: string; doc: string }[];   // pad power / lightbar
  remote_keys?: string[];         // keys input.remote.chords accepts (falls back to chord_keys + gesture_keys)
  defaults: { chords: Record<string, string>; remote_chords?: Record<string, string> };
}

/* /api/config -- the document is deliberately loose: known keys get
   purpose-built controls, everything else renders generically by type. */
export type Json = string | number | boolean | null | Json[] | { [k: string]: Json };
export type ConfigDoc = { [k: string]: Json };
export interface ConfigResponse { path: string; config: ConfigDoc; error?: string }
