/* What an action LOOKS like: icon, colour, short label, group. The engine
   serves names and one-line docs (/api/actions); this is the page's own
   knowledge of the built-ins, with a generic fallback so a registry action
   this build has never seen still gets a card. Macros come from the config
   document and are described from their definition. */
import type { LucideIcon } from "lucide-react";
import {
  ArrowLeftRight, CircleOff, Copy, HelpCircle, Keyboard, LayoutGrid, LightbulbOff, Mic, Minimize2, Monitor,
  MonitorDown, MonitorSpeaker, Mouse, MousePointer, MousePointerClick, PanelsTopLeft, Play, Power, Projector,
  RefreshCw, SkipBack, SkipForward, Sun, SunDim, Terminal, Volume1, Volume2, VolumeX,
} from "lucide-react";
import type { ActionsMeta, Json } from "./types";

export type Group = "none" | "media" | "volume" | "display" | "windows" | "mouse" | "voice" | "pad" | "macro" | "other";
export type IconType = LucideIcon;

export interface MacroDef { keys?: string[]; run?: string; label?: string; repeat?: boolean }

export interface ActionInfo {
  name: string;
  label: string;      // short, for the button face
  doc: string;        // one line under it
  group: Group;
  Icon: IconType;
  color: string;      // a theme token colour
  repeatable: boolean;
  macro?: MacroDef;
  unknown?: boolean;  // bound in config, but not in this build's vocabulary
}

export const GROUPS: { id: Group; title: string; color: string }[] = [
  { id: "none", title: "", color: "var(--color-ink-3)" },
  { id: "media", title: "Media", color: "var(--color-square)" },
  { id: "volume", title: "Volume", color: "var(--color-cyan)" },
  { id: "display", title: "Display", color: "var(--color-amber)" },
  { id: "windows", title: "Windows", color: "var(--color-cross)" },
  { id: "mouse", title: "Mouse", color: "var(--color-orange)" },
  { id: "voice", title: "Voice & typing", color: "var(--color-violet)" },
  { id: "pad", title: "Controller", color: "var(--color-circle)" },
  { id: "macro", title: "Your macros", color: "var(--color-triangle)" },
  { id: "other", title: "Other", color: "var(--color-ink-2)" },
];
const groupColor = (g: Group) => GROUPS.find((x) => x.id === g)!.color;

const BUILTIN: Record<string, { label: string; group: Group; Icon: IconType }> = {
  none:             { label: "Unbound",            group: "none",    Icon: CircleOff },
  pad_power_off:    { label: "Power pad off",      group: "pad",     Icon: Power },
  pad_lightbar_toggle: { label: "Lightbar off",     group: "pad",     Icon: LightbulbOff },
  media_play_pause: { label: "Play / pause",       group: "media",   Icon: Play },
  media_next:       { label: "Next track",         group: "media",   Icon: SkipForward },
  media_prev:       { label: "Previous track",     group: "media",   Icon: SkipBack },
  volume_up:        { label: "Volume up",          group: "volume",  Icon: Volume2 },
  volume_down:      { label: "Volume down",        group: "volume",  Icon: Volume1 },
  volume_mute:      { label: "Mute",               group: "volume",  Icon: VolumeX },
  brightness_up:    { label: "Brightness up",      group: "display", Icon: Sun },
  brightness_down:  { label: "Brightness down",    group: "display", Icon: SunDim },
  projection_cycle: { label: "Projection",         group: "display", Icon: MonitorDown },
  display_cycle:    { label: "Next display mode",  group: "display", Icon: RefreshCw },
  display_extend:   { label: "Extend displays",    group: "display", Icon: MonitorSpeaker },
  display_duplicate: { label: "Duplicate displays", group: "display", Icon: Copy },
  display_pc_only:  { label: "PC screen only",     group: "display", Icon: Monitor },
  display_second_only: { label: "Second screen only", group: "display", Icon: Projector },
  show_desktop:     { label: "Show desktop",       group: "windows", Icon: PanelsTopLeft },
  minimize_all:     { label: "Minimize all",       group: "windows", Icon: Minimize2 },
  task_view:        { label: "Task View",          group: "windows", Icon: LayoutGrid },
  alt_tab:          { label: "Alt+Tab step",       group: "windows", Icon: ArrowLeftRight },
  left_click:       { label: "Left click",         group: "mouse",   Icon: MousePointerClick },
  right_click:      { label: "Right click",        group: "mouse",   Icon: MousePointer },
  middle_click:     { label: "Middle click",       group: "mouse",   Icon: Mouse },
  dictation:        { label: "Voice typing",       group: "voice",   Icon: Mic },
  keyboard:         { label: "On-screen keyboard", group: "voice",   Icon: Keyboard },
};

