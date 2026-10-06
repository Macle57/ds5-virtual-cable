/* A small hand-drawn icon for each key in the config's chord/gesture
   vocabulary, drawn like the cartoon pad on the dashboard: flat crayon fills,
   thick ink outlines, nothing perfectly round. Falls back to a labelled tag. */

const INK = "var(--color-ink)";
const PAPER = "var(--color-panel)";

/* a circle that was drawn by hand: four arcs, each a touch off true */
function blob(r: number, cx = 0, cy = 0) {
  const k = r * 0.56;
  const a = r * 1.04, b = r * 0.97, c = r * 1.02, d = r * 0.98;
  return `M${cx} ${cy - a} C${cx + k} ${cy - a} ${cx + b} ${cy - k} ${cx + b} ${cy}`
    + ` C${cx + b} ${cy + k} ${cx + k} ${cy + c} ${cx} ${cy + c}`
    + ` C${cx - k} ${cy + c} ${cx - d} ${cy + k} ${cx - d} ${cy}`
    + ` C${cx - d} ${cy - k} ${cx - k} ${cy - a} ${cx} ${cy - a}Z`;
}

export default function ButtonGlyph({ name, size = 30 }: { name: string; size?: number }) {
  const s = size;
  const wrap = (child: React.ReactNode) => (
    <svg width={s} height={s} viewBox="-16 -16 32 32" fill="none" stroke={INK} strokeWidth={2.4}
         strokeLinejoin="round" strokeLinecap="round" aria-hidden className="flex-none overflow-visible">
      {child}
    </svg>
  );
  const face = (fill: string, mark: React.ReactNode) => wrap(<><path d={blob(13)} fill={fill} />{mark}</>);
  switch (name) {
    case "cross": return face("var(--color-cross)", <path d="M-5.5 -5 L5.5 5.5 M5 -5.5 L-5.5 5" strokeWidth={2.8} />);
    case "circle": return face("var(--color-circle)", <path d={blob(5.6)} strokeWidth={2.6} />);
    case "square": return face("var(--color-square)", <path d="M-5.5 -5 L5.2 -5.6 L5.6 5.3 L-5.2 5.6Z" strokeWidth={2.6} />);
    case "triangle": return face("var(--color-triangle)", <path d="M-6.4 4.6 L0 -6.4 L6.6 4.8Z" fill="var(--color-cyan)" strokeWidth={2.4} />);
    case "dpad_up": return wrap(dpad("up"));
    case "dpad_down": return wrap(dpad("down"));
    case "dpad_left": return wrap(dpad("left"));
    case "dpad_right": return wrap(dpad("right"));
    case "l1": case "r1": return wrap(bumper(name === "l1" ? "L1" : "R1", false));
    case "l2": case "r2": return wrap(bumper(name === "l2" ? "L2" : "R2", true));
    case "l3": case "r3": return wrap(<>
      <path d={blob(14)} fill="var(--color-cyan)" />
      <path d={blob(10)} fill="var(--color-circle)" />
      <text x={0} y={4.6} textAnchor="middle" fontSize={13} fontWeight={700} fill={INK} stroke="none"
            style={{ fontFamily: "var(--font-display)" }}>{name === "l3" ? "L" : "R"}</text>
    </>);
    case "create": return wrap(<>
      <path d="M-6 -10 C-6 -14 6 -14 6 -10 L6.2 10 C6.2 14 -6.2 14 -6.2 10Z" fill="var(--color-cyan)" />
      <path d="M-11.6 -7 l2.4 2 M-12.4 0 h2.6 M11.6 -7 l-2.4 2 M12.4 0 h-2.6" strokeWidth={1.9} />
      <path d="M-1.6 -4 v8 M1.6 -4 v8" strokeWidth={1.8} />
    </>);
    case "options": return wrap(<>
      <path d="M-6 -10 C-6 -14 6 -14 6 -10 L6.2 10 C6.2 14 -6.2 14 -6.2 10Z" fill="var(--color-cyan)" />
      <path d="M-2 -4.5 h4 M-2 0 h4 M-2 4.5 h4" strokeWidth={1.9} />
    </>);
    case "ps": return wrap(<>
      <path d={blob(13)} fill={PAPER} />
      <text x={0} y={4.2} textAnchor="middle" fontSize={11} fontWeight={700} fill={INK} stroke="none"
            style={{ fontFamily: "var(--font-display)" }}>ps</text>
    </>);
    case "mute": return wrap(<>
      <path d="M-11 -5.6 C-11 -8 11 -8.2 11.2 -5.6 L11 5.6 C11 8.2 -11 8 -11.2 5.6Z" fill="var(--color-orange)" />
      <path d="M-2.8 -4 C-2.8 -6 2.8 -6 2.8 -4 L2.8 1.6 C2.8 3.6 -2.8 3.6 -2.8 1.6Z" fill={PAPER} strokeWidth={1.8} />
      <path d="M-6.4 -5.6 L6.4 5" strokeWidth={2.2} />
    </>);
    case "touchpad_click": return wrap(<>
      {pad(PAPER)}
      <path d={blob(3.4, 2, 1.5)} fill="var(--color-circle)" strokeWidth={1.8} />
      <path d="M-6.5 -11.5 l1.6 2.4 M2 -13.2 v2.6 M10 -11.6 l-1.6 2.4" strokeWidth={1.9} />
    </>);
    default: {
      // the touch gestures: one glyph per motion; the "_pressed" twin is the
      // same motion on a pad drawn pressed in (yellow, with a click burst)
      const pressed = name.endsWith("_pressed");
      const base = pressed ? name.slice(0, -"_pressed".length) : name;
      const g = GESTURES[base];
      if (!g) return pill(name.slice(0, 3));
      return wrap(<>
        {pad(pressed ? "var(--color-amber)" : "var(--color-cyan)")}
        {pressed && <path d="M-13.5 -12 l-2 -2.4 M13.5 -12 l2 -2.4" strokeWidth={1.9} />}
        {g}
      </>);
    }
  }
}

