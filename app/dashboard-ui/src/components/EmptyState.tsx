import { motion } from "framer-motion";

/* An empty page from the notebook: a drawn pad with a dotted path to the
   little Bluetooth mark, and the two ways to make a controller show up. */
export default function EmptyState() {
  return (
    <motion.div initial={{ opacity: 0, scale: 0.94, rotate: -1 }} animate={{ opacity: 1, scale: 1, rotate: 0 }} exit={{ opacity: 0 }}
                transition={{ type: "spring", stiffness: 260, damping: 22 }}
                className="panel w2 mx-auto mt-10 max-w-[600px] px-8 pb-10 pt-8 text-center">
      <div className="mx-auto w-[260px]">
        <svg viewBox="0 0 260 120" fill="none" strokeLinecap="round" strokeLinejoin="round" aria-hidden className="w-full">
          <g className="sketch">
            {/* the pad, waiting */}
            <motion.g className="bob">
              <path d="M28 44h66c10 0 14 8 16 18l5 24c1 6-4 10-9 9-6-2-10-9-14-12H34c-4 3-8 10-14 12-5 1-10-3-9-9l5-24c2-10 6-18 12-18z"
                    fill="var(--color-cross)" stroke="var(--color-ink)" strokeWidth="4" />
              <circle cx="46" cy="66" r="7" fill="var(--color-circle)" stroke="var(--color-ink)" strokeWidth="3" />
              <circle cx="84" cy="66" r="7" fill="var(--color-circle)" stroke="var(--color-ink)" strokeWidth="3" />
              <circle cx="102" cy="54" r="3.5" fill="var(--color-triangle)" stroke="var(--color-ink)" strokeWidth="2.2" />
              <circle cx="112" cy="62" r="3.5" fill="var(--color-amber)" stroke="var(--color-ink)" strokeWidth="2.2" />
              <path d="M26 54v9M21.5 58.5h9" stroke="var(--color-ink)" strokeWidth="2.6" />
            </motion.g>
            {/* a dotted path to the Bluetooth mark */}
            <path d="M138 60c20-18 40-30 70-28" stroke="var(--color-ink-3)" strokeWidth="2.8" strokeDasharray="1 8" />
            <path d="M220 12v40l12-10-24-20 24-20-12-10" transform="translate(0 6)" stroke="var(--color-ink)" strokeWidth="3.5" />
            {/* a question mark hanging over the gap */}
            <path d="M166 26c0-6 10-8 12-2 1 4-5 6-5 10M173 42v2" stroke="var(--color-ink)" strokeWidth="3" />
          </g>
        </svg>
      </div>
      <h2 className="display mt-2 text-[28px] leading-tight">Waiting for a controller</h2>
      <p className="mx-auto mt-2 max-w-[440px] text-[14px] text-ink-2">
        Turn a DualSense on and bridge it from the tray, or run{" "}
        <code className="mono whitespace-nowrap border-2 border-line-2 bg-bg-2 px-1.5 py-0.5 text-ink" style={{ borderRadius: "var(--r-small)" }}>ds5bridge --all</code>.
        It shows up here, moving live.
      </p>
      <p className="mx-auto mt-3 max-w-[440px] text-[12.5px] text-ink-3">
        No pad handy? A pretend one:{" "}
        <code className="mono whitespace-nowrap border-2 border-line-2 bg-bg-2 px-1.5 py-0.5 text-ink-2" style={{ borderRadius: "var(--r-small)" }}>python -m ds5app.dashboard --fake</code>
      </p>
    </motion.div>
  );
}
