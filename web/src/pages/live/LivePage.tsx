/**
 * Stable, reload-safe `/live` route (#403).
 *
 * Gives the running roast a permanent address so an accidental refresh never
 * drops the live view. Three server-driven states:
 *
 * 1. Loading   — holds the loading placeholder until health resolves so the
 *    start form does not flash before the active-run status is known.
 * 2. Active run — the full live dashboard (DashboardPage). A reload on this URL
 *    re-hydrates from the server snapshot + SSE — the reload-safe guarantee.
 * 3. No run (idle, just-completed, or after the operator starts a new roast here)
 *    — the start-roast / profile-selection view, ready to begin. This is the
 *    "finished-roast summary" state after cooling: the operator lands on a /live-
 *    focused page with the start form immediately visible (not the home hub's
 *    two-tile landing at `/`), satisfying call #3's "stays on /live until the
 *    operator starts the next roast or navigates away".
 *
 * INVARIANTS: active-run presence comes from the SERVER's `/health` snapshot
 * (`active_run_id`) — never inferred client-side (architecture invariant, D8).
 * Phase is not read here; DashboardPage owns phase via SSE + snapshot.
 * Operator actions, SSE, and MCP access live entirely in DashboardPage.
 */

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { AppFrame } from "@/components/shared";
import {
  roastKeys,
  useBeanProfiles,
  useCreateBeanProfile,
  useDeleteBeanProfile,
  useHealth,
  useUpdateBeanProfile,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import type { BeanProfileInput, RoastProfile } from "@/lib/types";
import { DashboardPage } from "@/pages/dashboard/DashboardPage";
import { StartRoastForm } from "@/pages/dashboard/StartRoastForm";

export function LivePage(): React.JSX.Element {
  const health = useHealth();
  const activeRunId = health.data?.active_run_id ?? null;

  // On a health fetch error fall through to the start form — active run is
  // unknown, so treat it as idle (same as HomeGate's error fallback).
  if (health.isError) {
    return <LiveStartView />;
  }

  // Hold until health resolves — don't flash the start form before the active-run
  // status is known (mirrors HomeGate's hold pattern).
  if (!health.isSuccess) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  // Active run: the full live dashboard. DashboardPage owns all SSE / operator
  // actions / phase — LivePage is a thin routing gate, not a state container.
  // Reloading /live re-hydrates from the server snapshot + SSE (reload-safe).
  if (activeRunId !== null) {
    return <DashboardPage />;
  }

  // No active run: show the start-roast / profile-selection view directly.
  // After a roast ends (post-cooling, active_run_id → null), /live keeps the
  // operator on a start-form-focused page (not the home hub) — they can
  // immediately begin the next roast without navigating back from `/`.
  return <LiveStartView />;
}

// --- Internal start-roast view shown at /live when no run is active. ---

function LiveStartView(): React.JSX.Element {
  const queryClient = useQueryClient();

  const beanProfiles = useBeanProfiles();
  const createBeanProfile = useCreateBeanProfile();
  const updateBeanProfile = useUpdateBeanProfile();
  const deleteBeanProfile = useDeleteBeanProfile();

  const handleCreateProfile = useCallback(
    (input: BeanProfileInput) => createBeanProfile.mutateAsync(input),
    [createBeanProfile],
  );
  const handleUpdateProfile = useCallback(
    (id: string, input: BeanProfileInput) => updateBeanProfile.mutateAsync({ id, input }),
    [updateBeanProfile],
  );
  const handleArchiveProfile = useCallback(
    (id: string) => deleteBeanProfile.mutateAsync(id),
    [deleteBeanProfile],
  );

  // Start a roast: POST, then AWAIT a health refetch so the cache holds the new
  // active_run_id BEFORE re-evaluating. LiveStartView only ever mounts at /live,
  // so the health refetch alone re-renders the page into DashboardPage — no
  // navigate() call needed (which would push a duplicate /live history entry).
  const handleStartRoast = useCallback(
    async (profile: RoastProfile) => {
      await api.startRoast(profile);
      await queryClient.refetchQueries({ queryKey: roastKeys.health });
    },
    [queryClient],
  );

  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          New roast
        </span>
      }
    >
      <div className="flex flex-col gap-4" data-testid="live-start-view">
        <StartRoastForm
          onStart={handleStartRoast}
          profiles={beanProfiles.data?.profiles ?? []}
          profilesLoading={beanProfiles.isLoading}
          onCreateProfile={handleCreateProfile}
          onUpdateProfile={handleUpdateProfile}
          onArchiveProfile={handleArchiveProfile}
        />
      </div>
    </AppFrame>
  );
}
