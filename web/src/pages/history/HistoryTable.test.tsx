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
    advisor_consults: 0,
    advisor_clamped: 0,
    advisor_rejected: 0,
    advisor_failed: 0,
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

describe("HistoryTable weight loss % (#388)", () => {
  it("renders a Loss % column header", () => {
    renderTable([summary()]);
    expect(screen.getByRole("columnheader", { name: "Loss %" })).toBeInTheDocument();
  });

  it("shows the weight-loss % to one decimal", () => {
    renderTable([summary({ id: "wl", weight_loss_percent: 11.6 })]);
    expect(screen.getByTestId("history-weight-loss")).toHaveTextContent("11.6%");
  });

  it("shows an em-dash when the run was not weighed", () => {
    renderTable([summary({ id: "unweighed", weight_loss_percent: null })]);
    expect(screen.getByTestId("history-weight-loss")).toHaveTextContent("—");
  });
});

describe("HistoryTable corrected-charge indicator (#520 round-2 P5)", () => {
  it("marks the Loss % cell when the run's charge weight was corrected, honouring the no-silent-swap principle", () => {
    renderTable([
      summary({ id: "corrected", weight_loss_percent: 12.55, corrected_charge_grams: 255 }),
    ]);
    const marker = screen.getByTestId("history-weight-loss-corrected");
    expect(within(screen.getByTestId("history-weight-loss")).getByTestId(
      "history-weight-loss-corrected",
    )).toBeInTheDocument();
    expect(marker).toHaveAttribute("title", expect.stringContaining("255 g"));
  });

  it("omits the marker for a run whose charge was never corrected", () => {
    renderTable([
      summary({ id: "uncorrected", weight_loss_percent: 11.6, corrected_charge_grams: null }),
    ]);
    expect(screen.queryByTestId("history-weight-loss-corrected")).not.toBeInTheDocument();
  });

  it("omits the marker when corrected_charge_grams is absent from the payload (pre-#520 rows)", () => {
    renderTable([summary({ id: "pre-520", weight_loss_percent: 11.6 })]);
    expect(screen.queryByTestId("history-weight-loss-corrected")).not.toBeInTheDocument();
  });
});

describe("HistoryTable ambient (#464)", () => {
  it("renders an Ambient column header", () => {
    renderTable([summary()]);
    expect(screen.getByRole("columnheader", { name: "Ambient" })).toBeInTheDocument();
  });

  it("shows the charge-time temp + humidity when captured", () => {
    renderTable([
      summary({ id: "amb", ambient_temp_c: 22.4, ambient_humidity_pct: 41, ambient_pressure_hpa: 1013 }),
    ]);
    expect(screen.getByTestId("history-ambient")).toHaveTextContent("22.4°C · 41%");
  });

  it("shows an em-dash when ambient was never captured (pre-#342 / disabled)", () => {
    renderTable([summary({ id: "no-amb" })]);
    expect(screen.getByTestId("history-ambient")).toHaveTextContent("—");
  });
});
