import { AnimatePresence, motion } from "framer-motion";
import { Headphones, Mic, MicOff, Eye, EyeOff, Zap, Cable, Clock, MousePointer2, Keyboard, AlertTriangle } from "lucide-react";
import { keyboardOpen, remoteMode, useActiveController, useRemoteColor } from "../lib/store";

const HIDE_NOTE_FALLBACK = "Hidden requested, but the controller is still visible to other apps — HidHide's filter is not attached to it (restart the device or reboot).";

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
  const remote = remoteMode(c) === true;
  const kb = keyboardOpen(c);
  const rc = useRemoteColor();
  // hide_bluetooth is what was ASKED for; hide_effective is whether HidHide's
  // filter is verified on the pad's HID device. Only a verified "no" against a
  // request is a fault worth shouting about; null means nobody has checked.
  const hideBroken = c.hide_effective === false && c.hide_bluetooth !== false;

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

      {/* remote mode: the pad is a desktop remote, the game sees a neutral pad.
          Wears the lightbar colour the engine paints while it is on. */}
      <AnimatePresence initial={false}>
        {remote && (
          <motion.div key="remote" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22 }} className="overflow-hidden">
            <div className="mt-3 flex items-center gap-3 rounded-xl border px-3.5 py-2.5"
                 style={{ borderColor: rc, background: `color-mix(in oklab, ${rc} 14%, transparent)`, boxShadow: `0 0 28px -10px ${rc}` }}>
              <span className="grid h-9 w-9 flex-none place-items-center rounded-lg"
                    style={{ background: `color-mix(in oklab, ${rc} 24%, transparent)`, color: rc }}>
                <MousePointer2 size={18} />
              </span>
              <div className="min-w-0 flex-1 leading-tight">
                <div className="display text-[17px] uppercase tracking-[.1em] text-glow" style={{ color: rc }}>remote mode</div>
                <div className="mt-0.5 text-[12px] text-ink-2">the pad drives the OS — the game sees a neutral pad</div>
              </div>
              {kb && (
                <span className="display inline-flex flex-none items-center gap-1.5 rounded-lg border px-2 py-1 text-[11.5px] uppercase tracking-[.08em]"
                      style={{ borderColor: rc, color: rc, background: `color-mix(in oklab, ${rc} 12%, transparent)` }}>
                  <Keyboard size={13} /> keyboard open
                </span>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

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
        <Flag on={!!c.hide_bluetooth || hideBroken} icon={hideBroken ? <Eye size={13} /> : <EyeOff size={13} />}
              label={hideBroken ? "BT pad NOT hidden" : "BT pad hidden"} tone={hideBroken ? "var(--color-warn)" : "var(--color-square)"} />
      </div>

      {/* the requested hide is not in effect: other apps (Steam, the game) can
          still see the Bluetooth pad -- the doubled-controller symptom */}
      {hideBroken && (
        <div className="mt-3 flex items-start gap-3 rounded-xl border border-warn/70 bg-warn/10 px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2">
          <AlertTriangle size={18} className="mt-0.5 flex-none text-warn" />
          <div>
            <b className="text-warn">Hide requested, but not in effect.</b> {c.hide_note?.trim() || HIDE_NOTE_FALLBACK}
          </div>
        </div>
      )}
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
