import { motion } from "framer-motion";
import { Keyboard, MousePointer2 } from "lucide-react";
import { keyboardOpen, remoteMode, short, useRemoteColor, useStore } from "../lib/store";
import type { Controller } from "../lib/types";
import { InkDot } from "./Header";

/* One paper tag per controller, clipped to the top of the page like index
   tabs in a notebook. The selected one is pulled forward (ink shadow, a
   yellow number, a marker stroke underneath) and sits straight; the others
   lean a little. */
export default function PlayerTabs() {
  const controllers = useStore((s) => s.state.controllers);
  const active = useStore((s) => s.active);
  const setActive = useStore((s) => s.setActive);
  const serials = Object.keys(controllers).sort();
  if (!serials.length) return null;

  return (
    <div className="pop mb-6 flex flex-wrap gap-3" role="tablist" aria-label="controllers" style={{ "--i": 0 } as React.CSSProperties}>
      {serials.map((serial, i) => (
        <PlayerTab key={serial} index={i + 1} serial={serial} c={controllers[serial]}
                   active={serial === active} onSelect={() => setActive(serial)} />
      ))}
    </div>
  );
}

/* `present: false` is the manager keeping a switched-off pad's entry so the
   tab survives (it comes back cloaked, so the config it wears matters);
   shown dimmed, as "disconnected". */
const gone = (c: Controller) => c.present === false;

function tone(c: Controller) {
  if (gone(c)) return "var(--color-idle)";
  if (c.state === "running") return "var(--color-ok)";
  if (c.state === "controller offline") return "var(--color-warn)";
  if (c.error) return "var(--color-bad)";
  return "var(--color-idle)";
}

function PlayerTab({ index, serial, c, active, onSelect }:
  { index: number; serial: string; c: Controller; active: boolean; onSelect: () => void }) {
  const off = gone(c);
  const pct = off ? null : c.telemetry?.decoded?.battery_percent ?? c.battery_percent;
  const remote = !off && remoteMode(c) === true;
  const rc = useRemoteColor();
  const sub = off ? "switched off" : c.label ? short(serial) : c.state ?? "telemetry";
  const lean = index % 2 ? -1.2 : 1;
  return (
    <motion.button role="tab" aria-selected={active} onClick={onSelect} data-present={off ? "false" : "true"}
      title={off ? "switched off or out of range — its settings still apply when it returns" : undefined}
      whileTap={{ scale: 0.96, rotate: -1 }}
      animate={{ rotate: active ? 0 : lean, y: active ? 0 : 3 }}
      transition={{ type: "spring", stiffness: 500, damping: 28 }}
      className={
        "relative flex items-center gap-3 border-[2.5px] border-line bg-panel py-2 pl-2.5 pr-4 text-left " +
        (active ? "" : "hover:bg-bg-2") + (off ? " opacity-60 hover:opacity-90" : "")
      }
      style={{ borderRadius: index % 2 ? "14px 8px 12px 6px / 8px 12px 6px 14px" : "8px 14px 6px 12px / 12px 6px 14px 8px",
               boxShadow: active ? "4px 4px 0 var(--color-ink)" : "none" }}>
      {/* the "you are here" mark: one sky-blue marker stroke under the tab
          (the same stroke as under the wordmark) that slides between tabs */}
      {active && (
        <motion.svg layoutId="tab-flag" transition={{ type: "spring", stiffness: 500, damping: 36 }}
          className="pointer-events-none absolute -bottom-[11px] left-[6%] h-[9px] w-[88%]" viewBox="0 0 200 9"
          preserveAspectRatio="none" fill="none" aria-hidden>
          <path d="M3 6c30-5 50 2 80-1s45-4 70 0 30-2 44 0" stroke="var(--color-cross)" strokeWidth="3.5" strokeLinecap="round" />
        </motion.svg>
      )}
      <span className={"display grid h-9 w-9 place-items-center border-[2.5px] border-line text-[16px] leading-none " +
        (active ? "bg-amber" : "bg-panel-2 text-ink-2")}
        style={{ borderRadius: "50% 45% 55% 45% / 45% 55% 45% 55%" }}>
        P{index}
      </span>
      <span className="leading-tight">
        <span className={"display block text-[16px]" + (off ? " text-ink-2 line-through decoration-2" : "")}>
          {c.label || short(serial)}
        </span>
        <span className="eyebrow block text-[12.5px]">{sub}</span>
      </span>
      <span className="ml-1 flex items-center gap-2.5">
        {remote && (
          <span className="sticker" style={{ background: rc, color: "var(--color-ink)" }} title="remote mode: this pad is driving the OS">
            {keyboardOpen(c) ? <Keyboard size={12} strokeWidth={2.5} /> : <MousePointer2 size={12} strokeWidth={2.5} />} remote
          </span>
        )}
        {pct != null && <span className="num text-[14px] text-ink-2">{pct}%</span>}
        <InkDot tone={tone(c)} size={12} />
      </span>
    </motion.button>
  );
}
