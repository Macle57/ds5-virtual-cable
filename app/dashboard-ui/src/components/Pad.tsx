import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { ButtonName, Decoded, Telemetry } from "../lib/types";

/* ------------------------------------------------------------------------
   The controller, drawn from scratch (geometric primitives; layout
   conventions -- stick travel radius, touchpad coordinate mapping -- follow
   daidr/dualsense-tester, see docs/provenance.md). 1000 x 620 viewBox.
   ------------------------------------------------------------------------ */

const W = 1000, H = 620;
const STICK_TRAVEL = 22;
const TP = { x: 352, y: 134, w: 296, h: 122, rangeX: 1920, rangeY: 1080 };
const STICK = { l: [368, 400] as const, r: [632, 400] as const, well: 58, cap: 39 };
const FACE = { cx: 800, cy: 268, r: 27, off: 54 };
const DPAD = { cx: 200, cy: 268 };

/* Where each button lives, for the press burst. */
const BURST_AT: Partial<Record<ButtonName | "dpad", [number, number]>> = {
  tri: [FACE.cx, FACE.cy - FACE.off], o: [FACE.cx + FACE.off, FACE.cy],
  x: [FACE.cx, FACE.cy + FACE.off], sq: [FACE.cx - FACE.off, FACE.cy],
  L1: [190, 118], R1: [810, 118], L2: [190, 55], R2: [810, 55],
  create: [318, 172], options: [682, 172], PS: [500, 410], mute: [500, 458],
  touchpad: [500, 191], L3: [...STICK.l], R3: [...STICK.r], dpad: [DPAD.cx, DPAD.cy],
};

const FACE_COLOR: Record<"tri" | "o" | "x" | "sq", string> = {
  tri: "var(--color-triangle)", o: "var(--color-circle)",
  x: "var(--color-cross)", sq: "var(--color-square)",
};

interface Burst { id: number; x: number; y: number; color: string }
let burstSeq = 0;

