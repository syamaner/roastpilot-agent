import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RoastDetail, TelemetrySeries } from "@/lib/types";
import { roastKeys } from "@/hooks/queries";
import { FIXTURE_DETAIL, FIXTURE_TELEMETRY, FIXTURE_TIMELINE } from "./fixture";
import { DetailPage } from "./DetailPage";
import { __resetPartialFailureLocksForTests } from "./useSaveRating";

function renderAt(path: string, client?: QueryClient) {
  const queryClient =
    client ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/roasts/:runId" element={children} />
          <Route path="/roasts" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(<DetailPage />, { wrapper: Wrapper });
}

afterEach(() => {
  vi.restoreAllMocks();
  // See RoastTastings.test.tsx's afterEach for why this module-scoped store
  // (#568 round 5) needs an explicit reset between tests.
  __resetPartialFailureLocksForTests();
});

describe("DetailPage shell", () => {
  it("fetches by the route run id and renders the detail view", async () => {
    const detailSpy = vi.spyOn(api, "roast").mockResolvedValue(FIXTURE_DETAIL);
    vi.spyOn(api, "telemetry").mockResolvedValue(FIXTURE_TELEMETRY);
    vi.spyOn(api, "timeline").mockResolvedValue(FIXTURE_TIMELINE);

    renderAt(`/roasts/${FIXTURE_DETAIL.id}`);

    await waitFor(() => expect(screen.getByTestId("detail-view")).toBeInTheDocument());
    expect(detailSpy).toHaveBeenCalledWith(FIXTURE_DETAIL.id);
    expect(screen.getByTestId("decision-trace-table")).toBeInTheDocument();
  });

  it("shows a not-found message when the detail query errors", async () => {
    vi.spyOn(api, "roast").mockRejectedValue(new Error("404"));
    vi.spyOn(api, "telemetry").mockResolvedValue(FIXTURE_TELEMETRY);
    vi.spyOn(api, "timeline").mockResolvedValue(FIXTURE_TIMELINE);

    renderAt(`/roasts/${FIXTURE_DETAIL.id}`);
    await waitFor(() => expect(screen.getByTestId("detail-error")).toBeInTheDocument());
  });

  it("shows a no-run message when there is no run id", () => {
    renderAt("/roasts");
    expect(screen.getByTestId("detail-no-run")).toBeInTheDocument();
  });

  it("refreshes cached live telemetry once the detail poll observes completion", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const liveDetail = { ...FIXTURE_DETAIL, completed_at_utc: null };
    const finalTelemetry: TelemetrySeries = {
      ...FIXTURE_TELEMETRY,
      points: FIXTURE_TELEMETRY.points.map((point, index) =>
        index >= 11 && index <= 12
          ? {
              ...point,
              post_fc_recovery_enabled: true,
              post_fc_heat_authority_state: "recovering",
            }
          : index === 13
            ? {
                ...point,
                post_fc_recovery_enabled: true,
                post_fc_heat_authority_state: "holding",
              }
            : point,
      ),
    };
    const partialTelemetry: TelemetrySeries = {
      ...FIXTURE_TELEMETRY,
      point_count: 10,
      points: FIXTURE_TELEMETRY.points.slice(0, 10),
    };
    const client = new QueryClient({
      defaultOptions: {
        queries: { refetchOnWindowFocus: false, retry: false, staleTime: 30_000 },
      },
    });
    client.setQueryData(roastKeys.detail(FIXTURE_DETAIL.id), liveDetail);
    client.setQueryData(roastKeys.telemetry(FIXTURE_DETAIL.id, 1), partialTelemetry);
    const detailSpy = vi.spyOn(api, "roast").mockResolvedValue(FIXTURE_DETAIL);
    const telemetrySpy = vi.spyOn(api, "telemetry").mockResolvedValue(finalTelemetry);
    vi.spyOn(api, "timeline").mockResolvedValue(FIXTURE_TIMELINE);

    renderAt(`/roasts/${FIXTURE_DETAIL.id}`, client);
    expect(screen.getByTestId("post-fc-recovery-summary")).toHaveTextContent(
      "No observed recovery",
    );
    expect(telemetrySpy).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    await vi.waitFor(() => expect(detailSpy).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(telemetrySpy).toHaveBeenCalledWith(FIXTURE_DETAIL.id, 1));
    await vi.waitFor(() =>
      expect(screen.getByTestId("post-fc-recovery-summary")).toHaveTextContent("Recovery armed"),
    );
    expect(screen.getByTestId("post-fc-recovery-summary")).toHaveTextContent("Recovering01:00");
  });

  it("#568 Codex (PRRT_kwDOSzMG_c6Rdlk6 / PRRT_kwDOSzMG_c6RdxDQ): the read-only rating headline reflects a saved edit IMMEDIATELY, never a stale flash while the detail query re-settles", async () => {
    vi.spyOn(api, "roast").mockResolvedValue({ ...FIXTURE_DETAIL, rating: null, notes: null });
    vi.spyOn(api, "telemetry").mockResolvedValue(FIXTURE_TELEMETRY);
    vi.spyOn(api, "timeline").mockResolvedValue(FIXTURE_TIMELINE);
    // A real fetch boundary (not resolved instantly): if RoastRating's editor
    // closed on `onSuccess` while relying on invalidateQueries' own refetch to
    // eventually bring the fresh rating in, this stalled response would leave
    // the headline showing "Not yet rated." (or the OLD note) for as long as
    // this promise stays pending — the exact hazard the mutation-response
    // cache-seed fix closes.
    let resolveRate: ((detail: RoastDetail) => void) | undefined;
    const rated: RoastDetail = {
      ...FIXTURE_DETAIL,
      rating: 5,
      notes: "seeded straight from the mutation",
    };
    vi.spyOn(api, "rate").mockImplementation(
      () => new Promise((resolve) => { resolveRate = resolve; }),
    );

    renderAt(`/roasts/${FIXTURE_DETAIL.id}`);
    await waitFor(() => expect(screen.getByTestId("detail-view")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("star-5"));
    fireEvent.click(screen.getByTestId("rating-save"));

    // The mutation call is in flight but stalled — wait for it to actually
    // reach the mock before resolving it.
    await waitFor(() => expect(resolveRate).toBeDefined());
    // The refetch this invalidation would trigger is STILL stalled — the
    // headline must already show the fresh value from the mutation's own
    // response, not the placeholder.
    resolveRate?.(rated);
    await waitFor(() => expect(screen.getByTestId("rating-headline")).toHaveTextContent("★★★★★"));
    expect(screen.getByTestId("rating-headline")).toHaveTextContent(
      "seeded straight from the mutation",
    );
    expect(screen.queryByTestId("rating-headline")).not.toHaveTextContent("Not yet rated.");
  });
});
