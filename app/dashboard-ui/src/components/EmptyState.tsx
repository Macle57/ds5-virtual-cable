import { motion } from "framer-motion";
import { Gamepad2 } from "lucide-react";

export default function EmptyState() {
  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
                className="panel quiet mx-auto mt-10 max-w-[560px] px-8 py-14 text-center">
      <motion.div animate={{ y: [0, -6, 0] }} transition={{ repeat: Infinity, duration: 2.6, ease: "easeInOut" }}
                  className="mx-auto grid h-16 w-16 place-items-center rounded-2xl border border-cross/40 bg-cross/10 text-cross glow-cross">
        <Gamepad2 size={30} />
      </motion.div>
      <div className="display mt-5 text-[22px] uppercase tracking-[.08em]">Waiting for a controller</div>
      <p className="mx-auto mt-2 max-w-[420px] text-[13.5px] text-ink-2">
        Bridge a DualSense (the tray, or <code className="mono whitespace-nowrap rounded bg-well px-1.5 py-0.5 text-ink">ds5bridge --all</code>) and
        it appears here, moving live. For a synthetic feed:{" "}
        <code className="mono whitespace-nowrap rounded bg-well px-1.5 py-0.5 text-ink">python -m ds5app.dashboard --fake</code>
      </p>
      <div className="mt-6 flex justify-center gap-1.5">
        {[0, 1, 2].map((i) => (
          <motion.span key={i} className="h-1.5 w-1.5 rounded-full bg-cross"
            animate={{ opacity: [0.2, 1, 0.2] }} transition={{ repeat: Infinity, duration: 1.4, delay: i * 0.2 }} />
        ))}
      </div>
    </motion.div>
  );
}
