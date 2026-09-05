import { create } from "zustand";
import type { ActionsMeta, ConfigDoc, Controller, LiveState } from "./types";
import { rgbToHex } from "../components/settings/help";
import { mockActions, mockState } from "./devMock";

export type LinkStatus = "connecting" | "live" | "snapshot" | "reconnecting";

interface Toast { id: number; text: string; error: boolean }

interface Store {
  /* live telemetry */
  state: LiveState;
  link: LinkStatus;
  active: string | null;                     // selected serial
  setActive: (serial: string) => void;
  connect: () => void;

  /* settings */
  settingsOpen: boolean;
  toggleSettings: (open?: boolean) => void;
  settingsTab: string;
  setSettingsTab: (tab: string) => void;   // also opens the panel
  cfg: ConfigDoc | null;                     // the working copy the form binds to
  cfgPath: string;
  actions: ActionsMeta | null;
  /* input.remote.lightbar_color as last seen from the server -- the remote
     badges want the colour before (and without) the settings panel loading
     the whole document into `cfg`. */
  remoteColorSeed: number[] | null;
  dirty: boolean;
  loading: boolean;
  loadConfig: () => Promise<void>;
  saveConfig: () => Promise<void>;
  patchConfig: (mutate: (cfg: ConfigDoc) => void) => void;

  toasts: Toast[];
  toast: (text: string, error?: boolean) => void;
}

const EMPTY: LiveState = { ts: 0, controllers: {}, aggregate: {} };
let toastSeq = 0;

export const useStore = create<Store>((set, get) => ({
  state: EMPTY,
  link: "connecting",
  active: null,
  setActive: (serial) => set({ active: serial }),

  connect: () => {
    const apply = (incoming: LiveState) => {
      const state = mockState(incoming);
      const serials = Object.keys(state.controllers).sort();
      const prev = get().state.controllers;
      const active = get().active;
      // Remote mode flipping is the one lifecycle event the pad itself
      // announces (lightbar + rumble); echo it here so a glance at the page
      // says why the game just stopped listening.
      for (const serial of serials) {
        const was = remoteMode(prev[serial]);
        const now = remoteMode(state.controllers[serial]);
        if (was !== null && now !== null && was !== now) {
          const c = state.controllers[serial];
          get().toast(`${controllerName(serial, c.label)}: remote mode ${now ? "ON — the pad drives the OS" : "off — back to the game"}`);
        }
      }
      set({
        state,
        active: active && serials.includes(active) ? active : serials[0] ?? null,
      });
    };
    // Seed the remote lightbar colour without opening settings (one fetch;
    // the badges fall back to the engine default until it lands).
    void fetch("/api/config").then((r) => r.json())
      .then((doc) => set({ remoteColorSeed: remoteColorOf(doc?.config) }))
      .catch(() => { /* the default colour is right for an untouched config */ });
    // ?snap: poll the one-shot endpoint instead of holding the SSE stream
    // open -- for headless screenshots (an open EventSource never lets the
    // page reach network-idle) and for tests.
    if (new URLSearchParams(location.search).has("snap")) {
      const once = async () => {
        try {
          apply(await (await fetch("/api/state")).json());
          set({ link: "snapshot" });
        } catch { /* the next poll retries */ }
      };
      once();
      setInterval(once, 500);
      return;
    }
    const es = new EventSource("/api/stream");
    es.onopen = () => set({ link: "live" });
    es.onerror = () => set({ link: "reconnecting" });
    es.onmessage = (ev) => {
      try { apply(JSON.parse(ev.data)); } catch { /* a torn frame; the next one is whole */ }
    };
  },

  settingsOpen: false,
  settingsTab: "general",
  setSettingsTab: (tab) => { set({ settingsTab: tab }); get().toggleSettings(true); },
  toggleSettings: (open) => {
    const next = open ?? !get().settingsOpen;
    set({ settingsOpen: next });
    if (next && !get().cfg) void get().loadConfig();
  },
  cfg: null,
  cfgPath: "",
  actions: null,
  remoteColorSeed: null,
  dirty: false,
  loading: false,

  loadConfig: async () => {
    set({ loading: true });
    try {
      if (!get().actions) {
        // Static per server process: one fetch per page life is plenty. If it
        // is unreachable (an older server under a newer page?) the form falls
        // back to generic by-type rendering so nothing becomes uneditable.
        try { set({ actions: mockActions(await (await fetch("/api/actions")).json()) }); }
        catch { set({ actions: null }); }
      }
      const doc = await (await fetch("/api/config")).json();
      set({ cfg: doc.config, cfgPath: doc.path || "", dirty: false, remoteColorSeed: remoteColorOf(doc.config) });
    } catch (e) {
      get().toast("Could not load the config: " + e, true);
    } finally {
      set({ loading: false });
    }
  },

  saveConfig: async () => {
    const cfg = get().cfg;
    if (!cfg) return;
    set({ loading: true });
    try {
      const r = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      });
      const doc = await r.json();
      if (!r.ok || doc.error) throw new Error(doc.error || String(r.status));
      set({ cfg: doc.config, cfgPath: doc.path || "", dirty: false, remoteColorSeed: remoteColorOf(doc.config) });
      get().toast("Saved to " + doc.path);
    } catch (e) {
      get().toast("Save failed: " + (e as Error).message, true);
    } finally {
      set({ loading: false });
    }
  },

  // The form edits a working copy in place (cheap at 30 Hz of unrelated
  // re-renders) and bumps the reference so bound controls refresh.
  patchConfig: (mutate) => {
    const cfg = get().cfg;
    if (!cfg) return;
    mutate(cfg);
    set({ cfg: { ...cfg }, dirty: true });
  },

  toasts: [],
  toast: (text, error = false) => {
    const id = ++toastSeq;
    set({ toasts: [...get().toasts, { id, text, error }] });
    setTimeout(() => set({ toasts: get().toasts.filter((t) => t.id !== id) }), 3800);
  },
}));

