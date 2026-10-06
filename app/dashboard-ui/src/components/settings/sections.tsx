import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Hand, Link2, MousePointer2 } from "lucide-react";
import { getPath, setPath, useRemoteColor, useStore } from "../../lib/store";
import type { ActionsMeta, ConfigDoc, GestureRow, Json } from "../../lib/types";
import { pairing } from "../../lib/actionMeta";
import {
  ADVANCED_FIELDS, CTRL_FIELDS, GESTURE_FALLBACK, GESTURE_GROUPS, HELP, KNOWN_BATTERY, KNOWN_CTRL, KNOWN_GESTURES, KNOWN_GLOBAL,
  KNOWN_INPUT, KNOWN_LIGHTBAR, KNOWN_REMOTE, isGestureKey, keyLabel,
} from "./help";
import { Card, ColorField, JsonField, NumberField, RangeField, Reveal, Row, SelectField, TextField, Toggle } from "./controls";
import GenericRows from "./GenericRows";
import ButtonGlyph from "./ButtonGlyph";
import ActionPickerUI from "./ActionPicker";
import BridgingSection from "./BridgingSection";
export { MacrosSection } from "./MacrosSection";
export { default as NotificationsSection } from "./NotificationsSection";

const useCfg = () => useStore((s) => s.cfg as ConfigDoc);
const asObj = (v: unknown): Record<string, Json> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, Json>) : {};
const IC = { strokeWidth: 2.5 };

/* ---- General: the tray's switches, then the rarely-touched rest ---------- */
export function GeneralSection() {
  const cfg = useCfg();
  return (
    <div className="st-stack">
      <BridgingSection />
      <Card title="Advanced" hint="Ports and paths. Changes here apply the next time the tray starts.">
        {ADVANCED_FIELDS.map(([key, label, kind]) => (
          <Row key={key} label={label} keyName={key} root="">
            {kind === "bool" ? <Toggle path={[key]} />
              : kind === "number" ? <NumberField path={[key]} dflt={null} nullable width={130} />
              : <TextField path={[key]} width={220} />}
          </Row>
        ))}
        <GenericRows path={[]} obj={cfg} known={KNOWN_GLOBAL} />
      </Card>
    </div>
  );
}

/* ---- Shortcuts: general ------------------------------------------------- */
export function ShortcutsSection({ actions }: { actions: ActionsMeta }) {
  const cfg = useCfg();
  const inp = asObj(cfg.input);
  return (
    <Card title="Shortcuts" hint="How the chord button behaves, how long things wait, and the rumble that tells you a chord landed.">
      <Row label="Shortcut engine" keyName="enabled" help={HELP.enabled}><Toggle path={["input", "enabled"]} dflt /></Row>
      <Row label="Chord button (hold it to arm shortcuts)" keyName="chord_button" help={HELP.chord_button}>
        <SelectField path={["input", "chord_button"]} dflt="ps" width={170}
                     options={actions.chord_buttons.map((b) => ({ value: b, label: keyLabel(b) }))} />
      </Row>
      <Row label="Double-press window" keyName="double_press_ms" unit="ms" help={HELP.double_press_ms}><NumberField path={["input", "double_press_ms"]} dflt={400} /></Row>
      <Row label="Replayed tap hold" keyName="tap_replay_ms" unit="ms" help={HELP.tap_replay_ms}><NumberField path={["input", "tap_replay_ms"]} dflt={100} /></Row>
      <Row label="Held-chord repeat" keyName="repeat_ms" unit="ms" help={HELP.repeat_ms}><NumberField path={["input", "repeat_ms"]} dflt={150} /></Row>
      <Row label="Idle power-off" keyName="off_timer_minutes" unit="min" help={HELP.off_timer_minutes}><NumberField path={["input", "off_timer_minutes"]} dflt={15} /></Row>
      <Row label="Rumble when a chord lands" keyName="haptic_ack" help={HELP.haptic_ack}><Toggle path={["input", "haptic_ack"]} dflt /></Row>
      <Row label="Rumble strength" keyName="haptic_strength" help={HELP.haptic_strength} stack>
        <RangeField path={["input", "haptic_strength"]} dflt={25} min={0} max={100} step={1} fmt={(v) => v + "%"} />
      </Row>
      <Row label="Stick and touchpad move the mouse while chording" keyName="stick_mouse_in_chord" help={HELP.stick_mouse_in_chord}><Toggle path={["input", "stick_mouse_in_chord"]} dflt /></Row>
      {inp.actions && Object.keys(asObj(inp.actions)).length > 0 && (
        <Row label="Per-action parameters" stack><JsonField path={["input", "actions"]} /></Row>
      )}
      <GenericRows path={["input"]} obj={inp} known={KNOWN_INPUT} />
    </Card>
  );
}

