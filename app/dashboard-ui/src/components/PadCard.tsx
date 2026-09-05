import { AnimatePresence, motion } from "framer-motion";
import { Keyboard, MousePointer2 } from "lucide-react";
import { keyboardOpen, remoteMode, useActiveController, useRemoteColor, useStore } from "../lib/store";
import Pad from "./Pad";

export default function PadCard() {
  const c = useActiveController();
  const active = useStore((s) => s.active);
  const t = c?.telemetry;
  const d = t?.decoded;
  const pressed = d ? Object.entries(d.buttons).filter(([, v]) => v).map(([k]) => k) : [];
  if (d && d.dpad && d.dpad !== "-") pressed.push("dpad " + d.dpad);
  const remote = remoteMode(c) === true;
  const kb = remote && keyboardOpen(c);
  const rc = useRemoteColor();

  return (
    <section className="panel flex flex-col p-3 pb-2">
      <div className="flex flex-wrap items-center justify-between gap-y-2 px-2 pt-1">
        <div className="panel-title flex-1">controller</div>
        <div className="ml-3 flex items-center gap-2">
          {/* remote mode: the same colour the lightbar is showing right now */}
          <AnimatePresence>
            {remote && (
              <motion.span key="remote" initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.9 }}
                           className="display inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[12px] uppercase tracking-[.14em]"
                           style={{ borderColor: rc, color: rc, background: `color-mix(in oklab, ${rc} 16%, transparent)`, boxShadow: `0 0 14px -4px ${rc}` }}>
                <span className="h-2 w-2 rounded-full pulse" style={{ background: rc, color: rc }} />
                <MousePointer2 size={12} /> remote mode
              </motion.span>
            )}
            {kb && (
              <motion.span key="kb" initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.9 }}
                           className="display inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] uppercase tracking-[.12em]"
                           style={{ borderColor: `color-mix(in oklab, ${rc} 55%, transparent)`, color: rc }}>
                <Keyboard size={12} /> keyboard open
              </motion.span>
            )}
          </AnimatePresence>
          <span className="eyebrow">serial</span>
          <span className="mono rounded-md border border-line bg-well px-2 py-0.5 text-[12px] text-ink-2">{active}</span>
        </div>
      </div>

      <div className="relative flex flex-1 items-center px-1 pt-2">
        <Pad t={t} lightbar={remote ? rc : undefined} />
      </div>

      {/* pressed-input ticker: the names, as decoded, so a debug session can
          read the exact bits the game is being fed */}
      <div className="flex min-h-[34px] flex-wrap items-center gap-1.5 px-2 pb-1">
        <span className="eyebrow mr-1">input</span>
        {pressed.length === 0 && <span className="text-[12px] text-ink-3">idle</span>}
        {pressed.map((n) => (
          <span key={n} className="display rounded-md border border-cross/50 bg-cross/15 px-2 py-0.5 text-[12px] uppercase tracking-wider text-cross">
            {n}
          </span>
        ))}
      </div>
    </section>
  );
}
