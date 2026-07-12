/**
 * TanStack Query hooks for the REST surface (api.py).
 *
 * Snapshots and post-roast reads only — live telemetry/phase flow over SSE
 * (`useRoastStream`), not polling. Pages consume these read-only.
 */

import { useRef } from "react";
import {
  skipToken,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { BeanProfileInput } from "@/lib/types";

export const roastKeys = {
  health: ["health"] as const,
  history: ["roasts"] as const,
  detail: (runId: string) => ["roasts", runId] as const,
  timeline: (runId: string) => ["roasts", runId, "timeline"] as const,
  telemetry: (runId: string, downsample: number) =>
    ["roasts", runId, "telemetry", downsample] as const,
};

/** Query keys for the config surface (#419, D78). */
export const configKeys = {
  snapshot: ["config"] as const,
  devices: ["config", "devices"] as const,
};

/** Query keys for the bean-profile library (#303). */
export const beanProfileKeys = {
  all: ["bean-profiles"] as const,
};

export function useHealth() {
  return useQuery({ queryKey: roastKeys.health, queryFn: api.health });
}

/**
 * Shared "genuinely fresh since THIS hook instance mounted" derivation for any
 * TanStack Query result whose caller GATES a render decision on it (never
 * shows a form / never asserts an authoritative absence from a CACHED value
 * that might be up to `staleTime` stale). Factored out of `useFreshHealthGate`
 * once `useFreshHistoryGate` needed the identical pattern (#523 Codex
 * follow-up on #532) — see that hook's doc for the full empirically-verified
 * rationale of why `refetchOnMount: "always"` alone is insufficient and why
 * the snapshot must be taken once, not re-read every render.
 *
 * The initial `dataUpdatedAt` seen on mount (possibly `0` with no cache
 * entry, or a past timestamp if cached) is snapshotted once and never
 * reused; `isFresh` flips true the moment the live `dataUpdatedAt` advances
 * past that snapshot (confirmed empirically: `dataUpdatedAt` advances on
 * every settled fetch, even one that resolves with byte-identical data) OR
 * the query settles into an error state. Callers must pass a query created
 * with `refetchOnMount: "always"` — this helper does not create the query
 * itself, since each caller's `queryKey`/`queryFn` differ.
 */
function useFreshGate<T>(
  query: UseQueryResult<T>,
): UseQueryResult<T> & { isFresh: boolean } {
  const initialUpdatedAtRef = useRef<number | null>(null);
  if (initialUpdatedAtRef.current === null) {
    initialUpdatedAtRef.current = query.dataUpdatedAt;
  }
  const isFresh =
    query.dataUpdatedAt > initialUpdatedAtRef.current || query.isError;
  return { ...query, isFresh };
}

/**
 * Health, but for the two views that GATE a start form on active-run status
 * (`/live`'s `LiveStartView` and `/start`'s `StartRoastView` — #513 Codex
 * follow-up). `useHealth()`'s shared `staleTime: 30_000` is correct for
 * every OTHER consumer (the header/nav render stale-then-update, which is
 * fine — they don't gate a form on it) but wrong here: within that window a
 * remount renders a CACHED `active_run_id` with `isSuccess: true` and never
 * even issues a network request (confirmed empirically — TanStack Query
 * does not refetch on mount while data is still fresh by its own
 * accounting), so a start form could render from up-to-30s-stale data. A
 * second roastpilot process (or another tab) could have started a run in
 * that window; the 409 the real start attempt would hit bounds the damage
 * but does not prevent the same #513 flash-then-strand risk this whole
 * story exists to close.
 *
 * `refetchOnMount: "always"` alone is NOT enough either: it does force a
 * fresh network call (confirmed empirically), but `isSuccess` and the
 * cached (stale) `data` stay true/present for the ENTIRE in-flight window —
 * a naive `!isSuccess` hold would still let the bare form render from stale
 * data while the fresh fetch is still in the air. Gating views hold their
 * loading state while `!isFresh`.
 */
export function useFreshHealthGate() {
  const query = useQuery({
    queryKey: roastKeys.health,
    queryFn: api.health,
    refetchOnMount: "always",
  });
  return useFreshGate(query);
}

export function useHistory() {
  return useQuery({ queryKey: roastKeys.history, queryFn: api.history });
}

/**
 * History, but for `/live`'s idle state (#523 Codex follow-up on #532):
 * `useHistory()`'s shared `staleTime: 30_000` is correct for `/roasts` (the
 * history page renders stale-then-update, which is fine there) but wrong
 * here — `/live` now treats history as an AUTHORITATIVE source (the
 * persistent last-completed-run fallback), so the same #513-class hazard
 * `useFreshHealthGate` closes for health applies: a remount within the
 * staleTime window could render a CACHED (possibly EMPTY) history list with
 * `isSuccess: true` and no network request at all, rendering
 * `LiveNoRoastsView` — a false "this roaster has never completed a roast"
 * claim — or an OLD summary, when a genuinely fresh read would show the
 * true latest completed run. Same `useFreshGate` derivation as health;
 * `/live` holds its loading state while `!history.isFresh` (unless a
 * session-sticky id is already known, which doesn't depend on history at all).
 */
export function useFreshHistoryGate() {
  const query = useQuery({
    queryKey: roastKeys.history,
    queryFn: api.history,
    refetchOnMount: "always",
  });
  return useFreshGate(query);
}

// `skipToken` as the queryFn disables the query when there is no run id — the
// idiomatic TanStack form. It also narrows `runId` to a non-null string inside
// the fn (no `as string` cast) and avoids registering a dangling empty-key entry.
export function useRoast(runId: string | null) {
  return useQuery({
    queryKey: roastKeys.detail(runId ?? ""),
    queryFn: runId === null ? skipToken : () => api.roast(runId),
  });
}

export function useTimeline(runId: string | null) {
  return useQuery({
    queryKey: roastKeys.timeline(runId ?? ""),
    queryFn: runId === null ? skipToken : () => api.timeline(runId),
  });
}

export function useTelemetry(runId: string | null, downsample = 1) {
  return useQuery({
    queryKey: roastKeys.telemetry(runId ?? "", downsample),
    queryFn: runId === null ? skipToken : () => api.telemetry(runId, downsample),
  });
}

// --- Bean-profile library (#303, D45) — the Start-Roast dropdown's CRUD. ---

/** The saved bean-profile library for the Start-Roast dropdown (name-ordered
 *  server-side). The list the dropdown renders from. */
export function useBeanProfiles() {
  return useQuery({ queryKey: beanProfileKeys.all, queryFn: api.beanProfiles });
}

/** Create a saved bean profile; invalidates the list so the new row appears in
 *  the dropdown. The caller selects the returned profile (the mutation resolves
 *  to the created `BeanProfile`). */
export function useCreateBeanProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: BeanProfileInput) => api.createBeanProfile(input),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: beanProfileKeys.all }),
  });
}

