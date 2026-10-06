import React from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import "./settings.css";
import "./pad.css";
import App from "./App";
import { useStore } from "./lib/store";

// dev only: the store on window, so headless screenshot runs can stage
// states the fake backend cannot (an empty page, a toast). Not in the build.
if (import.meta.env.DEV) (window as unknown as { __ds5: typeof useStore }).__ds5 = useStore;

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