/* ---- Chords: a binding grid ---------------------------------------------- */

/* Which binding table a card edits: `input.chords` (while chording) or
   `input.remote.chords` (remote mode, no chord button). Both share the
   key vocabulary and the "none" tombstone. A row the document has not
   written yet shows `defaults[key]` -- the chord table is stored merged over
   its defaults so it never needs that, the remote table (a newer key) does.
   `accent` is the colour a bound card wears. */
interface Table { path: string[]; defaults: Record<string, string>; accent?: string }
const CHORD_TABLE: Table = { path: ["input", "chords"], defaults: {} };

const boundName = (raw: unknown, dflt?: string) =>
  typeof raw === "string" ? (raw.trim().toLowerCase() || "none") : (dflt?.trim().toLowerCase() || "none");

/* "(none)" serializes as the string "none" -- InputConfig.to_dict's contract
   for a removed default binding (absent would resurrect the default on the
   next load).

   A slide row has a `pair` (the row sharing its axis) and a `dir`. Picking a
   PAIRED action (scroll up / down / left / right, Alt+Tab) on one row binds
   the partner to its pair -- the two rows are one gesture. Picking anything
   else while the partner holds a paired action unbinds the partner: an axis
   is either one paired gesture or two independent one-shots (either of
   which may be unbound), never "scroll up" one way and something else the
   other. */
function ActionPicker({ chordKey, actions, table, pair, dir }:
  { chordKey: string; actions: ActionsMeta; table: Table; pair?: string; dir?: string }) {
  const bound = useStore((s) => getPath(s.cfg, [...table.path, chordKey]));
  const macros = useStore((s) => getPath(s.cfg, ["input", "macros"]));
  const patch = useStore((s) => s.patchConfig);
  return (
    <ActionPickerUI value={boundName(bound, table.defaults[chordKey])} meta={actions} macros={macros} dir={dir}
                    onChange={(name) => patch((cfg) => {
                      setPath(cfg, [...table.path, chordKey], name);
                      if (!pair) return;
                      const mirror = pairing(name, actions).pair;
                      const partner = boundName(getPath(cfg, [...table.path, pair]), table.defaults[pair]);
                      if (mirror) setPath(cfg, [...table.path, pair], mirror);
                      else if (pairing(partner, actions).pair) setPath(cfg, [...table.path, pair], "none");
                    })} />
  );
}

/* One binding: an index card with the button drawn on it, the action
   written underneath. Unbound cards are a dashed outline on the paper. */
function BindingCard({ chordKey, actions, help, label, table = CHORD_TABLE, pair, dir }:
  { chordKey: string; actions: ActionsMeta; help?: string; label?: string; table?: Table; pair?: string; dir?: string }) {
  const bound = useStore((s) => getPath(s.cfg, [...table.path, chordKey]));
  const name = boundName(bound, table.defaults[chordKey]);
  const isBound = name !== "none";
  const paired = !!pair && !!pairing(name, actions).pair;
  const accent = table.accent;
  return (
    <motion.div layout="position" className={"st-bind" + (isBound ? "" : " is-unbound")}
                style={accent ? ({ "--accent": accent } as React.CSSProperties) : undefined}>
      <div className="mb-2.5 flex items-center gap-2.5">
        <ButtonGlyph name={chordKey} size={34} />
        <div className="st-bind-name min-w-0 flex-1" title={"Saved as " + [...table.path, chordKey].join(".")}>
          {label ?? keyLabel(chordKey)}
        </div>
        {paired && (
          <span className="st-sticker tilt-r" style={{ "--tone": "var(--color-cyan)" } as React.CSSProperties}
                title={`One gesture with ${keyLabel(pair!)}: the two rows are set together`}>
            <Link2 size={12} {...IC} /> paired
          </span>
        )}
      </div>
      <ActionPicker chordKey={chordKey} actions={actions} table={table} pair={pair} dir={dir} />
      {help && <p className="st-bind-help">{help}</p>}
    </motion.div>
  );
}

