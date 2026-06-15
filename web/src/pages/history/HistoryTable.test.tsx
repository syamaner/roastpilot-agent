import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import type { RoastSummary } from "@/lib/types";
import { HistoryTable } from "./HistoryTable";

afterEach(cleanup);

function summary(overrides: Partial<RoastSummary> = {}): RoastSummary {
  return {
    id: "r1",
    started_at_utc: "2026-06-07T14:00:00Z",
    completed_at_utc: "2026-06-07T14:12:00Z",
    first_crack_at_utc: "2026-06-07T14:09:00Z",
    agent_phase: "complete",
    outcome: "completed",
    bean_origin: "Ethiopian Yirgacheffe",
    bean_varietal: "Heirloom",
    rating: 4,
    development_percent: 19,
    ...overrides,
  };
}

function renderTable(runs: RoastSummary[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <HistoryTable runs={runs} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("HistoryTable bean identity (#164)", () => {
  it("shows a Blend badge and the country for a blend run", () => {
    renderTable([summary({ id: "blend", is_blend: true, country: "Brazil" })]);
    const row = screen.getByTestId("history-row");
    expect(within(row).getByTestId("history-blend-badge")).toHaveTextContent(/blend/i);
    expect(within(row).getByText("Brazil")).toBeInTheDocument();
  });

  it("omits the Blend badge for a single-origin run (and pre-#164 rows)", () => {
    renderTable([summary({ id: "single", is_blend: false, country: null })]);
    expect(screen.queryByTestId("history-blend-badge")).not.toBeInTheDocument();
  });
});

describe("HistoryTable first-crack time (#111)", () => {
  it("renders an FC column header", () => {
    renderTable([summary()]);
    expect(screen.getByRole("columnheader", { name: "FC" })).toBeInTheDocument();
  });

  it("shows the first-crack time as UTC HH:MM", () => {
    renderTable([summary({ id: "fc", first_crack_at_utc: "2026-06-07T14:09:30Z" })]);
    expect(screen.getByTestId("history-fc")).toHaveTextContent("14:09");
  });

  it("shows an em-dash empty state when the run never reached first crack", () => {
    renderTable([summary({ id: "no-fc", first_crack_at_utc: null })]);
    expect(screen.getByTestId("history-fc")).toHaveTextContent("—");
  });
});
