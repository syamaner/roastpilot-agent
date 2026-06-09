import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api";
import type { RoastHistory, RoastSummary } from "@/lib/types";

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

  it("renders the first-run empty state when there are no roasts", async () => {
    mockHistory([]);
    renderPage();
    await screen.findByTestId("history-empty");
    expect(screen.queryByTestId("history-table")).not.toBeInTheDocument();
    expect(screen.queryByTestId("history-filter")).not.toBeInTheDocument();
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
