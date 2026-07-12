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
 * resolved-success-and-idle AND that read is GENUINELY FRESH (`useHealth`'s
 * shared 30s `staleTime` would otherwise let a remount within that window
 * render a cached "idle" snapshot with no network request at all — Codex
 * follow-up on #514/#515; see `useFreshHealthGate`'s doc for the full
 * empirically-verified rationale). Every other health state gets its own
 * explicit view, never the bare form. Layers: (0) while health is pending OR
 * a genuinely fresh read hasn't resolved yet, a neutral loading hold,
 * mirroring `LivePage`'s (post-#514 review: this route previously fell
 * through to the bare form for one `/health` round-trip on reload, the same
 * hazard class). (1) if the server ALREADY reports an active run (health
 * snapshot, e.g. this route was opened mid-roast, or reloaded after a
 * start), a banner with a direct link to the live dashboard replaces the
 * form outright. (2) if the health check itself persistently errors
 * (active-run status UNKNOWN, not "no run"), a neutral "can't confirm" state
 * replaces the form too — this is the route the incident screenshot was
 * almost certainly taken on, so it gets the same guard as `/live`.
 * (3) on a successful start, `navigate("/live")` fires unconditionally once
 * the POST is proven (201) — never gated on a health refetch here. The
 * guarantee chain is: the server COMMITS the run (writes it, on the same
 * store connection the 201 response depends on) BEFORE it ever returns the
 * 201, so by the time this handler navigates, the run is already durably
 * active server-side; `/live` then reads health via `useFreshHealthGate`,
 * which forces a genuinely fresh fetch on arrival (not a cached read) — so
 * that arrival read is guaranteed current, no retry loop needed. Worst case
 * (a persistent `/health` failure on arrival) resolves to `/live`'s own
 * `LiveStatusUnknownView`, never a bare/stranding state (#523). `/live` has
 * no start-form or confirm-retry machinery of its own any more — `LiveStartView`
 * (the pre-#523 idle-state form + retry loop this comment used to describe)
 * was deleted outright; `/start` is the sole start-form surface.
 */

import { useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";

import { AppFrame } from "@/components/shared";
import {
  useBeanProfiles,
  useCreateBeanProfile,
  useDeleteBeanProfile,
  useFreshHealthGate,
  useUpdateBeanProfile,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import type { BeanProfileInput, RoastProfile } from "@/lib/types";

import { StartRoastForm } from "@/pages/dashboard/StartRoastForm";

export function StartRoastView(): React.JSX.Element {
  const navigate = useNavigate();
  // #513 Codex follow-up: gate on `useFreshHealthGate`, not plain `useHealth`
  // — see its doc for the full rationale (a within-staleTime remount must
  // not render a cached "idle" snapshot as proof no run is active).
  const health = useFreshHealthGate();

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
  // with no error and no path to the dashboard). Nothing further to confirm
  // HERE: the server commits the run before it responds 201, and `/live`'s
  // own arrival read (`useFreshHealthGate`, forced-fresh, not cached) is
  // what actually confirms it — see the module doc for the full guarantee
  // chain and the worst-case (persistent health failure) fallback.
  const handleStartRoast = useCallback(
    async (profile: RoastProfile) => {
      await api.startRoast(profile);
      navigate("/live");
    },
    [navigate],
  );

  // #513 follow-up (post-#514/#515 review): hold until health has produced a
  // GENUINELY FRESH read (`useFreshHealthGate`'s `isFresh` — false while
  // pending, AND false while `isSuccess` is true only from stale cache with
  // a forced refetch still in flight, only becoming true once this mount's
  // own fetch settles, error or success). A single `!health.isFresh` check
  // is deliberately NOT combined with `!health.isSuccess`: `isFresh` already
  // implies "settled, one way or another" (`isError` alone makes it true),
  // so a combined `!isSuccess && !isFresh` would miss the stale-cache case
  // (isSuccess true, isFresh false) and a combined `!isSuccess || !isFresh`
  // would wrongly re-hold on a genuine, already-fresh error. Previously
  // (pre-Codex-follow-up) this route fell through straight to the bare form
  // both while genuinely pending AND while showing stale-but-cached "idle"
  // data with a fresher read in flight — a reload, or a remount within the
  // shared 30s staleTime, could show an untouched-looking form even though
  // another tab/process started a run in that window. Mirrors LivePage's
  // loading hold. A start form renders ONLY when health is
  // resolved-success-and-FRESH-and-idle; pending, error, and active-run
  // states each get their own explicit state, never the bare form.
  if (!health.isFresh) {
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
