/**
 * Shared rating-save mutation (#566 merge).
 *
 * Both `RoastRating` (direct edits to the headline rating) and `RoastTastings`
 * (the primary entry point — saving a tasting also updates the headline) POST
 * through the same `api.rate` call and invalidate the same `roastKeys.detail`
 * query, so the headline always re-reads from the server's own persisted
 * truth after either gesture. Page-local (not `hooks/queries.ts`): this
 * mutation is only ever driven from this page's two rating/tasting widgets.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { roastKeys } from "@/hooks/queries";
import type { OperatorRatingRequest } from "@/lib/types";

export function useSaveRating(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: OperatorRatingRequest) => api.rate(runId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: roastKeys.detail(runId) });
    },
  });
}
