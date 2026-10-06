import { useState } from "react";
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
import { Reveal, Row, Scribble } from "./controls";

type Kind = "keys" | "run";
interface Draft { name: string; label: string; kind: Kind; keys: string[]; run: string; repeat: MacroRepeat }

const asObj = (v: unknown): Record<string, Json> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, Json>) : {};
const IC = { strokeWidth: 2.5 };

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
  // Not memoised on rawMacros: patchConfig mutates input.macros in place (only
  // the top-level document is re-spread), so its reference never changes and a
  // memo would keep showing the list from before the add/delete.
  const macros = macroList(rawMacros);
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
    <section className="st-card">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        <div className="min-w-0 flex-1">
          <div className="st-h"><h3 className="st-h-text">Your macros<Scribble /></h3></div>
          <p className="st-hint">
            Your own actions: a keyboard shortcut sent as one press, or a program to launch. Once saved, they show up
            in every chord and gesture picker under “Your macros”.
          </p>
        </div>
        {!draft && (
          <button className="btn btn-primary" onClick={() => { setDraft(fresh()); setEditing(null); }}>
            <Plus size={16} strokeWidth={2.75} /> New macro
          </button>
        )}
      </div>

      <AnimatePresence initial={false}>
        {draft && (
          <Reveal key="editor">
            <MacroEditor draft={draft} setDraft={setDraft} editing={editing} keys={actions.macro_keys ?? []}
                         taken={new Set(macros.map(([n]) => n))} reserved={reserved}
                         onSave={save} onCancel={() => { setDraft(null); setEditing(null); }} />
          </Reveal>
        )}
      </AnimatePresence>

      {macros.length === 0 && !draft && (
        <div className="st-empty-card">
          <span className="st-blob mb-3" style={{ "--tone": "var(--color-orange)", width: 46, height: 46, transform: "rotate(-8deg)" } as React.CSSProperties}>
            <Keyboard size={22} {...IC} />
          </span>
          <div className="st-bind-name">No macros yet</div>
          <p className="st-small mt-1 max-w-[46ch]">
            Try Ctrl+Shift+Esc for Task Manager, Win+Shift+S for a screenshot, or a macro that opens your music player.
          </p>
        </div>
      )}

      {macros.length > 0 && (
        <div className="st-grid wide mt-4">
          {macros.map(([name, m]) => {
            const isRun = !(Array.isArray(m.keys) && m.keys.length);
            const Icon = isRun ? Terminal : Keyboard;
            const bound = boundTo(name);
            const rep = macroRepeat(m);
            return (
              <motion.div layout="position" key={name} className={"st-bind" + (editing === name ? " is-editing" : "")}
                          style={{ "--accent": "var(--color-orange)" } as React.CSSProperties}>
                <div className="flex items-start gap-2.5">
                  <span className="st-blob" style={{ "--tone": "var(--color-triangle)" } as React.CSSProperties}>
                    <Icon size={17} {...IC} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="st-bind-name truncate">{m.label?.trim() || name}</div>
                    <div className="mono text-[12px] text-ink-2" title="The name a chord binds to">{name}</div>
                  </div>
                  <button className="st-iconbtn" aria-label={"Edit macro " + (m.label?.trim() || name)} title="Edit"
                          onClick={() => { setDraft(fromDef(name, m)); setEditing(name); }}><Pencil size={16} {...IC} /></button>
                  <button className="st-iconbtn danger" aria-label={"Delete macro " + (m.label?.trim() || name)} title="Delete"
                          onClick={() => remove(name)}><Trash2 size={16} {...IC} /></button>
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-1.5">
                  {isRun
                    ? <code className="st-code">{macroSummary(m)}</code>
                    : (m.keys ?? []).map((k, i) => <KeyCap key={i} name={k} />)}
                  {rep.mode !== "once" && !isRun && <RepeatPill rep={rep} />}
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-1.5 st-small">
                  {bound.length === 0 ? "Not bound to a button yet." : <>
                    <span>Bound to</span>
                    {bound.map(({ k, remote: r }, i) => (
                      <span key={(r ? "r:" : "") + k} className={"st-sticker" + (i % 2 ? " tilt-r" : "")}
                            title={r ? "in remote mode" : "while chording"}>
                        <ButtonGlyph name={k} size={16} /> {keyLabel(k)}{r && <span className="font-normal text-ink-2">(remote)</span>}
                      </span>
                    ))}
                  </>}
                </div>
              </motion.div>
            );
          })}
        </div>
      )}
    </section>
  );
}

