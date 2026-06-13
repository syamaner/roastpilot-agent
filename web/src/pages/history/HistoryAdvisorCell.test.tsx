import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api";
import type { RoastTimeline } from "@/lib/types";

import { HistoryAdvisorCell } from "./HistoryAdvisorCell";

function timeline(overrides: Partial<RoastTimeline> = {}): RoastTimeline {
  return {
    run_id: "r1",
    events: [],
    safety_evaluations: [],
    advisor_decisions: [],
    commands: [],
    ...overrides,
  };
}

function renderCell(runId = "r1") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return render(<HistoryAdvisorCell runId={runId} />, { wrapper });
}

afterEach(() => vi.restoreAllMocks());

describe("HistoryAdvisorCell", () => {
  it("summarizes consults + clamped/rejected from the per-roast timeline", async () => {
    vi.spyOn(api, "timeline").mockResolvedValue(
      timeline({
        safety_evaluations: [
          { tick: 4, rule: "r", verdict: "allow", input_heat: null, input_fan: null, adjusted_heat: null, adjusted_fan: null, reason: "", recorded_at_utc: "t" },
          { tick: 8, rule: "r", verdict: "clamp", input_heat: null, input_fan: null, adjusted_heat: null, adjusted_fan: null, reason: "", recorded_at_utc: "t" },
        ],
        advisor_decisions: [
          { tick: 4, provider: "openrouter", model: "m", prompt_version: "v1", latency_ms: 100, status: "ok", decision: null, recorded_at_utc: "t" },
          { tick: 8, provider: "openrouter", model: "m", prompt_version: "v1", latency_ms: 100, status: "ok", decision: null, recorded_at_utc: "t" },
        ],
      }),
    );
    renderCell();
    const cell = await screen.findByTestId("history-advisor");
    expect(cell).toHaveTextContent("2 consults");
    expect(cell).toHaveTextContent("1 clamped");
  });

  it("reports failed consults for a roast where the advisor never returned", async () => {
    vi.spyOn(api, "timeline").mockResolvedValue(
      timeline({
        advisor_decisions: [
          { tick: 1, provider: "openrouter", model: "m", prompt_version: "v1", latency_ms: null, status: "provider_error", decision: null, recorded_at_utc: "t" },
          { tick: 4, provider: "openrouter", model: "m", prompt_version: "v1", latency_ms: null, status: "provider_error", decision: null, recorded_at_utc: "t" },
        ],
      }),
    );
    renderCell();
    const cell = await screen.findByTestId("history-advisor");
    expect(cell).toHaveTextContent("2 consults");
    expect(cell).toHaveTextContent("2 failed");
  });

  it("shows a 'no advice' hint for a roast with zero consults", async () => {
    vi.spyOn(api, "timeline").mockResolvedValue(timeline());
    renderCell();
    expect(await screen.findByTestId("history-advisor-none")).toHaveTextContent("no advice");
  });

  it("degrades to an em dash (does not break the row) when the timeline request fails", async () => {
    vi.spyOn(api, "timeline").mockRejectedValue(new ApiError(500, "boom"));
    renderCell();
    await waitFor(() =>
      expect(screen.getByTestId("history-advisor-pending")).toHaveTextContent("—"),
    );
  });
});
