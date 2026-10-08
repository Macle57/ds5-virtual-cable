import { useActiveController } from "../lib/store";

/* Gyro + accel as crayon strokes from a midline, plus an attitude doodle:
   the pad's tilt recovered from the gravity vector (accel y ~ +1 g at rest,
   8192 raw). +/-9000 raw shows normal motion well on the bars. */
export default function MotionCard() {
  const c = useActiveController();
  const d = c?.telemetry?.decoded ?? null;
  const g = d?.gyro ?? [0, 0, 0];
  const a = d?.accel ?? [0, 8192, 0];
  const roll = Math.atan2(a[0], Math.max(1, a[1])) * (180 / Math.PI);
  const pitch = Math.atan2(a[2], Math.max(1, a[1])) * (180 / Math.PI);

  return (
    <section className="panel pop p-4" style={{ "--i": 4 } as React.CSSProperties}>
      <h2 className="panel-title mb-3">Motion</h2>
      <div className="flex flex-wrap gap-5 px-1">
        <div className="grid min-w-[210px] flex-1 grid-cols-[44px_1fr_52px] items-center gap-x-2.5 gap-y-1.5 text-[12px]">
          <Group label="gyro" />
          <Bar name="pitch" v={g[0]} tone="var(--color-cross)" />
          <Bar name="yaw" v={g[1]} tone="var(--color-cross)" />
          <Bar name="roll" v={g[2]} tone="var(--color-cross)" />
          <Group label="accel" />
          <Bar name="x" v={a[0]} tone="var(--color-square)" />
          <Bar name="y" v={a[1]} tone="var(--color-square)" />
          <Bar name="z" v={a[2]} tone="var(--color-square)" />
        </div>
        <Attitude roll={roll} pitch={pitch} />
      </div>
    </section>
  );
}

function Group({ label }: { label: string }) {
  return <div className="eyebrow col-span-3 mt-1.5 first:mt-0">{label}</div>;
}

/* a crayon stroke from the midline: flat fill, ink outline, slightly uneven ends */
function Bar({ name, v, tone }: { name: string; v: number; tone: string }) {
  const frac = Math.max(-1, Math.min(1, v / 9000));
  const w = Math.abs(frac) * 50;
  return (
    <>
      <span className="text-[12.5px] text-ink-2">{name}</span>
      <div className="relative h-[12px]">
        <span className="absolute inset-x-0 top-1/2 h-[2px] -translate-y-1/2 bg-line-2" />
        <span className="absolute left-1/2 top-[-3px] h-[18px] w-[2.5px] -translate-x-1/2 bg-line" />
        <div className="absolute top-0 h-full border-[2px] border-line transition-[left,width] duration-75"
             style={{ left: frac < 0 ? 50 - w + "%" : "50%", width: w + "%", background: tone, minWidth: w > 0.5 ? 4 : 0,
                      opacity: w > 0.5 ? 1 : 0,
                      borderRadius: frac < 0 ? "7px 3px 3px 6px / 6px 3px 3px 7px" : "3px 7px 6px 3px / 3px 6px 7px 3px" }} />
      </div>
      <span className="num text-right text-[14px] text-ink-2">{v}</span>
    </>
  );
}

/* whole degrees with no "-0" (atan2 of a tiny negative rounds to -0) */
const deg = (v: number) => `${Math.round(v) || 0}°`;

/* the pad doodle tilts with the real thing */
function Attitude({ roll, pitch }: { roll: number; pitch: number }) {
  return (
    <div className="flex w-[118px] flex-col items-center justify-center gap-1 border-[2.5px] border-dashed border-line-2 p-2"
         style={{ borderRadius: "var(--r-small)" }}>
      <div className="eyebrow">attitude</div>
      <div className="grid h-[64px] w-[100px] place-items-center" style={{ perspective: 260 }}>
        <div className="transition-transform duration-100" style={{ transform: `rotateX(${-pitch}deg) rotateZ(${roll}deg)` }}>
          <svg width="84" height="48" viewBox="0 0 84 48" fill="none" aria-hidden className="sketch">
            <path d="M22 8h40c8 0 11 6 13 14l4 14c1 4-3 7-7 6-4-1-7-7-10-9H22c-3 2-6 8-10 9-4 1-8-2-7-6l4-14c2-8 5-14 13-14z"
                  fill="var(--color-cross)" stroke="var(--color-ink)" strokeWidth="3" strokeLinejoin="round" />
            <circle cx="30" cy="26" r="5" fill="var(--color-circle)" stroke="var(--color-ink)" strokeWidth="2.2" />
            <circle cx="54" cy="26" r="5" fill="var(--color-circle)" stroke="var(--color-ink)" strokeWidth="2.2" />
            <circle cx="66" cy="16" r="2.6" fill="var(--color-triangle)" />
            <circle cx="18" cy="16" r="2.6" fill="var(--color-amber)" />
          </svg>
        </div>
      </div>
      {/* two fixed columns, so a sign or a third digit never reflows the
          doodle above: label on top, the number under it, never wrapping */}
      <div className="grid w-full grid-cols-2 text-center leading-tight">
        <span className="eyebrow text-[12px]">roll</span>
        <span className="eyebrow text-[12px]">pitch</span>
        <span className="num whitespace-nowrap text-[13px] text-ink-2">{deg(roll)}</span>
        <span className="num whitespace-nowrap text-[13px] text-ink-2">{deg(pitch)}</span>
      </div>
    </div>
  );
}