export function ChordsSection({ actions }: { actions: ActionsMeta }) {
  const cfg = useCfg();
  const inp = asObj(cfg.input);
  const chordButton = actions.chord_buttons.includes(String(inp.chord_button)) ? String(inp.chord_button) : "ps";
  const chords = asObj(inp.chords);
  const listed = new Set<string>();
  const keys: string[] = [];
  for (const key of actions.chord_keys) {
    if (key === chordButton) continue;             // the arming button can't chord itself
    listed.add(key); keys.push(key);
  }
  // bindings for keys this build's vocabulary doesn't list: keep them editable
  // (the gestures have their own tab)
  for (const key of Object.keys(chords).sort()) {
    if (listed.has(key) || key === chordButton || isGestureKey(key)) continue;
    keys.push(key);
  }
  return (
    <div className="st-stack">
      <Card title={`Hold ${keyLabel(chordButton)}, then press a button`} hint={HELP.chords}>
        <div className="st-grid">
          {keys.map((k) => <BindingCard key={k} chordKey={k} actions={actions} />)}
        </div>
      </Card>
      <RemoteBindingsSection actions={actions} />
    </div>
  );
}

/* Whether remote mode reuses the chord table's BUTTON rows / GESTURE rows.
   Absent = true (the engine's default), so an older config reads exactly as
   it behaves. */
const useSameBindings = () =>
  useStore((s) => getPath(s.cfg, ["input", "remote", "same_bindings"]) !== false);
const useSameGestures = () =>
  useStore((s) => getPath(s.cfg, ["input", "remote", "same_gestures"]) !== false);

/* The remote table, as the engine keeps it: written rows over
   `defaults.remote_chords` (a 0.5 backend serves only the button defaults
   plus the horizontal slide; the 1.0 one serves every gesture's). */
function useRemoteTable(actions: ActionsMeta): Table {
  const color = useRemoteColor();
  return { path: ["input", "remote", "chords"], accent: color,
           defaults: actions.defaults.remote_chords ?? actions.defaults.chords };
}

/* The collapsible second table both remote cards share. */
function RemoteTable({ open, id, intro, children }: { open: boolean; id: string; intro: React.ReactNode; children: React.ReactNode }) {
  const color = useRemoteColor();
  return (
    <AnimatePresence initial={false}>
      {open && (
        <Reveal key={id}>
          <div className="st-callout" style={{ "--tone": color } as React.CSSProperties}>
            <span className="st-callout-ico"><MousePointer2 size={17} {...IC} /></span>
            <div>{intro}</div>
          </div>
          {children}
        </Reveal>
      )}
    </AnimatePresence>
  );
}

/* ---- Remote mode bindings: the second BUTTON table ------------------------ */
export function RemoteBindingsSection({ actions }: { actions: ActionsMeta }) {
  const cfg = useCfg();
  const inp = asObj(cfg.input);
  const same = useSameBindings();
  const sameGestures = useSameGestures();
  const setTab = useStore((s) => s.setSettingsTab);
  // Until the engine serves the remote vocabulary, the chord vocabulary
  // (minus the arming button -- a double-press of it is the mode toggle)
  // is the best description of what remote mode can bind. Gestures live on
  // their own tab under their own switch (input.remote.same_gestures).
  const chordButton = actions.chord_buttons.includes(String(inp.chord_button)) ? String(inp.chord_button) : "ps";
  const vocab = (actions.remote_keys ?? actions.chord_keys.filter((k) => k !== chordButton)).filter((k) => !isGestureKey(k));
  const table = useRemoteTable(actions);
  const written = asObj(asObj(inp.remote).chords);
  const keys = [...vocab];
  for (const key of Object.keys(written).sort()) if (!vocab.includes(key) && !isGestureKey(key)) keys.push(key);   // a newer build's key: keep it editable
  return (
    <Card title="Buttons in remote mode"
          hint="What each button does while the pad drives the desktop. Double-press the chord button to switch remote mode on or off. Saved as input.remote.same_bindings and input.remote.chords.">
      <Row label="Remote mode uses the same bindings" keyName="remote.same_bindings" help={HELP.same_bindings}>
        <Toggle path={["input", "remote", "same_bindings"]} dflt />
      </Row>
      <RemoteTable open={!same} id="remote-table" intro={<>
        <b>Remote mode has no chord button.</b> Nothing reaches the game while remote mode is on, so each button
        below fires its action directly, on its own. This table is the whole map: the defaults spell out the classic
        remote layout (Cross clicks, the d-pad sends arrow keys, Circle is Esc, Options is Enter). Set a row
        to <b>none</b> and that button does nothing in remote mode. Only the touchpad and stick pointer are fixed.
      </>}>
        <div className="st-grid">
          {keys.map((k) => <BindingCard key={k} chordKey={k} actions={actions} table={table} />)}
        </div>
      </RemoteTable>
      <div className="st-row">
        <button type="button" onClick={() => setTab("gestures")} className="st-link">
          <Hand size={16} {...IC} />
          {sameGestures ? "Touchpad gestures follow the Gestures tab. Edit them there" : "Touchpad gestures have their own remote table. Edit them on the Gestures tab"}
        </button>
      </div>
    </Card>
  );
}

