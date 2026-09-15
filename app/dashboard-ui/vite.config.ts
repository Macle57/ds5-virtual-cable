import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// The build target is ONE self-contained file, ../ds5app/dashboard.html,
// which ds5app/dashboard.py serves byte-for-byte and the PyInstaller spec
// ships as a data file. Every script, stylesheet and font is inlined; the
// page fetches nothing but its own /api/* -- the "nothing to fetch, nothing
// to leak to" rule the Python side documents.
//
// During development `vite` serves this same page with HMR and proxies /api
// to a running dashboard server:  python -m ds5app.dashboard --fake --port 8799
// (DS5_API=http://127.0.0.1:8765 points it at a live tray instead -- the way
// to see THIS page against an older backend, which serves its own copy).
declare const process: { env: Record<string, string | undefined> };   // no @types/node in this project
const API = process.env.DS5_API || "http://127.0.0.1:8799";
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile()],
  build: {
    outDir: "../ds5app",
    // Never wipe the Python package directory -- we only own dashboard.html.
    emptyOutDir: false,
    rollupOptions: { input: "dashboard.html" },
    // Rajdhani's woff2 files ride inline as data: URIs (singlefile raises
    // the inline limit; this keeps it explicit).
    assetsInlineLimit: 1 << 24,
    cssCodeSplit: false,
  },
  server: {
    port: 5173,
    open: "/dashboard.html",
    proxy: { "/api": { target: API, changeOrigin: false } },
  },
});
