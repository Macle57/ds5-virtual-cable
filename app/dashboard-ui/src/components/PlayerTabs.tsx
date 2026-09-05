import { motion } from "framer-motion";
import { BatteryMedium, Keyboard, MousePointer2 } from "lucide-react";
import { keyboardOpen, remoteMode, short, useRemoteColor, useStore } from "../lib/store";
import type { Controller } from "../lib/types";

/* One "player card" per controller, PS-style P1/P2 slots. */
export default function PlayerTabs() {
  const controllers = useStore((s) => s.state.controllers);
  const active = useStore((s) => s.active);
  const setActive = useStore((s) => s.setActive);
  const serials = Object.keys(controllers).sort();
  if (!serials.length) return null;

  return (
    <div className="mb-4 flex flex-wrap gap-2.5" role="tablist">
      {serials.map((serial, i) => (
        <PlayerTab key={serial} index={i + 1} serial={serial} c={controllers[serial]}
                   active={serial === active} onSelect={() => setActive(serial)} />
      ))}
    </div>
  );
}

function tone(c: Controller) {
  if (c.state === "running") return "ok";
  if (c.state === "controller offline") return "warn";
  if (c.error) return "bad";
  return "idle";
}

const TONE_CLASS = { ok: "bg-ok text-ok", warn: "bg-warn text-warn", bad: "bg-bad text-bad", idle: "bg-idle text-idle" };

function PlayerTab({ index, serial, c, active, onSelect }:
  { index: number; serial: string; c: Controller; active: boolean; onSelect: () => void }) {
  const t = tone(c);
  const pct = c.telemetry?.decoded?.battery_percent ?? c.battery_percent;
  const remote = remoteMode(c) === true;
  const rc = useRemoteColor();
  return (
    <button role="tab" aria-selected={active} onClick={onSelect}
      className={
        "relative flex items-center gap-3 rounded-xl border px-3.5 py-2 text-left transition-colors " +
        (active ? "border-cross/70 bg-cross/10" : "border-line bg-panel/60 hover:border-line-2")
      }>
      {active && (
        <motion.span layoutId="tab-glow" transition={{ type: "spring", stiffness: 500, damping: 40 }}
          className="pointer-events-none absolute inset-0 rounded-xl glow-cross" />
      )}
      <span className={"display grid h-8 w-8 place-items-center rounded-lg text-[14px] " +
        (active ? "bg-cross text-white" : "bg-panel-2 text-ink-2")}>
        P{index}
      </span>
      <span className="leading-tight">
        <span className="display block text-[15px]">
          {c.label || short(serial)}
        </span>
        <span className="mono block text-[10.5px] text-ink-3">{c.label ? short(serial) : c.state ?? "telemetry"}</span>
      </span>
      <span className="ml-1 flex items-center gap-2">
        {remote && (
          <span className="display inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10.5px] uppercase tracking-[.12em]"
                style={{ borderColor: rc, color: rc, background: `color-mix(in oklab, ${rc} 16%, transparent)` }}
                title="remote mode: this pad is driving the OS">
            {keyboardOpen(c) ? <Keyboard size={11} /> : <MousePointer2 size={11} />} remote
          </span>
        )}
        {pct != null && (
          <span className="flex items-center gap-1 text-[11px] text-ink-2">
            <BatteryMedium size={13} />
            <span className="num">{pct}%</span>
          </span>
        )}
        <span className={"h-2 w-2 rounded-full " + TONE_CLASS[t] + (t === "ok" ? " pulse" : "")} />
      </span>
    </button>
  );
}
