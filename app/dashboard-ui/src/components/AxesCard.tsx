import { useRef } from "react";
import { useActiveController } from "../lib/store";

/* Sticks on graph paper: a square of the notebook's grid per stick, a pencil
   trail, and a coral crayon dot where the stick is. Triggers are hatched
   bars that fill as you pull. */
export default function AxesCard() {
  const c = useActiveController();
  const d = c?.telemetry?.decoded ?? null;
  return (
    <section className="panel w3 pop p-4" style={{ "--i": 3 } as React.CSSProperties}>
      <h2 className="panel-title mb-3">Sticks and triggers</h2>
      {/* the readouts sit beside the scopes, and drop below them when the
          column is narrow (a 1024-wide window) rather than clipping */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3 px-1">
        <div className="flex gap-3">
          <Scope x={d?.lx ?? 128} y={d?.ly ?? 128} label="L" />
          <Scope x={d?.rx ?? 128} y={d?.ry ?? 128} label="R" />
        </div>
        <div className="grid min-w-[150px] flex-1 gap-2.5 text-[12px]">
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
  const px = ((x - 128) / 128) * 42, py = ((y - 128) / 128) * 42;   // percent from centre
  trail.current.push([px, py]);
  if (trail.current.length > 24) trail.current.shift();
  return (
    <div className="relative h-[96px] w-[96px] flex-none overflow-hidden border-[2.5px] border-line bg-panel"
         style={{ borderRadius: label === "L" ? "10px 6px 12px 5px / 6px 12px 5px 10px" : "6px 11px 5px 12px / 11px 5px 12px 6px" }}>
      <svg viewBox="-50 -50 100 100" className="absolute inset-0 h-full w-full">
        {/* graph paper */}
        <g stroke="var(--color-line-2)" strokeWidth="1">
          {[-37.5, -25, -12.5, 12.5, 25, 37.5].map((v) => (
            <g key={v}><line x1={v} x2={v} y1="-50" y2="50" /><line y1={v} y2={v} x1="-50" x2="50" /></g>
          ))}
        </g>
        <g className="sketch" fill="none" stroke="var(--color-ink)" strokeWidth="1.8" strokeLinecap="round">
          <line x1="-46" x2="46" y1="0" y2="0" /><line y1="-46" y2="46" x1="0" x2="0" />
          <circle r="42" strokeDasharray="3 4" />
        </g>
        {/* the pencil trail */}
        <polyline points={trail.current.map((p) => p.join(",")).join(" ")} fill="none"
                  stroke="var(--color-ink-3)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" opacity="0.6" />
        <line x1="0" y1="0" x2={px} y2={py} stroke="var(--color-ink)" strokeWidth="2" strokeLinecap="round" />
        <circle cx={px} cy={py} r="6.5" fill="var(--color-circle)" stroke="var(--color-ink)" strokeWidth="2.2" />
      </svg>
      <span className="display absolute left-1.5 top-0 text-[13px] text-ink-2">{label}</span>
    </div>
  );
}

function Readout({ label, a, b }: { label: string; a: number; b: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="eyebrow w-6">{label}</span>
      <span className="num w-10 border-b-2 border-dotted border-ink-3 text-center text-[15px]">{a}</span>
      <span className="num w-10 border-b-2 border-dotted border-ink-3 text-center text-[15px]">{b}</span>
    </div>
  );
}

function TriggerBar({ label, v }: { label: string; v: number }) {
  const pct = (v / 255) * 100;
  return (
    <div className="flex items-center gap-2">
      <span className="eyebrow w-6">{label}</span>
      <div className="relative h-[14px] flex-1 overflow-hidden border-[2.5px] border-line bg-panel"
           style={{ borderRadius: "6px 4px 7px 3px / 4px 7px 3px 6px" }}>
        <div className="hatch-cross absolute inset-y-0 left-0 border-r-[2.5px] border-line transition-[width] duration-75"
             style={{ width: pct + "%", borderRightWidth: v > 0 ? 2.5 : 0 }} />
      </div>
      <span className="num w-8 text-right text-[15px]">{v}</span>
    </div>
  );
}
