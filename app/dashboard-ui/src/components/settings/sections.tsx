import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, ArrowRight, Hand, MousePointer2 } from "lucide-react";
import { getPath, setPath, useRemoteColor, useStore } from "../../lib/store";
import type { ActionsMeta, ConfigDoc, GestureRow, Json } from "../../lib/types";
import {
  ADVANCED_FIELDS, CTRL_FIELDS, GESTURE_FALLBACK, GESTURE_GROUPS, HELP, KNOWN_BATTERY, KNOWN_CTRL, KNOWN_GLOBAL, KNOWN_INPUT,
  KNOWN_LIGHTBAR, KNOWN_REMOTE, isGestureKey, keyLabel,
} from "./help";
import { Card, ColorField, JsonField, NumberField, RangeField, Row, SelectField, TextField, Toggle } from "./controls";
import GenericRows from "./GenericRows";
import ButtonGlyph from "./ButtonGlyph";
import ActionPickerUI from "./ActionPicker";
import BridgingSection from "./BridgingSection";
export { MacrosSection } from "./MacrosSection";
export { default as NotificationsSection } from "./NotificationsSection";

const useCfg = () => useStore((s) => s.cfg as ConfigDoc);
const asObj = (v: unknown): Record<string, Json> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, Json>) : {};

/* ---- General: the tray's switches, then the rarely-touched rest ---------- */
export function GeneralSection() {
  const cfg = useCfg();
  return (
    <div className="grid gap-4">
      <BridgingSection />
      <Card title="Advanced" hint="Ports and paths. These apply at the next start of the tray.">
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
    <Card title="Shortcuts — general">
      <Row label="Shortcut engine" keyName="enabled" help={HELP.enabled}><Toggle path={["input", "enabled"]} dflt /></Row>
      <Row label="Chord button (hold to arm)" keyName="chord_button" help={HELP.chord_button}>
        <SelectField path={["input", "chord_button"]} dflt="ps" width={170}
                     options={actions.chord_buttons.map((b) => ({ value: b, label: keyLabel(b) }))} />
      </Row>
      <Row label="Double-press window" keyName="double_press_ms" unit="ms" help={HELP.double_press_ms}><NumberField path={["input", "double_press_ms"]} dflt={400} /></Row>
      <Row label="Replayed tap hold" keyName="tap_replay_ms" unit="ms" help={HELP.tap_replay_ms}><NumberField path={["input", "tap_replay_ms"]} dflt={100} /></Row>
      <Row label="Held-chord repeat" keyName="repeat_ms" unit="ms" help={HELP.repeat_ms}><NumberField path={["input", "repeat_ms"]} dflt={150} /></Row>
      <Row label="Idle power-off" keyName="off_timer_minutes" unit="min" help={HELP.off_timer_minutes}><NumberField path={["input", "off_timer_minutes"]} dflt={15} /></Row>
      <Row label="Rumble on chord accept" keyName="haptic_ack" help={HELP.haptic_ack}><Toggle path={["input", "haptic_ack"]} dflt /></Row>
      <Row label="Rumble strength" keyName="haptic_strength" help={HELP.haptic_strength} stack>
        <RangeField path={["input", "haptic_strength"]} dflt={25} min={0} max={100} step={1} fmt={(v) => v + "%"} />
      </Row>
      <Row label="Stick moves mouse while chording" keyName="stick_mouse_in_chord" help={HELP.stick_mouse_in_chord}><Toggle path={["input", "stick_mouse_in_chord"]} dflt /></Row>
      {inp.actions && Object.keys(asObj(inp.actions)).length > 0 && (
        <Row label="actions (per-action parameters)" stack><JsonField path={["input", "actions"]} /></Row>
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
   next load). */
function ActionPicker({ chordKey, actions, table }: { chordKey: string; actions: ActionsMeta; table: Table }) {
  const bound = useStore((s) => getPath(s.cfg, [...table.path, chordKey]));
  const macros = useStore((s) => getPath(s.cfg, ["input", "macros"]));
  const patch = useStore((s) => s.patchConfig);
  return (
    <ActionPickerUI value={boundName(bound, table.defaults[chordKey])} meta={actions} macros={macros}
                    onChange={(name) => patch((cfg) => setPath(cfg, [...table.path, chordKey], name))} />
  );
}

function BindingCard({ chordKey, actions, help, label, table = CHORD_TABLE }:
  { chordKey: string; actions: ActionsMeta; help?: string; label?: string; table?: Table }) {
  const bound = useStore((s) => getPath(s.cfg, [...table.path, chordKey]));
  const isBound = boundName(bound, table.defaults[chordKey]) !== "none";
  const accent = table.accent;
  return (
    <motion.div layout className={"rounded-xl border p-3 transition-colors " +
        (!isBound ? "border-line bg-well/50" : accent ? "" : "border-cross/40 bg-cross/[.06]")}
        style={isBound && accent ? { borderColor: `color-mix(in oklab, ${accent} 40%, transparent)`,
                                     background: `color-mix(in oklab, ${accent} 6%, transparent)` } : undefined}>
      <div className="mb-2 flex items-center gap-2.5">
        <ButtonGlyph name={chordKey} />
        <div className="min-w-0 leading-tight">
          <div className="display text-[14px]">{label ?? keyLabel(chordKey)}</div>
          <div className="mono text-[10.5px] text-ink-3">{chordKey}</div>
        </div>
      </div>
      <ActionPicker chordKey={chordKey} actions={actions} table={table} />
      {help && <p className="mt-2 text-[11.5px] leading-relaxed text-ink-3">{help}</p>}
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
    <div className="grid gap-4">
      <Card title={`Chords — hold ${keyLabel(chordButton)} + …`} hint={HELP.chords}>
        <div className="grid gap-3 pt-2 sm:grid-cols-2 xl:grid-cols-3">
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
        <motion.div key={id} initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22 }} className="overflow-hidden !border-t-0">
          <div className="my-2 flex items-start gap-3 rounded-xl border px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2"
               style={{ borderColor: `color-mix(in oklab, ${color} 55%, transparent)`, background: `color-mix(in oklab, ${color} 8%, transparent)` }}>
            <MousePointer2 size={18} className="mt-0.5 flex-none" style={{ color }} />
            <div>{intro}</div>
          </div>
          {children}
        </motion.div>
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
  const color = useRemoteColor();
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
    <Card title="Remote mode bindings"
          hint="What a button does while the pad is in remote mode (double-press the chord button). Saved as input.remote.same_bindings and input.remote.chords.">
      <Row label="Remote mode uses the same bindings" keyName="remote.same_bindings" help={HELP.same_bindings}>
        <Toggle path={["input", "remote", "same_bindings"]} dflt />
      </Row>
      <RemoteTable open={!same} id="remote-table" intro={<>
        <b style={{ color }}>No chord button in remote mode.</b> Nothing reaches the game while remote mode is on, so
        each button below fires its action <b className="text-ink">directly, on its own</b> — no button held
        first. This table is the whole truth: the defaults spell out the classic remote map (Cross clicks, d-pad
        arrows, Circle is Esc, Options is Enter), so a row set to <b className="text-ink">none</b> makes that button do
        nothing in remote mode. Only the touchpad and stick pointer controls are fixed.
      </>}>
        <div className="grid gap-3 pb-2 pt-1 sm:grid-cols-2 xl:grid-cols-3">
          {keys.map((k) => <BindingCard key={k} chordKey={k} actions={actions} table={table} />)}
        </div>
      </RemoteTable>
      <button type="button" onClick={() => setTab("gestures")}
              className="flex items-center gap-1.5 py-2.5 text-[12.5px] text-ink-2 hover:text-cross">
        <Hand size={13} /> Touchpad gestures {sameGestures ? "follow the Gestures tab" : "have their own remote table"} — edit them there <ArrowRight size={13} />
      </button>
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
  const groups = [...GESTURE_GROUPS, { id: "other", title: "Other gestures", hint: "keys from a newer build" }];
  return (
    <div className="grid gap-4 pb-2 pt-1">
      {groups.map((g) => {
        const items = rows.filter((r) => (GESTURE_GROUPS.some((x) => x.id === r.group) ? r.group : "other") === g.id);
        if (!items.length) return null;
        return (
          <div key={g.id}>
            <div className="mb-2 flex items-center gap-2">
              <span className="eyebrow text-cyan">{g.title}</span>
              <span className="text-[11px] text-ink-3">{g.hint}</span>
              <span className="h-px flex-1 bg-line" />
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {items.map((r) => <BindingCard key={r.key} chordKey={r.key} actions={actions} table={table}
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
  const color = useRemoteColor();
  const table = useRemoteTable(actions);
  const rows = gestureRows(actions, asObj(inp.chords), asObj(asObj(inp.remote).chords));
  return (
    <div className="grid gap-4">
      <Card title="Touch gestures — while chording"
            hint="Two-finger touchpad gestures, active while the chord button is held. Saved in input.chords.">
        <GestureGrid rows={rows} actions={actions} />
      </Card>
      <Card title="Remote mode gestures"
            hint="What the same gestures do while the pad is in remote mode. Saved as input.remote.same_gestures and input.remote.chords.">
        <Row label="Remote mode uses the same gestures" keyName="remote.same_gestures" help={HELP.same_gestures}>
          <Toggle path={["input", "remote", "same_gestures"]} dflt />
        </Row>
        <RemoteTable open={!same} id="remote-gestures" intro={<>
          <b style={{ color }}>Gestures in remote mode need no chord button.</b> A two-finger slide, swipe, tap or pinch
          fires its action <b className="text-ink">directly</b> while the pad drives the OS. The one-finger pointer, tap-to-click
          and the stick / trigger scroll are what remote mode <i>is</i> and are not bindings. A row set
          to <b className="text-ink">none</b> makes that gesture do nothing in remote mode.
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
    <Card title="Remote mode">
      <Row label="Remote mode" keyName="remote.enabled" help={HELP.remote_enabled}>
        <Toggle path={["input", "remote", "enabled"]} onChange={(on) => on && setWarn(true)} />
      </Row>
      <AnimatePresence>
        {warn && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }}
                      className="overflow-hidden !border-t-0">
            <div className="my-2 flex items-start gap-3 rounded-xl border border-warn/70 bg-warn/10 px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2">
              <AlertTriangle size={18} className="mt-0.5 flex-none text-warn" />
              <div>
                <b className="text-warn">Check Steam first.</b> If Steam is running, Steam Input's <b className="text-ink">Desktop Layout</b> is
                probably already translating this pad to mouse/keyboard whenever a non-game window has focus — with remote
                mode on top you get doubled input (the pointer moving twice, ghost keys). In Steam, set this controller's
                Desktop Layout to a plain gamepad, or disable Steam Input for it.
              </div>
              <button type="button" className="btn flex-none !px-3 !py-1 !text-[12px]" onClick={() => setWarn(false)}>Got it</button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      <Row label="Pointer speed" keyName="remote.mouse_speed" unit="×" help={HELP.mouse_speed}><NumberField path={["input", "remote", "mouse_speed"]} dflt={1.6} step={0.1} /></Row>
      <Row label="Scroll speed" keyName="remote.scroll_speed" unit="×" help={HELP.scroll_speed}><NumberField path={["input", "remote", "scroll_speed"]} dflt={1.0} step={0.1} /></Row>
      <Row label="Lightbar colour in remote mode" keyName="remote.lightbar_color" help={HELP.remote_lightbar}><ColorField path={["input", "remote", "lightbar_color"]} dflt={[255, 120, 0]} /></Row>
      <Row label="Remote mode uses the same bindings" keyName="remote.same_bindings" help={HELP.same_bindings}>
        <button type="button" className="btn !px-3 !py-1 !text-[12px]" onClick={() => setTab("chords")}>
          {same ? "Bindings" : "Remote bindings"} <ArrowRight size={13} />
        </button>
        <Toggle path={["input", "remote", "same_bindings"]} dflt />
      </Row>
      <Row label="Remote mode uses the same gestures" keyName="remote.same_gestures" help={HELP.same_gestures}>
        <button type="button" className="btn !px-3 !py-1 !text-[12px]" onClick={() => setTab("gestures")}>
          {sameGestures ? "Gestures" : "Remote gestures"} <ArrowRight size={13} />
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
    <Card title="Lightbar saver">
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
    <Card title="Battery alerts">
      <Row label="Flash lightbar when low" keyName="battery.enabled" help={HELP.battery_enabled}><Toggle path={P("enabled")} dflt /></Row>
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
  if (!serials.length) return <Card title="Controllers" hint="No controller has been configured yet." >{null}</Card>;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {serials.map((serial) => (
        <Card key={serial} title="Controller" tag={serial}>
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
  );
}

/* /api/actions unreachable: the whole input section, generically. */
export function GenericInputSection() {
  const cfg = useCfg();
  return (
    <Card title="input" hint="The action registry could not be loaded, so this section is shown as raw settings.">
      <GenericRows path={["input"]} obj={asObj(cfg.input)} />
    </Card>
  );
}
