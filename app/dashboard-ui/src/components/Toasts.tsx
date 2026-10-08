import { AnimatePresence, motion } from "framer-motion";
import { Check, AlertTriangle } from "lucide-react";
import { useStore } from "../lib/store";

/* Sticky notes: a yellow square for news, a coral one for trouble, each
   dropped onto the page with a little twist and pinned by a tape strip. */
export default function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return (
    <div className="pointer-events-none fixed bottom-6 left-1/2 z-50 flex -translate-x-1/2 flex-col items-center gap-3">
      <AnimatePresence>
        {toasts.map((t, i) => {
          const r = (i % 2 ? 1.6 : -2.2);
          return (
            <motion.div key={t.id}
              initial={{ opacity: 0, y: -40, rotate: r - 6, scale: 0.9 }}
              animate={{ opacity: 1, y: 0, rotate: r, scale: 1 }}
              exit={{ opacity: 0, y: 24, rotate: r + 4, transition: { duration: 0.18 } }}
              transition={{ type: "spring", stiffness: 420, damping: 22 }}
              className="relative flex max-w-[80vw] items-center gap-3 border-[2.5px] border-line px-4 py-3 pr-5 text-[13.5px] font-semibold text-ink-on"
              style={{ background: t.error ? "var(--color-circle)" : "var(--color-amber)", borderColor: "var(--color-ink-on)",
                       boxShadow: "4px 4px 0 var(--color-line)", borderRadius: "4px 6px 3px 7px / 6px 3px 7px 4px" }}>
              {/* the tape */}
              <span aria-hidden className="absolute -top-3 left-1/2 h-4 w-14 -translate-x-1/2 rotate-[-3deg] border-2 border-line bg-bg-2 opacity-90" />
              <span className="grid h-7 w-7 flex-none place-items-center border-[2.5px] bg-panel text-ink"
                    style={{ borderColor: "var(--color-ink-on)", borderRadius: "55% 45% 50% 50% / 50% 55% 45% 50%" }}>
                {t.error ? <AlertTriangle size={15} strokeWidth={2.5} /> : <Check size={16} strokeWidth={3} />}
              </span>
              <span className="break-words">{t.text}</span>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
