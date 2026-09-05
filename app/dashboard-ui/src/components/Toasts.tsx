import { AnimatePresence, motion } from "framer-motion";
import { CheckCircle2, AlertTriangle } from "lucide-react";
import { useStore } from "../lib/store";

export default function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return (
    <div className="pointer-events-none fixed bottom-6 left-1/2 z-50 flex -translate-x-1/2 flex-col items-center gap-2">
      <AnimatePresence>
        {toasts.map((t) => (
          <motion.div key={t.id} initial={{ opacity: 0, y: 16, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={{ opacity: 0, y: 8, scale: 0.98 }} transition={{ type: "spring", stiffness: 400, damping: 30 }}
                      className={"flex max-w-[80vw] items-center gap-2.5 rounded-xl border px-4 py-2.5 text-[13.5px] backdrop-blur-xl " +
                        (t.error ? "border-bad/70 bg-bad/15 text-ink" : "border-ok/60 bg-panel/90 text-ink")}>
            {t.error ? <AlertTriangle size={16} className="text-bad" /> : <CheckCircle2 size={16} className="text-ok" />}
            <span className="break-all">{t.text}</span>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
