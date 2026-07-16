/**
 * Shared rating-save mutation (#566 merge).
 *
 * Both `RoastRating` (direct edits to the headline rating) and `RoastTastings`
 * (the primary entry point — saving a tasting also updates the headline) POST
 * through the same `api.rate` call, so the headline always re-reads from the
 * server's own persisted truth after either gesture. Page-local (not
 * `hooks/queries.ts`): this mutation is only ever driven from this page's two
 * rating/tasting widgets.
 *
 * Codex round on #568 (PRRT_kwDOSzMG_c6Rdlk6 / PRRT_kwDOSzMG_c6RdxDQ): `onSuccess`
 * previously only INVALIDATED `roastKeys.detail` and discarded `api.rate`'s own
 * returned `RoastDetail` — so a caller that closes its editor on `onSuccess`
 * (`RoastRating`) could render its read-only headline from the STALE cached
 * `rating`/`notes` for the length of the refetch round-trip (or forever, if that
 * refetch itself failed), flashing "Not yet rated." or the OLD note right after a
 * successful save. Seeding the cache directly from the mutation's own response is
 * synchronous and authoritative — no round-trip to go stale during.
 *
 * A shared `mutationKey` (scoped by run) lets every caller ask "is a rating save
 * in flight RIGHT NOW, from ANYWHERE on this page" via `useIsMutating` — see
 * `RoastRating`'s own #568 fix (PRRT_kwDOSzMG_c6RdllD): the two entry points
 * (this widget's own edit form, and `RoastTastings`' one-gesture save) must never
 * race a rating write, since `useMutation` gives each call site its own local
 * pending flag otherwise.
 *
 * Round 2 (PRRT_kwDOSzMG_c6ReetW): the direct `setQueryData` seed itself opened
 * a SEPARATE race — `useRoast`'s own background `refetchInterval` (queries.ts,
 * polls every 5s while a run is still live) can have a GET for this same
 * `roastKeys.detail` key already in flight when the mutation resolves. If that
 * GET settles AFTER the seed, its resolver overwrites the just-seeded fresh
 * rating with the pre-save snapshot it fetched before the mutation even ran —
 * silently reverting the headline. `cancelQueries` BEFORE the seed aborts any
 * in-flight GET for the key (TanStack's documented mutation/query race
 * mitigation), so no stale resolver can land after and clobber it.
 *
 * Round 3 (PRRT_kwDOSzMG_c6RfBAA / PRRT_kwDOSzMG_c6RfBAJ): the seed above was
 * itself two edges too wide, both closed here in one surgical pass so the
 * cache write only ever touches what THIS mutation actually owns:
 *
 * 1. `roastKeys.detail(runId)` is `["roasts", runId]` — a PREFIX of the
 *    sibling keys (`roastKeys.timeline`/`telemetry`/`tastings`, each
 *    `["roasts", runId, "..."]`). TanStack's query-key matching is INCLUSIVE
 *    by default, so the bare `cancelQueries({ queryKey })` above cancelled
 *    any in-flight timeline/telemetry/tastings read too — the chart/trace/
 *    tasting list could be left permanently empty (nothing re-triggers those
 *    reads on a completed, non-polling detail view). `exact: true` scopes
 *    the cancel to ONLY the detail query.
 * 2. The whole-object `setQueryData(key, detail)` replaced the ENTIRE cached
 *    `RoastDetail` with the rating endpoint's own response — which reflects
 *    the state the SERVER saw when it handled the rating POST, not
 *    necessarily the latest state of every OTHER field. A concurrent
 *    roasted-weight/charge-weight save (their own mutations write straight
 *    into this same cache entry) could have its fresh value silently rolled
 *    back to whatever `api.rate`'s response happened to carry, reverted
 *    until an unrelated remount. A FUNCTIONAL update that merges only
 *    `rating`/`notes` — the two fields this mutation actually owns — fixes
 *    the original round-1 stale-headline flash exactly as before while
 *    never touching a sibling field's fresher value.
 *
 * Round 4 (PRRT_kwDOSzMG_c6RflFx): the seed above only ever touched
 * `roastKeys.detail` — `roastKeys.history` (the `/roasts` list, its own
 * `staleTime: 30_000`) never learns a rating changed, so navigating back to
 * the history page right after a save can show the OLD star value (and any
 * rating-based filter reading it) for up to 30s. History is read-heavy and
 * not gated the way the two detail-page rating widgets are, so an
 * invalidate-and-let-it-refetch is the right weight here (unlike the
 * detail seed, there's no single in-flight mutation response to seed FROM —
 * the list's own shape isn't `RoastDetail`).
 *
 * `roastKeys.history` is `["roasts"]` — the very PREFIX `roastKeys.detail`
 * is built from, so this repeats the round-3 lesson in the other direction:
 * a bare (non-`exact`) `invalidateQueries` here would ALSO mark the just-
 * seeded detail query (and every OTHER run's detail/timeline/telemetry/
 * tastings query) stale, inviting an unnecessary refetch that could race the
 * careful merge-only seed above. `exact: true` scopes this to ONLY the
 * history list.
 */

