import { AlertTriangle, Download, Eye, EyeOff, FolderOpen, Loader2, RefreshCw, Search, ShieldCheck, ShieldOff } from "lucide-react";
import { ACTION_LABEL, getPath, isConnected, short, useStore } from "../../lib/store";
import type { ActionOp, ConfigDoc, Controller, Json } from "../../lib/types";
import { HELP } from "./help";
import { Card, Row, Toggle } from "./controls";

const asObj = (v: unknown): Record<string, Json> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, Json>) : {};

const NO_ACTION_API = "Needs the 1.0 tray: this backend has no /api/action (the tray menu still works)";

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
  const serials = [...new Set([...Object.keys(live), ...Object.keys(asObj(cfg.controllers))])].sort();
  const mode = autostart?.mode;
  const modeNote = mode === "service" ? "as a service, before sign-in"
    : mode === "task" ? "at sign-in (scheduled task)"
    : mode === "none" ? "not installed by the installer" : undefined;

  return (
    <Card title="Bridging & tray"
          hint="The same switches as the tray menu. Switches apply on Save; the buttons act right away.">
      <Row label="Bridging enabled (master switch)" keyName="enabled" root="" help={HELP.master_enabled}>
        <Toggle path={["enabled"]} dflt />
      </Row>

      {/* per pad: bridge + hide, with the hide's EFFECTIVE state */}
      <div className={"py-2.5 transition-opacity " + (master ? "" : "opacity-50")}>
        <div className="mb-1.5 flex items-center gap-2">
          <span className="eyebrow">controllers</span>
          <span className="h-px flex-1 bg-line" />
          <span className="eyebrow !normal-case !tracking-normal text-[11px]">bridge · hide BT pad</span>
        </div>
        {serials.length === 0
          ? <div className="rounded-xl border border-dashed border-line-2 px-3 py-4 text-center text-[12.5px] text-ink-3">No controller yet — pair a DualSense and it appears here.</div>
          : serials.map((serial, i) => <PadRow key={serial} index={i + 1} serial={serial} c={live[serial]} />)}
      </div>

      <Row label="Bridge new controllers automatically" keyName="auto_bridge_new" root="" help={HELP.auto_bridge_new}>
        <Toggle path={["auto_bridge_new"]} dflt />
      </Row>
      <Row label="Hide new controllers' Bluetooth pad by default" keyName="hide_bluetooth_default" root="" help={HELP.hide_bluetooth_default}>
        <Toggle path={["hide_bluetooth_default"]} />
      </Row>
      <Row label={"Start with Windows" + (modeNote ? ` — ${modeNote}` : "")} keyName="autostart_on_login" root="" help={HELP.autostart_on_login}>
        {mode && mode !== "none" && (
          <span className="display rounded-md border border-line bg-well px-1.5 py-0.5 text-[10.5px] uppercase tracking-[.12em] text-ink-2">{mode}</span>
        )}
        <Toggle path={["autostart_on_login"]} />
      </Row>
      <Row label="Check for updates" keyName="update_check" root="" help={HELP.update_check}>
        <Toggle path={["update_check"]} dflt />
      </Row>
      <Row label="Install updates automatically" keyName="update_auto_install" root="" help={HELP.update_auto_install}>
        <Toggle path={["update_auto_install"]} dflt />
      </Row>
      <UpdateRow />
      <TrayActions />
    </Card>
  );
}

