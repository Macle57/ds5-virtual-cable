import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useStore } from "./lib/store";
import Atmosphere from "./components/Atmosphere";
import Header from "./components/Header";
import PlayerTabs from "./components/PlayerTabs";
import PadCard from "./components/PadCard";
import StatusCard from "./components/StatusCard";
import AxesCard from "./components/AxesCard";
import MotionCard from "./components/MotionCard";
import EmptyState from "./components/EmptyState";
import SettingsPanel from "./components/settings/SettingsPanel";
import Toasts from "./components/Toasts";

export default function App() {
  const connect = useStore((s) => s.connect);
  const toggleSettings = useStore((s) => s.toggleSettings);
  const setSettingsTab = useStore((s) => s.setSettingsTab);
  const hasPads = useStore((s) => Object.keys(s.state.controllers).length > 0);
  // "Waiting for a controller" is only true once we have heard from the
  // backend (or failed to): before that the page would flash the empty card,
  // fade it, and only then pop the real cards in. A dead backend still gets
  // the empty card after a moment.
  const heard = useStore((s) => s.state.ts > 0 || s.link === "reconnecting");
  const [waited, setWaited] = useState(false);
  const ready = hasPads || heard || waited;
  const settingsOpen = useStore((s) => s.settingsOpen);

  useEffect(() => {
    connect();
    // ?settings: open the panel on load (deep link + screenshot tests);
    // ?settings=chords lands on that tab.
    const q = new URLSearchParams(location.search);
    if (q.has("settings")) { const tab = q.get("settings"); tab ? setSettingsTab(tab) : toggleSettings(true); }
    const t = setTimeout(() => setWaited(true), 1500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <CrayonDefs />
      <Atmosphere />
      <Header />
      <main className="mx-auto max-w-[1320px] px-5 pb-20 pt-5">
        <PlayerTabs />
        <AnimatePresence mode="wait">
          {hasPads ? (
            <motion.div
              key="view"
              initial={false}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.25 }}
              className="grid gap-6 lg:grid-cols-[minmax(0,1.85fr)_minmax(320px,1fr)]"
            >
              <PadCard />
              <div className="flex flex-col gap-6">
                <StatusCard />
                <AxesCard />
                <MotionCard />
              </div>
            </motion.div>
          ) : ready ? (
            <EmptyState key="empty" />
          ) : null}
        </AnimatePresence>
        <AnimatePresence>{settingsOpen && <SettingsPanel key="settings" />}</AnimatePresence>
      </main>
      <Toasts />
    </>
  );
}

/* Shared SVG machinery, mounted once:
   - #crayon: the wobble filter (`.sketch` / `filter: url(#crayon)`) for
     decorative strokes and the pad outline. Small displacement so lines look
     drawn by hand, not melted.
   - #hatch-<colour>: diagonal crayon hatching for SVG fills
     (`fill="url(#hatch-cross)"`). The CSS twins are `.hatch-<colour>`. */
const HATCH: [string, string][] = [
  ["cross", "#5EB9DA"], ["triangle", "#4F9E5F"], ["circle", "#E8766F"],
  ["square", "#F29BB8"], ["amber", "#F2C94C"], ["ink", "var(--color-ink)"],
];

function CrayonDefs() {
  return (
    <svg width="0" height="0" aria-hidden style={{ position: "absolute" }}>
      <defs>
        <filter id="crayon" x="-5%" y="-5%" width="110%" height="110%" colorInterpolationFilters="sRGB">
          <feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="2" seed="7" result="noise" />
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="2.6" xChannelSelector="R" yChannelSelector="G" />
        </filter>
        <filter id="crayon-rough" x="-5%" y="-5%" width="110%" height="110%" colorInterpolationFilters="sRGB">
          <feTurbulence type="fractalNoise" baseFrequency="0.06" numOctaves="3" seed="3" result="noise" />
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="4" xChannelSelector="R" yChannelSelector="G" />
        </filter>
        {HATCH.map(([name, colour]) => (
          <pattern key={name} id={`hatch-${name}`} width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="8" stroke={colour} strokeWidth="2.4" strokeLinecap="round" />
          </pattern>
        ))}
      </defs>
    </svg>
  );
}
