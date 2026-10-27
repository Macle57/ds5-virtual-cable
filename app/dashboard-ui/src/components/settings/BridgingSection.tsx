import { AnimatePresence } from "framer-motion";
import { AlertTriangle, Download, Eye, EyeOff, FolderOpen, Loader2, RefreshCw, Search, ShieldCheck, ShieldOff } from "lucide-react";
import { ACTION_LABEL, getPath, isConnected, short, useStore } from "../../lib/store";
import type { ActionOp, ConfigDoc, Controller, Json } from "../../lib/types";
import { HELP } from "./help";
import { Card, NumberField, Reveal, Row, Toggle } from "./controls";

const asObj = (v: unknown): Record<string, Json> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, Json>) : {};
const IC = { strokeWidth: 2.5 };

const NO_ACTION_API = "This needs the 1.0 tray: this backend has no /api/action. The tray menu still works.";

/* The tray menu, on the page (CONTRACT section 5): the switches are config
   keys the tray applies the moment they are saved; the buttons POST
   /api/action and act at once. An older backend (no /api/action, no
   `update` / `autostart` in the state) greys the buttons out with a tooltip
   and keeps the switches, which have always been config. */
export default function BridgingSection() {
  const cfg = useStore((s) => s.cfg as ConfigDoc);
  const live = useStore((s) => s.state.controllers);
  const master = useStore((s) => s.cfg?.enabled !== false);
  const autostart = useStore((s) => s.state.autostart);
  const linger = useStore((s) => getPath(s.cfg, ["disconnect_linger", "enabled"]) === true);
  const serials = [...new Set([...Object.keys(live), ...Object.keys(asObj(cfg.controllers))])].sort();
  const mode = autostart?.mode;
  const modeNote = mode === "service" ? "as a service, before sign-in"
    : mode === "task" ? "at sign-in, as a scheduled task"
    : mode === "none" ? "not set up by the installer" : undefined;

  return (
    <Card title="Bridging & tray"
          hint="The same switches as the tray menu. Switches apply when you save; the buttons act right away.">
      <Row label="Bridging on (master switch)" keyName="enabled" root="" help={HELP.master_enabled}>
        <Toggle path={["enabled"]} dflt />
      </Row>

      {/* per pad: bridge + hide, with the hide's EFFECTIVE state */}
      <div className={"st-row transition-opacity " + (master ? "" : "opacity-50")}>
        <div className="st-sub mt-0">
          <span className="st-sub-title">Your controllers</span>
          <span className="st-sub-hint">Bridge each pad, and hide its Bluetooth twin from games.</span>
        </div>
        {serials.length === 0
          ? <div className="st-empty-card !mt-0 !py-6 st-small">No controller yet. Pair a DualSense and it shows up here.</div>
          : serials.map((serial, i) => <PadRow key={serial} index={i + 1} serial={serial} c={live[serial]} />)}
      </div>

      <Row label="Bridge new controllers automatically" keyName="auto_bridge_new" root="" help={HELP.auto_bridge_new}>
        <Toggle path={["auto_bridge_new"]} dflt />
      </Row>
      <Row label="Hide a new controller's Bluetooth pad by default" keyName="hide_bluetooth_default" root="" help={HELP.hide_bluetooth_default}>
        <Toggle path={["hide_bluetooth_default"]} />
      </Row>
      <Row label="Keep the controller connected after it disconnects" keyName="disconnect_linger.enabled" root="" help={HELP.disconnect_linger}>
        <Toggle path={["disconnect_linger", "enabled"]} />
      </Row>
      {/* Reveal is the .st-rows child that wears the rule: give it the rows' column width */}
      <AnimatePresence initial={false}>
        {linger && (
          <Reveal className="max-w-[780px]">
            <Row label="Wait for it to come back for" keyName="disconnect_linger.seconds" root="" unit="s" help={HELP.disconnect_linger_seconds}>
              <NumberField path={["disconnect_linger", "seconds"]} dflt={120} min={5} max={3600} step={5} />
            </Row>
          </Reveal>
        )}
      </AnimatePresence>
      <Row label={"Start with Windows" + (modeNote ? ` (${modeNote})` : "")} keyName="autostart_on_login" root="" help={HELP.autostart_on_login}>
        {mode && mode !== "none" && (
          <span className="st-sticker" style={{ "--tone": "var(--color-cross)" } as React.CSSProperties}>{mode}</span>
        )}
        <Toggle path={["autostart_on_login"]} />
      </Row>
      <Row label="Check for updates" keyName="update_check" root="" help={HELP.update_check}>
        <Toggle path={["update_check"]} dflt />
      </Row>
      <Row label="Ask to install updates" keyName="update_auto_install" root="" help={HELP.update_auto_install}>
        <Toggle path={["update_auto_install"]} dflt />
      </Row>
      <UpdateRow />
      <TrayActions />
    </Card>
  );
}