/* ---- one controller ------------------------------------------------------- */

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
  const status = !c ? "not connected" : c.present === false ? "disconnected" : c.state ?? "telemetry";
  return (
    <div className="mb-2 flex flex-wrap items-center gap-3 rounded-xl border border-line bg-well/50 px-3 py-2 last:mb-0">
      <span className="display grid h-7 w-7 flex-none place-items-center rounded-md bg-panel-2 text-[12.5px] text-ink-2">P{index}</span>
      <span className="min-w-0 flex-1 leading-tight">
        <span className="display block truncate text-[14px]">{name}</span>
        <span className="mono flex items-center gap-1.5 text-[10.5px] text-ink-3">
          <span className={"inline-block h-1.5 w-1.5 rounded-full" + (on ? " pulse" : "")} style={{ background: tone, color: tone }} />
          {short(serial)} · {status}
        </span>
      </span>
      <HidePill c={c} requested={hideRequested} />
      <span className="flex items-center gap-2" title={HELP.ctrl_enabled}>
        <span className="text-[11px] text-ink-3">bridge</span>
        <Toggle path={["controllers", serial, "enabled"]} dflt={c?.enabled !== false} />
      </span>
      <span className="flex items-center gap-2" title={HELP.ctrl_hide}>
        <span className="text-[11px] text-ink-3">hide</span>
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
  else if (c.hide_effective === false && requested) { text = "NOT hidden"; tone = "var(--color-warn)"; Icon = ShieldOff; title = c.hide_note?.trim() || "Hidden requested, but the controller is still visible to other apps"; }
  else if (requested) { text = "unverified"; tone = "var(--color-idle)"; Icon = EyeOff; title = "hide requested; HidHide has not been checked yet"; }
  else { text = "visible"; tone = "var(--color-ink-3)"; Icon = Eye; title = "other apps see the Bluetooth pad as well as the bridged one"; }
  return (
    <span className="display inline-flex items-center gap-1.5 rounded-lg border px-2 py-1 text-[11px] uppercase tracking-[.08em]"
          style={{ borderColor: tone, color: tone, background: `color-mix(in oklab, ${tone} 12%, transparent)` }} title={title}>
      <Icon size={12} />{text}
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
  const status = installing ? "installing…"
    : update?.error ? update.error
    : available ? `version ${available} is available`
    : update ? (update.checked_at ? `up to date · checked ${ago(update.checked_at)}` : "not checked yet")
    : dead ? "the tray checks on its own; this page cannot ask it (0.5 tray)" : "waiting for the tray…";
  return (
    <div className="py-2.5">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <label className="text-[13.5px] text-ink-2">Updates</label>
          <div className={"mt-0.5 text-[12px] " + (update?.error ? "text-bad" : available ? "text-triangle" : "text-ink-3")}>
            {status}
            {available && update?.url && (
              <> · <a className="text-cross hover:underline" href={update.url} target="_blank" rel="noreferrer">release notes</a></>
            )}
          </div>
        </div>
        <ActionButton op="update_check" icon={<RefreshCw size={14} />} label="Check now" />
        {(available || installing) && (
          <ActionButton op="update_install" icon={<Download size={14} />} label={installing ? "Installing…" : "Install update"} primary
                        disabled={installing} />
        )}
      </div>
      {installing && (
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-line bg-well px-3 py-2 text-[12px] text-ink-2">
          <Loader2 size={14} className="animate-spin text-cross" /> The installer runs silently and the tray restarts itself; this page reconnects on its own.
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
      <div className="flex flex-wrap items-center gap-2">
        <ActionButton op="rescan" icon={<Search size={14} />} label="Rescan controllers" />
        <ActionButton op="unhide_all" icon={<Eye size={14} />} label="Unhide all" />
        <ActionButton op="open_logs" icon={<FolderOpen size={14} />} label="Open logs" />
        {actionApi === "missing" && (
          <span className="flex items-center gap-1.5 text-[12px] text-amber"><AlertTriangle size={13} /> {NO_ACTION_API}</span>
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
    <button type="button" className={"btn !px-3 !py-1.5 !text-[12.5px]" + (primary ? " btn-primary" : "")}
            disabled={dead || disabled || busyOp !== null} title={dead ? NO_ACTION_API : ACTION_LABEL[op]}
            aria-label={label} onClick={() => void runAction(op)}>
      {busy ? <Loader2 size={14} className="animate-spin" /> : icon} {label}
    </button>
  );
}
