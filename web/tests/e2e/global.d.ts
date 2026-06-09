// Test hooks the SPA exposes on window for deterministic Playwright assertions
// (D24) — mirrors the `declare global` in the app source, redeclared here so the
// e2e specs see them without importing React app modules into the Node context.
declare global {
  interface Window {
    /** Highest applied SSE event id — the settle signal (see useRoastStream). */
    __lastEventId?: number;
  }
}

export {};
