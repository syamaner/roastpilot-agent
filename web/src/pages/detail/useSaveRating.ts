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
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { roastKeys } from "@/hooks/queries";
import type { OperatorRatingRequest } from "@/lib/types";

export function ratingMutationKey(runId: string) {
  return ["roasts", runId, "rating"] as const;
}

export function useSaveRating(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: ratingMutationKey(runId),
    mutationFn: (body: OperatorRatingRequest) => api.rate(runId, body),
    onSuccess: async (detail) => {
      await queryClient.cancelQueries({ queryKey: roastKeys.detail(runId) });
      queryClient.setQueryData(roastKeys.detail(runId), detail);
    },
  });
}