/* "While held, every 50 ms" / "Toggle, every 50 ms, 20 times at most" */
function RepeatPill({ rep }: { rep: MacroRepeat }) {
  const Icon = rep.mode === "toggle" ? ToggleRight : Repeat;
  const mode = REPEAT_MODES.find((r) => r.value === rep.mode);
  return (
    <span className="st-sticker tilt-r ml-1" style={{ "--tone": "var(--color-cyan)" } as React.CSSProperties} title={mode?.doc}>
      <Icon size={12} {...IC} /> {mode?.label ?? rep.mode}, every {rep.interval_ms} ms{rep.max_runs > 0 && `, ${rep.max_runs} times at most`}
    </span>
  );
}

/* ---- keycap ---------------------------------------------------------------- */
function KeyCap({ name, onRemove }: { name: string; onRemove?: () => void }) {
  const mod = MODIFIERS.includes(name);
  return (
    <span className={"st-keycap" + (mod ? " mod" : "")}>
      {keyCap(name)}
      {onRemove && (
        <button type="button" aria-label={"Remove " + keyCap(name)} onClick={onRemove}><X size={12} strokeWidth={3} /></button>
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
  if (!draft.name) problems.push("Give it a name.");
  else if (!/^[a-z][a-z0-9_]{0,39}$/.test(draft.name)) problems.push("Names use lowercase letters, digits and _, and start with a letter.");
  else if (reserved.has(draft.name)) problems.push(`“${draft.name}” is a built-in action. Pick another name.`);
  else if (!editing && taken.has(draft.name)) problems.push(`A macro named “${draft.name}” already exists.`);
  if (draft.kind === "keys" && draft.keys.length === 0) problems.push("Press a shortcut or add keys.");
  if (draft.kind === "keys" && draft.keys.length > 8) problems.push("Use 8 keys at most.");
  if (draft.kind === "keys" && draft.repeat.mode !== "once" && !(draft.repeat.interval_ms >= 10)) problems.push("Repeat every 10 ms or more.");
  if (draft.kind === "run" && !draft.run.trim()) problems.push("Type the program to launch.");

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
    <div className="st-editor">
      <div className="st-sub mt-0">
        <span className="st-sub-title">{editing ? "Edit macro" : "New macro"}</span>
      </div>

      <div className="grid gap-5 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        {/* identity */}
        <div className="grid content-start gap-4">
          <label className="grid gap-1.5">
            <span className="st-label">Label</span>
            <input className="field" value={draft.label} placeholder="Task Manager" autoFocus
                   onChange={(e) => setLabel(e.target.value)} />
          </label>
          <label className="grid gap-1.5">
            <span className="st-label">Name <small>(what a chord binds to)</small></span>
            <input className="field mono" value={draft.name} placeholder="task_manager" disabled={!!editing}
                   onChange={(e) => { setNameTouched(true); up({ name: e.target.value.toLowerCase() }); }} />
          </label>
          <div className="grid gap-1.5">
            <span className="st-label" id="macro-kind">What it does</span>
            <div className="st-seg" role="group" aria-labelledby="macro-kind">
              {([["keys", Keyboard, "Press keys"], ["run", Terminal, "Launch a program"]] as const).map(([k, I, t]) => (
                <button key={k} type="button" aria-pressed={draft.kind === k} onClick={() => up({ kind: k })}>
                  <I size={16} {...IC} /> {t}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* the payload */}
        <div className="grid content-start gap-3">
          {draft.kind === "keys" ? (
            <>
              <div className="grid gap-1.5">
                <span className="st-label">Shortcut</span>
                <div tabIndex={0} role="textbox" aria-label="Press the shortcut"
                     onFocus={() => setCapturing(true)} onBlur={() => { setCapturing(false); setPending([]); }}
                     onKeyDown={onCapture}
                     className={"st-capture" + (capturing ? " on" : "")}>
                  {pending.length > 0
                    ? <>{pending.map((k) => <KeyCap key={k} name={k} />)}<span className="st-plus">+</span></>
                    : draft.keys.length === 0
                      ? <span className="text-[13.5px] text-ink-2">{capturing ? "Press the shortcut now" : "Click here, then press the shortcut"}</span>
                      : draft.keys.map((k, i) => (
                          <span key={i} className="inline-flex items-center gap-1.5">
                            {i > 0 && <span className="st-plus">+</span>}
                            <KeyCap name={k} onRemove={() => up({ keys: draft.keys.filter((_, j) => j !== i) })} />
                          </span>
                        ))}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2.5">
                <select className="field w-auto flex-1" value="" aria-label="Add a key"
                        onChange={(e) => { if (e.target.value) up({ keys: [...draft.keys, e.target.value] }); }}>
                  <option value="">Add a key</option>
                  {keyGroups(keys).map(([g, ks]) => (
                    <optgroup key={g} label={g}>{ks.map((k) => <option key={k} value={k}>{keyCap(k)} ({k})</option>)}</optgroup>
                  ))}
                </select>
                {draft.keys.length > 0 && <button className="btn" onClick={() => up({ keys: [] })}>Clear</button>}
              </div>
              <p className="st-small">
                Some combinations never reach this page, like Win+E, Ctrl+Alt+Del and media keys. Build those with
                “Add a key”.
              </p>
            </>
          ) : (
            <>
              <label className="grid gap-1.5">
                <span className="st-label">Command</span>
                <input className="field mono" value={draft.run} placeholder='notepad.exe   or   "C:\Games\launcher.exe" --big'
                       onChange={(e) => up({ run: e.target.value })} />
              </label>
              <p className="st-small">
                Anything the Windows Run box accepts. It starts on its own, without a console window, and the chord never
                waits for it.
              </p>
            </>
          )}
        </div>
      </div>

      {/* repeat: saved as input.macros.<name>.repeat = {mode, interval_ms, max_runs} */}
      {draft.kind === "keys" && (
        <div className="mt-5">
          <Row label="Repeat" keyName={`macros.${draft.name || "<name>"}.repeat`} help={HELP.macro_repeat} stack>
            <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
              <div className="st-seg" role="group" aria-label="Repeat">
                {REPEAT_MODES.map((r) => (
                  <button key={r.value} type="button" aria-pressed={draft.repeat.mode === r.value}
                          onClick={() => upRepeat({ mode: r.value as RepeatMode })} title={r.doc}>
                    {r.value === "toggle" ? <ToggleRight size={15} {...IC} /> : r.value === "hold" ? <Repeat size={15} {...IC} /> : null}{r.label}
                  </button>
                ))}
              </div>
              <label className={"flex items-center gap-2 text-[13.5px] text-ink-2 transition-opacity " + (repeating ? "" : "opacity-45")}>
                Every
                <input type="number" className="field num" style={{ width: 84 }} min={10} step={10} disabled={!repeating}
                       value={draft.repeat.interval_ms} onChange={(e) => upRepeat({ interval_ms: Number(e.target.value) })} />
                ms
              </label>
              <label className={"flex items-center gap-2 text-[13.5px] text-ink-2 transition-opacity " + (repeating ? "" : "opacity-45")}>
                At most
                <input type="number" className="field num" style={{ width: 84 }} min={0} step={1} disabled={!repeating}
                       value={draft.repeat.max_runs} onChange={(e) => upRepeat({ max_runs: Math.max(0, Math.round(Number(e.target.value) || 0)) })} />
                times <span className="text-ink-3">(0 for no limit)</span>
              </label>
            </div>
          </Row>
        </div>
      )}

      <div className="mt-5 flex flex-wrap items-center gap-3">
        {problems.length > 0 && (
          <span className="st-problem"><AlertTriangle size={15} {...IC} /> {problems[0]}</span>
        )}
        <span className="flex-1" />
        <button className="btn" onClick={onCancel}>Cancel</button>
        <button className="btn btn-primary" disabled={problems.length > 0} onClick={() => onSave(draft)}>
          {editing ? "Update macro" : "Add macro"}
        </button>
      </div>
    </div>
  );
}
