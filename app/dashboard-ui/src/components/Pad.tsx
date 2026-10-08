import { useRef } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { Decoded, Telemetry } from "../lib/types";

/* ------------------------------------------------------------------------
   The controller, drawn like a crayon cartoon: a chunky sky-blue body with
   a thick warm-black ink outline, flat fills only, coral stick caps, yellow
   shoulder nubs. Every outline passes through a small turbulence wobble so
   nothing is ruler-straight. 1000 x 620 viewBox (same frame as before so
   PadCard's layout is unchanged). Stick travel radius and touchpad mapping
   follow daidr/dualsense-tester, see docs/provenance.md.

   Pressed = the same flat colour with an ink crayon scribble over it (and a
   nudge), never black; paper keys turn crayon-yellow. No burst on press:
   the scribble is the whole effect.
   ------------------------------------------------------------------------ */

const W = 1000, H = 620;
const STICK_TRAVEL = 22;
const TP = { x: 352, y: 118, w: 296, h: 120, rangeX: 1920, rangeY: 1080 };
const STICK = { l: [375, 386] as const, r: [625, 386] as const, well: 52, cap: 40 };
const FACE = { cx: 795, cy: 262, r: 25, off: 52 };
const DPAD = { cx: 205, cy: 262 };
/* shoulders: the L2/R2 paddle sits above the L1/R1 nub, both behind the body */
const SHOULDER = { l: 206, r: 794, padW: 76, padTop: 34, padH: 66, nubW: 108, nubTop: 98, nubH: 56 };
/* the lightbar: two slits hugging the touchpad's sides; create/options further out */
const LB = { w: 14, gap: 9, inset: 12 };
const PILL = { l: 300, r: 700, y: 180 };

const INK = "var(--color-ink)";
const PAPER = "var(--color-bg)";
const SKY = "var(--color-cross)";
const LIT = "var(--color-amber)";           // a paper key, pressed
const HATCH_INK = "url(#hatch-pad-ink)";

