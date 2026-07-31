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
 * as `/live`. (3) STALE SESSION (#523/#525): a history run with `outcome:
 * null` (still open in the store) that `health.active_run_id` does NOT point
 * to — the signature of the 12 Jul impostor-listener incident (runs
 * `99b2e272`/`6300be8f`, docs/recent-fixes.md), where a second process
 * answered `/health` with `active_run_id: null` while a genuinely stranded run
 * row sat open. This case is inherently multi-process: a SAME-process bare
 * orphan cannot exist by the time this route is ever polled (restart recovery
 * always finalises a stale `faulted` row, or promotes any other unfinalised
 * phase into `operator_recovery_required`, before this process ever serves a
 * request — that leaves no third "unfinalised, not faulted, not
 * recovery-required" bucket for a same-process orphan to occupy). Explains the
 * state and offers the operator BOTH the `/live` recovery-flow link (in case
 * the health-RECOGNISED case applies to a DIFFERENT run — see #525's #535
 * follow-up: it does NOT apply to THIS stale row, since `staleRun`'s own
 * derivation already excludes that) AND the explicit clear/end-stale-run
 * operator action (#525): a typed, audited, DB-liveness-gated finalize —
 * never drop/cool, never an MCP write. Per the #525 safety design (routed
 * through `safety-reviewer`, PASS-WITH-CONDITIONS): an own-process MCP-idle
 * check cannot observe a DIFFERENT process's roaster (this is the
 * fundamentally multi-process case), so the server gates on shared DB
 * evidence instead — recent telemetry for the run, not any one process's
 * self-report — and refuses with a distinct "actively driven, do not clear"
 * 409 if that evidence exists. Kept self-contained on `/start` (no `/live`
 * handoff needed for the clear action itself — the #535 follow-up ruled a
 * handoff broken anyway for the health-UNRECOGNISED case, since `/live`'s
 * dashboard only mounts for a non-null `active_run_id`).
 * (4) on a successful start, `navigate("/live")` fires unconditionally once
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
 *
 * History (the stale-session source, layer 3) earns the same freshness-and-
 * error treatment (Codex follow-up on #535, mirroring the identical triple
 * `/live` already built for its own history use, #532): gated on
 * `useFreshHistoryGate`, not plain `useHistory`, so a within-the-shared-30s-
 * staleTime remount can't render a cached (possibly stale-or-empty) history
 * list as proof no session is stale; and `history.isError` gets its own
 * neutral "can't verify" state rather than falling through to the form —
 * a persistent `/api/roasts` failure means the stale-session source is
 * UNKNOWN, not "no stale session", exactly the isSuccess≠current hazard
 * class this whole file already guards for health.
 */

import { useCallback, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";

import { AppFrame } from "@/components/shared";
import {
  roastKeys,
  useBeanProfiles,
  useCreateBeanProfile,
  useDeleteBeanProfile,
  useFreshHealthGate,
  useFreshHistoryGate,
  useUpdateBeanProfile,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import type { BeanProfileInput, RoastProfile } from "@/lib/types";
import type { LiveNavigationState } from "@/pages/live/LivePage";

import { StartRoastForm } from "@/pages/dashboard/StartRoastForm";

/**
 * The #525 clear/end-stale-run confirm step, embedded in `StartRoastView`'s
 * stale-session card. A required, non-empty reason (mirrors the server's own
 * validation) gates the action — no accidental one-click clears. On success,
 * invalidates BOTH history and health (the two sources `staleRun`'s own
 * derivation reads) so the stale card disappears and the plain form renders
 * next render, without a manual reload.
 */
function ClearStaleSessionAction({ runId }: { runId: string }): React.JSX.Element {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");

  const mutation = useMutation({
    // Send the TRIMMED reason — it must match what actually gated the
    // confirm button (whitespace-only is blocked below), so the persisted
    // audit value is never a padded/whitespace-only string.
    mutationFn: () => api.clearStaleSession(runId, { reason: reason.trim() }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: roastKeys.history });
      void queryClient.invalidateQueries({ queryKey: roastKeys.health });
    },
  });

  if (mutation.isSuccess) {
    // The invalidated queries above will re-render this page past the
    // stale-session branch shortly; a brief explicit success state avoids a
    // flash of the confirm UI while that refetch is still in flight.
    return (
      <p data-testid="start-roast-stale-session-cleared" className="text-sm text-roast-nominal">
        Cleared.
      </p>
    );
  }

  if (!confirming) {
    return (
      <button
        type="button"
        data-testid="start-roast-stale-session-clear-open"
        onClick={() => setConfirming(true)}
        className="rounded-md border border-roast-fault/50 px-5 py-3 text-sm font-semibold text-roast-fault transition-colors hover:bg-roast-fault/10"
      >
        This isn&apos;t my roast — end it
      </button>
    );
  }

  const trimmedReason = reason.trim();

  return (
    <div className="flex w-full flex-col items-center gap-2" data-testid="start-roast-stale-session-confirm">
      <p className="text-xs text-muted-foreground">
        This only finalises the leftover record — it never touches heat, fan, or cooling.
      </p>
      <input
        type="text"
        data-testid="start-roast-stale-session-reason"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Why are you clearing this? (required)"
        className="w-full rounded-md border border-border bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="start-roast-stale-session-clear-confirm"
          disabled={trimmedReason.length === 0 || mutation.isPending}
          onClick={() => mutation.mutate()}
          className="rounded-md bg-roast-fault px-4 py-2 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {mutation.isPending ? "Clearing…" : "Confirm end"}
        </button>
        <button
          type="button"
          data-testid="start-roast-stale-session-clear-cancel"
          disabled={mutation.isPending}
          onClick={() => {
            setConfirming(false);
            setReason("");
          }}
          className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground disabled:cursor-not-allowed disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
      {mutation.isError && (
        <span data-testid="start-roast-stale-session-clear-error" className="text-xs text-roast-fault">
          {mutation.error instanceof Error ? mutation.error.message : "Could not clear — try again."}
        </span>
      )}
    </div>
  );
}

export function StartRoastView(): React.JSX.Element {
  const navigate = useNavigate();
  // #513 Codex follow-up: gate on `useFreshHealthGate`, not plain `useHealth`
  // — see its doc for the full rationale (a within-staleTime remount must
  // not render a cached "idle" snapshot as proof no run is active).
  const health = useFreshHealthGate();

  // Stale-session detection (#523): the roast history. A STALE run is a
  // history row with `outcome: null` (still open — never finalised) whose id
  // the CURRENT health snapshot does not recognise as the active run. That
  // mismatch is exactly the 12 Jul incident signature: a second listener
  // answered `/health` with `active_run_id: null` while the genuine roast
  // server had a run row open that it never saw finalised. An
  // operator_recovery_required run does NOT hit this path — its
  // `active_run_id` stays non-null and is caught by the active-run banner
  // above (layer 1), which is the correct, safer state (heat/fan locked,
  // recovery actions available) rather than this explanatory dead-end.
  //
  // Codex follow-up on #535: gated on `useFreshHistoryGate`, not plain
  // `useHistory` — this route treats history as the STALE-SESSION
  // AUTHORITATIVE source, so it earns the same #513/#532-class treatment
  // `useFreshHealthGate` already gives health and `/live` already gives its
  // own history use: within the shared 30s `staleTime`, a remount would
  // otherwise render a CACHED (possibly stale-or-empty) history list with
  // `isSuccess: true` and no network request at all, silently missing a
  // stale session that started in that window. See `useFreshHistoryGate`'s
  // doc for the full empirically-verified rationale.
  const history = useFreshHistoryGate();
  // #557 field incident: `health.data?.active_run_id ?? null` silently
  // coalesces an UNRESOLVED health read (data undefined — e.g. the process's
  // first /health fetch failing against a still-booting server) to "no
  // active run", which classifies every unfinalized history row — including
  // THIS process's own active/recovering run — as stale. The incident's
  // exact path was never pinned to one mechanism, and doesn't need to be —
  // two distinct hypotheses both misrepresent "no active run" the same way,
  // and this guard closes both: (1) UNRESOLVED — `data` itself is
  // `undefined` (never fetched, or the fetch failed); (2) RESOLVED BUT NOT
  // THIS READ'S OWN — `useFreshGate` (hooks/queries.ts) pins `isFresh` to
  // `true` PERMANENTLY the first time a mount's own fetch settles and never
  // re-arms it, so a tab left open across a start-roast action and a
  // restart (one long-lived mount) could have `isFresh` already true from an
  // earlier settle while `data` reflects a stale cached snapshot on a later
  // render — `isFresh` alone is not proof this render's data is current.
  // `staleRun` must be null unless health is BOTH resolved AND this mount's
  // own fresh read — an unresolved/stale/errored health read is handled by
  // its own explicit leg above (health.isError) or the `!health.isFresh`
  // hold, never an authoritative stale claim here.
  const staleRun =
    !health.isFresh || health.data === undefined
      ? null
      : (history.data?.runs.find(
          (run) => run.outcome === null && run.id !== health.data.active_run_id,
        ) ?? null);

  // Bean-profile library (#303): the saved-profile dropdown + add/edit modals.
  // Read-only list + the typed CRUD mutations (each invalidates the list).
  const beanProfiles = useBeanProfiles();
  const createBeanProfile = useCreateBeanProfile();
  const updateBeanProfile = useUpdateBeanProfile();
  const deleteBeanProfile = useDeleteBeanProfile();

  const handleCreateProfile = useCallback(
    (input: BeanProfileInput, draftAttemptId?: string) =>
      createBeanProfile.mutateAsync({ input, draftAttemptId }),
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
  //
  // #516: the 201 response's `instance_id` (the process that just accepted
  // this start) is passed forward as router navigation STATE — never a
  // query param or persisted anywhere — so `/live`'s arrival health read can
  // compare against it. Router state is the right tool here specifically
  // because it does NOT survive a reload: a reload has no "prior start" to
  // compare against, so there is nothing to carry, and `/live` correctly
  // falls back to its plain fresh-health gate with no expected id to check.
  const handleStartRoast = useCallback(
    async (profile: RoastProfile) => {
      const started = await api.startRoast(profile);
      const state: LiveNavigationState | undefined = started.instance_id
        ? { expectedInstanceId: started.instance_id }
        : undefined;
      navigate("/live", { state });
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

  // #523: hold while history hasn't produced a GENUINELY FRESH read yet —
  // the stale-session check below needs it, and rendering the form first
  // (then possibly replacing it a moment later) would itself be a "form
  // flashes then disappears" hazard of the exact class #513 fixed. Gated on
  // `!history.isFresh` (mirrors the `!health.isFresh` hold above and
  // `/live`'s own `!history.isFresh` hold, #532), not `history.isPending` —
  // `isFresh` already implies "settled" (`isError` alone makes it true), so
  // this composes correctly with the error state below rather than holding
  // forever on an already-fresh error.
  if (!history.isFresh) {
    return (
      <AppFrame>
        <div data-testid="start-roast-loading" />
      </AppFrame>
    );
  }

  // Codex follow-up on #535: a persistent `/api/roasts` failure leaves
  // `history.data` undefined, so `staleRun` resolves to `null` below — never
  // let that fall through to the bare form. The stale-session SOURCE is
  // UNKNOWN here, not "no stale session"; this is the second source on this
  // route earning the same "unknown must never look like a clean idle form"
  // treatment `health.isError` already gets above, and mirrors `/live`'s own
  // `history.isError` → `LiveHistoryUnknownView` handling (#532).
  if (history.isError) {
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
          data-testid="start-roast-history-unknown"
        >
          <h2 className="text-lg font-bold uppercase tracking-wide">
            Can&apos;t verify roast history
          </h2>
          <p className="text-sm text-muted-foreground">
            This page could not reach the agent to check for a stale, unfinished
            session from an earlier process. This does not mean none exists — if
            the roaster might be hot, verify its status before starting a new
            roast. Reload to try again.
          </p>
          <a
            href="/start"
            data-testid="start-roast-history-unknown-reload"
            className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Reload
          </a>
        </div>
      </AppFrame>
    );
  }

  // #523/#525 stale session: a run row is open (`outcome: null`) that this
  // process's health snapshot does not recognise as active — never a form
  // here either, since starting a new roast on top of an unresolved stranded
  // run compounds the confusion the 12 Jul incident already caused. This
  // state is inherently multi-process (see the module doc): `staleRun`'s own
  // derivation already excludes the case where THIS row is what health
  // recognises as active, so the copy below never needs a conditional
  // "if this is actually yours" branch — it isn't, by construction. Offers
  // BOTH the `/live` recovery-flow link (for the DIFFERENT run health may
  // recognise, if any) and the explicit #525 clear/end action (self-contained
  // here — the #535 follow-up ruled a `/live` handoff broken for the clear
  // itself, since its dashboard only mounts for a health-RECOGNISED run).
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
            A run from an earlier session is still open, but this is not the
            roast this page currently recognises as active. If the roaster
            might still be hot, verify the hardware directly before starting a
            new one — a software check cannot rule that out on its own.
          </p>
          <Link
            to="/live"
            data-testid="start-roast-stale-session-link"
            className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
          >
            Open live view
          </Link>
          {/* #525 P2 (PR #548 round-1 Codex): key on staleRun.id so a
              staleRun IDENTITY change (e.g. clearing the newest of two
              stranded rows exposes the older one) forces a fresh mount —
              without this, the confirm/reason/success state from the JUST-
              CLEARED run would carry over onto the NEW runId. */}
          <ClearStaleSessionAction key={staleRun.id} runId={staleRun.id} />
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