export default function Pad({ t, lightbar }: { t?: Telemetry; lightbar?: string }) {
  const d = t?.decoded ?? null;
  const stale = !t || t.connected === false || (t.age_s ?? 99) > 2.5 ||
                (t.stale_s != null && t.stale_s > 2.5);
  const offline = !d || stale;
  const offlineText = !d ? "no telemetry" :
      (t?.connected === false ? "Bluetooth link lost" : "telemetry stalled");

  /* press bursts: a ring on every rising edge */
  const prev = useRef<Partial<Record<string, boolean>>>({});
  const [bursts, setBursts] = useState<Burst[]>([]);
  useEffect(() => {
    if (!d) return;
    const now: Partial<Record<string, boolean>> = { ...d.buttons, dpad: d.dpad !== "-" && !!d.dpad };
    const fresh: Burst[] = [];
    for (const [name, on] of Object.entries(now)) {
      if (on && !prev.current[name]) {
        const at = BURST_AT[name as ButtonName];
        if (at) fresh.push({
          id: ++burstSeq, x: at[0], y: at[1],
          color: FACE_COLOR[name as keyof typeof FACE_COLOR] ?? "var(--color-cyan)",
        });
      }
    }
    prev.current = now;
    if (fresh.length) {
      setBursts((b) => [...b, ...fresh]);
      const ids = fresh.map((f) => f.id);
      setTimeout(() => setBursts((b) => b.filter((x) => !ids.includes(x.id))), 600);
    }
  }, [d]);

  /* touch trails: last ~14 positions per finger */
  const trails = useRef<Record<number, [number, number][]>>({});
  if (d) {
    const seen = new Set<number>();
    for (const p of d.touch) {
      if (!p.active) continue;
      seen.add(p.id);
      const arr = (trails.current[p.id] ??= []);
      arr.push([TP.x + (p.x / TP.rangeX) * TP.w, TP.y + (p.y / TP.rangeY) * TP.h]);
      if (arr.length > 14) arr.shift();
    }
    for (const k of Object.keys(trails.current)) if (!seen.has(+k)) delete trails.current[+k];
  }

  const B = d?.buttons ?? {};
  const dp = d?.dpad ?? "-";
  const off = (v: number) => ((v - 128) / 128) * STICK_TRAVEL;

  return (
    <svg id="pad" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="DualSense controller state"
         className={"block h-auto w-full " + (offline ? "pad-offline" : "")}>
      <defs>
        <linearGradient id="body" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2a3244" />
          <stop offset="55%" stopColor="#1c2230" />
          <stop offset="100%" stopColor="#141924" />
        </linearGradient>
        <radialGradient id="sheen" cx="50%" cy="0%" r="80%">
          <stop offset="0%" stopColor="rgba(255,255,255,.10)" />
          <stop offset="60%" stopColor="rgba(255,255,255,0)" />
        </radialGradient>
        <radialGradient id="underglow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor={lightbar ?? "var(--color-cross)"} stopOpacity=".35" />
          <stop offset="100%" stopColor={lightbar ?? "var(--color-cross)"} stopOpacity="0" />
        </radialGradient>
        <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="4" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
        <filter id="glow-lg" x="-80%" y="-80%" width="260%" height="260%">
          <feGaussianBlur stdDeviation="9" result="b" />
          <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
        <clipPath id="clip-l2"><rect x={148} y={22} width={84} height={66} rx={12} /></clipPath>
        <clipPath id="clip-r2"><rect x={768} y={22} width={84} height={66} rx={12} /></clipPath>
        <clipPath id="clip-tp"><rect x={TP.x} y={TP.y} width={TP.w} height={TP.h} rx={18} /></clipPath>
      </defs>

      {/* under-glow puddle */}
      <ellipse cx={500} cy={380} rx={430} ry={180} fill="url(#underglow)" className="transition-opacity duration-500"
               style={{ opacity: offline ? 0.15 : 0.9 }} />

      <g className="dim">
        {/* ---- triggers: analogue fill + digital light ---- */}
        <Trigger x={148} clip="clip-l2" value={d?.l2 ?? 0} pressed={!!B.L2} label="L2" />
        <Trigger x={768} clip="clip-r2" value={d?.r2 ?? 0} pressed={!!B.R2} label="R2" />

        {/* ---- bumpers ---- */}
        <Bumper x={148} pressed={!!B.L1} label="L1" />
        <Bumper x={768} pressed={!!B.R1} label="R1" />

        {/* ---- body ---- */}
        <path className="body" fill="url(#body)" stroke="#39425a" strokeWidth={2} d={BODY} />
        <path fill="url(#sheen)" d={BODY} pointerEvents="none" />
        {/* the two-tone inner shell wrapping the sticks and the belly */}
        <path d={INNER} fill="#0e121b" stroke="#252d3e" strokeWidth={1.5} />

        {/* ---- lightbar: strips hugging the touchpad ---- */}
        <Lightbar color={lightbar} />

        {/* ---- touchpad ---- */}
        <g>
          <rect x={TP.x} y={TP.y} width={TP.w} height={TP.h} rx={18}
                fill={B.touchpad ? "color-mix(in oklab, var(--color-cross) 25%, #0b0f18)" : "#0b0f18"}
                stroke={B.touchpad ? "var(--color-cross)" : "#2a3244"} strokeWidth={2}
                style={{ transition: "fill .08s, stroke .08s" }} />
          <g clipPath="url(#clip-tp)">
            {Object.entries(trails.current).map(([id, pts]) => (
              <polyline key={id} points={pts.map((p) => p.join(",")).join(" ")}
                        fill="none" stroke="var(--color-cyan)" strokeWidth={3} strokeLinecap="round"
                        strokeLinejoin="round" opacity={0.45} />
            ))}
          </g>
          <AnimatePresence>
            {(d?.touch ?? []).filter((p) => p.active).map((p) => {
              const x = TP.x + (p.x / TP.rangeX) * TP.w, y = TP.y + (p.y / TP.rangeY) * TP.h;
              return (
                <motion.g key={p.id} initial={{ opacity: 0, scale: 0.4 }} animate={{ opacity: 1, scale: 1 }}
                          exit={{ opacity: 0, scale: 1.8 }} transition={{ duration: 0.18 }}
                          style={{ transformOrigin: `${x}px ${y}px`, transformBox: "view-box" }}>
                  <circle cx={x} cy={y} r={18} fill="none" stroke="var(--color-cyan)" strokeWidth={2} opacity={0.5} />
                  <circle cx={x} cy={y} r={11} fill="var(--color-cyan)" filter="url(#glow)" />
                  <text x={x} y={y + 4} textAnchor="middle" fill="#05070d"
                        style={{ font: "700 12px var(--font-display)" }}>{p.id}</text>
                </motion.g>
              );
            })}
          </AnimatePresence>
        </g>

        {/* ---- create / options ---- */}
        <Pill x={311} y={152} w={14} h={40} pressed={!!B.create} />
        <Pill x={675} y={152} w={14} h={40} pressed={!!B.options} />

        {/* ---- d-pad ---- */}
        <g transform={`translate(${DPAD.cx} ${DPAD.cy})`}>
          <circle r={64} fill="#0f1420" stroke="#2a3244" strokeWidth={1.5} />
          <DKey x={-17} y={-60} w={34} h={44} on={dp.includes("N")} arrow="M0 -44 l-8 10 h16z" />
          <DKey x={-17} y={16}  w={34} h={44} on={dp.includes("S")} arrow="M0 44 l-8 -10 h16z" />
          <DKey x={-60} y={-17} w={44} h={34} on={dp.includes("W")} arrow="M-44 0 l10 -8 v16z" />
          <DKey x={16}  y={-17} w={44} h={34} on={dp.includes("E")} arrow="M44 0 l-10 -8 v16z" />
        </g>

        {/* ---- face buttons ---- */}
        <g transform={`translate(${FACE.cx} ${FACE.cy})`}>
          <Face cx={0} cy={-FACE.off} on={!!B.tri} color={FACE_COLOR.tri}
                glyph={<path d="M-11 7 L0 -11 L11 7 Z" />} />
          <Face cx={FACE.off} cy={0} on={!!B.o} color={FACE_COLOR.o}
                glyph={<circle r={10} />} />
          <Face cx={0} cy={FACE.off} on={!!B.x} color={FACE_COLOR.x}
                glyph={<path d="M-9 -9 L9 9 M9 -9 L-9 9" />} />
          <Face cx={-FACE.off} cy={0} on={!!B.sq} color={FACE_COLOR.sq}
                glyph={<rect x={-9} y={-9} width={18} height={18} />} />
        </g>

        {/* ---- sticks ---- */}
        <Stick cx={STICK.l[0]} cy={STICK.l[1]} dx={off(d?.lx ?? 128)} dy={off(d?.ly ?? 128)} on={!!B.L3} />
        <Stick cx={STICK.r[0]} cy={STICK.r[1]} dx={off(d?.rx ?? 128)} dy={off(d?.ry ?? 128)} on={!!B.R3} />

        {/* ---- PS + mute ---- */}
        <g>
          <rect x={478} y={394} width={44} height={32} rx={11}
                fill={B.PS ? "var(--color-cross)" : "#151b2b"} stroke={B.PS ? "var(--color-cross)" : "#2a3244"}
                strokeWidth={1.5} filter={B.PS ? "url(#glow)" : undefined} style={{ transition: "fill .08s" }} />
          <text x={500} y={415} textAnchor="middle" fill={B.PS ? "#05070d" : "var(--color-ink-3)"}
                style={{ font: "700 13px var(--font-display)", letterSpacing: ".1em" }}>PS</text>
          <rect x={486} y={452} width={28} height={12} rx={6}
                fill={B.mute ? "var(--color-amber)" : "#151b2b"} stroke={B.mute ? "var(--color-amber)" : "#2a3244"}
                strokeWidth={1.5} filter={B.mute ? "url(#glow)" : undefined} style={{ transition: "fill .08s" }} />
          {/* mic grille dots */}
          {[-18, -9, 0, 9, 18].map((dx) => (
            <circle key={dx} cx={500 + dx} cy={480} r={1.6} fill="#2f3850" />
          ))}
        </g>
      </g>

      {/* ---- press bursts ---- */}
      <AnimatePresence>
        {bursts.map((b) => (
          <motion.circle key={b.id} cx={b.x} cy={b.y} fill="none" stroke={b.color} strokeWidth={3}
                         initial={{ r: 14, opacity: 0.9 }} animate={{ r: 64, opacity: 0 }}
                         transition={{ duration: 0.55, ease: "easeOut" }} pointerEvents="none" />
        ))}
      </AnimatePresence>

      {/* ---- offline veil ---- */}
      <AnimatePresence>
        {offline && (
          <motion.g initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <rect x={290} y={292} width={420} height={56} rx={14} fill="#05070d" opacity={0.9}
                  stroke="var(--color-warn)" strokeOpacity={0.6} />
            <text x={500} y={328} textAnchor="middle" fill="var(--color-warn)"
                  style={{ font: "700 22px var(--font-display)", letterSpacing: ".14em", textTransform: "uppercase" }}>
              {offlineText}
            </text>
          </motion.g>
        )}
      </AnimatePresence>
      <style>{`
        #pad .dim { transition: opacity .3s; }
        #pad.pad-offline .dim { opacity: .35; }
      `}</style>
    </svg>
  );
}

