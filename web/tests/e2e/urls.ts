/**
 * Per-fixture preview origins (D26 multi-fixture harness).
 *
 * Shared by `playwright.config.ts` (sets each `vite preview` port) and the
 * dashboard specs (which `page.goto` the preview backed by their fixture). Kept in
 * a tiny standalone module so a spec can import the URLs without importing the
 * whole `defineConfig()` config object.
 *
 * Pairs (agent ⇄ preview): see global-setup `AGENTS` for the agent side.
 *   session-2          :8000 ⇄ :4173  → dashboard-live + route-harness pages
 *   session-1          :8001 ⇄ :4174  → dashboard-fault
 *   fault-pre-t0       :8002 ⇄ :4175  → dashboard-recovery
 *   session-2 (devel.) :8003 ⇄ :4176  → dashboard-developed
 *
 * `dashboard-developed` reuses the session-2 fixture but at a LATER marker
 * (`first_crack`) than `dashboard-live` (`preheating`). `advance-to` is
 * monotonic-forward per agent, so the developed state gets its OWN session-2 agent
 * rather than re-advancing the live agent (which would couple the two specs'
 * ordering).
 */
export const WEB_URLS = {
  /** session-2 → dashboard-live + every route-harness page (fixture-independent). */
  session2: process.env.ROASTPILOT_WEB_URL ?? "http://127.0.0.1:4173",
  /** session-1 → dashboard-fault (real env-ceiling EMERGENCY_STOP → faulted). */
  session1: process.env.ROASTPILOT_WEB_URL_FAULT ?? "http://127.0.0.1:4174",
  /** fault-pre-t0 → dashboard-recovery (pre-T0 overrun → operator_recovery_required). */
  faultPreT0: process.env.ROASTPILOT_WEB_URL_RECOVERY ?? "http://127.0.0.1:4175",
  /** session-2 (own agent) → dashboard-developed (advance-to first_crack: real curve). */
  session2Developed: process.env.ROASTPILOT_WEB_URL_DEVELOPED ?? "http://127.0.0.1:4176",
} as const;
