import { useEffect, useRef } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  SlidersHorizontal, Keyboard, Grid3x3, Hand, MousePointer2, Lightbulb, BatteryWarning, Gamepad2,
  RotateCcw, Save, Loader2, Wand2,
} from "lucide-react";
import { useStore } from "../../lib/store";
import {
  BatterySection, ChordsSection, ControllersSection, GeneralSection, GenericInputSection,
  GesturesSection, LightbarSection, MacrosSection, RemoteSection, ShortcutsSection,
} from "./sections";

type Tab = "general" | "shortcuts" | "chords" | "gestures" | "macros" | "remote" | "lightbar" | "battery" | "controllers";

const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
  { id: "general", label: "General", icon: <SlidersHorizontal size={16} /> },
  { id: "shortcuts", label: "Shortcuts", icon: <Keyboard size={16} /> },
  { id: "chords", label: "Chords", icon: <Grid3x3 size={16} /> },
  { id: "gestures", label: "Gestures", icon: <Hand size={16} /> },
  { id: "macros", label: "Macros", icon: <Wand2 size={16} /> },
  { id: "remote", label: "Remote mode", icon: <MousePointer2 size={16} /> },
  { id: "lightbar", label: "Lightbar", icon: <Lightbulb size={16} /> },
  { id: "battery", label: "Battery", icon: <BatteryWarning size={16} /> },
  { id: "controllers", label: "Controllers", icon: <Gamepad2 size={16} /> },
];

/* The form edits a working copy of the document and Save POSTs it whole;
   the server merges + coerces + saves atomically through the config module. */
export default function SettingsPanel() {
  const cfg = useStore((s) => s.cfg);
  const cfgPath = useStore((s) => s.cfgPath);
  const actions = useStore((s) => s.actions);
  const dirty = useStore((s) => s.dirty);
  const loading = useStore((s) => s.loading);
  const loadConfig = useStore((s) => s.loadConfig);
  const saveConfig = useStore((s) => s.saveConfig);
  // The tab lives in the store so a picker's "New macro…" can jump here.
  const tabRaw = useStore((s) => s.settingsTab);
  const setTab = useStore((s) => s.setSettingsTab);
  const tab: Tab = TABS.some((t) => t.id === tabRaw) ? (tabRaw as Tab) : "general";
  const ref = useRef<HTMLElement>(null);

  useEffect(() => { ref.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }, []);

  const input = (() => {
    if (!cfg) return null;
    if (!actions) return cfg.input !== undefined ? <GenericInputSection /> : null;
    switch (tab) {
      case "shortcuts": return <ShortcutsSection actions={actions} />;
      case "chords": return <ChordsSection actions={actions} />;
      case "gestures": return <GesturesSection actions={actions} />;
      case "macros": return <MacrosSection actions={actions} />;
      case "remote": return <RemoteSection />;
      case "lightbar": return <LightbarSection />;
      case "battery": return <BatterySection />;
      default: return null;
    }
  })();

  return (
    <motion.section ref={ref} id="settings" className="panel mt-5 scroll-mt-24 p-4"
                    initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 12 }}
                    transition={{ duration: 0.3, ease: [0.2, 0.8, 0.2, 1] }}>
      {/* head */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="panel-title !text-ink flex-none text-[15px]">settings</div>
        <span className="mono min-w-0 flex-1 truncate text-[11.5px] text-ink-3" title={cfgPath}>{cfgPath}</span>
        <AnimatePresence>
          {dirty && (
            <motion.span initial={{ opacity: 0, scale: 0.9 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}
                         className="display rounded-md border border-amber/60 bg-amber/10 px-2 py-0.5 text-[11px] uppercase tracking-[.12em] text-amber">
              unsaved
            </motion.span>
          )}
        </AnimatePresence>
        <button className="btn" onClick={() => void loadConfig()} disabled={loading}><RotateCcw size={15} /> Reload</button>
        <button className="btn btn-primary" onClick={() => void saveConfig()} disabled={loading || !cfg}>
          {loading ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />} Save
        </button>
      </div>

      {!cfg ? (
        <div className="grid place-items-center py-16 text-ink-3"><Loader2 className="animate-spin" /></div>
      ) : (
        <div className="mt-4 grid gap-4 md:grid-cols-[200px_minmax(0,1fr)]">
          {/* category nav */}
          <nav className="flex gap-1 overflow-x-auto md:flex-col md:overflow-visible" aria-label="settings sections">
            {TABS.filter((t) => actions || t.id === "general" || t.id === "controllers").map((t) => (
              <button key={t.id} onClick={() => setTab(t.id)} aria-current={tab === t.id ? "page" : undefined}
                      className={"relative flex flex-none items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[13.5px] transition-colors " +
                        (tab === t.id ? "text-ink" : "text-ink-2 hover:bg-panel-2 hover:text-ink")}>
                {tab === t.id && (
                  <motion.span layoutId="settings-tab" transition={{ type: "spring", stiffness: 500, damping: 40 }}
                               className="absolute inset-0 rounded-lg border border-cross/50 bg-cross/10" />
                )}
                <span className={"relative " + (tab === t.id ? "text-cross" : "text-ink-3")}>{t.icon}</span>
                <span className="display relative text-[14px] tracking-wide">{t.label}</span>
              </button>
            ))}
          </nav>

          {/* the section */}
          <AnimatePresence mode="wait">
            <motion.div key={tab} initial={{ opacity: 0, x: 10 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -10 }}
                        transition={{ duration: 0.18 }} className="min-w-0">
              {tab === "general" && <GeneralSection />}
              {tab === "controllers" && <ControllersSection />}
              {input}
            </motion.div>
          </AnimatePresence>
        </div>
      )}

      <p className="mt-4 text-[12px] text-ink-3">
        Saved to the same config.json the tray uses (atomic write). Bridging switches apply when the tray notices; input
        and shortcut settings reach running bridges within a few seconds; ports and the dashboard port apply at the next start.
      </p>
    </motion.section>
  );
}