/* Docs for when /api/actions has not served one (an older server, or the
   engine's own actions). The server's line wins whenever it exists. */
const BUILTIN_DOC: Record<string, string> = {
  none: "the game keeps this button",
  pad_power_off: "power the pad off",
  pad_lightbar_toggle: "lightbar off; press again to bring it back",
  display_cycle: "Win+P: step to the next projection mode",
  display_extend: "Win+P: extend the desktop across every display",
  display_duplicate: "Win+P: the same picture on every display",
  display_pc_only: "Win+P: PC screen only",
  display_second_only: "Win+P: second screen only",
  left_click: "click the mouse where the pointer is",
  right_click: "right-click where the pointer is",
  middle_click: "middle-click where the pointer is",
  dictation: "Win+H: start / stop voice typing",
  keyboard: "show / hide the on-screen keyboard",
};

/* The engine's own actions: served by /api/actions when the server knows
   to, else the one every build has had. */
export const engineActions = (meta: ActionsMeta | null) =>
  meta?.engine_actions ?? [{ name: "pad_power_off", doc: BUILTIN_DOC.pad_power_off }];

export const asMacro = (v: unknown): MacroDef | null =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as MacroDef) : null;

/* Every macro in the document, tombstones (null) skipped. */
export function macroList(raw: unknown): [string, MacroDef][] {
  const out: [string, MacroDef][] = [];
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [name, def] of Object.entries(raw as Record<string, Json>)) {
      const m = asMacro(def);
      if (m) out.push([name, m]);
    }
  }
  return out.sort((a, b) => a[0].localeCompare(b[0]));
}

export function macroSummary(m: MacroDef): string {
  if (Array.isArray(m.keys) && m.keys.length) return m.keys.map(keyCap).join(" + ");
  if (typeof m.run === "string" && m.run.trim()) return m.run.trim();
  return "(incomplete)";
}

export function describeMacro(name: string, m: MacroDef): ActionInfo {
  const isRun = !(Array.isArray(m.keys) && m.keys.length) && typeof m.run === "string";
  return {
    name, label: m.label?.trim() || name, doc: macroSummary(m), group: "macro",
    Icon: isRun ? Terminal : Keyboard, color: groupColor("macro"),
    repeatable: !!m.repeat && !isRun, macro: m,
  };
}

export function describeAction(name: string, meta: ActionsMeta | null, macros: unknown): ActionInfo {
  const n = name.trim().toLowerCase() || "none";
  const m = asMacro(macros && typeof macros === "object" ? (macros as Record<string, unknown>)[n] : undefined);
  if (m) return describeMacro(n, m);
  const spec = meta?.actions.find((a) => a.name === n)
    ?? engineActions(meta).find((a) => a.name === n);
  const b = BUILTIN[n];
  if (b) {
    return { name: n, label: b.label, doc: spec?.doc ?? BUILTIN_DOC[n] ?? "", group: b.group,
             Icon: b.Icon, color: groupColor(b.group), repeatable: !!(spec as { repeatable?: boolean } | undefined)?.repeatable };
  }
  if (spec) {
    // in the registry, unknown to this page: a plain card with its doc
    return { name: n, label: n.replace(/_/g, " "), doc: spec.doc, group: "other",
             Icon: HelpCircle, color: groupColor("other"), repeatable: !!(spec as { repeatable?: boolean }).repeatable };
  }
  return { name: n, label: n, doc: "unknown to this build; kept as written", group: "other",
           Icon: HelpCircle, color: groupColor("other"), repeatable: false, unknown: true };
}

/* Everything a chord may bind to, in picker order: unbound, the pad, then
   the OS actions by group, then the user's macros. */
export function listActions(meta: ActionsMeta, macros: unknown): ActionInfo[] {
  const names = new Set<string>(["none", ...engineActions(meta).map((a) => a.name), ...meta.actions.map((a) => a.name)]);
  const infos = [...names].map((n) => describeAction(n, meta, null));
  const order = GROUPS.map((g) => g.id);
  infos.sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group) || a.label.localeCompare(b.label));
  for (const [name, def] of macroList(macros)) infos.push(describeMacro(name, def));
  return infos;
}

