/* Light or dark: the user's pick lives in localStorage; with no pick the
   page follows the OS and keeps following it. Applied as `data-theme` on
   <html>, which index.css keys its dark token block on. Called once before
   the first render (main.tsx) so the paper never flashes white. */
import { useEffect, useState } from "react";

export type Theme = "light" | "dark";
const KEY = "ds5bridge.theme";
const mq = () => window.matchMedia("(prefers-color-scheme: dark)");

function stored(): Theme | null {
  try { const v = localStorage.getItem(KEY); return v === "dark" || v === "light" ? v : null; } catch { return null; }
}
export function currentTheme(): Theme { return stored() ?? (mq().matches ? "dark" : "light"); }

function apply(t: Theme) { document.documentElement.dataset.theme = t; }

export function initTheme() {
  apply(currentTheme());
  // no explicit pick: track the OS as it changes
  mq().addEventListener("change", () => { if (!stored()) apply(currentTheme()); });
}

export function useTheme(): [Theme, () => void] {
  const [t, setT] = useState<Theme>(currentTheme);
  useEffect(() => {
    const obs = new MutationObserver(() => setT((document.documentElement.dataset.theme as Theme) || "light"));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, []);
  const toggle = () => {
    const next: Theme = currentTheme() === "dark" ? "light" : "dark";
    try { localStorage.setItem(KEY, next); } catch { /* private mode: the pick lasts the page */ }
    apply(next);
  };
  return [t, toggle];
}
