import { Settings2, MousePointer2 } from "lucide-react";
import { useRemoteColor, useRemoteCount, useStore } from "../lib/store";

const LINK_LABEL = {
  connecting: "connecting", live: "live", snapshot: "snapshot", reconnecting: "reconnecting",
};

/* The title bar: the wordmark written in marker with a scribbled underline,
   a few hand-lettered facts about the bridge, the link dot, and Settings. */
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
    <header className="sticky top-0 z-20 bg-bg">
      <div className="mx-auto flex max-w-[1320px] flex-wrap items-center gap-x-5 gap-y-2 px-5 pb-2 pt-3">
        <a className="flex items-center gap-3" href="/" aria-label="DualSense Bridge">
          <Logo />
          <span className="relative leading-none">
            <span className="display block text-[27px] leading-none tracking-[-.01em]">DualSense Bridge</span>
            <Underline />
          </span>
        </a>

        {count > 0 && (
          <div className="ml-1 hidden items-center gap-3 md:flex">
            <Fact value={`${agg.running ?? "?"} of ${agg.present ?? count}`} label="bridged" />
            <Fact value={String(Math.round(agg.reports_per_s ?? 0))} label="reports a second" />
            {agg.master_enabled === false && <Fact value="off" label="bridging" tone="var(--color-bad)" />}
            {remoteCount > 0 && (
              <Fact value={String(remoteCount)} label="in remote mode" tone={rc} icon={<MousePointer2 size={13} strokeWidth={2.5} />} />
            )}
          </div>
        )}

        <div className="flex-1" />

        <div className="display flex items-center gap-2 text-[15px] text-ink-2" title={`link: ${LINK_LABEL[link]}`}>
          <InkDot tone={on ? "var(--color-ok)" : "var(--color-bad)"} pulse={link === "live"} />
          {LINK_LABEL[link]}
        </div>

        <button className="btn" onClick={() => toggleSettings()} aria-pressed={settingsOpen}>
          <Settings2 size={17} strokeWidth={2.5} /> Settings
        </button>
      </div>
      <div className="rule-ink" aria-hidden />
    </header>
  );
}

/* a number and what it is, written like a note in the margin */
function Fact({ value, label, tone, icon }: { value: string; label: string; tone?: string; icon?: React.ReactNode }) {
  return (
    <span className="flex items-baseline gap-1.5 leading-none">
      {icon && <span className="self-center" style={{ color: tone }}>{icon}</span>}
      <span className="num text-[19px]" style={tone ? { color: tone } : undefined}>{value}</span>
      <span className="eyebrow">{label}</span>
    </span>
  );
}

/* a filled dot with an ink outline -- a crayon dot, not an LED */
export function InkDot({ tone, pulse, size = 13 }: { tone: string; pulse?: boolean; size?: number }) {
  return (
    <span className={"inline-block flex-none border-[2.5px] border-line " + (pulse ? "pulse" : "")}
          style={{ width: size, height: size, background: tone, borderRadius: "55% 45% 50% 50% / 50% 55% 45% 50%" }} />
  );
}

function Underline() {
  return (
    <svg className="absolute -bottom-2 left-0 h-[9px] w-full" viewBox="0 0 200 9" preserveAspectRatio="none" fill="none" aria-hidden>
      <path className="scribble-draw" d="M3 6c30-5 50 2 80-1s45-4 70 0 30-2 44 0" stroke="var(--color-cross)" strokeWidth="3.5" strokeLinecap="round" pathLength="400" />
    </svg>
  );
}

/* the little pad, drawn in four strokes */
function Logo() {
  return (
    <span className="grid h-11 w-12 flex-none place-items-center">
      <svg className="sketch" width="44" height="30" viewBox="0 0 44 30" fill="none" aria-hidden>
        <path d="M11 3h22c5 0 7 4 8 9l2 10c.5 3-2 5-5 4-3-1-5-5-7-6H13c-2 1-4 5-7 6-3 1-5.5-1-5-4l2-10c1-5 3-9 8-9z"
              fill="var(--color-cross)" stroke="var(--color-ink)" strokeWidth="2.6" strokeLinejoin="round" />
        <circle cx="16" cy="13" r="3.2" fill="var(--color-circle)" stroke="var(--color-ink)" strokeWidth="2" />
        <circle cx="29" cy="13" r="3.2" fill="var(--color-circle)" stroke="var(--color-ink)" strokeWidth="2" />
        <circle cx="35" cy="8.5" r="1.6" fill="var(--color-triangle)" />
        <circle cx="9" cy="8.5" r="1.6" fill="var(--color-amber)" />
      </svg>
    </span>
  );
}
