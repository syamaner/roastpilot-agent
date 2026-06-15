import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RoastSummary } from "@/lib/types";

import { HistoryAdvisorCell } from "./HistoryAdvisorCell";

function summary(overrides: Partial<RoastSummary> = {}): RoastSummary {
  return {
    id: "r1",
    started_at_utc: "2026-06-07T14:00:00Z",
    completed_at_utc: "2026-06-07T14:12:00Z",
    first_crack_at_utc: "2026-06-07T14:09:00Z",
    agent_phase: "complete",
    outcome: "completed",
    bean_origin: "Ethiopian Yirgacheffe",
    bean_varietal: "Medium",
    rating: 4,
    development_percent: 19,
    advisor_consults: 0,
    advisor_clamped: 0,
    advisor_rejected: 0,
    advisor_failed: 0,
    ...overrides,
  };
}

describe("HistoryAdvisorCell", () => {
  it("summarizes consults + clamped from the summary's advisor fields (#184)", () => {
    render(
      <HistoryAdvisorCell run={summary({ advisor_consults: 2, advisor_clamped: 1 })} />,
    );
    const cell = screen.getByTestId("history-advisor");
    expect(cell).toHaveTextContent("2 consults");
    expect(cell).toHaveTextContent("1 clamped");
  });

  it("reports failed consults for a roast where the advisor never returned", () => {
    render(
      <HistoryAdvisorCell run={summary({ advisor_consults: 2, advisor_failed: 2 })} />,
    );
    const cell = screen.getByTestId("history-advisor");
    expect(cell).toHaveTextContent("2 consults");
    expect(cell).toHaveTextContent("2 failed");
  });

  it("reports rejected consults", () => {
    render(
      <HistoryAdvisorCell run={summary({ advisor_consults: 3, advisor_rejected: 1 })} />,
    );
    expect(screen.getByTestId("history-advisor")).toHaveTextContent("1 rejected");
  });

  it("shows 'all allowed' when every consult passed clean", () => {
    render(<HistoryAdvisorCell run={summary({ advisor_consults: 5 })} />);
    expect(screen.getByTestId("history-advisor")).toHaveTextContent("all allowed");
  });

  it("uses the singular 'consult' for a single consult", () => {
    render(<HistoryAdvisorCell run={summary({ advisor_consults: 1 })} />);
    expect(screen.getByTestId("history-advisor")).toHaveTextContent("1 consult");
  });

  it("shows a 'no advice' hint for a roast with zero consults (back-compat)", () => {
    render(<HistoryAdvisorCell run={summary({ advisor_consults: 0 })} />);
    expect(screen.getByTestId("history-advisor-none")).toHaveTextContent("no advice");
  });
});
