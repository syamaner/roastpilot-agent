/**
 * Single live-roast home at `/live` (#423, D81, updated #523).
 *
 * `/live` is the state of the roaster, always — NEVER a form (#523 IA). Three
 * server-state-driven states (all phase comes from the server):
 *
 * 1. Loading   — holds until health AND history (the idle path's persistent
 *    fallback) each produce a GENUINELY FRESH read, so nothing flashes before
 *    the active-run status — and the last-completed summary it falls back
 *    to — are known. Also holds while a just-finished run's terminal-outcome
 *    fetch is in flight — including the FIRST COMMITTED RENDER of the
 *    active→null transition, before the effect that starts that fetch has
 *    even run — so an OLDER completed run's summary never flashes for even
 *    one frame before the just-finished run's own gate resolves (#523 Codex
 *    follow-up on #532, round 2: the naive state-only hold missed exactly
 *    that first frame, since `useEffect` runs post-paint).
 * 2. Active run — the full live dashboard (DashboardPage). A reload on this
 *    URL re-hydrates from the server snapshot + SSE — the reload-safe guarantee.
 * 3. No active run — the last completed run's summary (`LiveFinishedView`),
 *    PERSISTENT across reload: sourced from the history API
 *    (`GET /api/roasts`, newest-first), not session state. The just-finished
 *    run from THIS session (`stickyCompletedRunId`) is preferred while set —
 *    history can lag the terminal write by a beat — but a reload always falls
 *    through to the same history-derived id, so the summary survives it
 *    (unlike the pre-#523 session-only sticky). Before trusting a HISTORY-
 *    derived id specifically, its detail snapshot is fetched fresh
 *    (`staleTime: 0`, reusing `fetchTerminalOutcome`) and the render holds
 *    until that resolves — otherwise `LiveFinishedView` could mount against a
 *    STALE cached `roastKeys.detail(id)` left over from an earlier same-
 *    session dashboard view of that same run (#523 Codex follow-up on #532,
 *    round 2). The session-sticky path already fetches fresh by construction
 *    and needs no separate gate. If the operator has never completed a
 *    roast, `LiveNoRoastsView` shows a neutral "no roasts yet" state — still
 *    not a form — with a link to `/start`. A persistent history read failure
 *    gets its OWN neutral state (`LiveHistoryUnknownView`) — never
 *    `LiveNoRoastsView`, which would otherwise assert a false "this roaster
 *    has never completed a roast" on a network/server error.
 *
 * `/start` (StartRoastView.tsx) is the ONLY start-form surface under the
 * #523 IA; `/live` never renders one.
 *
 * INVARIANTS: active-run presence comes from the SERVER's `/health` snapshot
 * (`active_run_id`) — never inferred client-side (D8). Phase is not read here;
 * DashboardPage owns phase via SSE + snapshot. Operator actions and MCP access
 * live entirely in DashboardPage.
 *
 * IMPOSTOR-PROCESS DEFENCE (#516, follow-up to the #513 port-impostor
 * incident — docs/recent-fixes.md, 12 Jul): a second process wildcard-bound
 * to the same port can answer `/health` with a plausible-looking idle body
 * while the real server is elsewhere. `StartRoastView` captures the
 * `instance_id` from the 201 response and passes it forward as router
 * navigation STATE (`LiveNavigationState`, below) — deliberately NOT a query
 * param or anything persisted, since it only means anything for the ONE
 * navigation immediately following a start, and correctly disappears on a
 * reload (a reload has no "prior start" to compare against). If the fresh
 * health read on arrival here reports a DIFFERENT `instance_id` (or NO
 * `instance_id` at all — round-2 Codex fold: the process that genuinely
 * accepted the start always includes the field, since this same commit
 * guarantees it, so an absent field while armed is itself impostor
 * evidence, not something to shrug off), that is surfaced as its own
 * distinct message on `LiveStatusUnknownView` — never silently ignored,
 * and never conflated with the generic "can't confirm" case a
 * `health.isError` would produce.
 *
 * ONE-SHOT DISARM (round-2 Codex fold): the check is armed only until the
 * FIRST verdict — a match disarms it PERMANENTLY (latched in a ref, plus a
 * `replace` navigation that strips `expectedInstanceId` out of router state
 * so even a remount can't re-arm it). Without this, `location.state`
 * persists for the whole history entry's lifetime, so a LEGITIMATE later
 * restart (a new process id while an active/recovery run genuinely exists)
 * would false-alarm this check indefinitely — blocking the dashboard
 * exactly when the operator needs it most. Restart handling belongs to the
 * fresh-health-gate / recovery flow already in place, not to this one-shot
 * start-confirmation check.
 *
 * Passive health consumers elsewhere (NavBar, DashboardPage) never read or
 * compare `instance_id` — only this one-shot comparison does, so a normal
 * server RESTART (once this check has disarmed) never false-alarms them.
 */

import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";

