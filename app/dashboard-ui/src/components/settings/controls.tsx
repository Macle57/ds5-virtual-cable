import { useId, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Info } from "lucide-react";
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
  return (
    <button type="button" role="switch" aria-checked={!!v} className="switch"
            onClick={() => { set(!v); onChange?.(!v); }} />
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

export function RangeField({ path, dflt, min, max, step, fmt }:
  { path: string[]; dflt: number; min: number; max: number; step: number; fmt: (v: number) => string }) {
  const [v, set] = useBind<number>(path, dflt);
  const pct = ((v - min) / (max - min)) * 100;
  return (
    <div className="flex flex-1 items-center gap-3">
      <input type="range" className="range" min={min} max={max} step={step} value={v}
             style={{ "--pct": pct + "%" } as React.CSSProperties}
             onChange={(e) => set(Number(e.target.value))} />
      <span className="num w-12 text-right text-[14px]">{fmt(v)}</span>
    </div>
  );
}

export function ColorField({ path, dflt }: { path: string[]; dflt: number[] }) {
  const [v, set] = useBind<unknown>(path, dflt);
  const hex = rgbToHex(v);
  return (
    <label className="flex cursor-pointer items-center gap-2">
      {/* the glowing swatch is the control; the native picker sits under it */}
      <span className="relative h-8 w-12 rounded-md border border-line-2" style={{ background: hex, boxShadow: `0 0 14px -2px ${hex}` }}>
        <input type="color" className="absolute inset-0 h-full w-full cursor-pointer opacity-0" value={hex}
               onChange={(e) => set(hexToRgb(e.target.value))} />
      </span>
      <span className="mono text-[11px] text-ink-3">{hex}</span>
    </label>
  );
}

/* Arrays and anything exotic: raw JSON, validated on the fly. */
export function JsonField({ path }: { path: string[] }) {
  const [v, set] = useBind<unknown>(path, null);
  const [text, setText] = useState(() => JSON.stringify(v, null, 1));
  const [bad, setBad] = useState(false);
  return (
    <textarea className="field" value={text} style={bad ? { borderColor: "var(--color-bad)" } : undefined}
              onChange={(e) => {
                setText(e.target.value);
                try { set(JSON.parse(e.target.value)); setBad(false); } catch { setBad(true); }
              }} />
  );
}

/* ------------------------------------------------------------------------
   Layout: a section card and a setting row (label line + optional (i) help
   toggled inline -- a hover title is useless on a touchscreen and invisible
   to a keyboard) with the control beside it, or beneath it when stacked.
   ------------------------------------------------------------------------ */

export function Card({ title, tag, hint, children, className = "" }:
  { title: string; tag?: string; hint?: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={"rounded-2xl border border-line bg-panel-2/50 p-4 " + className}>
      <div className="flex items-baseline gap-2">
        <span className="display text-[15px] uppercase tracking-[.1em] text-cross">{title}</span>
        {/* serials and the like: mono, so 0/O and 1/l stay apart */}
        {tag && <span className="mono rounded-md border border-line bg-well px-1.5 text-[12px] text-ink-2">{tag}</span>}
      </div>
      {hint && <p className="mb-2 mt-1 text-[12.5px] leading-relaxed text-ink-3">{hint}</p>}
      <div className="mt-2 divide-y divide-line/60">{children}</div>
    </div>
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
    <div className="py-2.5">
      <div className={"flex gap-3 " + (stack ? "flex-col items-stretch" : "items-center justify-between")}>
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <label className="min-w-0 text-[13.5px] text-ink-2 [overflow-wrap:anywhere]">{label}</label>
          {keyName && stack && <span className="mono flex-none text-[10.5px] text-ink-3">{keyName}</span>}
          {help && (
            <button type="button" aria-expanded={open} aria-controls={id} aria-label="explain this setting"
                    onClick={() => setOpen(!open)}
                    className={"grid h-5 w-5 flex-none place-items-center rounded-full border transition-colors " +
                      (open ? "border-cross text-cross" : "border-line text-ink-3 hover:border-cross hover:text-cross")}>
              <Info size={12} />
            </button>
          )}
        </div>
        <div className={"flex items-center gap-2 " + (stack ? "" : "flex-none")}>
          {children}
          {unit && !stack && <span className="w-7 text-[12px] text-ink-3">{unit}</span>}
        </div>
      </div>
      <AnimatePresence initial={false}>
        {open && help && (
          <motion.div id={id} initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }} className="overflow-hidden">
            <div className="mt-2 rounded-lg border border-line border-l-[3px] border-l-cross bg-well px-3 py-2 text-[12.5px] leading-relaxed text-ink-2">
              {help}
              {keyName && !stack && <div className="mono mt-1.5 text-[10.5px] text-ink-3">config key: {fullKey}</div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
