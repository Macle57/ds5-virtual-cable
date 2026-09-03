import { create } from "zustand";
import type { ActionsMeta, ConfigDoc, LiveState } from "./types";

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
  cfg: ConfigDoc | null;                     // the working copy the form binds to
  cfgPath: string;
  actions: ActionsMeta | null;
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
    const apply = (state: LiveState) => {
      const serials = Object.keys(state.controllers).sort();
      const active = get().active;
      set({
        state,
        active: active && serials.includes(active) ? active : serials[0] ?? null,
      });
    };
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
  toggleSettings: (open) => {
    const next = open ?? !get().settingsOpen;
    set({ settingsOpen: next });
    if (next && !get().cfg) void get().loadConfig();
  },
  cfg: null,
  cfgPath: "",
  actions: null,
  dirty: false,
  loading: false,

  loadConfig: async () => {
    set({ loading: true });
    try {
      if (!get().actions) {
        // Static per server process: one fetch per page life is plenty. If it
        // is unreachable (an older server under a newer page?) the form falls
        // back to generic by-type rendering so nothing becomes uneditable.
        try { set({ actions: await (await fetch("/api/actions")).json() }); }
        catch { set({ actions: null }); }
      }
      const doc = await (await fetch("/api/config")).json();
      set({ cfg: doc.config, cfgPath: doc.path || "", dirty: false });
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
      set({ cfg: doc.config, cfgPath: doc.path || "", dirty: false });
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