const FACE_COLOR: Record<"tri" | "o" | "x" | "sq", string> = {
  tri: "var(--color-triangle)", o: "var(--color-circle)",
  x: "var(--color-cross)", sq: "var(--color-square)",
};
export default function Pad({ t, lightbar }: { t?: Telemetry; lightbar?: string }) {
  const d = t?.decoded ?? null;
  const stale = !t || t.connected === false || (t.age_s ?? 99) > 2.5 ||
                (t.stale_s != null && t.stale_s > 2.5);
  const offline = !d || stale;
  const offlineText = !d ? "no telemetry" :
      (t?.connected === false ? "Bluetooth link lost" : "telemetry stalled");

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
  /* no colour asked for = the bar is "off": the lighter sky, so it still reads as a drawn slit */
  const lb = lightbar ?? "var(--color-cyan)";

  return (
    <svg id="pad" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="DualSense controller state"
         className={"block h-auto w-full " + (offline ? "pad-offline" : "")}>
      <defs>
        {/* the wobble: every outline goes through this so nothing is ruler-straight */}
        <filter id="crayon-pad" x="-4%" y="-4%" width="108%" height="108%" colorInterpolationFilters="sRGB">
          <feTurbulence type="fractalNoise" baseFrequency="0.028" numOctaves="2" seed="7" result="n" />
          <feDisplacementMap in="SourceGraphic" in2="n" scale="3.5" xChannelSelector="R" yChannelSelector="G" />
        </filter>
        {/* crayon hatching: diagonal strokes */}
        <pattern id="hatch-pad-sky" patternUnits="userSpaceOnUse" width="9" height="9" patternTransform="rotate(-38)">
          <line x1="0" y1="0" x2="0" y2="9" stroke={SKY} strokeWidth="4" strokeLinecap="round" />
        </pattern>
        <pattern id="hatch-pad-ink" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(-38)">
          <line x1="0" y1="0" x2="0" y2="8" stroke={INK} strokeWidth="2.6" strokeLinecap="round" />
        </pattern>
        <clipPath id="clip-pad-l2"><rect x={SHOULDER.l - SHOULDER.padW / 2} y={SHOULDER.padTop} width={SHOULDER.padW} height={SHOULDER.padH} rx={16} /></clipPath>
        <clipPath id="clip-pad-r2"><rect x={SHOULDER.r - SHOULDER.padW / 2} y={SHOULDER.padTop} width={SHOULDER.padW} height={SHOULDER.padH} rx={16} /></clipPath>
        <clipPath id="clip-pad-tp"><rect x={TP.x + 3} y={TP.y + 3} width={TP.w - 6} height={TP.h - 6} rx={14} /></clipPath>
      </defs>

      <g className="pad-art">
        {/* ------------------------------------------------------------
            Drawn layer: everything with an ink outline, wobbled.
            ------------------------------------------------------------ */}
        <g filter="url(#crayon-pad)">
          {/* triggers (paddles), then nubs, then the body on top of both */}
          <Paddle cx={SHOULDER.l} value={d?.l2 ?? 0} pressed={!!B.L2} clip="clip-pad-l2" />
          <Paddle cx={SHOULDER.r} value={d?.r2 ?? 0} pressed={!!B.R2} clip="clip-pad-r2" />
          <Nub cx={SHOULDER.l} pressed={!!B.L1} />
          <Nub cx={SHOULDER.r} pressed={!!B.R1} />

          {/* body: fill, then the ink line drawn twice so its weight wavers */}
          <path d={BODY} fill={SKY} stroke={INK} strokeWidth={7.5} strokeLinejoin="round" />
          <path d={BODY} fill="none" stroke={INK} strokeWidth={3.5} strokeLinejoin="round" opacity={0.55}
                transform="translate(1.5 1.2)" />

          {/* grip scribbles, like the reference's short curved lines on each handle */}
          <g fill="none" stroke={INK} strokeWidth={5.5} strokeLinecap="round">
            <path d="M 150 432 C 142 468 146 510 162 544" />
            <path d="M 850 432 C 858 468 854 510 838 544" />
            <path d="M 862 190 C 872 200 878 212 882 226" />
          </g>

          {/* touchpad: a paper rectangle straddling the top edge */}
          <rect x={TP.x} y={TP.y} width={TP.w} height={TP.h} rx={16} className="pad-fill"
                fill={B.touchpad ? "var(--color-panel-2)" : PAPER} stroke={INK} strokeWidth={5.5} />
          {B.touchpad && (
            <rect x={TP.x} y={TP.y} width={TP.w} height={TP.h} rx={16} fill="url(#hatch-pad-sky)" opacity={0.55} />
          )}

          {/* lightbar: two slits down the touchpad's sides, coloured by the prop */}
          <Lightbar color={lb} />

          {/* create / options: paper pills further out */}
          <Pill cx={PILL.l} cy={PILL.y} pressed={!!B.create} />
          <Pill cx={PILL.r} cy={PILL.y} pressed={!!B.options} />

          {/* d-pad: four little paper keys */}
          <g transform={`translate(${DPAD.cx} ${DPAD.cy})`}>
            <DKey x={-11} y={-50} w={22} h={32} on={dp.includes("N")} arrow="M0 -40 l-6 8 h12z" />
            <DKey x={-11} y={18}  w={22} h={32} on={dp.includes("S")} arrow="M0 40 l-6 -8 h12z" />
            <DKey x={-50} y={-11} w={32} h={22} on={dp.includes("W")} arrow="M-40 0 l8 -6 v12z" />
            <DKey x={18}  y={-11} w={32} h={22} on={dp.includes("E")} arrow="M40 0 l-8 -6 v12z" />
          </g>

          {/* face buttons: plain coloured discs with a thin ink glyph */}
          <g transform={`translate(${FACE.cx} ${FACE.cy})`}>
            <Face cx={0} cy={-FACE.off} on={!!B.tri} color={FACE_COLOR.tri}
                  glyph={<path d="M-8 5.5 L0 -8 L8 5.5 Z" />} />
            <Face cx={FACE.off} cy={0} on={!!B.o} color={FACE_COLOR.o}
                  glyph={<circle r={7} />} />
            <Face cx={0} cy={FACE.off} on={!!B.x} color={FACE_COLOR.x}
                  glyph={<path d="M-6.5 -6.5 L6.5 6.5 M6.5 -6.5 L-6.5 6.5" />} />
            <Face cx={-FACE.off} cy={0} on={!!B.sq} color={FACE_COLOR.sq}
                  glyph={<rect x={-6.5} y={-6.5} width={13} height={13} rx={1} />} />
          </g>

          {/* stick wells: a slightly darker dish in the body (the caps live in
              the unfiltered layer so they move cheaply) */}
          <Well cx={STICK.l[0]} cy={STICK.l[1]} />
          <Well cx={STICK.r[0]} cy={STICK.r[1]} />

          {/* PS, mute, speaker scribble */}
          <circle cx={500} cy={412} r={17} className="pad-fill" fill={B.PS ? LIT : PAPER} stroke={INK} strokeWidth={5} />
          <rect x={484} y={444} width={32} height={13} rx={6.5} className="pad-fill"
                fill={B.mute ? LIT : PAPER} stroke={INK} strokeWidth={4.5} />
          <g stroke={INK} strokeWidth={4} strokeLinecap="round">
            {[-20, -10, 0, 10, 20].map((dx) => <line key={dx} x1={500 + dx} y1={471} x2={500 + dx + 2} y2={477} />)}
          </g>
        </g>

        {/* ------------------------------------------------------------
            Live layer: things that move every frame or carry a number.
            ------------------------------------------------------------ */}
        <TriggerValue cx={SHOULDER.l} value={d?.l2 ?? 0} label="L2" />
        <TriggerValue cx={SHOULDER.r} value={d?.r2 ?? 0} label="R2" />
        <text x={500} y={417} textAnchor="middle" className="pad-label" fill={INK}>ps</text>
        {/* mic LED: a dot in the mute pill, orange when muted */}
        <circle cx={500} cy={450.5} r={3.2} fill={d?.mic_muted ? "var(--color-orange)" : (B.mute ? INK : "var(--color-line-2)")}
                className="pad-fill" />

        {/* touchpad fingers: crayon dots with a fading trail */}
        <g clipPath="url(#clip-pad-tp)">
          {Object.entries(trails.current).map(([id, pts]) => (
            <polyline key={id} points={pts.map((p) => p.join(",")).join(" ")}
                      fill="none" stroke="var(--color-amber)" strokeWidth={5} strokeLinecap="round"
                      strokeLinejoin="round" opacity={0.6} />
          ))}
        </g>
        <AnimatePresence>
          {(d?.touch ?? []).filter((p) => p.active).map((p) => {
            const x = TP.x + (p.x / TP.rangeX) * TP.w, y = TP.y + (p.y / TP.rangeY) * TP.h;
            return (
              <motion.g key={p.id} initial={{ opacity: 0, scale: 0.4 }} animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 1.6 }} transition={{ duration: 0.18 }}
                        style={{ transformOrigin: `${x}px ${y}px`, transformBox: "view-box" }}>
                <circle cx={x} cy={y} r={12} fill="var(--color-amber)" stroke={INK} strokeWidth={3.5} />
                <text x={x} y={y + 4.5} textAnchor="middle" className="pad-label" fill={INK}>{p.id}</text>
              </motion.g>
            );
          })}
        </AnimatePresence>

        {/* sticks */}
        <Stick cx={STICK.l[0]} cy={STICK.l[1]} dx={off(d?.lx ?? 128)} dy={off(d?.ly ?? 128)} on={!!B.L3} />
        <Stick cx={STICK.r[0]} cy={STICK.r[1]} dx={off(d?.rx ?? 128)} dy={off(d?.ry ?? 128)} on={!!B.R3} />

      </g>

      {/* offline: the pad goes pencil-grey and the reason is written across it */}
      <AnimatePresence>
        {offline && (
          <motion.g initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}
                    transition={{ type: "spring", stiffness: 400, damping: 22 }}
                    style={{ transformOrigin: "500px 330px", transformBox: "view-box" }}>
            <g transform="rotate(-4 500 330)">
              <text x={500} y={346} textAnchor="middle" className="pad-note" fill={INK}
                    stroke={PAPER} strokeWidth={14} strokeLinejoin="round" paintOrder="stroke">{offlineText}</text>
              {/* a crayon underline, drawn in the "bad" colour */}
              <path d={`M ${500 - offlineText.length * 11} 368 q 90 -8 ${offlineText.length * 11} 0 q 60 6 ${offlineText.length * 11} -4`}
                    fill="none" stroke="var(--color-circle)" strokeWidth={7} strokeLinecap="round" filter="url(#crayon-pad)" />
            </g>
          </motion.g>
        )}
      </AnimatePresence>
    </svg>
  );
}