import { AppFrame, LiveCurve } from "@/components/shared";
import {
  roastKeys,
  useFreshHealthGate,
  useFreshHistoryGate,
  useRoast,
  useTelemetry,
} from "@/hooks/queries";
import { api } from "@/lib/api";
import { DashboardPage } from "@/pages/dashboard/DashboardPage";
import { headlineStats, toCurveMarkers, toCurvePoints } from "@/pages/detail/traceModel";

/**
 * Router navigation state `StartRoastView` hands to `/live` on a successful
 * start (#516) — see the module doc's "IMPOSTOR-PROCESS DEFENCE" note.
 */
export interface LiveNavigationState {
  /** The `instance_id` observed on the start-roast 201 response. */
  expectedInstanceId: string;
}

/**
 * Fetch the terminal RoastDetail snapshot for a just-ended run and return its
 * outcome. Always re-fetches (staleTime: 0) so the cache reflects the server's
 * FINAL state, not the stale in-progress snapshot that had `outcome: null` while
 * the run was live (P2-3). Populates the TanStack Query cache so a subsequent
 * `useRoast(runId)` in LiveFinishedView resolves synchronously from the fresh data.
 * Returns `null` on any network error (safe no-op: the session-sticky summary is
 * silently suppressed for this transition — the persistent history-derived
 * fallback, #523, still applies on the next render/reload).
 */
async function fetchTerminalOutcome(
  queryClient: QueryClient,
  runId: string,
): Promise<string | null> {
  try {
    const detail = await queryClient.fetchQuery({
      queryKey: roastKeys.detail(runId),
      queryFn: () => api.roast(runId),
      staleTime: 0,
    });
    return detail.outcome ?? null;
  } catch {
    return null;
  }
}

/**
 * The history-detail-verification effect's own fetch outcome (#523 Codex
 * follow-up on #532, round 3) — DISTINCT from `fetchTerminalOutcome`'s
 * collapsed `string | null`, which cannot tell "genuinely succeeded with a
 * null outcome" apart from "the fetch itself failed". That collapse is fine
 * for the session-sticky path (a failure there safely no-ops, #523) but not
 * here: whether to fail OPEN or fail CLOSED depends on whether a cache entry
 * existed BEFORE this fetch, which requires knowing success vs failure
 * unambiguously, plus that pre-fetch cache state.
 */
interface HistoryDetailFetchResult {
  /** Whether the network fetch itself succeeded (not the run's outcome). */
  succeeded: boolean;
  /** Whether `roastKeys.detail(runId)` already had a cache entry BEFORE this
   *  fetch ran — the signal for whether failing open would risk rendering
   *  stale data (a cache entry existed) vs. having nothing to mislead with
   *  (no cache entry, `LiveFinishedView`'s own `useRoast` gets a clean shot). */
  hadCachedDetailBeforeFetch: boolean;
}

async function fetchHistoryRunDetail(
  queryClient: QueryClient,
  runId: string,
): Promise<HistoryDetailFetchResult> {
  const hadCachedDetailBeforeFetch =
    queryClient.getQueryData(roastKeys.detail(runId)) !== undefined;
  try {
    await queryClient.fetchQuery({
      queryKey: roastKeys.detail(runId),
      queryFn: () => api.roast(runId),
      staleTime: 0,
    });
    return { succeeded: true, hadCachedDetailBeforeFetch };
  } catch {
    return { succeeded: false, hadCachedDetailBeforeFetch };
  }
}

