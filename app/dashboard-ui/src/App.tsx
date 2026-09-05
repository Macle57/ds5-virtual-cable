import { useEffect } from "react";
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
  const settingsOpen = useStore((s) => s.settingsOpen);

  useEffect(() => {
    connect();
    // ?settings: open the panel on load (deep link + screenshot tests);
    // ?settings=chords lands on that tab.
    const q = new URLSearchParams(location.search);
    if (q.has("settings")) { const tab = q.get("settings"); tab ? setSettingsTab(tab) : toggleSettings(true); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <Atmosphere />
      <Header />
      <main className="mx-auto max-w-[1320px] px-5 pb-20 pt-5">
        <PlayerTabs />
        <AnimatePresence mode="wait">
          {hasPads ? (
            <motion.div
              key="view"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.35, ease: [0.2, 0.8, 0.2, 1] }}
              className="grid gap-4 lg:grid-cols-[minmax(0,1.85fr)_minmax(320px,1fr)]"
            >
              <PadCard />
              <div className="flex flex-col gap-4">
                <StatusCard />
                <AxesCard />
                <MotionCard />
              </div>
            </motion.div>
          ) : (
            <EmptyState key="empty" />
          )}
        </AnimatePresence>
        <AnimatePresence>{settingsOpen && <SettingsPanel key="settings" />}</AnimatePresence>
      </main>
      <Toasts />
    </>
  );
}