/* the touchpad: a wide rounded slab */
function pad(fill: string) {
  return <path d="M-13 -6.5 C-13 -9.6 -10.5 -9.8 -8 -9.8 L8.4 -10 C11 -10 13.4 -9.6 13.2 -6.4 L13 6.6 C13 9.6 10.6 9.8 8 9.8 L-8.4 10 C-11 10 -13.2 9.6 -13.2 6.4Z" fill={fill} />;
}

/* a fingertip on the pad */
const dot = (cx: number, cy: number) => <path d={blob(2.3, cx, cy)} fill="var(--color-circle)" strokeWidth={1.5} />;
const arrow = (d: string) => <path d={d} strokeWidth={2.2} />;
const GESTURES: Record<string, React.ReactNode> = {
  touch_slide_horizontal: <>{arrow("M-8 2 h16 M-8 2 l3 -3 M-8 2 l3 3 M8 2 l-3 -3 M8 2 l-3 3")}{dot(-3, -4)}{dot(3, -4)}</>,
  touch_slide_vertical: <>{arrow("M2 -7 v14 M2 -7 l-3 3 M2 -7 l3 3 M2 7 l-3 -3 M2 7 l3 -3")}{dot(-6, -2)}{dot(-6, 3)}</>,
  touch_swipe_up: <>{arrow("M0 6.5 v-13 M0 -6.5 l-3.4 3.4 M0 -6.5 l3.4 3.4")}{dot(-7, 4)}{dot(7, 4)}</>,
  touch_swipe_down: <>{arrow("M0 -6.5 v13 M0 6.5 l-3.4 -3.4 M0 6.5 l3.4 -3.4")}{dot(-7, -4)}{dot(7, -4)}</>,
  touch_slide_up: <>{arrow("M0 6.5 v-13 M0 -6.5 l-3.4 3.4 M0 -6.5 l3.4 3.4")}{dot(-7, 4)}{dot(7, 4)}</>,
  touch_slide_down: <>{arrow("M0 -6.5 v13 M0 6.5 l-3.4 -3.4 M0 6.5 l3.4 -3.4")}{dot(-7, -4)}{dot(7, -4)}</>,
  touch_slide_left: <>{arrow("M8 0 h-15 M-7 0 l3.6 -3.6 M-7 0 l3.6 3.6")}{dot(8.5, -5)}{dot(8.5, 5)}</>,
  touch_slide_right: <>{arrow("M-8 0 h15 M7 0 l-3.6 -3.6 M7 0 l-3.6 3.6")}{dot(-8.5, -5)}{dot(-8.5, 5)}</>,
  touch_tap_2f: <>{dot(-3.5, 1.5)}{dot(4, -1)}<path d="M-9 -4.5 C-8.4 -6.4 -7.4 -7 -6 -7.4 M9.6 4 C9 5.8 8 6.6 6.6 7" strokeWidth={1.7} /></>,
  touch_click_2f: <>{dot(-4, 1)}{dot(4, 1)}<path d="M0 -6.5 v2.4 M-5 -6 l1.4 1.8 M5 -6 l-1.4 1.8" strokeWidth={1.7} /></>,
  touch_pinch: <>{arrow("M-3 -2 L-9 -7 M-9 -7 h4 M-9 -7 v4 M3 2 L9 7 M9 7 h-4 M9 7 v-4")}{dot(-2, 4)}{dot(2, -4)}</>,
};

function dpad(dir: "up" | "down" | "left" | "right") {
  const rot = { up: 0, right: 90, down: 180, left: 270 }[dir];
  return (
    <>
      <path d="M-4.6 -13 L4.4 -13.2 L4.6 -4.6 L13 -4.4 L13.2 4.6 L4.6 4.4 L4.4 13 L-4.6 13.2 L-4.4 4.6 L-13 4.4 L-13.2 -4.6 L-4.4 -4.4Z"
            fill={PAPER} />
      <g transform={`rotate(${rot})`}>
        {/* the pressed arm: an ink key with a paper arrow, like the pad's own d-pad */}
        <path d="M-4.6 -13 L4.4 -13.2 L4.6 -3.6 L0 0.6 L-4.6 -3.6Z" fill={INK} />
        <path d="M0 -10.8 L-2.9 -6.4 L2.9 -6.4Z" fill={PAPER} stroke={PAPER} strokeWidth={1} />
      </g>
    </>
  );
}

/* a shoulder button: a yellow nub; triggers are taller */
function bumper(text: string, trigger: boolean) {
  const body = trigger
    ? "M-11 9 C-11.6 -2 -9 -12 0 -12.4 C9 -12 11.6 -2 11.2 9 C6 10.6 -6 10.6 -11 9Z"
    : "M-14.5 8 C-15 -2 -10 -9.6 0 -9.8 C10 -9.6 15.2 -2 14.6 8 C8 10 -8 10 -14.5 8Z";
  return (
    <>
      <path d={body} fill="var(--color-amber)" />
      <text x={0} y={trigger ? 3 : 4.4} textAnchor="middle" fontSize={12} fontWeight={700} fill={INK} stroke="none"
            style={{ fontFamily: "var(--font-display)" }}>{text}</text>
    </>
  );
}

function pill(text: string) {
  return <span className="st-glyph-pill" aria-hidden>{text}</span>;
}
