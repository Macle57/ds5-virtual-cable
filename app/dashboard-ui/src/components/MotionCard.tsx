import { useActiveController } from "../lib/store";

/* Gyro + accel as bipolar bars from the midline, plus an attitude widget:
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
    <section className="panel p-4">
      <div className="panel-title mb-3">motion</div>
      <div className="grid grid-cols-[1fr_auto] gap-4">
        <div className="grid grid-cols-[44px_1fr_52px] items-center gap-x-2.5 gap-y-1.5 text-[12px]">
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
  return <div className="eyebrow col-span-3 mt-1 first:mt-0">{label}</div>;
}

function Bar({ name, v, tone }: { name: string; v: number; tone: string }) {
  const frac = Math.max(-1, Math.min(1, v / 9000));
  return (
    <>
      <span className="text-ink-3">{name}</span>
      <div className="relative h-2 overflow-hidden rounded-full bg-well ring-1 ring-line">
        <span className="absolute left-1/2 top-0 h-full w-px bg-line-2" />
        <div className="absolute top-0 h-full rounded-full transition-[left,width] duration-75"
             style={{ left: frac < 0 ? 50 + frac * 50 + "%" : "50%", width: Math.abs(frac) * 50 + "%",
                      background: tone, boxShadow: `0 0 8px ${tone}` }} />
      </div>
      <span className="num text-right text-ink-2">{v}</span>
    </>
  );
}

function Attitude({ roll, pitch }: { roll: number; pitch: number }) {
  return (
    <div className="flex w-[112px] flex-col items-center justify-center gap-2 rounded-xl border border-line bg-well/60 p-2">
      <div className="eyebrow">attitude</div>
      <div className="grid h-[64px] w-[96px] place-items-center" style={{ perspective: 260 }}>
        <div className="h-[36px] w-[76px] rounded-[10px] border border-cross/60 bg-cross/15 transition-transform duration-100"
             style={{ transform: `rotateX(${-pitch}deg) rotateZ(${roll}deg)`, boxShadow: "0 0 18px -4px var(--color-cross), inset 0 1px 0 rgba(255,255,255,.15)" }}>
          <div className="mx-auto mt-2 h-[12px] w-[34px] rounded-[4px] bg-cross/40" />
        </div>
      </div>
      <div className="mono flex gap-2 text-[10px] text-ink-3">
        <span>r {roll.toFixed(0)}°</span><span>p {pitch.toFixed(0)}°</span>
      </div>
    </div>
  );
}
