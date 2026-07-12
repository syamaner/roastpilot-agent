/**
 * Start-a-roast route (#324, updated #403) — the `/start` entry point from the
 * home hub. No longer linked from the UI (the operator's path is `/` →
 * `/live`, D81/#423), but still directly reachable by URL/bookmark, so it
 * keeps the same safety guarantees as `/live`.
 *
 * Reuses the dashboard's `StartRoastForm` (the same component shown in the
 * dashboard's idle state, #158/#303) rather than re-implementing the form: this
 * view only wires the bean-profile library hooks + the start handler around it.
 *
 * #513: a bare form must never be the only thing an operator can see once a
 * run is active, or when whether one is active is unknown — either leaves no
 * path to the emergency stop. A start form renders ONLY when health is
 * resolved-success-and-idle; every other health state gets its own explicit
 * view, never the bare form. Four layers: (0) while health is still pending
 * (the initial fetch in flight — neither `isSuccess` nor `isError` yet), a
 * neutral loading hold, mirroring `LivePage`'s (post-#514 review: this route
 * previously fell through to the bare form for one `/health` round-trip on
 * reload, the same hazard class). (1) if the server ALREADY reports an active
 * run (health snapshot, e.g. this route was opened mid-roast, or reloaded
 * after a start), a banner with a direct link to the live dashboard replaces
 * the form outright. (2) if the health check itself persistently errors
 * (active-run status UNKNOWN, not "no run"), a neutral "can't confirm" state
 * replaces the form too — this is the route the incident screenshot was
 * almost certainly taken on, so it gets the same guard as `/live`.
 * (3) on a successful start, `navigate("/live")` fires unconditionally once
 * the POST is proven (201) — never gated on the health refetch actually
 * confirming the new run, since `LivePage` itself (`/live`'s idle state,
 * `LiveStartView`) owns the resilient confirm-with-retry + fallback flow.
 */

import { useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";

import { AppFrame } from "@/components/shared";
import {
  useBeanProfiles,
  useCreateBeanProfile,
  useDeleteBeanProfile,
  useHealth,
  useUpdateBeanProfile,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import type { BeanProfileInput, RoastProfile } from "@/lib/types";

import { StartRoastForm } from "@/pages/dashboard/StartRoastForm";

export function StartRoastView(): React.JSX.Element {
  const navigate = useNavigate();
  const health = useHealth();

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

  // Start a roast: POST, then navigate to `/live` on the PROVEN 201 —
  // unconditionally, never gated on a health refetch that can itself fail
  // (#513: `refetchQueries` always resolves even when the underlying fetch
  // failed, so awaiting it before navigating left the operator on this form
  // with no error and no path to the dashboard). `/live`'s idle state owns
  // confirming the new run against `/health` with retries + a manual fallback.
  const handleStartRoast = useCallback(
    async (profile: RoastProfile) => {
      await api.startRoast(profile);
      navigate("/live");
    },
    [navigate],
  );

  // #513 follow-up (post-#514 review): hold until health resolves — pending
  // (isSuccess false, isError false, the initial fetch in flight) fell through
  // both existing guards straight to the bare form, the SAME hazard class this
  // whole story fixes: a reload of this still-URL-reachable route mid-roast
  // showed an untouched-looking form for one /health round-trip before the
  // active-run banner appeared. Mirrors LivePage's loading hold. A start form
  // renders ONLY when health is resolved-success-and-idle; pending, error, and
  // active-run states each get their own explicit state, never the bare form.
  if (!health.isSuccess && !health.isError) {
    return (
      <AppFrame>
        <div data-testid="start-roast-loading" />
      </AppFrame>
    );
  }

  // #513 defensive layer: the server already reports an active run (this route
  // opened mid-roast, or reloaded after a start) — never show the bare form,
  // which would strand the operator without a path to the live dashboard/
  // emergency stop. Phase itself is not read here; only active-run presence.
  if (health.isSuccess && health.data.active_run_id !== null) {
    return (
      <AppFrame
        headerRight={
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Roast in progress
          </span>
        }
      >
        <div
          className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-caution/50 bg-roast-caution/10 p-8 text-center"
          data-testid="start-roast-active-run-banner"
        >
          <h2 className="text-lg font-bold uppercase tracking-wide">A roast is already running</h2>
          <p className="text-sm text-muted-foreground">
            Open the live dashboard for status and controls, including emergency stop.
          </p>
          <Link
            to="/live"
            data-testid="start-roast-active-run-link"
            className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Open live dashboard
          </Link>
        </div>
      </AppFrame>
    );
  }

  // #513 medium: a persistent health error (useHealth's own retry:1 already
  // rode out a single blip) means active-run status is UNKNOWN — this must
  // NEVER fall through to the bare form either, for the same reason as above.
  // This is exactly the incident route (still URL-reachable), so it gets the
  // same guard as the banner case, not just /live's idle state.
  if (health.isError) {
    return (
      <AppFrame
        headerRight={
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Status unknown
          </span>
        }
      >
        <div
          className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-8 text-center"
          data-testid="start-roast-status-unknown"
        >
          <h2 className="text-lg font-bold uppercase tracking-wide">Can&apos;t confirm roaster status</h2>
          <p className="text-sm text-muted-foreground">
            This page could not reach the agent to check whether a roast is active. If
            one is running, it is still live and heating — reload to reconnect before
            assuming it is safe to start a new one.
          </p>
          <a
            href="/start"
            data-testid="start-roast-status-unknown-reload"
            className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Reload
          </a>
        </div>
      </AppFrame>
    );
  }

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