export function LivePage(): React.JSX.Element {
  // #513 Codex follow-up: `useHealth()`'s shared 30s `staleTime` means a
  // remount within that window would render a CACHED `active_run_id` with
  // `isSuccess: true` and no network request at all — a second process/tab
  // could have started a run in that window, and the summary/no-roasts view
  // would flash on stale "idle" data before the gate below ever fires. Gate on
  // `useFreshHealthGate` instead of `useHealth` here (see its doc for the
  // full empirically-verified rationale); non-gating consumers elsewhere
  // (header/nav) keep using plain `useHealth` unchanged.
  const health = useFreshHealthGate();
  const activeRunId = health.data?.active_run_id ?? null;
  const queryClient = useQueryClient();

  // #516: the instance_id StartRoastView observed on the 201 response, if
  // this render is the direct result of a start-roast navigation. `state` is
  // `unknown` by React Router's own typing — narrowed defensively rather than
  // cast, since it can be anything (a browser back/forward restore, a direct
  // link, or absent on a reload) and must never be trusted uncritically.
  const location = useLocation();
  const navigate = useNavigate();
  const expectedInstanceId =
    location.state !== null &&
    typeof location.state === "object" &&
    "expectedInstanceId" in location.state &&
    typeof (location.state as LiveNavigationState).expectedInstanceId === "string"
      ? (location.state as LiveNavigationState).expectedInstanceId
      : null;

  // ONE-SHOT DISARM (round-2 Codex fold): `location.state` persists for the
  // WHOLE lifetime of this history entry, so without a latch the check would
  // stay armed indefinitely — a LEGITIMATE later restart (a new process id
  // while an active/recovery run genuinely exists) would then false-alarm
  // this check exactly when the operator needs the dashboard most. `verified`
  // becomes `true` PERMANENTLY the first time a fresh health read confirms a
  // match, and the mismatch check below is skipped forever after — restart
  // handling belongs to the fresh-health-gate/recovery flow, not here.
  const [verified, setVerified] = useState(expectedInstanceId === null);
  const armed = expectedInstanceId !== null && !verified;

  // A verdict is only meaningful once health has produced a genuinely fresh
  // read (health.data could still be the PRE-fetch cached snapshot from an
  // in-flight forced refetch) — the `!health.isFresh` hold below already
  // covers that ordering for the RENDER path, but this effect must apply the
  // same guard itself since effects run independently of the render gates.
  useEffect(() => {
    if (!armed || !health.isFresh || health.data === undefined) return;
    if (health.data.instance_id === expectedInstanceId) {
      setVerified(true);
      // Strip expectedInstanceId out of router state via a REPLACE
      // navigation — belt-and-braces on top of the `verified` latch: even a
      // remount of this component (which would re-derive `expectedInstanceId`
      // fresh from `location.state`) can no longer re-arm the check, since
      // the state itself no longer carries the field.
      navigate(location.pathname, { replace: true, state: null });
    }
    // health.data is intentionally read fresh each run rather than added as
    // a dependency object (it's a new reference every fetch) — instance_id
    // and isFresh are the only fields this effect's decision depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [armed, health.isFresh, health.data?.instance_id, expectedInstanceId, navigate, location.pathname]);

  // round-2 Codex fold: FAIL CLOSED on an ABSENT instance_id while armed —
  // not just a DIFFERENT one. The process that genuinely accepted the start
  // always includes instance_id in its /health response (this same commit
  // guarantees the field on every code path), so an armed check seeing NO
  // field at all is itself impostor evidence (an older-code process that
  // predates this feature) — never a benign "field not implemented yet" case
  // to shrug past.
  const instanceMismatch =
    armed && health.isFresh && health.data?.instance_id !== expectedInstanceId;

  // Persistent idle fallback (#523): the roast history, newest-first
  // (`GET /api/roasts` — store.py orders `started_at_utc DESC`). #523 Codex
  // follow-up on #532: history is now an AUTHORITATIVE source for this idle
  // state (the persistent last-completed-run fallback), so it earns the same
  // `useFreshHealthGate`-class treatment health already has — gate on
  // `useFreshHistoryGate`, not plain `useHistory`, so a within-staleTime
  // remount can't render a cached (possibly empty) history list as proof the
  // roaster has never completed a roast.
  const history = useFreshHistoryGate();
  const lastCompletedRunId =
    history.data?.runs.find((run) => run.outcome === "completed")?.id ?? null;

  // Track the most recent non-null active_run_id across renders. When the id
  // transitions non-null → null we fetch the terminal run snapshot so the
  // session-sticky summary can show IMMEDIATELY on this render, before the
  // history list has necessarily caught up (it can lag the terminal write by
  // a beat). `stickyCompletedRunId` is a same-session convenience layered on
  // top of `lastCompletedRunId`; a reload always falls back to the latter, so
  // the summary survives it (unlike the pre-#523 session-only sticky).
  //   - `completed`: latch as stickyCompletedRunId → LiveFinishedView.
  //   - anything else (faulted, aborted): do NOT latch — the fault flow in
  //     DashboardPage owns that path (P2-4 / P2-3 / #423).
  //
  // Fetching with staleTime:0 ensures we get the SERVER'S TERMINAL snapshot, not
  // the stale in-progress cache that had outcome:null while the run was live (P2-3).
  //
  // #523 Codex follow-up on #532 (transition-flash): while THIS fetch is in
  // flight, `stickyCompletedRunId` is still `null` — without a guard, the
  // render in that window would fall through to `lastCompletedRunId`, which
  // (if an OLDER completed run exists in history) flashes that older run's
  // summary before swapping to the just-finished one a moment later.
  // `terminalFetchPending` holds the idle branch through that window, so the
  // just-finished run's own gate always resolves before ANY summary renders.
  const prevRunIdRef = useRef<string | null>(null);
  const [stickyCompletedRunId, setStickyCompletedRunId] = useState<string | null>(null);
  const [terminalFetchPending, setTerminalFetchPending] = useState(false);
  // #526: unmount guard for the fetchTerminalOutcome `.then()` below — the
  // same class as the #514 confirm-loop BLOCKER (docs/recent-fixes.md: "Any
  // confirm/retry loop in a component MUST guard against unmount"). Plain
  // `useRef(true)` set `false` in a cleanup, mirroring the repo convention
  // (no library) — checked after the awaited fetch, before either state
  // write below.
  //
  // NO REGRESSION TEST for this specific guard, deliberately (#526 finding,
  // recorded on the issue + docs/recent-fixes.md): empirically verified
  // (five isolated probe components against this exact React 18 + jsdom +
  // testing-library stack, plain unmount / StrictMode / act()-wrapped
  // resolve) that a `useState` setter called from an orphaned `.then()`
  // after unmount produces NO observable signal here — not the classic
  // "state update on an unmounted component" warning (removed for
  // hooks-based updates in React 18), not even the more general "not
  // wrapped in act()" warning. A component-local `useState` write on an
  // unmounted fiber is an inert no-op with zero cross-instance effect,
  // unlike the ORIGINAL #514 hazard (a `queryClient.setQueryData` write —
  // genuinely SHARED state a remount's fresh loop could race). A
  // console-error-spy test, or an unmount-then-remount cross-instance test,
  // both pass IDENTICALLY with or without this guard — confirmed by
  // reverting it and rerunning. Ship the guard anyway: it is the repo's
  // safe-by-default convention, and its value is FUTURE-PROOFING — the day
  // this `.then()` grows a `queryClient.setQueryData` or any other
  // shared-state write (exactly how the original #514 blocker arose), the
  // guard is already there. Do not add a test here that merely re-asserts
  // "no console output" — it would prove nothing; see the doc entry above
  // for what a non-vacuous test for THIS class actually requires (a shared-
  // state observable, not a local one).
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (activeRunId !== null) {
      // A run is active — remember it.
      prevRunIdRef.current = activeRunId;
    } else if (prevRunIdRef.current !== null) {
      // Transition: active_run_id was non-null, is now null. Fetch the terminal
      // snapshot to gate on outcome (P2-3 + P2-4). fetchQuery populates the cache
      // so LiveFinishedView's useRoast sees the terminal data immediately on mount.
      const finishedId = prevRunIdRef.current;
      prevRunIdRef.current = null;
      setTerminalFetchPending(true);
      void fetchTerminalOutcome(queryClient, finishedId).then((outcome) => {
        // #526: bail before any state write if this component has since
        // unmounted — the query cache write inside fetchTerminalOutcome
        // itself already happened (harmless/shared — TanStack Query's cache
        // is meant to be written from anywhere), only the LOCAL state
        // setters below are guarded. See the guard's own doc above for why
        // this specific case has no regression test.
        if (!mountedRef.current) return;
        if (outcome === "completed") {
          setStickyCompletedRunId(finishedId);
        }
        // Non-completed outcomes (faulted, aborted, null) don't show the summary.
        // DashboardPage retains the faulted run via stickyFaultedRunId for ack.
        setTerminalFetchPending(false);
      });
      // The history list is also invalidated on `run_completed` /
      // fault-acknowledge (see queries.ts) so `lastCompletedRunId` picks up
      // the new run independently of this session-sticky path.
    }
  }, [activeRunId, queryClient]);

  // #523 Codex follow-up on #532, round 2: the HISTORY-derived id
  // (`lastCompletedRunId`) is not automatically fresh the way the session-
  // sticky path is. `fetchTerminalOutcome`'s `staleTime: 0` fetch only runs
  // for THIS session's own just-finished run; a history-derived id (e.g. on
  // a reload, or when the operator lands on /live idle without having just
  // finished a roast this session) would otherwise hand `LiveFinishedView` a
  // runId whose `roastKeys.detail(id)` cache entry might be STALE — e.g. a
  // mid-roast snapshot cached earlier in the same browser session from an
  // open dashboard/detail view of that same run, with `outcome: null` and
  // partial stats. Mirror the sticky path: before trusting a history-derived
  // id, fetch its detail fresh (reusing `fetchTerminalOutcome`, which already
  // populates the cache with the server's terminal snapshot) and hold the
  // fallback render until that resolves. Runs only when relevant — i.e. NOT
  // while the session-sticky path is already supplying the summary (it needs
  // no separate verification) — and re-fires whenever `lastCompletedRunId`
  // itself changes (a new run completing elsewhere invalidates history, see
  // above, which can surface a different id here).
  const [freshHistoryRunId, setFreshHistoryRunId] = useState<string | null>(null);
  const [historyDetailPending, setHistoryDetailPending] = useState(false);
  // #523 Codex follow-up on #532, round 3: refines the fail-open design
  // below. Set when the verification fetch FAILED for an id that already
  // had a cached detail entry (so failing open would risk rendering STALE
  // data under a "verified" flag) — routes to `LiveHistoryUnknownView`
  // instead of the summary. Cleared on any successful verification.
  const [historyDetailFetchFailedWithStaleCache, setHistoryDetailFetchFailedWithStaleCache] =
    useState(false);

  useEffect(() => {
    if (
      stickyCompletedRunId !== null ||
      lastCompletedRunId === null ||
      // A run just transitioned active→null and its OWN terminal-outcome
      // fetch is still pending (or hasn't even started this render, per the
      // synchronous `prevRunIdRef` check above) — the transition hold
      // already blocks any fallback render regardless, so verifying an
      // older history-derived id's freshness here would be redundant work
      // AND would race the sticky path's own `fetchTerminalOutcome` call for
      // the SAME shared query client / underlying `api.roast`. Skip; this
      // effect re-runs once the transition settles (`terminalFetchPending`
      // is a dep below).
      prevRunIdRef.current !== null ||
      terminalFetchPending
    ) {
      return;
    }
    if (freshHistoryRunId === lastCompletedRunId) {
      // Already verified fresh for this exact id — no need to re-fetch on
      // every render (this effect's dep array still re-runs it if the id
      // itself changes, which is the only case that needs a new fetch).
      return;
    }
    let cancelled = false;
    setHistoryDetailPending(true);
    // Reset the round-3 failure flag as soon as a NEW attempt starts (rather
    // than only on that attempt's own success) — otherwise a stale `true`
    // from a PREVIOUS (now-superseded) id would incorrectly route this
    // brand-new id to the history-error state before its own fetch has even
    // resolved, since the render-time checks below only gate on
    // `historyDetailPending`/`freshHistoryRunId`, not on which id the flag
    // was set for.
    setHistoryDetailFetchFailedWithStaleCache(false);
    // #523 Codex follow-up on #532, round 3: refreshing DETAIL alone is not
    // enough — `LiveFinishedView`'s headline stats and mini curve come from
    // `useTelemetry(runId, 1)` / `useTelemetry(runId, 5)`, which share the
    // app's default 30s `staleTime`. A same-session telemetry cache entry
    // for this exact run id (e.g. from an earlier open dashboard/detail view
    // in this session) can still be within that window, so a freshly-
    // verified DETAIL could render alongside a STALE curve/stats. Invalidate
    // BOTH telemetry variants for this id alongside the detail fetch — a
    // prefix match on `["roasts", runId, "telemetry"]` catches both the
    // downsample=1 (stats) and downsample=5 (curve) keys in one call.
    void Promise.all([
      fetchHistoryRunDetail(queryClient, lastCompletedRunId),
      queryClient.invalidateQueries({
        queryKey: ["roasts", lastCompletedRunId, "telemetry"],
      }),
    ]).then(([detailResult]) => {
      if (cancelled) return;
      // #523 Codex follow-up on #532, round 3: fail OPEN only when there is
      // NOTHING TO MISLEAD WITH — no cached detail existed before this
      // fetch, so `LiveFinishedView`'s own `useRoast` starts from a clean
      // slate and gets an independent shot at succeeding (mirrors
      // `fetchTerminalOutcome`'s "never block the session-sticky path
      // indefinitely on one network blip" design). But if a CACHED detail
      // ALREADY existed and this forced refresh FAILED, failing open would
      // render that potentially-stale data under a "verified" flag — worse
      // than the neutral history-error state, since it looks trustworthy
      // when it might not be. Route that specific case to
      // `LiveHistoryUnknownView` instead.
      if (!detailResult.succeeded && detailResult.hadCachedDetailBeforeFetch) {
        setHistoryDetailFetchFailedWithStaleCache(true);
        setHistoryDetailPending(false);
        return;
      }
      setHistoryDetailFetchFailedWithStaleCache(false);
      setFreshHistoryRunId(lastCompletedRunId);
      setHistoryDetailPending(false);
    });
    return () => {
      cancelled = true;
    };
  }, [stickyCompletedRunId, lastCompletedRunId, freshHistoryRunId, terminalFetchPending, queryClient]);

  // Health error (#513 medium): active-run status is UNKNOWN — never fall
  // through to a state that implies "no run" (a run could genuinely be active
  // and the operator would have no path to the dashboard/e-stop, the exact
  // hazard this PR fixes elsewhere). `useHealth`'s default `retry: 1` already
  // rides out a single blip before `isError` is true, so this is a persistent
  // failure, not noise. Show a neutral "can't confirm" state instead.
  if (health.isError) {
    return <LiveStatusUnknownView />;
  }

  // Hold until health has produced a GENUINELY FRESH read (same pattern as
  // the old HomeGate hold, extended #513 Codex follow-up). `health.isFresh`
  // (`useFreshHealthGate`) is false both while genuinely pending AND while
  // `isSuccess` is true only from stale cache with a forced refetch still in
  // flight — a within-staleTime remount would otherwise let a cached "idle"
  // snapshot render as proof no run is active, when another tab/process
  // could have started one in the last 30s. The `health.isError` branch
  // above already handles the persistent-failure case (which `isFresh`
  // treats as settled, not pending), so this check is a single condition.
  if (!health.isFresh) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  // #516 impostor-process defence: while ARMED (an expectedInstanceId is
  // present and not yet verified), the fresh health read reports a
  // DIFFERENT instance_id — or NO instance_id at all (round-2 fold: fail
  // CLOSED, not just on a different value) — than the one StartRoastView
  // observed on the 201 that led here. A different/absent server process
  // answered than the one that just accepted the start (the #513
  // port-impostor signature). This takes PRECEDENCE over the active-run
  // branch below: if the process itself is wrong, its active_run_id (even
  // if non-null) cannot be trusted either. Renders the SAME
  // LiveStatusUnknownView component as the generic health-error case, with
  // a distinct message — no new page state, per design (the status-unknown
  // surface already exists for "can't trust what health just told us").
  // Once `verified` latches true (see the module doc's ONE-SHOT DISARM
  // note), `armed` is permanently false and this branch is never reached
  // again for this history entry, however health subsequently changes.
  if (instanceMismatch) {
    return <LiveStatusUnknownView variant="instance-mismatch" />;
  }

  // Active run: the full live dashboard.
  if (activeRunId !== null) {
    return <DashboardPage />;
  }

  // No active run: never a form (#523). Hold while a terminal-outcome fetch
  // for the just-finished run is in flight — see the transition-flash note
  // above — so an older completed run's summary never renders as a flash
  // before the just-finished run's own gate resolves. This check does NOT
  // depend on `stickyCompletedRunId` being null (unlike the history hold
  // below): the fetch itself, not just its outcome, is what must finish
  // before any fallback is allowed to render.
  //
  // #523 Codex follow-up on #532, round 2: `terminalFetchPending` ALONE
  // arrives one frame late — it's set inside the `useEffect` above, which
  // runs AFTER this component's first commit for the active→null transition,
  // so that first painted frame would still fall through to the fallback
  // below before the effect ever fires. `prevRunIdRef.current` is read
  // SYNCHRONOUSLY during render and is not yet cleared on that exact frame
  // (the effect that clears it to `null` hasn't run yet either) — checking
  // it here closes the gap the state-only hold missed. The ref covers the
  // one pre-effect frame; the state covers the whole fetch duration after.
  if (prevRunIdRef.current !== null || terminalFetchPending) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  // History error (#523 Codex follow-up on #532): never render
  // `LiveNoRoastsView` on a history read failure — that would assert a false
  // "this roaster has never completed a roast" on what might just be a
  // network/server blip. A session-sticky id (this session's own just-
  // finished run) does not depend on history at all and may still render.
  if (history.isError && stickyCompletedRunId === null) {
    return <LiveHistoryUnknownView />;
  }

  // No active run: never a form (#523). Hold briefly for history to settle
  // too, so a reload doesn't flash "no roasts yet" before the persistent
  // fallback has had a chance to resolve — the session-sticky id (if any) is
  // already known synchronously and doesn't need this hold. `history.isFresh`
  // (not `isPending`) closes the #532 staleness gap: a within-staleTime
  // remount must not render a CACHED history list — possibly empty — as
  // proof no roast has ever completed.
  if (stickyCompletedRunId === null && !history.isFresh) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  // #523 Codex follow-up on #532, round 2: before trusting a HISTORY-derived
  // id (never the session-sticky one, which is already fetched fresh by
  // construction — see the effect above), hold while its detail snapshot is
  // being fetched fresh, and require it to have actually SETTLED for THIS
  // exact id — either verified fresh (`freshHistoryRunId === lastCompletedRunId`)
  // OR failed-with-a-stale-cache (`historyDetailFetchFailedWithStaleCache`,
  // #523 round 3 — that failure is itself a settled, terminal state for this
  // id; it must fall through to the round-3 branch below, not hold forever
  // waiting for `freshHistoryRunId` to update, which it deliberately never
  // does on that path). Composes with the transition hold above:
  // `lastCompletedRunId` and these two flags only matter once the process has
  // settled past the active-run/transition/history-error/history-staleness
  // gates already checked.
  if (
    stickyCompletedRunId === null &&
    lastCompletedRunId !== null &&
    historyDetailPending
  ) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }
  if (
    stickyCompletedRunId === null &&
    lastCompletedRunId !== null &&
    freshHistoryRunId !== lastCompletedRunId &&
    !historyDetailFetchFailedWithStaleCache
  ) {
    return (
      <AppFrame>
        <div data-testid="live-page-loading" />
      </AppFrame>
    );
  }

  // #523 Codex follow-up on #532, round 3: the verification fetch above
  // SETTLED (we're past the hold) but failed AND a stale cached detail
  // already existed for this id — failing open here would render that
  // potentially-stale data under a "verified" flag, which is worse than the
  // neutral history-error state (it looks trustworthy when it might not be).
  // Route to the same `LiveHistoryUnknownView` the history-list read-failure
  // case uses. Does NOT apply to the session-sticky path (that state is only
  // ever set by the history-derived effect) or when no cache existed before
  // the fetch (that case fails open via `freshHistoryRunId` above instead).
  if (stickyCompletedRunId === null && historyDetailFetchFailedWithStaleCache) {
    return <LiveHistoryUnknownView />;
  }

  const summaryRunId = stickyCompletedRunId ?? lastCompletedRunId;
  if (summaryRunId !== null) {
    return <LiveFinishedView runId={summaryRunId} />;
  }

  // No active run, and no completed run exists (ever) — a neutral, still-not-
  // a-form state pointing to /start, the only start-form surface (#523).
  return <LiveNoRoastsView />;
}

