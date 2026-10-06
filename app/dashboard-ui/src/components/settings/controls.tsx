import { useId, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { getPath, setPath, useStore } from "../../lib/store";
import { hexToRgb, rgbToHex } from "./help";

/* ------------------------------------------------------------------------
   Controls bound to CFG paths. Each renders the engine default when the
   running config predates the key, and writes only once the user touches
   it -- an untouched default stays the engine's business.
   ------------------------------------------------------------------------ */

function useBind<T>(path: string[], dflt: T): [T, (v: T) => void] {
  const raw = useStore((s) => (s.cfg ? getPath(s.cfg, path) : undefined));
  const patch = useStore((s) => s.patchConfig);
  const value = raw === undefined || raw === null ? dflt : (raw as T);
  return [value, (v) => patch((cfg) => setPath(cfg, path, v))];
}

export function Toggle({ path, dflt = false, onChange }: { path: string[]; dflt?: boolean; onChange?: (v: boolean) => void }) {
  const [v, set] = useBind<boolean>(path, dflt);
  const [touched, setTouched] = useState(false);     // the knob hops only when you flip it, not on mount
  return (
    <button type="button" role="switch" aria-checked={!!v} className="switch" data-flipped={touched || undefined}
            onClick={() => { setTouched(true); set(!v); onChange?.(!v); }} />
  );
}

export function NumberField({ path, dflt, step, width = 110, nullable = false }:
  { path: string[]; dflt: number | null; step?: number; width?: number; nullable?: boolean }) {
  const [v, set] = useBind<number | null>(path, dflt);
  return (
    <input type="number" className="field num" step={step} style={{ width }}
           value={v ?? ""}
           onChange={(e) => set(e.target.value === "" ? (nullable ? null : dflt) : Number(e.target.value))} />
  );
}

export function TextField({ path, width }: { path: string[]; width?: number }) {
  const [v, set] = useBind<string | null>(path, null);
  return (
    <input type="text" className="field" style={width ? { width } : undefined} value={v ?? ""}
           onChange={(e) => set(e.target.value === "" ? null : e.target.value)} />
  );
}

export function SelectField({ path, dflt, options, onChange, width }:
  { path: string[]; dflt: string; options: { value: string; label: string }[]; onChange?: (v: string) => void; width?: number }) {
  const [v, set] = useBind<string>(path, dflt);
  const current = options.some((o) => o.value === v) ? v : dflt;
  return (
    <select className="field" value={current} style={width ? { width } : undefined}
            onChange={(e) => { set(e.target.value); onChange?.(e.target.value); }}>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

/* A crayon-filled tube with a paper knob: the native range sits on top, so
   keyboard and screen readers get the real control. */
export function RangeField({ path, dflt, min, max, step, fmt }:
  { path: string[]; dflt: number; min: number; max: number; step: number; fmt: (v: number) => string }) {
  const [v, set] = useBind<number>(path, dflt);
  const pct = Math.max(0, Math.min(100, ((v - min) / (max - min)) * 100));
  return (
    <div className="flex w-full flex-1 items-center gap-3">
      <div className="st-range" style={{ "--pct": pct + "%" } as React.CSSProperties}>
        <div className="st-range-track"><div className="st-range-fill" /></div>
        <input type="range" min={min} max={max} step={step} value={v}
               onChange={(e) => set(Number(e.target.value))} />
      </div>
      <span className="st-range-val num">{fmt(v)}</span>
    </div>
  );
}

/* A dab of paint: the swatch is the control, the native picker sits on it. */
export function ColorField({ path, dflt }: { path: string[]; dflt: number[] }) {
  const [v, set] = useBind<unknown>(path, dflt);
  const hex = rgbToHex(v);
  return (
    <label className="flex cursor-pointer items-center gap-2.5">
      <span className="st-swatch" style={{ background: hex }}>
        <input type="color" className="absolute inset-0 h-full w-full cursor-pointer opacity-0" value={hex}
               aria-label="pick a colour" onChange={(e) => set(hexToRgb(e.target.value))} />
      </span>
      <span className="mono text-[12px] text-ink-2">{hex}</span>
    </label>
  );
}

/* Arrays and anything exotic: raw JSON, validated on the fly. */
export function JsonField({ path }: { path: string[] }) {
  const [v, set] = useBind<unknown>(path, null);
  const [text, setText] = useState(() => JSON.stringify(v, null, 1));
  const [bad, setBad] = useState(false);
  return (
    <textarea className={"field" + (bad ? " is-bad" : "")} value={text} aria-invalid={bad || undefined}
              onChange={(e) => {
                setText(e.target.value);
                try { set(JSON.parse(e.target.value)); setBad(false); } catch { setBad(true); }
              }} />
  );
}

/* ------------------------------------------------------------------------
   Layout: a section (handwritten heading with a scribble under it) and a
   setting row (label + optional (i) help toggled inline -- a hover title is
   useless on a touchscreen and invisible to a keyboard) with the control
   beside it, or beneath it when stacked.
   ------------------------------------------------------------------------ */

/* Something that unrolls open: clipped only while it moves, so the cards'
   offset shadows, focus rings and an action picker's menu are never cut off
   once it is open. Render it inside an AnimatePresence. */
export function Reveal({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  const [moving, setMoving] = useState(true);
  return (
    <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.22 }} className={className}
                onAnimationStart={() => setMoving(true)} onAnimationComplete={() => setMoving(false)}
                style={{ overflow: moving ? "hidden" : "visible" }}>
      {children}
    </motion.div>
  );
}

/* The hand-drawn underline: one wobbly crayon stroke that draws itself in. */
export function Scribble() {
  return (
    <svg className="st-scribble" viewBox="0 0 200 12" preserveAspectRatio="none" aria-hidden>
      <path d="M3 8 C 30 3.5, 52 9.5, 84 6 S 140 3, 168 6.5 S 190 7.5, 197 4.5" pathLength={1}
            fill="none" stroke="var(--tab, var(--color-cross))" strokeWidth={4.2} strokeLinecap="round" />
    </svg>
  );
}

export function Card({ title, tag, hint, children, className = "" }:
  { title: string; tag?: string; hint?: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={"st-card " + className}>
      <div className="st-h">
        <h3 className="st-h-text">{title}<Scribble /></h3>
        {/* serials and the like: mono, so 0/O and 1/l stay apart */}
        {tag && <span className="st-sticker tilt-r mono">{tag}</span>}
      </div>
      {hint && <p className="st-hint">{hint}</p>}
      <div className="st-rows">{children}</div>
    </section>
  );
}

/* `root` is the section `keyName` lives under in config.json ("input" for
   the shortcut engine's rows; "" for a top-level key). */
export function Row({ label, keyName, help, unit, stack, root = "input", children }:
  { label: string; keyName?: string; help?: string; unit?: string; stack?: boolean; root?: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const fullKey = keyName ? (root ? root + "." + keyName : keyName) : "";
  return (
    <div className="st-row">
      <div className={"flex gap-x-4 gap-y-2.5 " + (stack ? "flex-col items-stretch" : "flex-wrap items-center justify-between")}>
        <div className="flex min-w-0 flex-1 items-center gap-2" style={stack ? undefined : { flexBasis: 220 }}>
          <label className="st-row-label min-w-0">{label}</label>
          {help && (
            <button type="button" aria-expanded={open} aria-controls={id} aria-label={"Explain: " + label}
                    onClick={() => setOpen(!open)} className="st-info">
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
                <path d="M6 5.4 L6 9.6" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
                <circle cx="6" cy="2.6" r="1.35" fill="currentColor" />
              </svg>
            </button>
          )}
        </div>
        <div className={"flex items-center gap-2.5 " + (stack ? "" : "flex-none")}>
          {children}
          {unit && !stack && <span className="st-unit">{unit}</span>}
        </div>
      </div>
      <AnimatePresence initial={false}>
        {open && help && (
          <motion.div id={id} initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }} className="overflow-hidden">
            <div className="st-note">
              {help}
              {keyName && <div className="mt-1.5 text-[12px] text-ink-2">Saved as <span className="mono">{fullKey}</span></div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
