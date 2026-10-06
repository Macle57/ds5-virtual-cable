import { AnimatePresence, motion } from "framer-motion";
import { Keyboard, MousePointer2 } from "lucide-react";
import { keyboardOpen, remoteMode, useActiveController, useRemoteColor, useStore } from "../lib/store";
import Pad from "./Pad";

/* The frame around the drawing: a big card, the title in marker, stickers
   for what mode the pad is in, and a row of crayon keycaps for what is
   pressed right now. The pad itself is Pad.tsx. */
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
    <section className="panel pop flex flex-col p-4 pb-3" style={{ "--i": 1 } as React.CSSProperties}>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-1">
        <h2 className="panel-title">Controller</h2>
        <div className="flex flex-wrap items-center gap-2.5">
          <AnimatePresence>
            {remote && (
              <motion.span key="remote" initial={{ scale: 0.6, rotate: -10, opacity: 0 }} animate={{ scale: 1, rotate: -2, opacity: 1 }}
                           exit={{ scale: 0.6, opacity: 0 }} transition={{ type: "spring", stiffness: 500, damping: 22 }}
                           className="sticker flat" style={{ background: rc }} title="remote mode: this pad is driving the OS">
                <MousePointer2 size={13} strokeWidth={2.5} /> remote mode
              </motion.span>
            )}
            {kb && (
              <motion.span key="kb" initial={{ scale: 0.6, rotate: 8, opacity: 0 }} animate={{ scale: 1, rotate: 1.5, opacity: 1 }}
                           exit={{ scale: 0.6, opacity: 0 }} transition={{ type: "spring", stiffness: 500, damping: 22 }}
                           className="sticker flat bg-panel">
                <Keyboard size={13} strokeWidth={2.5} /> keyboard open
              </motion.span>
            )}
          </AnimatePresence>
          <span className="flex items-baseline gap-1.5">
            <span className="eyebrow">serial</span>
            <span className="mono text-[12.5px] text-ink-2" style={{ textDecoration: "underline dotted var(--color-ink-3) 2px", textUnderlineOffset: 4 }}>{active}</span>
          </span>
        </div>
      </div>

      <div className="relative flex flex-1 items-center justify-center px-1 pt-3">
        <Pad t={t} lightbar={remote ? rc : undefined} />
      </div>

      {/* what is pressed right now, as keycaps -- the exact names the decoder
          uses, so a debug session reads the same bits the game gets */}
      <div className="flex min-h-[38px] flex-wrap items-center gap-2 px-1 pt-1">
        <span className="eyebrow mr-1">pressing</span>
        {pressed.length === 0 && <span className="display text-[13.5px] text-ink-3">nothing</span>}
        {pressed.map((n) => (
          <span key={n} className="keycap keycap-in">{n}</span>
        ))}
      </div>
    </section>
  );
}