// --- LiveStatusUnknownView: shown at /live when /health persistently errors. ---

/**
 * Neutral "can't confirm roaster status" state (#513 medium). Shown when
 * `useHealth()` errors persistently (after its own retry budget) — active-run
 * status is genuinely UNKNOWN, so this must never fall through to a state
 * that implies no run is active (the last-completed summary or the no-roasts
 * view, #523): a run could be active and heating, and the operator would have
 * no path to the dashboard/emergency stop. `useHealth` is refetched on-focus
 * disabled but still observed here, so a manual reload or the browser's own
 * retry is the recovery path; this view offers an explicit reload link too.
 *
 * `variant="instance-mismatch"` (#516) reuses this same component — no new
 * page state — for the impostor-process case: health resolved successfully,
 * but the fresh read's `instance_id` does not match the one observed on the
 * start-roast 201. Carries a DISTINCT message from the generic "can't reach
 * the agent" copy, per the issue's explicit requirement.
 */
function LiveStatusUnknownView({
  variant = "health-error",
}: {
  variant?: "health-error" | "instance-mismatch";
} = {}): React.JSX.Element {
  const isMismatch = variant === "instance-mismatch";
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
        data-testid="live-status-unknown"
        data-variant={variant}
      >
        <h2 className="text-lg font-bold uppercase tracking-wide">Can&apos;t confirm roaster status</h2>
        {isMismatch ? (
          <p className="text-sm text-muted-foreground" data-testid="live-status-unknown-message">
            Answers are coming from a different server process than the one that
            accepted this roast start. Reload before trusting anything shown here —
            if a roast is genuinely running, another process may be unreachable.
          </p>
        ) : (
          <p className="text-sm text-muted-foreground" data-testid="live-status-unknown-message">
            This page could not reach the agent to check whether a roast is active. If
            one is running, it is still live and heating — reload to reconnect before
            assuming it is safe to start a new one.
          </p>
        )}
        <a
          href="/live"
          data-testid="live-status-unknown-reload"
          className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Reload
        </a>
      </div>
    </AppFrame>
  );
}