/* The silhouette. Symmetric about x=500; grips flare down and out. */
const BODY = [
  "M 500 112",
  "C 410 110 300 116 236 128",            // top edge to the left shoulder
  "C 196 136 164 150 144 172",            // shoulder round
  "C 104 218 78 296 66 356",              // outer flank
  "C 56 410 46 486 62 536",               // grip outer
  "C 80 594 138 612 182 586",             // grip bottom
  "C 220 562 250 516 276 480",            // grip inner edge, rising
  "C 296 460 330 458 380 470",            // crotch into the belly
  "C 420 482 460 500 500 500",
  "C 540 500 580 482 620 470",
  "C 670 458 704 460 724 480",
  "C 750 516 780 562 818 586",
  "C 862 612 920 594 938 536",
  "C 954 486 944 410 934 356",
  "C 922 296 896 218 856 172",
  "C 836 150 804 136 764 128",
  "C 700 116 590 110 500 112 Z",
].join(" ");

/* The DualSense's black inner panel: from the touchpad's flanks, around
   both stick wells, forming the belly edge between the grips. */
const INNER = [
  "M 346 258",
  "C 318 300 288 330 292 386",
  "C 296 446 328 478 382 480",
  "C 430 488 462 494 500 494",
  "C 538 494 570 488 618 480",
  "C 672 478 704 446 708 386",
  "C 712 330 682 300 654 258 Z",
].join(" ");

