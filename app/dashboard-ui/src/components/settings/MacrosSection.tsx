import { useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Keyboard, Pencil, Plus, Repeat, Terminal, ToggleRight, Trash2, X } from "lucide-react";
import type { ActionsMeta, Json, MacroRepeat, RepeatMode } from "../../lib/types";
import { getPath, useStore } from "../../lib/store";
import {
  DEFAULT_REPEAT, MODIFIERS, REPEAT_MODES, engineActions, eventKeyName, keyCap, keyGroups, macroList, macroRepeat, macroSummary,
  slugify, type MacroDef,
} from "../../lib/actionMeta";
import { HELP, keyLabel } from "./help";
import ButtonGlyph from "./ButtonGlyph";
import { Row } from "./controls";

type Kind = "keys" | "run";
interface Draft { name: string; label: string; kind: Kind; keys: string[]; run: string; repeat: MacroRepeat }

const asObj = (v: unknown): Record<string, Json> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, Json>) : {};

const fresh = (): Draft => ({ name: "", label: "", kind: "keys", keys: [], run: "", repeat: { ...DEFAULT_REPEAT } });
const fromDef = (name: string, m: MacroDef): Draft => ({
  name, label: m.label ?? "", kind: Array.isArray(m.keys) && m.keys.length ? "keys" : "run",
  keys: Array.isArray(m.keys) ? m.keys.slice() : [], run: m.run ?? "", repeat: macroRepeat(m),
});