// --- LiveHistoryUnknownView: shown at /live when history persistently errors. ---

/**
 * Neutral "can't load roast history" state (#523 Codex follow-up on #532).
 * Shown when `useFreshHistoryGate()` errors persistently (after its own
 * retry budget) and no session-sticky summary is available to render
 * instead. Distinct from `LiveStatusUnknownView`: THIS run's active-run
 * status is known (health resolved fine, or `activeRunId` is null) — what's
 * unknown is whether a completed roast exists to summarise. Falling through
 * to `LiveNoRoastsView` here would assert a false "this roaster has never
 * completed a roast" on what might be a transient network/server error, the
 * exact isSuccess≠current hazard class `useFreshHealthGate` already guards
 * against for health. A manual reload is the recovery path.
 */
function LiveHistoryUnknownView(): React.JSX.Element {
  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          History unavailable
        </span>
      }
    >
      <div
        className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-roast-fault/50 bg-roast-fault/10 p-8 text-center"
        data-testid="live-history-unknown"
      >
        <h2 className="text-lg font-bold uppercase tracking-wide">Can&apos;t load roast history</h2>
        <p className="text-sm text-muted-foreground">
          This page could not reach the agent to check for a completed roast to
          summarise. This does not mean none exists — reload to try again.
        </p>
        <a
          href="/live"
          data-testid="live-history-unknown-reload"
          className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Reload
        </a>
      </div>
    </AppFrame>
  );
}

