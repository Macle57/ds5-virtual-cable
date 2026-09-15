/* A 28px icon for each key in the config's chord/gesture vocabulary, in the
   face-button colour where it has one. Falls back to a labelled pill. */
export default function ButtonGlyph({ name, size = 30 }: { name: string; size?: number }) {
  const s = size;
  const wrap = (child: React.ReactNode, color = "var(--color-ink-2)") => (
    <svg width={s} height={s} viewBox="-15 -15 30 30" fill="none" stroke={color} strokeWidth={2.2}
         strokeLinejoin="round" strokeLinecap="round" style={{ filter: `drop-shadow(0 0 5px ${color})` }} aria-hidden>
      {child}
    </svg>
  );
  switch (name) {
    case "cross": return wrap(<><circle r={13} strokeOpacity={0.35} /><path d="M-6 -6 L6 6 M6 -6 L-6 6" /></>, "var(--color-cross)");
    case "circle": return wrap(<><circle r={13} strokeOpacity={0.35} /><circle r={6.5} /></>, "var(--color-circle)");
    case "square": return wrap(<><circle r={13} strokeOpacity={0.35} /><rect x={-6} y={-6} width={12} height={12} /></>, "var(--color-square)");
    case "triangle": return wrap(<><circle r={13} strokeOpacity={0.35} /><path d="M-7 5 L0 -7 L7 5 Z" /></>, "var(--color-triangle)");
    case "dpad_up": return wrap(dpad("M0 -13 l-6 7 h12z"));
    case "dpad_down": return wrap(dpad("M0 13 l-6 -7 h12z"));
    case "dpad_left": return wrap(dpad("M-13 0 l7 -6 v12z"));
    case "dpad_right": return wrap(dpad("M13 0 l-7 -6 v12z"));
    case "l1": case "r1": return pill(name.toUpperCase());
    case "l3": case "r3": return wrap(<><circle r={12} /><circle r={5} fill="currentColor" stroke="none" style={{ color: "var(--color-cross)" }} /></>, "var(--color-cross)");
    case "create": return wrap(<><rect x={-4} y={-11} width={8} height={22} rx={4} /><path d="M-9 -3 v6 M9 -3 v6" strokeOpacity={0.4} /></>);
    case "options": return wrap(<><rect x={-4} y={-11} width={8} height={22} rx={4} /><path d="M-2 -5 h4 M-2 0 h4 M-2 5 h4" strokeWidth={1.4} /></>);
    case "ps": return pill("PS", "var(--color-cross)");
    case "touchpad_click": return wrap(<><rect x={-13} y={-8} width={26} height={16} rx={4} /><circle cx={4} cy={1} r={2.5} fill="var(--color-cyan)" stroke="none" /></>, "var(--color-cyan)");
    case "mute": return wrap(<><rect x={-3} y={-11} width={6} height={13} rx={3} /><path d="M-7 -2 a7 7 0 0 0 14 0 M0 5 v6 M-4 11 h8" /><path d="M-9 -9 L9 9" strokeOpacity={0.8} /></>, "var(--color-amber)");
    default: {
      // the touch gestures: one glyph per motion, and a "_pressed" variant
      // framed by the clicked pad's outline
      const pressed = name.endsWith("_pressed");
      const base = pressed ? name.slice(0, -"_pressed".length) : name;
      const g = GESTURES[base];
      if (!g) return pill(name.slice(0, 3).toUpperCase());
      return wrap(<>{pressed && <rect x={-14} y={-14} width={28} height={28} rx={5} strokeOpacity={0.55} strokeWidth={1.6} />}{g}</>, "var(--color-cyan)");
    }
  }
}

const dot = (cx: number, cy: number) => <circle cx={cx} cy={cy} r={2} fill="var(--color-cyan)" stroke="none" />;
const GESTURES: Record<string, React.ReactNode> = {
  touch_slide_horizontal: <><path d="M-12 0 h24 M-12 0 l4 -4 M-12 0 l4 4 M12 0 l-4 -4 M12 0 l-4 4" />{dot(-3, -7)}{dot(3, -7)}</>,
  touch_slide_vertical: <><path d="M0 -12 v24 M0 -12 l-4 4 M0 -12 l4 4 M0 12 l-4 -4 M0 12 l4 -4" />{dot(-7, -3)}{dot(-7, 3)}</>,
  touch_swipe_up: <><path d="M0 12 v-22 M0 -10 l-5 5 M0 -10 l5 5" />{dot(-8, 9)}{dot(8, 9)}</>,
  touch_swipe_down: <><path d="M0 -12 v22 M0 10 l-5 -5 M0 10 l5 -5" />{dot(-8, -9)}{dot(8, -9)}</>,
  touch_tap_2f: <><circle cx={-4} cy={0} r={4} /><circle cx={5} cy={-3} r={4} /><path d="M-11 -8 a11 11 0 0 1 4 -4 M8 6 a11 11 0 0 1 -4 4" strokeOpacity={0.6} /></>,
  touch_click_2f: <><rect x={-13} y={-8} width={26} height={16} rx={4} strokeOpacity={0.55} />{dot(-4, 0)}{dot(4, 0)}<path d="M0 -14 v3 M-5 -12 l2 2 M5 -12 l-2 2" strokeOpacity={0.8} /></>,
  touch_pinch: <><path d="M-4 -4 L-11 -11 M-11 -11 h5 M-11 -11 v5 M4 4 L11 11 M11 11 h-5 M11 11 v-5" />{dot(-3, 4)}{dot(3, -4)}</>,
};

function dpad(arrow: string) {
  return (
    <>
      <path d="M-4 -12 h8 v8 h8 v8 h-8 v8 h-8 v-8 h-8 v-8 h8z" strokeOpacity={0.4} />
      <path d={arrow} fill="var(--color-ink)" stroke="none" />
    </>
  );
}

function pill(text: string, color = "var(--color-ink-2)") {
  return (
    <span className="display grid h-[30px] min-w-[30px] place-items-center rounded-md border px-1.5 text-[12px] tracking-wider"
          style={{ borderColor: color, color, boxShadow: `0 0 8px -2px ${color}` }} aria-hidden>
      {text}
    </span>
  );
}
