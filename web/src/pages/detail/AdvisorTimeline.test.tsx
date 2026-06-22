import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AdvisorTimeline } from "./AdvisorTimeline";
import type { AdvisorRow } from "./advisorModel";

function row(overrides: Partial<AdvisorRow> = {}): AdvisorRow {
  return {
    tick: 4,
    elapsedSeconds: 120,
    beanTempC: 178,
    recordedAtUtc: "2026-06-07T09:14:00Z",
    provider: "openrouter",
    model: "anthropic/claude-opus-4.8",
    promptVersion: "v1",
    status: "ok",
    latencyMs: 820,
    recommendedHeat: 80,
    recommendedFan: 40,
    shouldDrop: false,
    confidence: 0.86,
    rationale: "Holding heat; RoR climbing as expected.",
    verdict: "allow",
    verdictReason: "within limits",
    ...overrides,
  };
}

describe("AdvisorTimeline", () => {
  it("renders an ok consult with its recommendation, latency, and linked verdict badge", () => {
    render(
      <AdvisorTimeline
        rows={[row({ recommendedHeat: 105, verdict: "clamp" })]}
        selectedTick={null}
        onSelect={() => {}}
      />,
    );
    const tr = screen.getByTestId("advisor-row");
    expect(within(tr).getByText("105 %")).toBeInTheDocument();
    expect(within(tr).getByText("820 ms")).toBeInTheDocument();
    // The shared VerdictBadge renders the CLAMP badge (D15 spelling).
    const badge = within(tr).getByTestId("verdict-badge");
    expect(badge).toHaveAttribute("data-verdict", "clamp");
    expect(badge).toHaveTextContent("CLAMP");
    // Status chip reads OK.
    expect(within(tr).getByTestId("advisor-status")).toHaveTextContent("OK");
  });

  it("renders the consult's bean-temp alongside its time (#325)", () => {
    render(
      <AdvisorTimeline
        rows={[row({ elapsedSeconds: 240, beanTempC: 178 })]}
        selectedTick={null}
        onSelect={() => {}}
      />,
    );
    const tr = screen.getByTestId("advisor-row");
    expect(within(tr).getByText("04:00")).toBeInTheDocument(); // 240 s
    expect(within(tr).getByTestId("advisor-row-temp")).toHaveTextContent("178.0 °C");
  });

  it("shows a placeholder temp when the consult has no bean-temp join (#325)", () => {
    render(
      <AdvisorTimeline
        rows={[row({ beanTempC: null })]}
        selectedTick={null}
        onSelect={() => {}}
      />,
    );
    // formatTemp(null) → em dash; never a fabricated 0 °C.
    expect(screen.getByTestId("advisor-row-temp")).toHaveTextContent("—");
  });

  it("renders a failed consult's status and no fabricated recommendation/verdict", () => {
    render(
      <AdvisorTimeline
        rows={[
          row({
            status: "provider_error",
            recommendedHeat: null,
            recommendedFan: null,
            confidence: null,
            rationale: null,
            verdict: null,
            verdictReason: null,
          }),
        ]}
        selectedTick={null}
        onSelect={() => {}}
      />,
    );
    const tr = screen.getByTestId("advisor-row");
    expect(within(tr).getByTestId("advisor-status")).toHaveTextContent("PROVIDER ERROR");
    expect(tr).toHaveAttribute("data-status", "provider_error");
    // No verdict badge (the failed consult produced no verdict).
    expect(within(tr).queryByTestId("verdict-badge")).toBeNull();
    // The row explains itself rather than rendering blank.
    expect(within(tr).getByTestId("advisor-rationale")).toHaveTextContent(/provider error/i);
  });

  it("renders multiple failure rows (a roast where the advisor never returned), not a blank panel", () => {
    const rows = [1, 4, 8].map((tick) =>
      row({
        tick,
        status: "provider_error",
        recommendedHeat: null,
        verdict: null,
        rationale: null,
      }),
    );
    render(<AdvisorTimeline rows={rows} selectedTick={null} onSelect={() => {}} />);
    expect(screen.queryByTestId("advisor-timeline-empty")).toBeNull();
    expect(screen.getAllByTestId("advisor-row")).toHaveLength(3);
    expect(screen.getAllByTestId("advisor-status")).toHaveLength(3);
  });

  it("falls back to a plain label for a non-badge verdict (e.g. fault)", () => {
    render(
      <AdvisorTimeline
        rows={[row({ verdict: "fault" })]}
        selectedTick={null}
        onSelect={() => {}}
      />,
    );
    // VerdictBadge renders nothing for fault; the row shows the label instead.
    expect(screen.queryByTestId("verdict-badge")).toBeNull();
    expect(screen.getByTestId("advisor-verdict-label")).toHaveTextContent("FAULT");
  });

  it("calls onSelect with the row's tick on click and marks the selected row", () => {
    const onSelect = vi.fn();
    render(
      <AdvisorTimeline rows={[row({ tick: 8 })]} selectedTick={8} onSelect={onSelect} />,
    );
    const tr = screen.getByTestId("advisor-row");
    expect(tr).toHaveAttribute("data-selected", "true");
    fireEvent.click(tr);
    expect(onSelect).toHaveBeenCalledWith(8);
  });

  it("renders an empty state only when there were zero consults", () => {
    render(<AdvisorTimeline rows={[]} selectedTick={null} onSelect={() => {}} />);
    expect(screen.getByTestId("advisor-timeline-empty")).toBeInTheDocument();
  });
});