/* ---- selectors -------------------------------------------------------- */

export const useActiveController = () =>
  useStore((s) => (s.active ? s.state.controllers[s.active] : undefined));

export const short = (serial: string) =>
  serial.length <= 8 ? serial : serial.slice(0, 4) + "…" + serial.slice(-4);

export const controllerName = (serial: string, label?: string) =>
  label ? label : short(serial);

/* ---- remote mode ------------------------------------------------------- */

/* The manager's flag first (it is the authority on lifecycle), the
   interceptor's telemetry mirror second; null while neither side has said. */
export const remoteMode = (c: Controller | undefined): boolean | null => {
  const v = c?.remote_mode ?? c?.telemetry?.remote_mode;
  return typeof v === "boolean" ? v : null;
};
export const keyboardOpen = (c: Controller | undefined): boolean =>
  (c?.keyboard_open ?? c?.telemetry?.keyboard_open) === true;

export const DEFAULT_REMOTE_COLOR = [255, 120, 0];   // RemoteMode.lightbar_color's default

function remoteColorOf(cfg: unknown): number[] | null {
  const v = getPath(cfg, ["input", "remote", "lightbar_color"]);
  return Array.isArray(v) && v.length === 3 ? v.map(Number) : null;
}

/* The remote-mode colour as #rrggbb: the working copy while settings are
   open (so a colour being picked previews live), else the last saved one. */
export const useRemoteColor = () =>
  useStore((s) => rgbToHex((s.cfg ? remoteColorOf(s.cfg) : null) ?? s.remoteColorSeed ?? DEFAULT_REMOTE_COLOR));

/* How many pads are in remote mode right now (the header HUD). */
export const useRemoteCount = () =>
  useStore((s) => Object.values(s.state.controllers).filter((c) => remoteMode(c) === true).length);

/* Path helpers over the loose config document. */
export function getPath(obj: unknown, path: (string | number)[]): unknown {
  return path.reduce<unknown>((o, k) => (o as Record<string, unknown> | undefined)?.[k as string], obj);
}
export function setPath(obj: Record<string, unknown>, path: string[], value: unknown) {
  let o = obj;
  for (const k of path.slice(0, -1)) {
    if (o[k] === null || typeof o[k] !== "object" || Array.isArray(o[k])) o[k] = {};
    o = o[k] as Record<string, unknown>;
  }
  o[path[path.length - 1]] = value;
}
