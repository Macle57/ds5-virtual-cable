import type { Json } from "../../lib/types";
import { JsonField, NumberField, Row, TextField, Toggle } from "./controls";

/* Unknown keys and whole unknown sections: render by JSON type, recursively,
   so a section another build adds appears and is editable without this page
   changing. */
export default function GenericRows({ path, obj, known }:
  { path: string[]; obj: Record<string, Json> | undefined; known?: Set<string> }) {
  const entries = Object.entries(obj ?? {}).filter(([k]) => !known || !known.has(k));
  if (!entries.length) return null;
  return (
    <>
      {entries.map(([key, value]) => {
        const p = [...path, key];
        if (value !== null && typeof value === "object" && !Array.isArray(value)) {
          return (
            <div key={key} className="py-2.5">
              <div className="mono mb-1 text-[11px] uppercase tracking-wider text-ink-3">{key}</div>
              <div className="rounded-xl border border-line pl-3 pr-1">
                <GenericRows path={p} obj={value as Record<string, Json>} />
              </div>
            </div>
          );
        }
        if (typeof value === "boolean") return <Row key={key} label={key}><Toggle path={p} /></Row>;
        if (typeof value === "number") return <Row key={key} label={key}><NumberField path={p} dflt={null} nullable /></Row>;
        if (value === null || typeof value === "string") return <Row key={key} label={key}><TextField path={p} width={180} /></Row>;
        return <Row key={key} label={key} stack><JsonField path={p} /></Row>;
      })}
    </>
  );
}