/** Edit a saved bean profile (future roasts only); invalidates the list. */
export function useUpdateBeanProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: BeanProfileInput }) =>
      api.updateBeanProfile(id, input),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: beanProfileKeys.all }),
  });
}

/** Archive (soft-delete) a saved bean profile; invalidates the list. */
export function useDeleteBeanProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.deleteBeanProfile(id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: beanProfileKeys.all }),
  });
}

// --- Config (#419, D78) ---

/**
 * GET /api/config/devices — enumerate connected serial + audio devices.
 * staleTime: 0 (devices change on USB plug); refetchOnWindowFocus: false
 * (avoids a rescan burst when the operator alt-tabs; Rescan button is the
 * explicit trigger).
 */
export function useDevices() {
  return useQuery({
    queryKey: configKeys.devices,
    queryFn: api.devices,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
}

/**
 * GET /api/config — the full config snapshot with per-field metadata.
 * Stale time: 30 s (config changes only on PUT; no background polling needed).
 */
export function useConfig() {
  return useQuery({
    queryKey: configKeys.snapshot,
    queryFn: api.config,
    staleTime: 30_000,
  });
}

/**
 * PUT /api/config — persist a partial edit (controller + advisor only).
 * On success the cache is updated to the server's response (the authoritative
 * effective snapshot post-save), so the UI immediately reflects saved values
 * without a second GET.
 *
 * Cancels any in-flight GET /api/config before writing the response (#483 fix
 * round): a background refetch that started before the PUT can otherwise
 * resolve AFTER onSuccess and overwrite the cache with a pre-save snapshot,
 * silently reverting the just-cleared dirty state and displayed values.
 */
export function useSaveConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (edit: Record<string, unknown>) => api.saveConfig(edit),
    onSuccess: async (snapshot) => {
      await queryClient.cancelQueries({ queryKey: configKeys.snapshot });
      queryClient.setQueryData(configKeys.snapshot, snapshot);
    },
  });
}
