/* Development-only fixtures for the fields the engine/manager sides serve
   but the --fake feed does not yet: `npm run dev` with `?mock` on the URL
   marks the first pad as being in remote mode with the keyboard open, the
   second (if any) as "hidden requested, but HidHide's filter is not
   attached", adds the 1.0 action names + gesture rows to the picker and the
   `update` / `autostart` state the Bridging card reads.

     ?mock          the above, remote mode held on
     ?mock=flip     remote mode toggles every ~12 s (toast + badges)
     ?mock=all      the hide fault on the first pad too
     ?mock=offline  the LAST pad is switched off (present:false, "controller
                    offline") -- the auto-select must land on the other one
     ?mock=update   an update is available (the Install button lights up)

   Compiled out of the production build (import.meta.env.DEV is false there). */
import type { ActionsMeta, LiveState } from "./types";
import { GESTURE_FALLBACK as MOCK_GESTURES } from "../components/settings/help";

const param = () => import.meta.env.DEV ? new URLSearchParams(location.search).get("mock") : null;
const on = () => param() !== null;

export function mockState(state: LiveState): LiveState {
  if (!on()) return state;
  const serials = Object.keys(state.controllers).sort();
  const controllers = { ...state.controllers };
  const mode = param();
  if (serials[0]) {
    // `?mock=flip` toggles every ~12 s so the toast + badges can be watched
    // coming and going; plain `?mock` holds remote mode on for screenshots.
    const flip = mode === "flip";
    const remote = mode !== "offline" && (!flip || Math.floor(Date.now() / 12000) % 2 === 0);
    controllers[serials[0]] = {
      ...controllers[serials[0]], remote_mode: remote,
      keyboard_open: remote && (!flip || Math.floor(Date.now() / 4000) % 3 === 0),
    };
  }
  // the hide fault goes on the second pad (`?mock=all`: on the first as well)
  const faulty = mode === "all" ? serials[0] : serials[1];
  if (faulty && mode !== "offline") {
    controllers[faulty] = {
      ...controllers[faulty], hide_bluetooth: true, hide_effective: false,
      hide_note: "Hidden requested, but the controller is still visible to other apps — HidHide's filter is not attached (restart the device or reboot).",
    };
  }
  if (serials[0]) controllers[serials[0]] = { ...controllers[serials[0]], hide_bluetooth: true, hide_effective: true };
  let aggregate = state.aggregate;
  if (mode === "offline" && serials.length > 1) {
    // the manager keeps the stale entry (present:false) so the page can show
    // a "disconnected" tab; aggregate.present reflects reality (section 4)
    const off = serials[serials.length - 1];
    const { telemetry: _t, ...rest } = controllers[off];
    void _t;
    controllers[off] = { ...rest, present: false, state: "controller offline", attached: false, reports_per_s: 0, battery_percent: null };
    aggregate = { ...aggregate, present: serials.length - 1, running: serials.length - 1, attached: serials.length - 1 };
  }
  return {
    ...state, controllers, aggregate,
    update: state.update ?? (mode === "update"
      ? { available: "1.0.1", url: "https://github.com/Macle57/ds5-virtual-usb/releases/tag/v1.0.1", checked_at: Date.now() / 1000 - 90, installing: false }
      : { available: null, url: null, checked_at: Date.now() / 1000 - 3600, installing: false }),
    autostart: state.autostart ?? { enabled: true, mode: "service" },
  };
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
    ["scroll", "scroll wheel, proportional to the slide (precision-touchpad style)"],
    ["scroll_horizontal", "horizontal scroll, proportional to the slide"],
    ["pinch_zoom", "touch-injected pinch: zooms like a precision touchpad"],
    ["ctrl_zoom", "Ctrl + wheel zoom"],
    ["show_battery", "toast this pad's battery level"],
    ["mystery_action", "a registry action this page has never heard of"],
  ] as const;
  const have = new Set(meta.actions.map((a) => a.name));
  return {
    ...meta,
    actions: [...meta.actions, ...extra.filter(([n]) => !have.has(n)).map(([name, doc]) => ({ name, doc, repeatable: false }))],
    remote_keys: meta.remote_keys ?? [...meta.chord_keys.filter((k) => k !== "ps"), ...meta.gesture_keys],
    gestures: meta.gestures ?? MOCK_GESTURES,
    gesture_keys: meta.gestures ? meta.gesture_keys : MOCK_GESTURES.map((g) => g.key),
    defaults: {
      ...meta.defaults,
      chords: {
        ...meta.defaults.chords, touch_tap_2f: "none", touch_click_2f: "right_click", touch_slide_horizontal: "scroll_horizontal",
        touch_slide_vertical: "scroll", touch_swipe_up: "none", touch_swipe_down: "none", touch_slide_horizontal_pressed: "alt_tab",
        touch_swipe_up_pressed: "task_view", touch_swipe_down_pressed: "minimize_all", touch_pinch: "pinch_zoom", touch_pinch_pressed: "ctrl_zoom",
        r3: "show_battery",
      },
      remote_chords: meta.defaults.remote_chords ?? {
        ...meta.defaults.chords, cross: "left_click", circle: "right_click", options: "keyboard", create: "dictation",
      },
    },
  };
}