function Trigger({ x, clip, value, pressed, label }:
  { x: number; clip: string; value: number; pressed: boolean; label: string }) {
  const h = 66 * (value / 255);
  const lit = pressed || value > 120;
  return (
    <g>
      <rect x={x} y={22} width={84} height={66} rx={12} fill="#0b0f18" stroke={pressed ? "var(--color-cross)" : "#2a3244"} strokeWidth={1.5} />
      <rect clipPath={`url(#${clip})`} x={x} y={88 - h} width={84} height={h}
            fill="var(--color-cross)" opacity={0.85} style={{ transition: "y .04s linear, height .04s linear" }} />
      <text x={x + 42} y={52} textAnchor="middle" fill={lit ? "#fff" : "var(--color-ink-3)"}
            style={{ font: "700 15px var(--font-display)", letterSpacing: ".08em" }}>{label}</text>
      <text x={x + 42} y={76} textAnchor="middle" fill={lit ? "#fff" : "var(--color-ink-3)"} opacity={lit ? 0.85 : 1}
            style={{ font: "600 12px var(--font-display)" }}>{Math.round((value / 255) * 100)}%</text>
    </g>
  );
}

function Bumper({ x, pressed, label }: { x: number; pressed: boolean; label: string }) {
  return (
    <g>
      <path d={`M${x} 112 q42 -16 84 0 l0 22 q-42 -12 -84 0 z`} strokeWidth={1.5}
            fill={pressed ? "var(--color-cross)" : "#151b2b"} stroke={pressed ? "var(--color-cross)" : "#2a3244"}
            filter={pressed ? "url(#glow)" : undefined} style={{ transition: "fill .08s" }} />
      <text x={x + 42} y={123} textAnchor="middle" fill={pressed ? "#05070d" : "var(--color-ink-3)"}
            style={{ font: "700 11px var(--font-display)", letterSpacing: ".08em" }}>{label}</text>
    </g>
  );
}

