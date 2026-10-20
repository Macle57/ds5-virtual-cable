import { AnimatePresence, motion } from "framer-motion";
import { Headphones, Mic, MicOff, Eye, EyeOff, Zap, MousePointer2, Keyboard, AlertTriangle, Ear, EarOff } from "lucide-react";
import { keyboardOpen, remoteMode, useActiveController, useRemoteColor } from "../lib/store";
import { InkDot } from "./Header";

const ECHO_NOTE_FALLBACK = "the bridge's echo canceller is not running";

const HIDE_NOTE_FALLBACK = "Hidden requested, but the controller is still visible to other apps — HidHide's filter is not attached to it (restart the device or reboot).";

const STATE_TONE: Record<string, string> = {
  "running": "var(--color-ok)", "controller offline": "var(--color-warn)",
  "starting": "var(--color-cross)", "stopping": "var(--color-idle)",
  "stopped": "var(--color-idle)", "error": "var(--color-bad)",
};

/* How the bridge is doing: the state in big marker letters, a drawn battery,
   a drawn dial for the input rate, and stickers for the flags. */
export default function StatusCard() {
  const c = useActiveController();
  const rc = useRemoteColor();
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
  // hide_bluetooth is what was ASKED for; hide_effective is whether HidHide's
  // filter is verified on the pad's HID device. Only a verified "no" against a
  // request is a fault worth shouting about; null means nobody has checked.
  const hideBroken = c.hide_effective === false && c.hide_bluetooth !== false;
  // echo cancellation: "degraded" means the pad still cancels its own speaker
  // but the PC's other speakers (TV, desktop speakers) reach the voice chat.
  const echoBroken = c.echo === "degraded";
  const echoOn = c.echo === "on" || c.echo === "idle";

  return (
    <section className="panel w2 pop p-4" style={{ "--i": 2 } as React.CSSProperties}>
      <h2 className="panel-title mb-3">Status</h2>

      {/* bridge state */}
      <div className="flex items-center gap-3 px-1">
        <InkDot tone={tone} size={18} pulse={st === "running"} />
        <div className="min-w-0 flex-1 leading-tight">
          <div className="display text-[24px] leading-none">{st}</div>
          <div className="mt-1 flex flex-wrap gap-x-3 text-[12.5px] text-ink-2">
            {c.port && <span>usbip port {c.port}</span>}
            {up > 0 && <span>up {Math.floor(up / 60)} min {Math.floor(up % 60)} s</span>}
            {c.error && <span className="font-semibold text-bad">{c.error}</span>}
          </div>
        </div>
      </div>

      {/* remote mode: the pad is a desktop remote, the game sees a neutral pad.
          Wears the lightbar colour the engine paints while it is on. */}
      <AnimatePresence initial={false}>
        {remote && (
          <motion.div key="remote" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22 }} className="overflow-hidden">
            <div className="note mt-3 flex-wrap" style={{ "--tone": rc } as React.CSSProperties}>
              <MousePointer2 size={22} strokeWidth={2.5} className="flex-none" style={{ color: rc }} />
              <div className="min-w-0 flex-1 basis-[170px] leading-tight">
                <div className="display text-[18px] leading-none">Remote mode</div>
                <div className="mt-1 text-[12.5px] text-ink-2">the pad drives the OS, the game sees a neutral pad</div>
              </div>
              {kb && (
                <span className="sticker flat bg-panel"><Keyboard size={13} strokeWidth={2.5} /> keyboard open</span>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="mt-4 grid grid-cols-2 gap-3">
        {/* battery, drawn */}
        <div className="flex items-center gap-3 px-1">
          <Battery pct={pct} low={low} charging={charging} />
          <div className="min-w-0 leading-tight">
            <div className="eyebrow">battery</div>
            <div className="num text-[26px] leading-none" style={{ color: low ? "var(--color-bad)" : "var(--color-ink)" }}>
              {pct == null ? "—" : pct}<span className="text-[15px] text-ink-2">%</span>
              {charging && <Zap size={16} strokeWidth={2.5} className="ml-1 inline text-triangle" />}
            </div>
            <div className="text-[11.5px] text-ink-3">{bstate || (pct == null ? "unknown" : "")}</div>
          </div>
        </div>

        {/* input rate dial */}
        <div className="flex items-center gap-3 px-1">
          <RateDial rate={rate} />
          <div className="min-w-0 leading-tight">
            <div className="eyebrow">input rate</div>
            <div className="num text-[26px] leading-none">{rate != null ? Math.round(rate) : "—"}</div>
            <div className="text-[11.5px] text-ink-3">reports a second{t?.stale_s != null && <span className="mono">, {(t.stale_s * 1000).toFixed(0)} ms ago</span>}</div>
          </div>
        </div>
      </div>

      {/* flags */}
      <div className="mt-4 flex flex-wrap gap-2.5 px-1">
        <Flag on={!!d?.headphone} icon={<Headphones size={13} strokeWidth={2.5} />} label="headphones" />
        <Flag on={!!d?.mic} icon={<Mic size={13} strokeWidth={2.5} />} label="mic" />
        <Flag on={!!d?.mic_muted} icon={<MicOff size={13} strokeWidth={2.5} />} label="muted" tone="var(--color-amber)" />
        <Flag on={!!c.hide_bluetooth || hideBroken} icon={hideBroken ? <Eye size={13} strokeWidth={2.5} /> : <EyeOff size={13} strokeWidth={2.5} />}
              label={hideBroken ? "BT pad not hidden" : "BT pad hidden"} tone={hideBroken ? "var(--color-warn)" : "var(--color-square)"} />
        {c.echo != null && (
          <Flag on={echoOn || echoBroken} icon={echoBroken || c.echo === "off" ? <EarOff size={13} strokeWidth={2.5} /> : <Ear size={13} strokeWidth={2.5} />}
                label={echoBroken ? "echo cancel degraded" : "echo cancel"} tone={echoBroken ? "var(--color-warn)" : "var(--color-ok)"} />
        )}
      </div>

      {/* the requested hide is not in effect: other apps (Steam, the game) can
          still see the Bluetooth pad -- the doubled-controller symptom */}
      {hideBroken && (
        <div className="note loud mt-3 items-start text-[12.5px] leading-relaxed" style={{ "--tone": "var(--color-amber)" } as React.CSSProperties}>
          <AlertTriangle size={20} strokeWidth={2.5} className="mt-0.5 flex-none" />
          <div>
            <b className="display hl text-[15px]">Hide requested, but not in effect.</b> {c.hide_note?.trim() || HIDE_NOTE_FALLBACK}
          </div>
        </div>
      )}

      {/* echo cancellation is on but the software canceller is not working:
          sound from the PC's other speakers reaches the voice chat */}
      {echoBroken && (
        <div className="note loud mt-3 items-start text-[12.5px] leading-relaxed" style={{ "--tone": "var(--color-amber)" } as React.CSSProperties}>
          <EarOff size={20} strokeWidth={2.5} className="mt-0.5 flex-none" />
          <div>
            <b className="display hl text-[15px]">Echo cancellation only partly working.</b> The controller still removes its own
            speaker from the mic, but game sound from your TV or other speakers can be heard in voice chat
            ({c.echo_note?.trim() || ECHO_NOTE_FALLBACK}). Restarting the bridge usually fixes it.
          </div>
        </div>
      )}
    </section>
  );
}

function Flag({ on, icon, label, tone = "var(--color-cross)" }:
  { on: boolean; icon: React.ReactNode; label: string; tone?: string }) {
  return (
    <span className={"sticker " + (on ? "" : "quiet")} style={on ? { background: tone, color: "var(--color-ink-on)" } : undefined}>
      {icon}{label}
    </span>
  );
}

/* a battery drawn in ink, filled with crayon to the level */
function Battery({ pct, low, charging }: { pct: number | null; low: boolean; charging: boolean }) {
  const frac = pct == null ? 0 : Math.max(0, Math.min(1, pct / 100));
  const fill = low ? "var(--color-circle)" : charging ? "var(--color-cross)" : "var(--color-triangle)";
  return (
    <svg width="64" height="36" viewBox="0 0 64 36" className="flex-none sketch" aria-hidden>
      <rect x="3" y="5" width="52" height="26" rx="6" fill="var(--color-panel)" stroke="var(--color-ink)" strokeWidth="3" />
      <rect x="56" y="12" width="6" height="12" rx="2" fill="var(--color-ink)" />
      <motion.rect x="8" y="10" height="16" rx="3" fill={fill} initial={false}
                   animate={{ width: Math.max(0, 42 * frac) }} transition={{ type: "spring", stiffness: 120, damping: 20 }} />
    </svg>
  );
}

/* 250 Hz is the pad's native rate over the bridge: that is the full sweep. */
function RateDial({ rate }: { rate: number | null }) {
  const frac = Math.max(0, Math.min(1, (rate ?? 0) / 250));
  const tone = frac > 0.8 ? "var(--color-triangle)" : frac > 0.3 ? "var(--color-amber)" : "var(--color-circle)";
  const angle = -120 + 240 * frac;
  return (
    <svg width="64" height="56" viewBox="0 0 64 56" className="flex-none" aria-hidden>
      <g className="sketch">
        <path d="M10 44A26 26 0 1 1 54 44" fill="none" stroke="var(--color-ink)" strokeWidth="3" strokeLinecap="round" />
        {[0, 0.25, 0.5, 0.75, 1].map((f) => {
          const a = (-120 + 240 * f) * Math.PI / 180;
          return <line key={f} x1={32 + 21 * Math.sin(a)} y1={32 - 21 * Math.cos(a)} x2={32 + 26 * Math.sin(a)} y2={32 - 26 * Math.cos(a)}
                       stroke="var(--color-ink)" strokeWidth="2.5" strokeLinecap="round" />;
        })}
      </g>
      <motion.g initial={false} animate={{ rotate: angle }} transition={{ type: "spring", stiffness: 90, damping: 16 }} style={{ transformOrigin: "32px 32px" }}>
        <line x1="32" y1="34" x2="32" y2="12" stroke={tone} strokeWidth="5" strokeLinecap="round" />
        <line x1="32" y1="34" x2="32" y2="12" stroke="var(--color-ink)" strokeWidth="1.5" strokeLinecap="round" strokeDasharray="0 100" />
      </motion.g>
      <circle cx="32" cy="33" r="4.5" fill="var(--color-ink)" />
    </svg>
  );
}
