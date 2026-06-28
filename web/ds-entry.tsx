/**
 * Design-system bundle entry for /design-sync (claude.ai/design).
 *
 * Re-exports the reusable shared components so the converter can bundle them
 * into `window.RoastPilotDS.*`. NOT imported by the app — it exists only as the
 * esbuild entry the design-sync converter points `--entry` at. Page-level UI is
 * intentionally out of scope; this is the on-brand, reusable surface.
 */

export { AppFrame } from "@/components/shared/AppFrame";
export { VerdictBadge } from "@/components/shared/VerdictBadge";
export { ConnectionIndicator } from "@/components/shared/ConnectionIndicator";
export { LiveCurve } from "@/components/shared/LiveCurve/LiveCurve";