import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { roastKeys } from "@/hooks/queries";
import type { OperatorRatingRequest, RoastDetail } from "@/lib/types";

export function ratingMutationKey(runId: string) {
  return ["roasts", runId, "rating"] as const;
}

export function useSaveRating(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: ratingMutationKey(runId),
    mutationFn: (body: OperatorRatingRequest) => api.rate(runId, body),
    onSuccess: async (detail) => {
      await queryClient.cancelQueries({ queryKey: roastKeys.detail(runId), exact: true });
      queryClient.setQueryData(roastKeys.detail(runId), (old: RoastDetail | undefined) =>
        old === undefined ? old : { ...old, rating: detail.rating, notes: detail.notes },
      );
      void queryClient.invalidateQueries({ queryKey: roastKeys.history, exact: true });
    },
  });
}

/**
 * Round 4 (PRRT_kwDOSzMG_c6RflFr / PRRT_kwDOSzMG_c6RflF1): a shared
 * "partial-failure window is open" signal, so `RoastRating` can freeze its
 * own Edit for the ENTIRE window between `RoastTastings`' partial failure
 * and its retry (or an explicit discard) — not just while a rating WRITE is
 * literally in flight (`ratingWriteInFlight`/`useIsMutating`), which is
 * `false` for the whole gap in between. Without this, an external direct
 * edit landing in that gap goes unobserved by `RoastTastings`, and its next
 * retry silently re-posts (and overwrites) the now-stale captured rating.
 *
 * Modeled as a tiny cache-resident boolean flag rather than reaching into
 * TanStack's own mutation-state history (`useMutationState`) — the latter's
 * behavior across `.reset()` calls and retries is exactly the kind of
 * implicit bookkeeping this whole Codex arc has been finding edges in;
 * an explicit flag this module owns end-to-end is easier to reason about
 * and test. `RoastTastings` is the only writer (`setPartialFailureLock`);
 * `RoastRating` (and `RoastTastings` itself, for its own Edit-adjacent
 * affordances) are readers via `usePartialFailureLock`.
 */
export function partialFailureLockKey(runId: string) {
  return ["roasts", runId, "rating-partial-failure-lock"] as const;
}

export function usePartialFailureLock(runId: string): boolean {
  const query = useQuery({
    queryKey: partialFailureLockKey(runId),
    queryFn: () => false,
    initialData: false,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  return query.data;
}

export function useSetPartialFailureLock(runId: string) {
  const queryClient = useQueryClient();
  // Stable identity across renders (a `useEffect` in RoastTastings depends on
  // this) — otherwise a plain inline arrow would re-run that effect on every
  // render regardless of whether the lock value actually changed.
  return useCallback(
    (locked: boolean) => {
      queryClient.setQueryData(partialFailureLockKey(runId), locked);
    },
    [queryClient, runId],
  );
}