function Lightbar({ color }: { color?: string }) {
  const c = color ?? "var(--color-cross)";
  return (
    <g filter="url(#glow-lg)">
      <path d={`M ${TP.x - 8} ${TP.y + 6} q -6 60 0 ${TP.h - 12}`} fill="none" stroke={c} strokeWidth={5} strokeLinecap="round" opacity={0.95} />
      <path d={`M ${TP.x + TP.w + 8} ${TP.y + 6} q 6 60 0 ${TP.h - 12}`} fill="none" stroke={c} strokeWidth={5} strokeLinecap="round" opacity={0.95} />
    </g>
  );
}

function Pill({ x, y, w, h, pressed }: { x: number; y: number; w: number; h: number; pressed: boolean }) {
  return (
    <rect x={x} y={y} width={w} height={h} rx={w / 2} strokeWidth={1.5}
          fill={pressed ? "var(--color-cross)" : "#151b2b"} stroke={pressed ? "var(--color-cross)" : "#2a3244"}
          filter={pressed ? "url(#glow)" : undefined} style={{ transition: "fill .08s" }} />
  );
}

function DKey({ x, y, w, h, on, arrow }: { x: number; y: number; w: number; h: number; on: boolean; arrow: string }) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={9} strokeWidth={1.5}
            fill={on ? "var(--color-cross)" : "#151b2b"} stroke={on ? "var(--color-cross)" : "#2a3244"}
            filter={on ? "url(#glow)" : undefined} style={{ transition: "fill .08s" }} />
      <path d={arrow} fill={on ? "#05070d" : "#3a4460"} />
    </g>
  );
}

function Face({ cx, cy, on, color, glyph }:
  { cx: number; cy: number; on: boolean; color: string; glyph: React.ReactNode }) {
  return (
    <g transform={`translate(${cx} ${cy})`} filter={on ? "url(#glow)" : undefined}>
      <circle r={FACE.r} fill={on ? color : "#151b2b"} stroke={on ? color : "#2a3244"} strokeWidth={1.5}
              style={{ transition: "fill .08s" }} />
      <g fill="none" stroke={on ? "#05070d" : color} strokeWidth={3} strokeLinejoin="round" strokeLinecap="round">
        {glyph}
      </g>
    </g>
  );
}

function Stick({ cx, cy, dx, dy, on }: { cx: number; cy: number; dx: number; dy: number; on: boolean }) {
  const mag = Math.min(1, Math.hypot(dx, dy) / STICK_TRAVEL);
  return (
    <g>
      <circle cx={cx} cy={cy} r={STICK.well} fill="#080b13" stroke="#2a3244" strokeWidth={1.5} />
      <circle cx={cx} cy={cy} r={STICK.well - 6} fill="none" stroke="var(--color-cross)" strokeWidth={1.5}
              strokeDasharray="3 6" opacity={0.25 + mag * 0.6} />
      <line x1={cx} y1={cy} x2={cx + dx} y2={cy + dy} stroke="var(--color-cyan)" strokeWidth={3}
            strokeLinecap="round" opacity={mag * 0.9} style={{ transition: "x2 .04s linear, y2 .04s linear" }} />
      <g style={{ transform: `translate(${dx}px, ${dy}px)`, transition: "transform .04s linear" }}>
        <circle cx={cx} cy={cy} r={STICK.cap} fill={on ? "var(--color-cross)" : "#1c2230"}
                stroke={on ? "var(--color-cross)" : "#3a4460"} strokeWidth={2}
                filter={on ? "url(#glow)" : undefined} style={{ transition: "fill .08s" }} />
        <circle cx={cx} cy={cy} r={STICK.cap - 12} fill="none" stroke={on ? "#05070d" : "#3a4460"} strokeWidth={2} />
        <circle cx={cx} cy={cy} r={4} fill={on ? "#05070d" : "var(--color-cross)"} opacity={0.9} />
      </g>
    </g>
  );
}

export type { Decoded };
