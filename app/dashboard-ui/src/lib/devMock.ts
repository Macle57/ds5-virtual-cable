/* Development-only fixtures for the fields the engine/installer sides serve
   but the --fake feed does not yet: `npm run dev` with `?mock` on the URL
   marks the first pad as being in remote mode with the keyboard open, the
   second (if any) as "hidden requested, but HidHide's filter is not
   attached", and adds the new action names to the picker. Compiled out of
   the production build (import.meta.env.DEV is false there). */
import type { ActionsMeta, LiveState } from "./types";

const param = () => import.meta.env.DEV ? new URLSearchParams(location.search).get("mock") : null;
const on = () => param() !== null;

export function mockState(state: LiveState): LiveState {
  if (!on()) return state;
  const serials = Object.keys(state.controllers).sort();
  const controllers = { ...state.controllers };
  if (serials[0]) {
    // `?mock=flip` toggles every ~12 s so the toast + badges can be watched
    // coming and going; plain `?mock` holds remote mode on for screenshots.
    const flip = param() === "flip";
    const remote = !flip || Math.floor(Date.now() / 12000) % 2 === 0;
    controllers[serials[0]] = {
      ...controllers[serials[0]], remote_mode: remote,
      keyboard_open: remote && (!flip || Math.floor(Date.now() / 4000) % 3 === 0),
    };
  }
  // the hide fault goes on the second pad (`?mock=all`: on the first as well)
  const faulty = param() === "all" ? serials[0] : serials[1];
  if (faulty) {
    controllers[faulty] = {
      ...controllers[faulty], hide_bluetooth: true, hide_effective: false,
      hide_note: "Hidden requested, but the controller is still visible to other apps — HidHide's filter is not attached (restart the device or reboot).",
    };
  }
  return { ...state, controllers };
}

export function mockActions(meta: ActionsMeta): ActionsMeta {
  if (!on()) return meta;
  const extra = [
    ["display_extend", "Win+P: extend the desktop across every display"],
    ["display_second_only", "Win+P: second screen only"],
    ["display_pc_only", "Win+P: PC screen only"],
    ["display_duplicate", "Win+P: duplicate the desktop"],
    ["display_cycle", "Win+P: step to the next projection mode"],
    ["dictation", "Win+H: start / stop voice typing"],
    ["keyboard", "toggle the on-screen keyboard"],
    ["left_click", "click the mouse"],
    ["right_click", "right-click the mouse"],
    ["middle_click", "middle-click the mouse"],
    ["mystery_action", "a registry action this page has never heard of"],
  ] as const;
  const have = new Set(meta.actions.map((a) => a.name));
  return {
    ...meta,
    actions: [...meta.actions, ...extra.filter(([n]) => !have.has(n)).map(([name, doc]) => ({ name, doc, repeatable: false }))],
    remote_keys: meta.remote_keys ?? [...meta.chord_keys.filter((k) => k !== "ps"), ...meta.gesture_keys],
    defaults: {
      ...meta.defaults,
      remote_chords: meta.defaults.remote_chords ?? {
        ...meta.defaults.chords, cross: "left_click", circle: "right_click", options: "keyboard", create: "dictation",
      },
    },
  };
}
