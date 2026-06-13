import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api";
import type { RoastHistory, RoastSummary, RoastTimeline } from "@/lib/types";

import { HistoryPage } from "./HistoryPage";

function summary(overrides: Partial<RoastSummary> = {}): RoastSummary {
  return {
    id: "r1",
    started_at_utc: "2026-06-07T14:00:00Z",
    completed_at_utc: "2026-06-07T14:12:00Z",
    agent_phase: "complete",
    outcome: "completed",
    bean_origin: "Ethiopian Yirgacheffe",
    bean_varietal: "Medium",
    rating: 4,
    development_percent: 19,
    ...overrides,
  };
}

const FIXTURE: RoastSummary[] = [
  summary({ id: "a", bean_origin: "Ethiopian Yirgacheffe", outcome: "completed", rating: 5 }),
  summary({ id: "b", bean_origin: "Colombian Supremo", outcome: "aborted", rating: 2 }),
  summary({ id: "c", bean_origin: "Kenyan AA", bean_varietal: null, outcome: "faulted", rating: null, development_percent: null }),
];

/** Render the page with a fresh QueryClient + a router that records navigation. */
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/roasts"]}>
        <Routes>
          <Route path="/roasts" element={children} />
          <Route path="/roasts/:runId" element={<div data-testid="detail-landing" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return render(<HistoryPage />, { wrapper });
}

afterEach(() => vi.restoreAllMocks());

function mockHistory(runs: RoastSummary[]): void {
  vi.spyOn(api, "history").mockResolvedValue({ runs } satisfies RoastHistory);
  // Each visible row lazily fetches its own advisor timeline (#170). Stub it so
  // the rows render the advisor cell deterministically (and never leak rejects).
  vi.spyOn(api, "timeline").mockImplementation(async (runId: string) =>
    ({
      run_id: runId,
      events: [],
      safety_evaluations: [
        { tick: 8, rule: "bounds", verdict: "clamp", input_heat: null, input_fan: null, adjusted_heat: null, adjusted_fan: null, reason: "", recorded_at_utc: "t" },
      ],
      advisor_decisions: [
        { tick: 4, provider: "openrouter", model: "m", prompt_version: "v1", latency_ms: 100, status: "ok", decision: null, recorded_at_utc: "t" },
        { tick: 8, provider: "openrouter", model: "m", prompt_version: "v1", latency_ms: 100, status: "ok", decision: null, recorded_at_utc: "t" },
      ],
      commands: [],
    }) satisfies RoastTimeline,
  );
}

describe("HistoryPage", () => {
  it("renders one row per run with the contract columns", async () => {
    mockHistory(FIXTURE);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("history-row")).toHaveLength(3));
    const first = screen.getAllByTestId("history-row")[0];
    expect(within(first).getByText("Ethiopian Yirgacheffe")).toBeInTheDocument();
    expect(within(first).getByTestId("outcome-badge")).toHaveTextContent("COMPLETED");
  });

  it("renders a per-roast advisor summary column from the timeline (#170)", async () => {
    mockHistory(FIXTURE);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("history-row")).toHaveLength(3));
    // Each row resolves its own timeline query independently; wait for all three.
    await waitFor(() =>
      expect(screen.getAllByTestId("history-advisor")).toHaveLength(3),
    );
    const cells = screen.getAllByTestId("history-advisor");
    expect(cells[0]).toHaveTextContent("2 consults");
    expect(cells[0]).toHaveTextContent("1 clamped");
  });

  it("renders the first-run empty state when there are no roasts", async () => {
    mockHistory([]);
    renderPage();
    await screen.findByTestId("history-empty");
    expect(screen.queryByTestId("history-table")).not.toBeInTheDocument();
    expect(screen.queryByTestId("history-filter")).not.toBeInTheDocument();
    // The empty state links to the dashboard (an honest route nav, not a
    // fabricated "start roast" action this page can't perform).
    expect(screen.getByTestId("history-empty-dashboard-link")).toHaveAttribute("href", "/");
  });

  it("shows an error state when the request fails", async () => {
    vi.spyOn(api, "history").mockRejectedValue(new ApiError(500, "boom"));
    renderPage();
    const err = await screen.findByTestId("history-error");
    expect(err).toHaveTextContent("boom");
  });

  it("filters rows by the search box (interaction)", async () => {
    mockHistory(FIXTURE);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("history-row")).toHaveLength(3));
    await userEvent.type(screen.getByLabelText("Search beans"), "colomb");
    expect(screen.getAllByTestId("history-row")).toHaveLength(1);
    expect(screen.getByText("Colombian Supremo")).toBeInTheDocument();
  });

  it("filters by outcome", async () => {
    mockHistory(FIXTURE);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("history-row")).toHaveLength(3));
    await userEvent.selectOptions(screen.getByLabelText("Filter by outcome"), "faulted");
    const rows = screen.getAllByTestId("history-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveAttribute("data-run-id", "c");
  });

  it("filters by minimum rating, excluding unrated runs", async () => {
    mockHistory(FIXTURE);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("history-row")).toHaveLength(3));
    await userEvent.selectOptions(screen.getByLabelText("Filter by minimum rating"), "3");
    const rows = screen.getAllByTestId("history-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveAttribute("data-run-id", "a");
  });

  it("shows the no-matches state and clears filters back to the full list", async () => {
    mockHistory(FIXTURE);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("history-row")).toHaveLength(3));
    await userEvent.type(screen.getByLabelText("Search beans"), "nonexistent");
    expect(await screen.findByTestId("history-no-matches")).toBeInTheDocument();
    expect(screen.queryByTestId("history-table")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /clear filters/i }));
    expect(screen.getAllByTestId("history-row")).toHaveLength(3);
  });

  it("navigates to the detail page when a row is clicked", async () => {
    mockHistory(FIXTURE);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("history-row")).toHaveLength(3));
    await userEvent.click(screen.getByText("Colombian Supremo"));
    expect(await screen.findByTestId("detail-landing")).toBeInTheDocument();
  });

  it("activates a row from the keyboard (Enter)", async () => {
    mockHistory(FIXTURE);
    renderPage();
    await waitFor(() => expect(screen.getAllByTestId("history-row")).toHaveLength(3));
    const row = screen.getAllByTestId("history-row")[1];
    row.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByTestId("detail-landing")).toBeInTheDocument();
  });
});
