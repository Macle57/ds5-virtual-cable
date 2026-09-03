import { motion } from "framer-motion";
import { Headphones, Mic, MicOff, EyeOff, Zap, Cable, Clock } from "lucide-react";
import { useActiveController } from "../lib/store";

const STATE_TONE: Record<string, string> = {
  "running": "var(--color-ok)", "controller offline": "var(--color-warn)",
  "starting": "var(--color-cross)", "stopping": "var(--color-idle)",
  "stopped": "var(--color-idle)", "error": "var(--color-bad)",
};

export default function StatusCard() {
  const c = useActiveController();
  if (!c) return null;
  const t = c.telemetry;
  const d = t?.decoded ?? null;

  const st = c.state || (d ? "telemetry only" : "unknown");
  const tone = STATE_TONE[st] ?? "var(--color-idle)";
  const pct = d ? d.battery_percent : c.battery_percent ?? null;
  const bstate = d ? d.battery_state : "";
  const charging = String(bstate).startsWith("charging");
  const low = pct != null && pct <= 20 && !charging;
  const rate = t?.rps ?? c.reports_per_s ?? null;
  const up = c.uptime_s ?? 0;

  return (
    <section className="panel p-4">
      <div className="panel-title mb-3">status</div>

      {/* bridge state */}
      <div className="flex items-center gap-3 rounded-xl border border-line bg-well/60 px-3.5 py-3">
        <span className="relative grid h-9 w-9 place-items-center rounded-lg" style={{ background: `color-mix(in oklab, ${tone} 18%, transparent)` }}>
          <span className="h-3 w-3 rounded-full pulse" style={{ background: tone, color: tone }} />
        </span>
        <div className="min-w-0 flex-1 leading-tight">
          <div className="display text-[19px] uppercase tracking-[.08em] text-glow" style={{ color: tone }}>{st}</div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 text-[12px] text-ink-2">
            {c.port && <span className="inline-flex items-center gap-1"><Cable size={12} className="text-ink-3" />usbip :{c.port}</span>}
            {up > 0 && <span className="inline-flex items-center gap-1"><Clock size={12} className="text-ink-3" />{Math.floor(up / 60)}m {Math.floor(up % 60)}s</span>}
            {c.error && <span className="text-bad">{c.error}</span>}
          </div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3">
        {/* battery as a segmented health bar */}
        <div className="rounded-xl border border-line bg-well/60 p-3">
          <div className="flex items-baseline justify-between">
            <span className="eyebrow">battery</span>
            <span className="text-[11px] text-ink-3">{bstate || (pct == null ? "unknown" : "")}</span>
          </div>
          <div className="num mt-1 text-[30px] leading-none" style={{ color: low ? "var(--color-bad)" : "var(--color-ink)" }}>
            {pct == null ? "—" : pct}<span className="text-[14px] text-ink-2">%</span>
            {charging && <Zap size={16} className="ml-1 inline text-triangle" />}
          </div>
          <div className="mt-2.5 grid grid-cols-10 gap-[3px]">
            {Array.from({ length: 10 }, (_, i) => {
              const filled = pct != null && pct >= (i + 1) * 10 - 5;
              return (
                <motion.span key={i} initial={false}
                  animate={{ opacity: filled ? 1 : 0.18, scaleY: filled ? 1 : 0.7 }}
                  transition={{ delay: i * 0.02 }}
                  className="h-2.5 rounded-[3px]"
                  style={{ background: low ? "var(--color-bad)" : charging ? "var(--color-triangle)" : "var(--color-ok)",
                           boxShadow: filled ? "0 0 8px -1px currentColor" : "none",
                           color: low ? "var(--color-bad)" : "var(--color-ok)" }} />
              );
            })}
          </div>
        </div>

        {/* input rate gauge */}
        <div className="flex items-center gap-3 rounded-xl border border-line bg-well/60 p-3">
          <RateGauge rate={rate} />
          <div className="min-w-0 leading-tight">
            <div className="eyebrow">input rate</div>
            <div className="num text-[24px] leading-none">{rate != null ? Math.round(rate) : "—"}</div>
            <div className="text-[11px] text-ink-3">reports/s</div>
            {t?.stale_s != null && (
              <div className="mono mt-1 text-[10.5px] text-ink-3">{(t.stale_s * 1000).toFixed(0)} ms ago</div>
            )}
          </div>
        </div>
      </div>

      {/* flags */}
      <div className="mt-3 flex flex-wrap gap-2">
        <Flag on={!!d?.headphone} icon={<Headphones size={13} />} label="headphones" />
        <Flag on={!!d?.mic} icon={<Mic size={13} />} label="mic" />
        <Flag on={!!d?.mic_muted} icon={<MicOff size={13} />} label="muted" tone="var(--color-amber)" />
        <Flag on={!!c.hide_bluetooth} icon={<EyeOff size={13} />} label="BT pad hidden" tone="var(--color-square)" />
      </div>
    </section>
  );
}

function Flag({ on, icon, label, tone = "var(--color-cross)" }:
  { on: boolean; icon: React.ReactNode; label: string; tone?: string }) {
  return (
    <span className="display inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[12px] uppercase tracking-[.08em] transition-colors"
          style={on
            ? { borderColor: tone, color: tone, background: `color-mix(in oklab, ${tone} 14%, transparent)`, boxShadow: `0 0 12px -4px ${tone}` }
            : { borderColor: "var(--color-line)", color: "var(--color-ink-3)" }}>
      {icon}{label}
    </span>
  );
}

/* 250 Hz is the pad's native rate over the bridge: that is the full arc. */
function RateGauge({ rate }: { rate: number | null }) {
  const r = 26, C = 2 * Math.PI * r;
  const frac = Math.max(0, Math.min(1, (rate ?? 0) / 250));
  const arc = C * 0.75;
  const tone = frac > 0.8 ? "var(--color-ok)" : frac > 0.3 ? "var(--color-amber)" : "var(--color-bad)";
  return (
    <svg width="68" height="68" viewBox="0 0 68 68" className="flex-none">
      <circle cx="34" cy="34" r={r} fill="none" stroke="var(--color-line)" strokeWidth="6"
              strokeDasharray={`${arc} ${C}`} strokeLinecap="round" transform="rotate(135 34 34)" />
      <motion.circle cx="34" cy="34" r={r} fill="none" stroke={tone} strokeWidth="6" strokeLinecap="round"
              transform="rotate(135 34 34)" style={{ filter: `drop-shadow(0 0 5px ${tone})` }}
              initial={false} animate={{ strokeDasharray: `${arc * frac} ${C}` }} transition={{ type: "spring", stiffness: 120, damping: 20 }} />
      <text x="34" y="38" textAnchor="middle" fill={tone} style={{ font: "700 12px var(--font-display)" }}>{Math.round(frac * 100)}%</text>
    </svg>
  );
}