/* The silhouette. Symmetric about x=500; squared shoulders for the nubs to
   sit on, grips flaring down and out. */
const BODY = [
  "M 500 122",
  "C 430 120 330 122 250 132",   // top edge out to the left shoulder
  "C 200 136 170 150 152 184",   // shoulder corner
  "C 118 236 92 320 80 384",     // outer flank
  "C 68 444 60 504 80 552",      // grip outer
  "C 102 604 176 614 216 580",   // grip bottom
  "C 250 552 272 508 294 474",   // grip inner, rising
  "C 314 450 346 448 390 462",   // crotch into the belly
  "C 430 476 464 490 500 490",
  "C 536 490 570 476 610 462",
  "C 654 448 686 450 706 474",
  "C 728 508 750 552 784 580",
  "C 824 614 898 604 920 552",
  "C 940 504 932 444 920 384",
  "C 908 320 882 236 848 184",
  "C 830 150 800 136 750 132",
  "C 670 122 570 120 500 122 Z",
].join(" ");

/* L2 / R2: a paper paddle that fills with hatching as the trigger goes down. */
function Paddle({ cx, value, pressed, clip }: { cx: number; value: number; pressed: boolean; clip: string }) {
  const x = cx - SHOULDER.padW / 2, y = SHOULDER.padTop, w = SHOULDER.padW, h = SHOULDER.padH;
  const fill = h * (value / 255);
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={16} fill={PAPER} stroke={INK} strokeWidth={5.5} />
      <rect clipPath={`url(#${clip})`} x={x} y={y + h - fill} width={w} height={fill}
            fill={pressed ? HATCH_INK : "url(#hatch-pad-sky)"}
            style={{ transition: "y .04s linear, height .04s linear" }} />
    </g>
  );
}