// --- LiveFinishedView: persistent last-completed-run summary at /live. ---

interface LiveFinishedViewProps {
  /** The last completed run's id — either this session's just-finished run
   *  (session-sticky, immediate) or the history-derived fallback (#523,
   *  persistent — survives reload). Either way it is a genuine `completed`
   *  outcome; LivePage never passes a faulted/aborted run here. */
  runId: string;
}

/**
 * Persistent "roaster's last completed roast" summary shown at `/live` when no
 * run is active (#523) — survives reload, sourced from the history API, not
 * session state. Immediately after a roast ends in the CURRENT session, the
 * RoastDetail snapshot was already fetched (with staleTime:0) by LivePage's
 * `fetchTerminalOutcome` call before this view mounts, so `useRoast` resolves
 * synchronously from the cache — the terminal snapshot with the final outcome
 * (P2-3). On a reload, or once `lastCompletedRunId` takes over, `useRoast`
 * fetches normally.
 *
 * Headline stats (drop temp / dev% / total time) come from the FULL-RESOLUTION
 * telemetry series (`downsample=1`), guaranteeing that the drop/terminal rows are
 * included regardless of stride position (P2-2). The mini curve uses the
 * downsampled series (`downsample=5`) to keep the fetch lightweight.
 *
 * 'Start next roast' links to `/start` — the only start-form surface under the
 * #523 IA. This view itself is never a form.
 */
