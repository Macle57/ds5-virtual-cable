import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, Plus, Repeat, Search } from "lucide-react";
import type { ActionsMeta } from "../../lib/types";
import { GROUPS, describeAction, listActions, type ActionInfo } from "../../lib/actionMeta";
import { useStore } from "../../lib/store";

/* The action picker: a button wearing the bound action's icon, opening a
   searchable, grouped menu. Replaces a <select> of "name — doc" strings,
   which read as a wall of text once there were fifteen of them. `dir` is
   the direction of a slide row: a directional action (scroll up / down /
   left / right) is offered only on the row it belongs to. */
export default function ActionPicker({ value, onChange, meta, macros, dir }:
  { value: string; onChange: (name: string) => void; meta: ActionsMeta; macros: unknown; dir?: string }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [hi, setHi] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const setSettingsTab = useStore((s) => s.setSettingsTab);

  const current = describeAction(value, meta, macros);
  const all = useMemo(() => {
    const list = listActions(meta, macros).filter((a) => !dir || !a.dir || a.dir === dir || a.name === current.name);
    if (current.unknown) list.push(current);
    return list;
  }, [meta, macros, dir, current.unknown, current.name]);

  const needle = q.trim().toLowerCase();
  const shown = needle
    ? all.filter((a) => (a.name + " " + a.label + " " + a.doc).toLowerCase().includes(needle))
    : all;

  useEffect(() => {
    if (!open) return;
    setQ(""); setHi(Math.max(0, all.findIndex((a) => a.name === current.name)));
    const t = setTimeout(() => search.current?.focus(), 30);
    const away = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => { clearTimeout(t); document.removeEventListener("mousedown", away); };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (a: ActionInfo) => { onChange(a.name); setOpen(false); };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { setOpen(false); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(shown.length - 1, h + 1)); }
    if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(0, h - 1)); }
    if (e.key === "Enter" && shown[hi]) { e.preventDefault(); pick(shown[hi]); }
  };

  const Icon = current.Icon;
  const none = current.group === "none";
  return (
    <div ref={root} className="relative">
      <button type="button" onClick={() => setOpen(!open)} aria-haspopup="listbox" aria-expanded={open}
              className={"st-pick" + (none ? " is-none" : "")}>
        <span className={"st-blob" + (none ? " none" : "")} style={{ "--tone": current.color } as React.CSSProperties}>
          <Icon size={17} strokeWidth={2.5} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="st-pick-label">{current.label}</span>
          <span className="st-pick-doc">{current.doc}</span>
        </span>
        {current.repeatable && <Repeat size={14} strokeWidth={2.5} className="flex-none text-ink-2" aria-label="repeats while held" />}
        <ChevronDown size={17} strokeWidth={2.5} className={"flex-none text-ink transition-transform " + (open ? "rotate-180" : "")} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div initial={{ opacity: 0, y: -6, rotate: -1.2, scale: 0.97 }} animate={{ opacity: 1, y: 0, rotate: 0, scale: 1 }}
                      exit={{ opacity: 0, y: -4, scale: 0.98 }} transition={{ type: "spring", stiffness: 520, damping: 26 }}
                      className="st-menu" style={{ transformOrigin: "30% 0" }} onKeyDown={onKey}>
            <div className="st-menu-search">
              <Search size={16} strokeWidth={2.5} className="flex-none text-ink-2" />
              <input ref={search} value={q} onChange={(e) => { setQ(e.target.value); setHi(0); }}
                     placeholder="Search actions" aria-label="Search actions" />
            </div>
            <div role="listbox" className="st-menu-list">
              {shown.length === 0 && <div className="st-empty">Nothing matches “{q.trim()}”.</div>}
              {GROUPS.map((g) => {
                const items = shown.filter((a) => a.group === g.id);
                if (!items.length) return null;
                return (
                  <div key={g.id} className="mb-1">
                    {g.title && (
                      <div className="st-menu-group"><i style={{ background: g.color }} />{g.title}</div>
                    )}
                    {items.map((a) => {
                      const i = shown.indexOf(a);
                      const selected = a.name === current.name;
                      const AIcon = a.Icon;
                      return (
                        <button key={a.name} type="button" role="option" aria-selected={selected}
                                onMouseEnter={() => setHi(i)} onClick={() => pick(a)}
                                className={"st-opt" + (i === hi ? " hi" : "")}>
                          <span className={"st-blob sm" + (a.group === "none" ? " none" : "")} style={{ "--tone": a.color } as React.CSSProperties}>
                            <AIcon size={15} strokeWidth={2.5} />
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="st-opt-label">{a.label}</span>
                            <span className="st-opt-doc">{a.doc}</span>
                          </span>
                          {a.repeatable && <Repeat size={13} strokeWidth={2.5} className="flex-none text-ink-2" aria-label="repeats while held" />}
                          {selected && <Check size={18} strokeWidth={3} className="flex-none text-triangle" aria-label="current" />}
                        </button>
                      );
                    })}
                  </div>
                );
              })}
            </div>
            <button type="button" onClick={() => { setOpen(false); setSettingsTab("macros"); }} className="st-menu-new">
              <Plus size={16} strokeWidth={2.75} /> Make a new macro
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