/* ---- the section ---------------------------------------------------------- */
export function MacrosSection({ actions }: { actions: ActionsMeta }) {
  const rawMacros = useStore((s) => getPath(s.cfg, ["input", "macros"]));
  const rawChords = useStore((s) => getPath(s.cfg, ["input", "chords"]));
  const rawRemote = useStore((s) => getPath(s.cfg, ["input", "remote", "chords"]));
  const patch = useStore((s) => s.patchConfig);
  const macros = useMemo(() => macroList(rawMacros), [rawMacros]);
  const chords = asObj(rawChords);
  const remote = asObj(rawRemote);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editing, setEditing] = useState<string | null>(null);   // name being edited, or null = new

  const boundTo = (name: string) => [
    ...Object.entries(chords).filter(([, v]) => v === name).map(([k]) => ({ k, remote: false })),
    ...Object.entries(remote).filter(([, v]) => v === name).map(([k]) => ({ k, remote: true })),
  ];
  const reserved = new Set(["none", ...engineActions(actions).map((a) => a.name), ...actions.actions.map((a) => a.name)]);

  // The POST is a deep merge, so every field is written explicitly: a kind
  // switch or "repeat: once" must not leave the old value behind on disk.
  const save = (d: Draft) => {
    const def: MacroDef = { label: d.label.trim() || undefined };
    if (d.kind === "keys") { def.keys = d.keys; def.run = ""; def.repeat = { ...d.repeat }; }
    else { def.run = d.run.trim(); def.keys = []; def.repeat = { ...DEFAULT_REPEAT }; }
    patch((cfg) => {
      const inp = asObj(cfg.input); cfg.input = inp;
      const ms = asObj(inp.macros); inp.macros = ms;
      ms[d.name] = def as unknown as Json;
    });
    setDraft(null); setEditing(null);
  };

  const remove = (name: string) => {
    patch((cfg) => {
      const inp = asObj(cfg.input); cfg.input = inp;
      const ms = asObj(inp.macros); inp.macros = ms;
      ms[name] = null;                                   // tombstone: the POST is a merge
      const ch = asObj(inp.chords); inp.chords = ch;
      for (const k of Object.keys(ch)) if (ch[k] === name) ch[k] = "none";
      const rm = asObj(inp.remote); inp.remote = rm;
      const rc = asObj(rm.chords); rm.chords = rc;
      for (const k of Object.keys(rc)) if (rc[k] === name) rc[k] = "none";
    });
    if (editing === name) { setDraft(null); setEditing(null); }
  };

  return (
    <div className="rounded-2xl border border-line bg-panel-2/50 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="display text-[15px] uppercase tracking-[.1em] text-cross">Custom macros</div>
          <p className="mt-1 text-[12.5px] leading-relaxed text-ink-3">
            Your own actions: a keyboard shortcut sent as one atomic press, or a program to launch. Once saved they
            appear in every chord and gesture picker under “Your macros”.
          </p>
        </div>
        {!draft && (
          <button className="btn btn-primary" onClick={() => { setDraft(fresh()); setEditing(null); }}>
            <Plus size={15} /> New macro
          </button>
        )}
      </div>

      <AnimatePresence initial={false}>
        {draft && (
          <motion.div key="editor" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}
                      exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.2 }} className="overflow-hidden">
            <MacroEditor draft={draft} setDraft={setDraft} editing={editing} keys={actions.macro_keys ?? []}
                         taken={new Set(macros.map(([n]) => n))} reserved={reserved}
                         onSave={save} onCancel={() => { setDraft(null); setEditing(null); }} />
          </motion.div>
        )}
      </AnimatePresence>

      {macros.length === 0 && !draft && (
        <div className="mt-4 grid place-items-center rounded-xl border border-dashed border-line-2 py-10 text-center">
          <Keyboard size={28} className="mb-2 text-ink-3" />
          <div className="display text-[15px] text-ink-2">No macros yet</div>
          <div className="mt-1 max-w-[380px] text-[12.5px] text-ink-3">
            Try Ctrl+Shift+Esc for Task Manager, Win+Shift+S for a screenshot, or launch your music player.
          </div>
        </div>
      )}

      {macros.length > 0 && (
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          {macros.map(([name, m]) => {
            const isRun = !(Array.isArray(m.keys) && m.keys.length);
            const Icon = isRun ? Terminal : Keyboard;
            const bound = boundTo(name);
            const rep = macroRepeat(m);
            return (
              <motion.div layout key={name}
                          className={"rounded-xl border p-3 " + (editing === name ? "border-triangle/60 bg-triangle/[.06]" : "border-line bg-well/50")}>
                <div className="flex items-start gap-2.5">
                  <span className="grid h-9 w-9 flex-none place-items-center rounded-md border border-triangle/40 bg-triangle/10 text-triangle">
                    <Icon size={18} />
                  </span>
                  <div className="min-w-0 flex-1 leading-tight">
                    <div className="display flex items-center gap-2 text-[14.5px]">
                      <span className="truncate">{m.label?.trim() || name}</span>
                      {rep.mode !== "once" && !isRun && <RepeatPill rep={rep} />}
                    </div>
                    <div className="mono text-[10.5px] text-ink-3">{name}</div>
                  </div>
                  <button className="rounded-md p-1.5 text-ink-3 hover:bg-panel-2 hover:text-ink" aria-label="edit macro"
                          onClick={() => { setDraft(fromDef(name, m)); setEditing(name); }}><Pencil size={14} /></button>
                  <button className="rounded-md p-1.5 text-ink-3 hover:bg-circle/15 hover:text-circle" aria-label="delete macro"
                          onClick={() => remove(name)}><Trash2 size={14} /></button>
                </div>
                <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                  {isRun
                    ? <code className="mono truncate rounded-md border border-line bg-well px-2 py-1 text-[12px] text-ink-2">{macroSummary(m)}</code>
                    : (m.keys ?? []).map((k, i) => <KeyCap key={i} name={k} />)}
                </div>
                <div className="mt-2.5 flex flex-wrap items-center gap-1.5 text-[11px] text-ink-3">
                  {bound.length === 0 ? "not bound yet" : <>
                    <span>bound to</span>
                    {bound.map(({ k, remote: r }) => (
                      <span key={(r ? "r:" : "") + k} className="inline-flex items-center gap-1 rounded-md border border-line bg-well px-1.5 py-0.5 text-ink-2"
                            title={r ? "in remote mode" : "while chording"}>
                        <ButtonGlyph name={k} size={14} /> {keyLabel(k)}{r && <span className="text-ink-3"> · remote</span>}
                      </span>
                    ))}
                  </>}
                </div>
              </motion.div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* "hold · 50 ms" / "toggle · 50 ms · ×20" */
function RepeatPill({ rep }: { rep: MacroRepeat }) {
  const Icon = rep.mode === "toggle" ? ToggleRight : Repeat;
  return (
    <span className="inline-flex flex-none items-center gap-1 rounded-md border border-line bg-well px-1.5 py-0.5 text-[10.5px] font-normal text-ink-3"
          title={REPEAT_MODES.find((r) => r.value === rep.mode)?.doc}>
      <Icon size={11} /> {rep.mode} · {rep.interval_ms} ms{rep.max_runs > 0 && ` · ×${rep.max_runs}`}
    </span>
  );
}

/* ---- keycap ---------------------------------------------------------------- */
function KeyCap({ name, onRemove }: { name: string; onRemove?: () => void }) {
  const mod = MODIFIERS.includes(name);
  return (
    <span className={"display inline-flex h-7 items-center gap-1 rounded-md border px-2 text-[12.5px] tracking-wide " +
        (mod ? "border-cross/50 bg-cross/10 text-cross" : "border-line-2 bg-panel-2 text-ink")}
          style={{ boxShadow: "inset 0 -2px 0 rgba(0,0,0,.45)" }}>
      {keyCap(name)}
      {onRemove && (
        <button type="button" aria-label={"remove " + name} onClick={onRemove}
                className="-mr-1 rounded p-0.5 text-ink-3 hover:text-circle"><X size={11} /></button>
      )}
    </span>
  );
}

/* ---- the editor ------------------------------------------------------------ */
function MacroEditor({ draft, setDraft, editing, keys, taken, reserved, onSave, onCancel }: {
  draft: Draft; setDraft: (d: Draft) => void; editing: string | null; keys: string[];
  taken: Set<string>; reserved: Set<string>; onSave: (d: Draft) => void; onCancel: () => void;
}) {
  const [pending, setPending] = useState<string[]>([]);     // modifiers held in the capture box
  const [capturing, setCapturing] = useState(false);
  const up = (p: Partial<Draft>) => setDraft({ ...draft, ...p });
  const upRepeat = (p: Partial<MacroRepeat>) => up({ repeat: { ...draft.repeat, ...p } });

  // the name follows the label until the user edits it, and is fixed once
  // the macro exists (chords bind by name)
  const [nameTouched, setNameTouched] = useState(!!editing);
  const setLabel = (label: string) => up(nameTouched ? { label } : { label, name: slugify(label) });

  const problems: string[] = [];
  if (!draft.name) problems.push("give it a name");
  else if (!/^[a-z][a-z0-9_]{0,39}$/.test(draft.name)) problems.push("name: letters, digits and _ only, starting with a letter");
  else if (reserved.has(draft.name)) problems.push(`"${draft.name}" is a built-in action`);
  else if (!editing && taken.has(draft.name)) problems.push(`a macro named "${draft.name}" already exists`);
  if (draft.kind === "keys" && draft.keys.length === 0) problems.push("press a shortcut or add keys");
  if (draft.kind === "keys" && draft.keys.length > 8) problems.push("at most 8 keys");
  if (draft.kind === "keys" && draft.repeat.mode !== "once" && !(draft.repeat.interval_ms >= 10)) problems.push("repeat interval: at least 10 ms");
  if (draft.kind === "run" && !draft.run.trim()) problems.push("what should it launch?");

  const onCapture = (e: React.KeyboardEvent<HTMLDivElement>) => {
    e.preventDefault(); e.stopPropagation();
    if (e.key === "Escape" && !e.ctrlKey && !e.altKey && !e.shiftKey && !e.metaKey) {
      (e.target as HTMLElement).blur(); return;
    }
    const mods = [e.ctrlKey && "ctrl", e.shiftKey && "shift", e.altKey && "alt", e.metaKey && "win"].filter(Boolean) as string[];
    const key = eventKeyName(e);
    if (key && !MODIFIERS.includes(key)) { up({ keys: [...mods, key] }); setPending([]); }
    else setPending(mods);
  };

  const repeating = draft.repeat.mode !== "once";
  return (
    <div className="mt-4 rounded-xl border border-triangle/50 bg-well/60 p-4">
      <div className="mb-3 flex items-center gap-2">
        <span className="display text-[14px] uppercase tracking-[.12em] text-triangle">{editing ? "Edit macro" : "New macro"}</span>
        <span className="h-px flex-1 bg-line" />
      </div>

      <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        {/* identity */}
        <div className="grid gap-3">
          <label className="grid gap-1">
            <span className="eyebrow">Label</span>
            <input className="field" value={draft.label} placeholder="Task Manager" autoFocus
                   onChange={(e) => setLabel(e.target.value)} />
          </label>
          <label className="grid gap-1">
            <span className="eyebrow">Name <span className="normal-case tracking-normal text-ink-3">(what a chord binds to)</span></span>
            <input className="field mono" value={draft.name} placeholder="task_manager" disabled={!!editing}
                   onChange={(e) => { setNameTouched(true); up({ name: e.target.value.toLowerCase() }); }} />
          </label>
          <div className="grid gap-1">
            <span className="eyebrow">Kind</span>
            <div className="flex gap-1 rounded-lg border border-line bg-well p-1">
              {([["keys", Keyboard, "Key combo"], ["run", Terminal, "Launch program"]] as const).map(([k, I, t]) => (
                <button key={k} type="button" onClick={() => up({ kind: k })}
                        className={"display flex flex-1 items-center justify-center gap-2 rounded-md px-3 py-1.5 text-[13px] transition-colors " +
                          (draft.kind === k ? "bg-cross/15 text-cross" : "text-ink-2 hover:text-ink")}>
                  <I size={15} /> {t}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* the payload */}
        <div className="grid content-start gap-3">
          {draft.kind === "keys" ? (
            <>
              <div className="grid gap-1">
                <span className="eyebrow">Shortcut</span>
                <div tabIndex={0} role="textbox" aria-label="press the shortcut"
                     onFocus={() => setCapturing(true)} onBlur={() => { setCapturing(false); setPending([]); }}
                     onKeyDown={onCapture}
                     className={"flex min-h-[46px] flex-wrap items-center gap-1.5 rounded-lg border px-3 py-2 outline-none transition-colors " +
                       (capturing ? "border-cross bg-well shadow-[0_0_0_3px_color-mix(in_oklab,var(--color-cross)_20%,transparent)]" : "border-line bg-well")}>
                  {pending.length > 0
                    ? <>{pending.map((k) => <KeyCap key={k} name={k} />)}<span className="text-[12px] text-ink-3">+ …</span></>
                    : draft.keys.length === 0
                      ? <span className="text-[12.5px] text-ink-3">{capturing ? "press the shortcut now…" : "click here, then press the shortcut"}</span>
                      : draft.keys.map((k, i) => <KeyCap key={i} name={k} onRemove={() => up({ keys: draft.keys.filter((_, j) => j !== i) })} />)}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <select className="field w-auto flex-1" value=""
                        onChange={(e) => { if (e.target.value) up({ keys: [...draft.keys, e.target.value] }); }}>
                  <option value="">Add a key…</option>
                  {keyGroups(keys).map(([g, ks]) => (
                    <optgroup key={g} label={g}>{ks.map((k) => <option key={k} value={k}>{keyCap(k)} ({k})</option>)}</optgroup>
                  ))}
                </select>
                {draft.keys.length > 0 && <button className="btn" onClick={() => up({ keys: [] })}>Clear</button>}
              </div>
              <p className="text-[11.5px] leading-relaxed text-ink-3">
                Some combinations never reach the page (Win+E, Ctrl+Alt+Del, media keys): build those with “Add a key”.
              </p>
            </>
          ) : (
            <>
              <label className="grid gap-1">
                <span className="eyebrow">Command</span>
                <input className="field mono" value={draft.run} placeholder='notepad.exe   or   "C:\Games\launcher.exe" --big'
                       onChange={(e) => up({ run: e.target.value })} />
              </label>
              <p className="text-[11.5px] leading-relaxed text-ink-3">
                Anything the Run box accepts. It starts detached, without a console, and the chord never waits for it.
              </p>
            </>
          )}
        </div>
      </div>

      {/* repeat: saved as input.macros.<name>.repeat = {mode, interval_ms, max_runs} */}
      {draft.kind === "keys" && (
        <div className="mt-3 rounded-lg border border-line bg-well/60 px-3">
          <Row label="Repeat" keyName={`macros.${draft.name || "<name>"}.repeat`} help={HELP.macro_repeat} stack>
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex gap-1 rounded-lg border border-line bg-well p-1">
                {REPEAT_MODES.map((r) => (
                  <button key={r.value} type="button" onClick={() => upRepeat({ mode: r.value as RepeatMode })} title={r.doc}
                          className={"display flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12.5px] transition-colors " +
                            (draft.repeat.mode === r.value ? "bg-cross/15 text-cross" : "text-ink-2 hover:text-ink")}>
                    {r.value === "toggle" ? <ToggleRight size={13} /> : r.value === "hold" ? <Repeat size={13} /> : null}{r.label}
                  </button>
                ))}
              </div>
              <label className={"flex items-center gap-2 text-[12.5px] text-ink-2 transition-opacity " + (repeating ? "" : "opacity-40")}>
                every
                <input type="number" className="field num" style={{ width: 84 }} min={10} step={10} disabled={!repeating}
                       value={draft.repeat.interval_ms} onChange={(e) => upRepeat({ interval_ms: Number(e.target.value) })} />
                ms
              </label>
              <label className={"flex items-center gap-2 text-[12.5px] text-ink-2 transition-opacity " + (repeating ? "" : "opacity-40")}>
                max runs
                <input type="number" className="field num" style={{ width: 84 }} min={0} step={1} disabled={!repeating}
                       value={draft.repeat.max_runs} onChange={(e) => upRepeat({ max_runs: Math.max(0, Math.round(Number(e.target.value) || 0)) })} />
                <span className="text-ink-3">(0 = unlimited)</span>
              </label>
            </div>
          </Row>
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-3">
        {problems.length > 0 && (
          <span className="flex items-center gap-1.5 text-[12px] text-amber"><AlertTriangle size={13} /> {problems[0]}</span>
        )}
        <span className="flex-1" />
        <button className="btn" onClick={onCancel}>Cancel</button>
        <button className="btn btn-primary" disabled={problems.length > 0} onClick={() => onSave(draft)}>
          {editing ? "Apply" : "Add macro"}
        </button>
      </div>
    </div>
  );
}
