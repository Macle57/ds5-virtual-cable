import { motion } from "framer-motion";
import { Settings2, Activity, Radio, MousePointer2 } from "lucide-react";
import { useRemoteColor, useRemoteCount, useStore } from "../lib/store";

const LINK_LABEL = {
  connecting: "connecting…", live: "live", snapshot: "snapshot", reconnecting: "reconnecting…",
};

export default function Header() {
  const link = useStore((s) => s.link);
  const agg = useStore((s) => s.state.aggregate);
  const count = useStore((s) => Object.keys(s.state.controllers).length);
  const settingsOpen = useStore((s) => s.settingsOpen);
  const toggleSettings = useStore((s) => s.toggleSettings);
  const remoteCount = useRemoteCount();
  const rc = useRemoteColor();
  const on = link === "live" || link === "snapshot";

  return (
    <header className="sticky top-0 z-20 border-b border-line/80 bg-bg/70 backdrop-blur-xl">
      <div className="mx-auto flex max-w-[1320px] items-center gap-5 px-5 py-3">
        <a className="flex items-center gap-3" href="/">
          <Logo />
          <div className="leading-none">
            <div className="display text-[20px] tracking-[.06em]">
              DS5<span className="text-cross">BRIDGE</span>
            </div>
            <div className="eyebrow mt-1 text-[10px]">virtual wired dualsense</div>
          </div>
        </a>

        {count > 0 && (
          <motion.div
            initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }}
            className="ml-2 hidden items-center gap-2 md:flex"
          >
            <Hud label="bridged" value={`${agg.running ?? "?"}/${agg.present ?? count}`} />
            <Hud label="reports/s" value={String(Math.round(agg.reports_per_s ?? 0))} icon={<Activity size={13} />} />
            {agg.master_enabled === false && (
              <Hud label="bridging" value="OFF" tone="bad" />
            )}
            {remoteCount > 0 && (
              <Hud label={remoteCount === 1 ? "remote mode" : "in remote mode"} value={String(remoteCount)}
                   icon={<MousePointer2 size={13} />} color={rc} />
            )}
          </motion.div>
        )}

        <div className="flex-1" />

        <div className="flex items-center gap-2 text-[12.5px] text-ink-2">
          <span
            className={"inline-block h-2.5 w-2.5 rounded-full " + (on ? "bg-ok text-ok pulse" : "bg-bad")}
          />
          <span className="display text-[13px] uppercase tracking-[.14em]">{LINK_LABEL[link]}</span>
        </div>

        <button
          className={"btn " + (settingsOpen ? "border-cross text-cross" : "")}
          onClick={() => toggleSettings()}
          aria-pressed={settingsOpen}
        >
          <Settings2 size={16} /> Settings
        </button>
      </div>
    </header>
  );
}

/* `color` paints the tile in an arbitrary colour (the remote lightbar's);
   `tone` is the fixed bad state. */
function Hud({ label, value, icon, tone, color }:
  { label: string; value: string; icon?: React.ReactNode; tone?: "bad"; color?: string }) {
  return (
    <div className={
      "flex items-center gap-2 rounded-lg border px-2.5 py-1 " +
      (tone === "bad" ? "border-bad/60 bg-bad/10 text-bad" : color ? "" : "border-line bg-panel/60 text-ink")
    } style={color ? { borderColor: `color-mix(in oklab, ${color} 65%, transparent)`, color,
                       background: `color-mix(in oklab, ${color} 12%, transparent)`, boxShadow: `0 0 14px -5px ${color}` } : undefined}>
      {icon ?? <Radio size={13} className="text-ink-3" />}
      <span className="num text-[15px] leading-none">{value}</span>
      <span className="eyebrow text-[9.5px]" style={color ? { color } : undefined}>{label}</span>
    </div>
  );
}

function Logo() {
  return (
    <div className="relative grid h-10 w-10 place-items-center rounded-xl border border-cross/50 bg-cross/10 glow-cross">
      <svg width="24" height="16" viewBox="0 0 24 16" fill="none" aria-hidden>
        <path d="M6 1h12l4 5-1.5 8-4.5-3.5h-8L3.5 14 2 6l4-5z" stroke="var(--color-cross)" strokeWidth="1.6" strokeLinejoin="round" />
        <circle cx="8" cy="7" r="1.4" fill="var(--color-cross)" />
        <circle cx="16" cy="7" r="1.4" fill="var(--color-cross)" />
      </svg>
    </div>
  );
}
