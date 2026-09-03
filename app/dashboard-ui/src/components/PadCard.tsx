import { useActiveController, useStore } from "../lib/store";
import Pad from "./Pad";

export default function PadCard() {
  const c = useActiveController();
  const active = useStore((s) => s.active);
  const t = c?.telemetry;
  const d = t?.decoded;
  const pressed = d ? Object.entries(d.buttons).filter(([, v]) => v).map(([k]) => k) : [];
  if (d && d.dpad && d.dpad !== "-") pressed.push("dpad " + d.dpad);

  return (
    <section className="panel flex flex-col p-3 pb-2">
      <div className="flex items-center justify-between px-2 pt-1">
        <div className="panel-title flex-1">controller</div>
        <div className="ml-3 flex items-center gap-2">
          <span className="eyebrow">serial</span>
          <span className="mono rounded-md border border-line bg-well px-2 py-0.5 text-[12px] text-ink-2">{active}</span>
        </div>
      </div>

      <div className="relative flex flex-1 items-center px-1 pt-2">
        <Pad t={t} />
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
