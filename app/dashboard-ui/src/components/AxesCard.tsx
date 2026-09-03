import { useRef } from "react";
import { useActiveController } from "../lib/store";

/* Stick scopes with a fading trail, raw 0..255 read-outs, trigger bars. */
export default function AxesCard() {
  const c = useActiveController();
  const d = c?.telemetry?.decoded ?? null;
  return (
    <section className="panel p-4">
      <div className="panel-title mb-3">sticks &amp; triggers</div>
      <div className="grid grid-cols-[auto_auto_1fr] items-center gap-3">
        <Scope x={d?.lx ?? 128} y={d?.ly ?? 128} label="L" />
        <Scope x={d?.rx ?? 128} y={d?.ry ?? 128} label="R" />
        <div className="grid gap-2 text-[12px]">
          <Readout label="L" a={d?.lx ?? 128} b={d?.ly ?? 128} />
          <Readout label="R" a={d?.rx ?? 128} b={d?.ry ?? 128} />
          <TriggerBar label="L2" v={d?.l2 ?? 0} />
          <TriggerBar label="R2" v={d?.r2 ?? 0} />
        </div>
      </div>
    </section>
  );
}

function Scope({ x, y, label }: { x: number; y: number; label: string }) {
  const trail = useRef<[number, number][]>([]);
  const px = ((x - 128) / 128) * 44, py = ((y - 128) / 128) * 44;   // percent from centre
  trail.current.push([px, py]);
  if (trail.current.length > 24) trail.current.shift();
  const mag = Math.min(1, Math.hypot(x - 128, y - 128) / 128);
  return (
    <div className="relative h-[92px] w-[92px] flex-none overflow-hidden rounded-xl border border-line bg-well">
      <svg viewBox="-50 -50 100 100" className="absolute inset-0 h-full w-full">
        <circle r="44" fill="none" stroke="var(--color-line)" strokeWidth="1" />
        <circle r="22" fill="none" stroke="var(--color-line)" strokeWidth="1" strokeDasharray="2 3" />
        <line x1="-48" x2="48" y1="0" y2="0" stroke="var(--color-line)" strokeWidth="1" />
        <line y1="-48" y2="48" x1="0" x2="0" stroke="var(--color-line)" strokeWidth="1" />
        <polyline points={trail.current.map((p) => p.join(",")).join(" ")} fill="none"
                  stroke="var(--color-cyan)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" opacity="0.5" />
        <line x1="0" y1="0" x2={px} y2={py} stroke="var(--color-cross)" strokeWidth="2" opacity={mag} />
        <circle cx={px} cy={py} r="5" fill="var(--color-cross)" style={{ filter: "drop-shadow(0 0 4px var(--color-cross))" }} />
      </svg>
      <span className="display absolute left-1.5 top-0.5 text-[11px] text-ink-3">{label}</span>
    </div>
  );
}

function Readout({ label, a, b }: { label: string; a: number; b: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="eyebrow w-5">{label}</span>
      <span className="num rounded-md border border-line bg-well px-2 py-0.5 text-[13px]">{a}</span>
      <span className="num rounded-md border border-line bg-well px-2 py-0.5 text-[13px]">{b}</span>
    </div>
  );
}

function TriggerBar({ label, v }: { label: string; v: number }) {
  const pct = (v / 255) * 100;
  return (
    <div className="flex items-center gap-2">
      <span className="eyebrow w-5">{label}</span>
      <div className="relative h-2.5 flex-1 overflow-hidden rounded-full bg-well ring-1 ring-line">
        <div className="h-full rounded-full bg-gradient-to-r from-cross to-cyan transition-[width] duration-75"
             style={{ width: pct + "%", boxShadow: "0 0 10px var(--color-cross)" }} />
      </div>
      <span className="num w-8 text-right text-[13px]">{v}</span>
    </div>
  );
}