function LiveFinishedView({ runId }: LiveFinishedViewProps): React.JSX.Element {
  const roast = useRoast(runId);
  // Full-resolution telemetry for accurate headline stats (P2-2): downsample=1
  // ensures the drop/terminal rows are included regardless of stride position.
  const telemetryFull = useTelemetry(runId, 1);
  // Downsampled telemetry for the mini curve only — lightweight fetch for display.
  const telemetryCurve = useTelemetry(runId, 5);

  const stats = headlineStats(undefined, telemetryFull.data);
  const points = toCurvePoints(telemetryCurve.data);
  const markers = toCurveMarkers(undefined, telemetryCurve.data);

  const beanOrigin = roast.data?.profile.bean_origin ?? null;
  const outcome = roast.data?.outcome ?? null;

  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Roast complete
        </span>
      }
    >
      <div className="mx-auto max-w-3xl" data-testid="live-finished-view">
        {/* Run identity */}
        <header className="mb-6">
          <h2 className="font-mono text-2xl text-foreground">
            {beanOrigin ?? "Roast complete"}
          </h2>
          {outcome !== null && (
            <p
              className="mt-1 text-sm capitalize text-muted-foreground"
              data-testid="live-finished-outcome"
            >
              {outcome}
            </p>
          )}
        </header>

        {/* Headline stats */}
        <div
          className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4"
          data-testid="live-finished-stats"
        >
          <StatTile
            testId="stat-drop-temp"
            label="Drop temp"
            value={stats.dropTempC !== null ? `${Math.round(stats.dropTempC)} °C` : "—"}
          />
          <StatTile
            testId="stat-dev-percent"
            label="Dev %"
            value={
              stats.developmentPercent !== null
                ? `${stats.developmentPercent.toFixed(1)} %`
                : "—"
            }
          />
          <StatTile
            testId="stat-total-time"
            label="Total time"
            value={stats.totalSeconds !== null ? formatDuration(stats.totalSeconds) : "—"}
          />
          <StatTile
            testId="stat-weight-loss"
            label="Weight loss"
            value={
              roast.data?.weight_loss_percent != null
                ? `${roast.data.weight_loss_percent.toFixed(1)} %`
                : "—"
            }
          />
        </div>

        {/* Mini curve — only when we have data */}
        {points.length > 0 && (
          <div className="mb-6 rounded-lg border border-border bg-card p-4" data-testid="live-finished-curve">
            <LiveCurve points={points} markers={markers} height={180} />
          </div>
        )}

        {/* Actions */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Link
            to={`/roasts/${runId}`}
            className="flex-1 rounded-md border border-border bg-card px-5 py-3 text-center text-sm font-medium text-foreground transition-colors hover:bg-accent/40"
            data-testid="live-finished-view-detail"
          >
            View full detail
          </Link>
          <Link
            to="/start"
            className="flex-1 rounded-md bg-primary px-5 py-3 text-center text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
            data-testid="live-finished-start-next"
          >
            Start next roast
          </Link>
        </div>
      </div>
    </AppFrame>
  );
}