/* The number on the paddle lives outside the wobble so it stays crisp. */
function TriggerValue({ cx, value, label }: { cx: number; value: number; label: string }) {
  const pct = Math.round((value / 255) * 100);
  return (
    <g textAnchor="middle" fill={INK} stroke={PAPER} strokeLinejoin="round" paintOrder="stroke">
      <text x={cx} y={SHOULDER.padTop + 24} className="pad-label" strokeWidth={4}>{label}</text>
      <text x={cx} y={SHOULDER.padTop + 54} className="pad-num" strokeWidth={6}>{pct}%</text>
    </g>
  );
}

/* L1 / R1: the chunky yellow nub on the shoulder. Pressed, it scribbles
   over in ink and sinks a touch. */
function Nub({ cx, pressed }: { cx: number; pressed: boolean }) {
  const x = cx - SHOULDER.nubW / 2, top = SHOULDER.nubTop, r = 24;
  const d = `M ${x} ${top + SHOULDER.nubH} V ${top + r} q 0 -${r} ${r} -${r} h ${SHOULDER.nubW - 2 * r} q ${r} 0 ${r} ${r} V ${top + SHOULDER.nubH} z`;
  return (
    <g transform={pressed ? "translate(0 4)" : undefined}>
      <path d={d} fill={LIT} stroke={INK} strokeWidth={5.5} strokeLinejoin="round" />
      {pressed && <path d={d} fill={HATCH_INK} opacity={0.55} />}
    </g>
  );
}

/* The lightbar: two slits beside the touchpad, filled with the prop colour
   and outlined in ink so they read even when the colour matches the body. */
