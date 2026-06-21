/**
 * Start-a-roast route (#324) — the `/start` entry point from the home hub.
 *
 * Reuses the dashboard's `StartRoastForm` (the same component shown in the
 * dashboard's idle state, #158/#303) rather than re-implementing the form: this
 * view only wires the bean-profile library hooks + the start handler around it.
 *
 * On a successful start the server's `/health` refetch surfaces the new
 * `active_run_id`; we navigate to `/`, where `HomeGate` then renders the live
 * dashboard. We do NOT fabricate local run state (render from server, invariant).
 * Errors (e.g. 409 a roast is already active) are surfaced inline by the form,
 * which catches the thrown `ApiError`.
 */

import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { AppFrame } from "@/components/shared";
import {
  roastKeys,
  useBeanProfiles,
  useCreateBeanProfile,
  useDeleteBeanProfile,
  useUpdateBeanProfile,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import type { BeanProfileInput, RoastProfile } from "@/lib/types";

import { StartRoastForm } from "@/pages/dashboard/StartRoastForm";

export function StartRoastView(): React.JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Bean-profile library (#303): the saved-profile dropdown + add/edit modals.
  // Read-only list + the typed CRUD mutations (each invalidates the list).
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
  // `active_run_id` BEFORE we route to `/`. `invalidateQueries` alone would leave
  // the previous (idle) health data in place while the refetch is in flight, so
  // `HomeGate` could briefly render the idle hub at `/` until `/health` returns —
  // `refetchQueries` (awaited) closes that window. We still fabricate no run state:
  // the active run is discovered from the server's refreshed snapshot, then
  // `HomeGate` resolves to the live dashboard with no idle-hub flash.
  const handleStartRoast = useCallback(
    async (profile: RoastProfile) => {
      await api.startRoast(profile);
      await queryClient.refetchQueries({ queryKey: roastKeys.health });
      navigate("/");
    },
    [navigate, queryClient],
  );

  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          New roast
        </span>
      }
    >
      <div className="flex flex-col gap-4" data-testid="start-roast-view">
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
