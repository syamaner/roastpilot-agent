/**
 * TanStack Query hooks for the REST surface (api.py).
 *
 * Snapshots and post-roast reads only — live telemetry/phase flow over SSE
 * (`useRoastStream`), not polling. Pages consume these read-only.
 */

import {
  skipToken,
  useMutation,
  useQuery,
  useQueryClient,
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

export function useHistory() {
  return useQuery({ queryKey: roastKeys.history, queryFn: api.history });
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
 */
export function useSaveConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (edit: Record<string, unknown>) => api.saveConfig(edit),
    onSuccess: (snapshot) =>
      queryClient.setQueryData(configKeys.snapshot, snapshot),
  });
}
