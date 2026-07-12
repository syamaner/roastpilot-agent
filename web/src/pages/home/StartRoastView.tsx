/**
 * Start-a-roast route (#324, updated #403, #523) — under the #523 IA this is
 * THE ONLY place a start form exists (`/live` never renders one; the home hub
 * links here for "Start a new roast").
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
 * snapshot — this covers `operator_recovery_required` too, since that phase's
 * run row still has a non-null `active_run_id`), a banner with a direct link
 * to the live dashboard/recovery flow replaces the form outright. (2) if the
 * health check itself persistently errors (active-run status UNKNOWN, not "no
 * run"), a neutral "can't confirm" state replaces the form too — this is the
 * route the #513 incident screenshot was taken on, so it gets the same guard
 * as `/live`. (3) STALE SESSION (#523): a history run with `outcome: null`
 * (still open in the store) that `health.active_run_id` does NOT point to —
 * the signature of the 12 Jul impostor-listener incident (runs
 * `99b2e272`/`6300be8f`, docs/recent-fixes.md), where a second process
 * answered `/health` with `active_run_id: null` while a genuinely stranded run
 * row sat open. Explains the state and offers a link to `/live`'s recovery
 * flow; the explicit clear/end-stale-run operator action is OUT OF SCOPE here
 * — it needs its own safety design (a typed operator action, audit event, and
 * a guard that the MCP child is actually idle/cooled before allowing it, so
 * it can never race a genuinely hot machine); see the in-line note at the
 * stale-session branch below and #523's tracking issue for the gap.
 * (4) on a successful start, `navigate("/live")` fires
 * unconditionally once the POST is proven (201) — never gated on the health
 * refetch actually confirming the new run, since `LivePage` itself (`/live`'s
 * idle state) owns the resilient confirm-with-retry + fallback flow.
 */

import { useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";

import { AppFrame } from "@/components/shared";
import {
  useBeanProfiles,
  useCreateBeanProfile,
  useDeleteBeanProfile,
  useFreshHealthGate,
  useHistory,
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

  // Stale-session detection (#523): the roast history, read unconditionally
  // (cheap — the same list `/roasts` already fetches, TanStack Query
  // dedupes/caches it). A STALE run is a history row with `outcome: null`
  // (still open — never finalised) whose id the CURRENT health snapshot does
  // not recognise as the active run. That mismatch is exactly the 12 Jul
  // incident signature: a second listener answered `/health` with
  // `active_run_id: null` while the genuine roast server had a run row open
  // that it never saw finalised. An operator_recovery_required run does NOT
  // hit this path — its `active_run_id` stays non-null and is caught by the
  // active-run banner above (layer 1), which is the correct, safer state
  // (heat/fan locked, recovery actions available) rather than this
  // explanatory dead-end.
  const history = useHistory();
  const staleRun =
    history.data?.runs.find(
      (run) => run.outcome === null && run.id !== (health.data?.active_run_id ?? null),
    ) ?? null;

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

  // #523: hold briefly for history to settle before falling through to the
  // form — the stale-session check below needs it, and rendering the form
  // first (then possibly replacing it a moment later) would itself be a
  // "form flashes then disappears" hazard of the exact class #513 fixed.
  if (history.isPending) {
    return (
      <AppFrame>
        <div data-testid="start-roast-loading" />
      </AppFrame>
    );
  }

  // #523 stale session: a run row is open (`outcome: null`) that this
  // process's health snapshot does not recognise as active — never a form
  // here either, since starting a new roast on top of an unresolved stranded
  // run compounds the confusion the 12 Jul incident already caused. Explains
  // the state and offers the one thing this route CAN safely do: hand off to
  // `/live`'s recovery flow (resume/drop/cool/end via the existing operator
  // actions). The dedicated clear/end-stale-run action the spec calls for is
  // OUT OF SCOPE for this story — it needs its own safety design (a typed
  // operator action, audit event, and a guard that the MCP child is actually
  // idle/cooled before allowing it, so it can never race a genuinely hot
  // machine) — see the module doc and #523's tracking issue for the gap.
  if (staleRun !== null) {
    return (
      <AppFrame
        headerRight={
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Stale session
          </span>
        }
      >
        <div
          className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-caution/50 bg-roast-caution/10 p-8 text-center"
          data-testid="start-roast-stale-session"
        >
          <h2 className="text-lg font-bold uppercase tracking-wide">
            A previous roast wasn&apos;t finished
          </h2>
          <p className="text-sm text-muted-foreground">
            A run from an earlier session is still open, but this page can&apos;t
            confirm it as the active roast. If the roaster is hot, don&apos;t start a
            new one. Open the live view to check its status — if it&apos;s still
            recognised as active you can resume, drop, cool, or end it there;
            if not, verify the hardware directly before starting again.
          </p>
          <Link
            to="/live"
            data-testid="start-roast-stale-session-link"
            className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Open live view
          </Link>
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