/* ---- one controller ------------------------------------------------------- */

const PAD_TONES = ["var(--color-amber)", "var(--color-cyan)", "var(--color-square)", "var(--color-triangle)"];

function PadRow({ index, serial, c }: { index: number; serial: string; c: Controller | undefined }) {
  const label = useStore((s) => getPath(s.cfg, ["controllers", serial, "label"]));
  const hideRequested = useStore((s) => {
    const v = getPath(s.cfg, ["controllers", serial, "hide_bluetooth"]);
    return typeof v === "boolean" ? v : c?.hide_bluetooth === true;
  });
  const name = (typeof label === "string" && label.trim()) || c?.label || short(serial);
  const on = isConnected(c);
  const tone = !c || c.present === false ? "var(--color-idle)" : on ? "var(--color-ok)"
    : c.state === "controller offline" ? "var(--color-warn)" : c.error ? "var(--color-bad)" : "var(--color-idle)";
  const status = !c ? "not connected" : c.present === false ? "disconnected"
    : c.linger_left_s != null ? `waiting to reconnect, ${c.linger_left_s} s left` : c.state ?? "telemetry";
  return (
    <div className="st-padrow">
      <span className="st-pnum" style={{ background: PAD_TONES[(index - 1) % PAD_TONES.length], color: "var(--color-ink-on)" }}>P{index}</span>
      <span className="min-w-0 flex-1">
        <span className="st-padname block truncate">{name}</span>
        <span className="st-mini flex flex-wrap items-center gap-x-2">
          <span className="flex items-center gap-1.5">
            <span className={"st-dot" + (on ? " pulse" : "")} style={{ background: tone }} />{status}
          </span>
          {name !== short(serial) && <span className="mono text-ink-3" title={serial}>{short(serial)}</span>}
        </span>
      </span>
      <HidePill c={c} requested={hideRequested} />
      <span className="flex items-center gap-2" title={HELP.ctrl_enabled}>
        <span className="st-mini">Bridge</span>
        <Toggle path={["controllers", serial, "enabled"]} dflt={c?.enabled !== false} />
      </span>
      <span className="flex items-center gap-2" title={HELP.ctrl_hide}>
        <span className="st-mini">Hide</span>
        <Toggle path={["controllers", serial, "hide_bluetooth"]} dflt={c?.hide_bluetooth === true} />
      </span>
    </div>
  );
}

/* hide_bluetooth is what was ASKED; hide_effective is whether HidHide's
   filter is verified on the pad's HID device (null = nobody checked). */
function HidePill({ c, requested }: { c: Controller | undefined; requested: boolean }) {
  let text: string, tone: string, Icon = EyeOff, title: string | undefined;
  if (!c || c.present === false) { text = requested ? "hide on return" : "visible on return"; tone = "var(--color-idle)"; Icon = requested ? EyeOff : Eye; }
  else if (c.hide_effective === true) { text = "hidden"; tone = "var(--color-square)"; Icon = ShieldCheck; title = "HidHide's filter is verified on this pad"; }
  else if (c.hide_effective === false && requested) { text = "not hidden"; tone = "var(--color-warn)"; Icon = ShieldOff; title = c.hide_note?.trim() || "Hiding was asked for, but other apps can still see the controller"; }
  else if (requested) { text = "unverified"; tone = "var(--color-idle)"; Icon = EyeOff; title = "Hiding was asked for; HidHide has not been checked yet"; }
  else { text = "visible"; tone = "var(--color-line-2)"; Icon = Eye; title = "Other apps see the Bluetooth pad as well as the bridged one"; }
  return (
    <span className="st-sticker" style={{ "--tone": tone } as React.CSSProperties} title={title}>
      <Icon size={13} {...IC} />{text}
    </span>
  );
}

