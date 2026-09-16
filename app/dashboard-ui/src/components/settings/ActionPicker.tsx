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
  return (
    <div ref={root} className="relative">
      <button type="button" onClick={() => setOpen(!open)} aria-haspopup="listbox" aria-expanded={open}
              className={"flex w-full items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left transition-colors " +
                (open ? "border-cross bg-well shadow-[0_0_0_3px_color-mix(in_oklab,var(--color-cross)_20%,transparent)]"
                      : "border-line bg-well hover:border-line-2")}>
        <span className="grid h-8 w-8 flex-none place-items-center rounded-md border"
              style={{ color: current.color, borderColor: `color-mix(in oklab, ${current.color} 45%, transparent)`,
                       background: `color-mix(in oklab, ${current.color} 12%, transparent)` }}>
          <Icon size={17} />
        </span>
        <span className="min-w-0 flex-1 leading-tight">
          <span className={"display block truncate text-[14px] " + (current.group === "none" ? "text-ink-3" : "text-ink")}>
            {current.label}
          </span>
          <span className="block truncate text-[11px] text-ink-3">{current.doc}</span>
        </span>
        {current.repeatable && <Repeat size={12} className="flex-none text-ink-3" aria-label="repeats while held" />}
        <ChevronDown size={15} className={"flex-none text-ink-3 transition-transform " + (open ? "rotate-180" : "")} />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div initial={{ opacity: 0, y: -4, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={{ opacity: 0, y: -4, scale: 0.98 }} transition={{ duration: 0.14 }}
                      className="absolute left-0 right-0 top-[calc(100%+6px)] z-40 min-w-[300px] overflow-hidden rounded-xl border border-line-2 bg-panel shadow-[0_24px_48px_-16px_rgba(0,0,0,.9)]"
                      onKeyDown={onKey}>
            <div className="flex items-center gap-2 border-b border-line px-3 py-2">
              <Search size={14} className="text-ink-3" />
              <input ref={search} value={q} onChange={(e) => { setQ(e.target.value); setHi(0); }}
                     placeholder="Search actions…" className="w-full bg-transparent text-[13px] text-ink outline-none placeholder:text-ink-3" />
            </div>
            <div role="listbox" className="max-h-[320px] overflow-y-auto p-1.5">
              {shown.length === 0 && <div className="px-3 py-4 text-center text-[12.5px] text-ink-3">nothing matches</div>}
              {GROUPS.map((g) => {
                const items = shown.filter((a) => a.group === g.id);
                if (!items.length) return null;
                return (
                  <div key={g.id} className="mb-1">
                    {g.title && (
                      <div className="eyebrow flex items-center gap-2 px-2 pb-1 pt-2 text-[10px]" style={{ color: g.color }}>
                        {g.title}<span className="h-px flex-1 bg-line" />
                      </div>
                    )}
                    {items.map((a) => {
                      const i = shown.indexOf(a);
                      const selected = a.name === current.name;
                      const AIcon = a.Icon;
                      return (
                        <button key={a.name} type="button" role="option" aria-selected={selected}
                                onMouseEnter={() => setHi(i)} onClick={() => pick(a)}
                                className={"flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left transition-colors " +
                                  (i === hi ? "bg-cross/15" : "")}>
                          <span className="grid h-7 w-7 flex-none place-items-center rounded-md" style={{ color: a.color, background: `color-mix(in oklab, ${a.color} 12%, transparent)` }}>
                            <AIcon size={15} />
                          </span>
                          <span className="min-w-0 flex-1 leading-tight">
                            <span className="display block truncate text-[13.5px] text-ink">{a.label}</span>
                            <span className="block truncate text-[11px] text-ink-3">{a.doc}</span>
                          </span>
                          {a.repeatable && <Repeat size={11} className="flex-none text-ink-3" />}
                          {selected && <Check size={14} className="flex-none text-cross" />}
                        </button>
                      );
                    })}
                  </div>
                );
              })}
            </div>
            <button type="button" onClick={() => { setOpen(false); setSettingsTab("macros"); }}
                    className="flex w-full items-center gap-2 border-t border-line px-3 py-2 text-[12.5px] text-triangle hover:bg-triangle/10">
              <Plus size={14} /> New macro…
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
