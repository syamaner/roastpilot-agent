import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The agent serves the API (REST + the SSE stream) on the FastAPI port; the
// Vite dev server proxies `/api` to it so the SPA runs against a live or
// replayed roast without CORS. The SSE route needs no special proxy config —
// it is a plain GET; `changeOrigin` keeps the Host header sane.
const API_TARGET = process.env.ROASTPILOT_API ?? "http://127.0.0.1:8000";

const apiProxy = {
  "/api": {
    target: API_TARGET,
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  // Both the dev server and `vite preview` (the Playwright snapshot target)
  // proxy /api to the agent so the SPA reaches the live/replayed roast same-origin
  // (REST + the SSE stream) without CORS.
  server: { proxy: apiProxy },
  preview: { proxy: apiProxy },
});
