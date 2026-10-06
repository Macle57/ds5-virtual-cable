import { useEffect, useRef } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  Radio, Keyboard, Grid3x3, Hand, MousePointer2, Lightbulb, BatteryWarning, Gamepad2, Bell,
  RotateCcw, Save, Loader2, Wand2,
} from "lucide-react";
import { useStore } from "../../lib/store";
import {
  BatterySection, ChordsSection, ControllersSection, GeneralSection, GenericInputSection,
  GesturesSection, LightbarSection, MacrosSection, NotificationsSection, RemoteSection, ShortcutsSection,
} from "./sections";
import { Scribble } from "./controls";

type Tab = "general" | "shortcuts" | "chords" | "gestures" | "macros" | "remote" | "lightbar" | "battery" | "notifications" | "controllers";

/* "general" keeps its id (deep links: ?settings=general) but is the tray's
   switches now -- what the user reaches for first. Each tab is an index tab
   in its own crayon; the sheet's scribbles take the open tab's colour. */
const I = { size: 16, strokeWidth: 2.5 };
const TABS: { id: Tab; label: string; icon: React.ReactNode; color: string }[] = [
  { id: "general", label: "Bridging & tray", icon: <Radio {...I} />, color: "var(--color-cross)" },
  { id: "shortcuts", label: "Shortcuts", icon: <Keyboard {...I} />, color: "var(--color-amber)" },
  { id: "chords", label: "Chords", icon: <Grid3x3 {...I} />, color: "var(--color-square)" },
  { id: "gestures", label: "Gestures", icon: <Hand {...I} />, color: "var(--color-triangle)" },
  { id: "macros", label: "Macros", icon: <Wand2 {...I} />, color: "var(--color-orange)" },
  { id: "remote", label: "Remote mode", icon: <MousePointer2 {...I} />, color: "var(--color-circle)" },
  { id: "lightbar", label: "Lightbar", icon: <Lightbulb {...I} />, color: "var(--color-amber)" },
  { id: "battery", label: "Battery", icon: <BatteryWarning {...I} />, color: "var(--color-triangle)" },
  { id: "notifications", label: "Notifications", icon: <Bell {...I} />, color: "var(--color-violet)" },
  { id: "controllers", label: "Controllers", icon: <Gamepad2 {...I} />, color: "var(--color-cross)" },
];
/* Tabs that need no action registry (they edit top-level config only). */
const PLAIN_TABS: Tab[] = ["general", "notifications", "controllers"];

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
  // The tab lives in the store so a picker's "New macro" can jump here.
  const tabRaw = useStore((s) => s.settingsTab);
  const setTab = useStore((s) => s.setSettingsTab);
  const tab: Tab = TABS.some((t) => t.id === tabRaw) ? (tabRaw as Tab) : "general";
  const color = TABS.find((t) => t.id === tab)!.color;
  const ref = useRef<HTMLElement>(null);
  const still = useReducedMotion();

  // Scroll the panel into view -- and again once the live view above has
  // arrived and settled (a ?settings= deep link mounts the panel before the
  // first state frame; the pad card then mounts above it and pushes it down).
  const hasPads = useStore((s) => Object.keys(s.state.controllers).length > 0);
  useEffect(() => {
    const go = () => ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    go();
    const ts = [450, 1200].map((ms) => setTimeout(go, ms));
    return () => ts.forEach(clearTimeout);
  }, [hasPads]);

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
    <motion.section ref={ref} id="settings" className="mt-10 scroll-mt-32" aria-labelledby="settings-title"
                    initial={{ opacity: 0, y: 18, rotate: -0.6 }} animate={{ opacity: 1, y: 0, rotate: 0 }} exit={{ opacity: 0, y: 12 }}
                    transition={{ type: "spring", stiffness: 260, damping: 22 }}>
      {/* head: the title, the sticky note, the two buttons */}
      <div className="st-head">
        <h2 id="settings-title" className="st-title" style={{ "--tab": "var(--color-cross)" } as React.CSSProperties}>
          Settings<Scribble />
        </h2>
        <span className="st-path" title={cfgPath}>{cfgPath}</span>
        <div className="st-head-actions">
          <AnimatePresence>
            {dirty && (
              <motion.span key="unsaved" className="st-sticky" role="status"
                           initial={still ? { opacity: 0 } : { opacity: 0, y: -26, rotate: -14, scale: 0.9 }}
                           animate={{ opacity: 1, y: 0, rotate: -4, scale: 1 }}
                           exit={still ? { opacity: 0 } : { opacity: 0, y: 10, rotate: 6 }}
                           transition={{ type: "spring", stiffness: 420, damping: 15 }}>
                Unsaved changes
              </motion.span>
            )}
          </AnimatePresence>
          <button className="btn" onClick={() => void loadConfig()} disabled={loading}>
            <RotateCcw size={16} strokeWidth={2.5} /> Reload
          </button>
          <button className="btn btn-primary" onClick={() => void saveConfig()} disabled={loading || !cfg}>
            {loading ? <Loader2 size={16} strokeWidth={2.5} className="animate-spin" /> : <Save size={16} strokeWidth={2.5} />} Save
          </button>
        </div>
      </div>

      {!cfg ? (
        <div className="st-sheet mt-5 grid place-items-center text-ink-2" style={{ minHeight: 220 }}>
          <span className="flex items-center gap-2"><Loader2 className="animate-spin" strokeWidth={2.5} /> Loading your settings</span>
        </div>
      ) : (
        <div className="st-book">
          {/* the index tabs down the sheet's edge */}
          <nav className="st-tabs" aria-label="Settings sections">
            {TABS.filter((t) => actions || PLAIN_TABS.includes(t.id)).map((t) => (
              <button key={t.id} type="button" onClick={() => setTab(t.id)} aria-current={tab === t.id ? "page" : undefined}
                      className="st-tab" style={{ "--tab": t.color } as React.CSSProperties}>
                {tab === t.id && (
                  <motion.span layoutId="settings-tab" className="st-tab-face" aria-hidden
                               transition={{ type: "spring", stiffness: 520, damping: 34 }} />
                )}
                <span className="st-tab-ico">{t.icon}</span>
                <span className="st-tab-label">{t.label}</span>
              </button>
            ))}
          </nav>

          {/* the sheet */}
          <div className="st-sheet" style={{ "--tab": color } as React.CSSProperties}>
            <AnimatePresence mode="wait" initial={false}>
              <motion.div key={tab} className="min-w-0"
                          initial={still ? { opacity: 0 } : { opacity: 0, y: 8, rotate: 0.4 }}
                          animate={{ opacity: 1, y: 0, rotate: 0 }}
                          exit={still ? { opacity: 0 } : { opacity: 0, y: -6 }}
                          transition={{ duration: 0.18 }}>
                {tab === "general" && <GeneralSection />}
                {tab === "notifications" && <NotificationsSection />}
                {tab === "controllers" && <ControllersSection />}
                {input}
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      )}

      <p className="st-foot">
        Settings are saved to the same config.json the tray uses. Bridging and tray switches apply as soon as you save.
        Shortcut and input settings reach running bridges within a few seconds. Port changes apply the next time the
        tray starts.
      </p>
    </motion.section>
  );
}
