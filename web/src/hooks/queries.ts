/**
 * TanStack Query hooks for the REST surface (api.py).
 *
 * Snapshots and post-roast reads only — live telemetry/phase flow over SSE
 * (`useRoastStream`), not polling. Pages consume these read-only.
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

export const roastKeys = {
  health: ["health"] as const,
  history: ["roasts"] as const,
  detail: (runId: string) => ["roasts", runId] as const,
  timeline: (runId: string) => ["roasts", runId, "timeline"] as const,
  telemetry: (runId: string, downsample: number) =>
    ["roasts", runId, "telemetry", downsample] as const,
};

export function useHealth() {
  return useQuery({ queryKey: roastKeys.health, queryFn: api.health });
}

export function useHistory() {
  return useQuery({ queryKey: roastKeys.history, queryFn: api.history });
}

export function useRoast(runId: string | null) {
  return useQuery({
    queryKey: roastKeys.detail(runId ?? ""),
    queryFn: () => api.roast(runId as string),
    enabled: runId !== null,
  });
}

export function useTimeline(runId: string | null) {
  return useQuery({
    queryKey: roastKeys.timeline(runId ?? ""),
    queryFn: () => api.timeline(runId as string),
    enabled: runId !== null,
  });
}

export function useTelemetry(runId: string | null, downsample = 1) {
  return useQuery({
    queryKey: roastKeys.telemetry(runId ?? "", downsample),
    queryFn: () => api.telemetry(runId as string, downsample),
    enabled: runId !== null,
  });
}