function Lightbar({ color }: { color: string }) {
  const y = TP.y + LB.inset, h = TP.h - LB.inset * 2;
  const l = TP.x - LB.gap - LB.w, r = TP.x + TP.w + LB.gap;
  return (
    <g className="pad-fill" fill={color} stroke={INK} strokeWidth={4.5}>
      <rect x={l} y={y} width={LB.w} height={h} rx={LB.w / 2} />
      <rect x={r} y={y} width={LB.w} height={h} rx={LB.w / 2} />
    </g>
  );
}

function Pill({ cx, cy, pressed }: { cx: number; cy: number; pressed: boolean }) {
  return (
    <rect x={cx - 8} y={cy - 20} width={16} height={40} rx={8} className="pad-fill"
          fill={pressed ? LIT : PAPER} stroke={INK} strokeWidth={4.5} />
  );
}

function DKey({ x, y, w, h, on, arrow }: { x: number; y: number; w: number; h: number; on: boolean; arrow: string }) {
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx={7} className="pad-fill"
            fill={on ? LIT : PAPER} stroke={INK} strokeWidth={4.5} />
      <path d={arrow} fill={INK} />
    </g>
  );
}

function Face({ cx, cy, on, color, glyph }:
  { cx: number; cy: number; on: boolean; color: string; glyph: React.ReactNode }) {
  return (
    <g transform={`translate(${cx} ${cy})${on ? " scale(.9)" : ""}`}>
      <circle r={FACE.r} fill={color} stroke={INK} strokeWidth={5} />
      {on && <circle r={FACE.r} fill={HATCH_INK} opacity={0.5} />}
      <g fill="none" stroke={INK} strokeWidth={3} strokeLinejoin="round" strokeLinecap="round">
        {glyph}
      </g>
    </g>
  );
}

/* The dish a stick sits in: the body colour, shaded a step darker with a
   flat ink wash, and outlined. */
function Well({ cx, cy }: { cx: number; cy: number }) {
  return (
    <g>
      <circle cx={cx} cy={cy} r={STICK.well} fill={INK} opacity={0.16} />
      <circle cx={cx} cy={cy} r={STICK.well} fill="none" stroke={INK} strokeWidth={5.5} />
    </g>
  );
}

/* A coral cap that slides with the axis; pushed, it drags three short
   motion lines behind it. Clicked (L3/R3) it scribbles over and shrinks. */
function Stick({ cx, cy, dx, dy, on }: { cx: number; cy: number; dx: number; dy: number; on: boolean }) {
  const mag = Math.min(1, Math.hypot(dx, dy) / STICK_TRAVEL);
  const back = Math.atan2(dy, dx) + Math.PI;           // where it came from
  const show = mag > 0.3;
  /* speed lines: three short strokes just outside the dish, on the side the
     cap left; they stay put while the cap moves */
  const lines = [-0.42, 0, 0.42].map((da, i) => {
    const a = back + da, r0 = STICK.well + 9, r1 = r0 + (i === 1 ? 14 : 8) + mag * 8;
    return <line key={i} x1={cx + Math.cos(a) * r0} y1={cy + Math.sin(a) * r0}
                 x2={cx + Math.cos(a) * r1} y2={cy + Math.sin(a) * r1} />;
  });
  return (
    <g filter="url(#crayon-pad)">
      <g stroke={INK} strokeWidth={4.5} strokeLinecap="round" opacity={show ? Math.min(1, (mag - 0.3) * 1.6) : 0}
         style={{ transition: "opacity .1s" }}>
        {lines}
      </g>
      <g style={{ transform: `translate(${dx}px, ${dy}px)${on ? ` translate(${cx}px, ${cy}px) scale(.92) translate(${-cx}px, ${-cy}px)` : ""}`,
                  transition: "transform .04s linear" }}>
        <circle cx={cx} cy={cy} r={STICK.cap} fill="var(--color-circle)" stroke={INK} strokeWidth={5.5} />
        {on && <circle cx={cx} cy={cy} r={STICK.cap} fill={HATCH_INK} opacity={0.5} />}
        <path d={`M ${cx - 16} ${cy - 10} q 7 -12 22 -8`} fill="none"
              stroke={INK} strokeWidth={4} strokeLinecap="round" opacity={0.45} />
      </g>
    </g>
  );
}

export type { Decoded };