/** A single headline-stat card. */
function StatTile({
  label,
  value,
  testId,
}: {
  label: string;
  value: string;
  testId: string;
}): React.JSX.Element {
  return (
    <div
      className="flex flex-col gap-1 rounded-lg border border-border bg-card px-4 py-3"
      data-testid={testId}
    >
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="font-mono text-xl font-semibold text-foreground">{value}</span>
    </div>
  );
}

/** Format total seconds as `M:SS`. */
function formatDuration(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = Math.round(totalSeconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

// --- LiveNoRoastsView: shown at /live when there is no active run and no
// completed run has EVER been recorded (a fresh install, or every past run
// faulted/aborted). Never a form (#523) — a neutral state pointing to /start,
// the only start-form surface under the new IA. ---

function LiveNoRoastsView(): React.JSX.Element {
  return (
    <AppFrame
      headerRight={
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          No roasts yet
        </span>
      }
    >
      <div
        className="mx-auto flex max-w-xl flex-col items-center gap-4 rounded-lg border border-border bg-card p-8 text-center"
        data-testid="live-no-roasts-view"
      >
        <h2 className="text-lg font-bold uppercase tracking-wide">No roasts yet</h2>
        <p className="text-sm text-muted-foreground">
          This roaster hasn&apos;t completed a roast. Start one to see its live status
          and, afterward, its summary here.
        </p>
        <Link
          to="/start"
          data-testid="live-no-roasts-start-link"
          className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Start a new roast
        </Link>
      </div>
    </AppFrame>
  );
}