/* ---- keycaps ------------------------------------------------------------ */

const CAPS: Record<string, string> = {
  ctrl: "Ctrl", shift: "Shift", alt: "Alt", win: "Win", enter: "Enter", esc: "Esc", tab: "Tab",
  space: "Space", backspace: "Bksp", delete: "Del", insert: "Ins", home: "Home", end: "End",
  pageup: "PgUp", pagedown: "PgDn", up: "↑", down: "↓", left: "←", right: "→",
  printscreen: "PrtSc", pause: "Pause", capslock: "Caps", numlock: "NumLk", scrolllock: "ScrLk", menu: "Menu",
  volume_up: "Vol +", volume_down: "Vol −", volume_mute: "Mute", media_play_pause: "Play/Pause",
  media_next: "Next", media_prev: "Prev", media_stop: "Stop",
  minus: "-", equals: "=", comma: ",", period: ".", slash: "/", backslash: "\\", semicolon: ";",
  quote: "'", lbracket: "[", rbracket: "]", grave: "`",
};
export const keyCap = (k: string): string =>
  CAPS[k] ?? (k.length === 1 ? k.toUpperCase() : k.startsWith("numpad") ? "Num " + k.slice(6) : k.toUpperCase());

export const MODIFIERS = ["ctrl", "shift", "alt", "win"];

/* A browser KeyboardEvent -> engine key name (null for a bare modifier or
   a key the engine has no name for). */
export function eventKeyName(e: { code: string }): string | null {
  const c = e.code;
  if (/^Key[A-Z]$/.test(c)) return c[3].toLowerCase();
  if (/^Digit[0-9]$/.test(c)) return c[5];
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(c)) return c.toLowerCase();
  if (/^Numpad[0-9]$/.test(c)) return "numpad" + c[6];
  const map: Record<string, string> = {
    Enter: "enter", NumpadEnter: "enter", Escape: "esc", Tab: "tab", Space: "space", Backspace: "backspace",
    Delete: "delete", Insert: "insert", Home: "home", End: "end", PageUp: "pageup", PageDown: "pagedown",
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right", PrintScreen: "printscreen",
    Pause: "pause", CapsLock: "capslock", NumLock: "numlock", ScrollLock: "scrolllock", ContextMenu: "menu",
    Minus: "minus", Equal: "equals", Comma: "comma", Period: "period", Slash: "slash", Backslash: "backslash",
    Semicolon: "semicolon", Quote: "quote", BracketLeft: "lbracket", BracketRight: "rbracket", Backquote: "grave",
    AudioVolumeUp: "volume_up", AudioVolumeDown: "volume_down", AudioVolumeMute: "volume_mute",
    MediaPlayPause: "media_play_pause", MediaTrackNext: "media_next", MediaTrackPrevious: "media_prev", MediaStop: "media_stop",
  };
  return map[c] ?? null;
}

/* The key vocabulary, bucketed for a <select>. */
export function keyGroups(keys: string[]): [string, string[]][] {
  const b: Record<string, string[]> = {
    Modifiers: [], Navigation: [], Editing: [], "Function keys": [], Letters: [], Digits: [], Media: [], Punctuation: [], Numpad: [],
  };
  for (const k of keys) {
    if (MODIFIERS.includes(k)) b.Modifiers.push(k);
    else if (/^f\d+$/.test(k)) b["Function keys"].push(k);
    else if (/^[a-z]$/.test(k)) b.Letters.push(k);
    else if (/^[0-9]$/.test(k)) b.Digits.push(k);
    else if (k.startsWith("numpad")) b.Numpad.push(k);
    else if (/^(volume|media)_/.test(k)) b.Media.push(k);
    else if (["up", "down", "left", "right", "home", "end", "pageup", "pagedown", "tab", "esc", "enter", "menu"].includes(k)) b.Navigation.push(k);
    else if (["space", "backspace", "delete", "insert", "printscreen", "pause", "capslock", "numlock", "scrolllock"].includes(k)) b.Editing.push(k);
    else b.Punctuation.push(k);
  }
  return Object.entries(b).filter(([, v]) => v.length);
}

/* A label -> the config identifier a chord binds to. */
export function slugify(label: string): string {
  let s = label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
  if (!s) return "";
  if (!/^[a-z]/.test(s)) s = "m_" + s.slice(0, 38);
  return s;
}
