import { QueryClient } from "@tanstack/react-query";

/** Shared TanStack Query client for the REST surface (history/detail/timeline/
 *  telemetry). Live data flows over SSE, not polling, so refetch-on-focus is off
 *  and stale times are generous — REST is for snapshots and post-roast reads. */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 30_000,
    },
  },
});