/* ---- the updater ----------------------------------------------------------- */

const ago = (ts: number) => {
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 90 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 * 2 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

function UpdateRow() {
  const update = useStore((s) => s.state.update);
  const actionApi = useStore((s) => s.actionApi);
  const busyOp = useStore((s) => s.busyOp);
  const dead = actionApi === "missing";
  const installing = update?.installing === true || busyOp === "update_install";
  const available = update?.available || null;
  const status = installing ? "Installing…"
    : update?.error ? update.error
    : available ? `Version ${available} is available.`
    : update ? (update.checked_at ? `Up to date. Checked ${ago(update.checked_at)}.` : "Not checked yet.")
    : dead ? "The tray checks on its own; this page cannot ask it (0.5 tray)." : "Waiting for the tray…";
  return (
    <div className="st-row">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5">
        <div className="min-w-0 flex-1" style={{ flexBasis: 220 }}>
          <label className="st-row-label">Updates</label>
          <div className={"mt-0.5 text-[13px] " + (update?.error ? "font-semibold text-bad-ink" : available ? "font-semibold text-triangle" : "text-ink-2")}>
            {status}
            {available && update?.url && (
              <> <a className="st-inline-link" href={update.url} target="_blank" rel="noreferrer">Read the release notes</a></>
            )}
          </div>
        </div>
        <ActionButton op="update_check" icon={<RefreshCw size={15} {...IC} />} label="Check now" />
        {(available || installing) && (
          <ActionButton op="update_install" icon={<Download size={15} {...IC} />} label={installing ? "Installing…" : "Install update"} primary
                        disabled={installing} />
        )}
      </div>
      {installing && (
        <div className="st-note flex items-center gap-2">
          <Loader2 size={15} {...IC} className="flex-none animate-spin" /> The installer runs silently and the tray restarts itself. This page reconnects on its own.
        </div>
      )}
    </div>
  );
}

/* ---- rescan / unhide all / open logs ---------------------------------------- */

function TrayActions() {
  const actionApi = useStore((s) => s.actionApi);
  return (
    <Row label="Tray actions" help={HELP.tray_actions} stack>
      <div className="flex flex-wrap items-center gap-3">
        <ActionButton op="rescan" icon={<Search size={15} {...IC} />} label="Rescan controllers" />
        <ActionButton op="unhide_all" icon={<Eye size={15} {...IC} />} label="Unhide all" />
        <ActionButton op="open_logs" icon={<FolderOpen size={15} {...IC} />} label="Open logs" />
        {actionApi === "missing" && (
          <span className="st-problem"><AlertTriangle size={15} {...IC} /> {NO_ACTION_API}</span>
        )}
      </div>
    </Row>
  );
}

function ActionButton({ op, icon, label, primary, disabled }:
  { op: ActionOp; icon: React.ReactNode; label: string; primary?: boolean; disabled?: boolean }) {
  const actionApi = useStore((s) => s.actionApi);
  const busyOp = useStore((s) => s.busyOp);
  const runAction = useStore((s) => s.runAction);
  const dead = actionApi === "missing";
  const busy = busyOp === op;
  return (
    <button type="button" className={"btn !px-3.5 !py-2 !text-[14px]" + (primary ? " btn-primary" : "")}
            disabled={dead || disabled || busyOp !== null} title={dead ? NO_ACTION_API : ACTION_LABEL[op]}
            aria-label={label} onClick={() => void runAction(op)}>
      {busy ? <Loader2 size={15} {...IC} className="animate-spin" /> : icon} {label}
    </button>
  );
}