/* ---- Gestures: chording table + the remote table under its own switch ---- */

/* The rows: the engine's list (1.0), else the hard-coded vocabulary; plus
   any `touch_*` key a table has written that neither lists, kept editable. */
function gestureRows(actions: ActionsMeta, ...tables: Record<string, Json>[]): GestureRow[] {
  const rows = actions.gestures?.length ? [...actions.gestures] : [...GESTURE_FALLBACK];
  const have = new Set(rows.map((r) => r.key));
  for (const t of tables) {
    for (const key of Object.keys(t).sort()) {
      if (isGestureKey(key) && !have.has(key)) { have.add(key); rows.push({ key, label: keyLabel(key), help: HELP[key] ?? "", group: "other" }); }
    }
  }
  return rows;
}

function GestureGrid({ rows, actions, table }: { rows: GestureRow[]; actions: ActionsMeta; table?: Table }) {
  const groups = [...GESTURE_GROUPS, { id: "other", title: "Other gestures", hint: "Keys from a newer build." }];
  return (
    <div className="grid gap-6 pb-2 pt-1">
      {groups.map((g) => {
        const items = rows.filter((r) => (GESTURE_GROUPS.some((x) => x.id === r.group) ? r.group : "other") === g.id);
        if (!items.length) return null;
        return (
          <div key={g.id}>
            <div className="st-sub">
              <span className="st-sub-title">{g.title}</span>
              <span className="st-sub-hint">{g.hint}</span>
            </div>
            <div className="st-grid wide">
              {items.map((r) => <BindingCard key={r.key} chordKey={r.key} actions={actions} table={table} pair={r.pair} dir={r.dir}
                                             label={r.label || keyLabel(r.key)} help={r.help || HELP[r.key]} />)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function GesturesSection({ actions }: { actions: ActionsMeta }) {
  const cfg = useCfg();
  const inp = asObj(cfg.input);
  const same = useSameGestures();
  const table = useRemoteTable(actions);
  const rows = gestureRows(actions, asObj(inp.chords), asObj(asObj(inp.remote).chords));
  const G = (k: string) => ["input", "gestures", k];
  return (
    <div className="st-stack">
      <Card title="Touchpad gestures while chording"
            hint="Two-finger touchpad gestures that work while you hold the chord button. Saved in input.chords.">
        <GestureGrid rows={rows} actions={actions} />
      </Card>
      <Card title="How scrolling and zoom feel"
            hint="How far a two-finger slide scrolls, how much a pinch zooms, and which way. Applies while chording and in remote mode. Saved in input.gestures.">
        <Row label="Scroll sensitivity" keyName="gestures.scroll_sensitivity" help={HELP.scroll_sensitivity} stack>
          <RangeField path={G("scroll_sensitivity")} dflt={1} min={0.25} max={4} step={0.05} fmt={(v) => v.toFixed(2) + "×"} />
        </Row>
        <Row label="Reverse scroll direction" keyName="gestures.scroll_reverse" help={HELP.scroll_reverse}><Toggle path={G("scroll_reverse")} /></Row>
        <Row label="Zoom sensitivity" keyName="gestures.zoom_sensitivity" help={HELP.zoom_sensitivity} stack>
          <RangeField path={G("zoom_sensitivity")} dflt={1} min={0.25} max={4} step={0.05} fmt={(v) => v.toFixed(2) + "×"} />
        </Row>
        <Row label="Reverse pinch direction" keyName="gestures.zoom_reverse" help={HELP.zoom_reverse}><Toggle path={G("zoom_reverse")} /></Row>
        <GenericRows path={["input", "gestures"]} obj={asObj(inp.gestures)} known={KNOWN_GESTURES} />
      </Card>
      <Card title="Gestures in remote mode"
            hint="What the same gestures do while the pad drives the desktop. Saved as input.remote.same_gestures and input.remote.chords.">
        <Row label="Remote mode uses the same gestures" keyName="remote.same_gestures" help={HELP.same_gestures}>
          <Toggle path={["input", "remote", "same_gestures"]} dflt />
        </Row>
        <RemoteTable open={!same} id="remote-gestures" intro={<>
          <b>Gestures in remote mode need no chord button.</b> A two-finger slide, swipe, tap or pinch fires its action
          directly while the pad drives the desktop. The one-finger pointer, tap-to-click and the stick and trigger
          scroll are what remote mode is made of, so they are not bindings. Set a row to <b>none</b> and that gesture
          does nothing in remote mode.
        </>}>
          <GestureGrid rows={rows} actions={actions} table={table} />
        </RemoteTable>
      </Card>
    </div>
  );
}

/* ---- Remote mode --------------------------------------------------------- */
export function RemoteSection() {
  const cfg = useCfg();
  const inp = asObj(cfg.input);
  // The Steam Desktop Layout warning: shown the moment the switch flips ON,
  // dismissable, inline -- never an alert(). Doubled input from Steam's own
  // desktop translation is a real, observed confusion.
  const [warn, setWarn] = useState(false);
  const same = useSameBindings();
  const sameGestures = useSameGestures();
  const setTab = useStore((s) => s.setSettingsTab);
  return (
    <Card title="Remote mode" hint="Double-press the chord button and the pad drives the desktop: the touchpad and left stick move the pointer, and the game sees a still pad.">
      <Row label="Remote mode" keyName="remote.enabled" help={HELP.remote_enabled}>
        <Toggle path={["input", "remote", "enabled"]} onChange={(on) => on && setWarn(true)} />
      </Row>
      <AnimatePresence>
        {warn && (
          <Reveal>
            <div className="st-callout" style={{ "--tone": "var(--color-amber)" } as React.CSSProperties} role="note">
              <span className="st-callout-ico"><AlertTriangle size={17} {...IC} /></span>
              <div className="min-w-0 flex-1">
                <b>Check Steam first.</b> If Steam is running, its <b>Desktop Layout</b> probably already turns this pad
                into a mouse and keyboard whenever a window other than a game has focus. With remote mode on as well,
                you get doubled input: the pointer moves twice and keys fire twice. In Steam, set this controller's
                Desktop Layout to a plain gamepad, or turn Steam Input off for it.
              </div>
              <button type="button" className="btn flex-none !px-3 !py-1.5 !text-[13px]" onClick={() => setWarn(false)}>Got it</button>
            </div>
          </Reveal>
        )}
      </AnimatePresence>
      <Row label="Pointer speed" keyName="remote.mouse_speed" unit="×" help={HELP.mouse_speed}><NumberField path={["input", "remote", "mouse_speed"]} dflt={1.6} step={0.1} /></Row>
      <Row label="Scroll speed (stick and triggers)" keyName="remote.scroll_speed" unit="×" help={HELP.scroll_speed}><NumberField path={["input", "remote", "scroll_speed"]} dflt={1.0} step={0.1} /></Row>
      <Row label="Lightbar colour in remote mode" keyName="remote.lightbar_color" help={HELP.remote_lightbar}><ColorField path={["input", "remote", "lightbar_color"]} dflt={[255, 120, 0]} /></Row>
      <Row label="Remote mode uses the same bindings" keyName="remote.same_bindings" help={HELP.same_bindings}>
        <button type="button" className="btn !px-3 !py-1.5 !text-[13px]" onClick={() => setTab("chords")}>
          {same ? "Edit bindings" : "Edit remote bindings"}
        </button>
        <Toggle path={["input", "remote", "same_bindings"]} dflt />
      </Row>
      <Row label="Remote mode uses the same gestures" keyName="remote.same_gestures" help={HELP.same_gestures}>
        <button type="button" className="btn !px-3 !py-1.5 !text-[13px]" onClick={() => setTab("gestures")}>
          {sameGestures ? "Edit gestures" : "Edit remote gestures"}
        </button>
        <Toggle path={["input", "remote", "same_gestures"]} dflt />
      </Row>
      <GenericRows path={["input", "remote"]} obj={asObj(inp.remote)} known={KNOWN_REMOTE} />
    </Card>
  );
}

/* ---- Lightbar + battery -------------------------------------------------- */
export function LightbarSection() {
  const cfg = useCfg();
  const inp = asObj(cfg.input);
  return (
    <Card title="Lightbar saver" hint="Dim the lightbar after a while to make the battery last longer. The game's own colour is kept, just darker.">
      <Row label="Dim after" keyName="lightbar.dim_after_minutes" unit="min" help={HELP.dim_after_minutes}><NumberField path={["input", "lightbar", "dim_after_minutes"]} dflt={0} /></Row>
      <Row label="Dimmed brightness" keyName="lightbar.dim_level" help={HELP.dim_level} stack>
        <RangeField path={["input", "lightbar", "dim_level"]} dflt={0.3} min={0} max={1} step={0.05} fmt={(v) => Math.round(v * 100) + "%"} />
      </Row>
      <GenericRows path={["input", "lightbar"]} obj={asObj(inp.lightbar)} known={KNOWN_LIGHTBAR} />
    </Card>
  );
}

export function BatterySection() {
  const cfg = useCfg();
  const inp = asObj(cfg.input);
  const P = (k: string) => ["input", "battery", k];
  return (
    <Card title="Battery alerts" hint="Flash the lightbar when a pad's battery runs low, and faster when it is nearly empty.">
      <Row label="Flash the lightbar when low" keyName="battery.enabled" help={HELP.battery_enabled}><Toggle path={P("enabled")} dflt /></Row>
      <Row label="Low below" keyName="battery.low_percent" unit="%" help={HELP.low_percent}><NumberField path={P("low_percent")} dflt={20} /></Row>
      <Row label="Critical below" keyName="battery.critical_percent" unit="%" help={HELP.critical_percent}><NumberField path={P("critical_percent")} dflt={10} /></Row>
      <Row label="Low flash every" keyName="battery.low_interval_s" unit="s" help={HELP.low_interval_s}><NumberField path={P("low_interval_s")} dflt={30} /></Row>
      <Row label="Critical flash every" keyName="battery.critical_interval_s" unit="s" help={HELP.critical_interval_s}><NumberField path={P("critical_interval_s")} dflt={10} /></Row>
      <Row label="Low colour" keyName="battery.low_color"><ColorField path={P("low_color")} dflt={[255, 140, 0]} /></Row>
      <Row label="Critical colour" keyName="battery.critical_color"><ColorField path={P("critical_color")} dflt={[255, 0, 0]} /></Row>
      <Row label="Blinks per low flash" keyName="battery.low_blinks" help={HELP.low_blinks}><NumberField path={P("low_blinks")} dflt={2} /></Row>
      <Row label="Blinks per critical flash" keyName="battery.critical_blinks" help={HELP.critical_blinks}><NumberField path={P("critical_blinks")} dflt={3} /></Row>
      <GenericRows path={["input", "battery"]} obj={asObj(inp.battery)} known={KNOWN_BATTERY} />
    </Card>
  );
}

/* ---- Per-controller ------------------------------------------------------ */
export function ControllersSection() {
  const cfg = useCfg();
  const controllers = asObj(cfg.controllers);
  const serials = Object.keys(controllers).sort();
  if (!serials.length) return <Card title="Controllers" hint="No controller is set up yet. Pair a DualSense and it shows up here.">{null}</Card>;
  return (
    <Card title="Controllers" hint="Every pad the tray has seen, by serial number. A label replaces the serial on the dashboard and in the tray.">
      <div className="grid gap-6 pt-3 lg:grid-cols-2">
        {serials.map((serial) => (
          <Card key={serial} title={(typeof asObj(controllers[serial]).label === "string" && String(asObj(controllers[serial]).label).trim()) || "Controller"}
                tag={serial} className="st-boxed">
            {CTRL_FIELDS.map(([key, label, kind, choices]) => (
              <Row key={key} label={label}>
                {kind === "bool" ? <Toggle path={["controllers", serial, key]} />
                  : kind === "number" ? <NumberField path={["controllers", serial, key]} dflt={null} nullable width={130} />
                  : kind === "choice" ? <SelectField path={["controllers", serial, key]} dflt={choices![0]} width={150}
                                                     options={choices!.map((c) => ({ value: c, label: c }))} />
                  : <TextField path={["controllers", serial, key]} width={180} />}
              </Row>
            ))}
            <GenericRows path={["controllers", serial]} obj={asObj(controllers[serial])} known={KNOWN_CTRL} />
          </Card>
        ))}
      </div>
    </Card>
  );
}

/* /api/actions unreachable: the whole input section, generically. */
export function GenericInputSection() {
  const cfg = useCfg();
  return (
    <Card title="Input settings" hint="The list of actions could not be loaded, so these settings are shown as they are stored.">
      <GenericRows path={["input"]} obj={asObj(cfg.input)} />
    </Card>
  );
}
