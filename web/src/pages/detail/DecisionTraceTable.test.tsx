import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SafetyVerdict } from "@/lib/types";
import { DecisionTraceTable } from "./DecisionTraceTable";
import type { TraceRow } from "./traceModel";

function row(overrides: Partial<TraceRow> = {}): TraceRow {
  return {
    tick: 1,
    elapsedSeconds: 60,
    recordedAtUtc: "2026-06-07T09:01:00Z",
    verdict: "allow",
    rule: "rate_limit",
    reason: "within limits",
    recommendedHeat: 80,
    recommendedFan: 40,
    shouldDrop: false,
    confidence: 0.8,
    rationale: "short rationale",
    executedHeat: 80,
    executedFan: 40,
    commandTool: "set_heat",
    commandStatus: "ok",
    ...overrides,
  };
}

const ALL_SIX: SafetyVerdict[] = [
  "allow",
  "clamp",
  "reject",
  "recovery",
  "fault",
  "emergency_stop",
];

describe("DecisionTraceTable", () => {
  it("renders all six verdict labels in the verdict column (it shows history)", () => {
    const rows = ALL_SIX.map((verdict, i) => row({ tick: i, verdict }));
    render(<DecisionTraceTable rows={rows} selectedTick={null} onSelect={() => {}} />);
    const cells = screen.getAllByTestId("trace-verdict");
    const labels = cells.map((c) => c.textContent);
    // The enum spelling — EMERGENCY STOP, not ESTOP; ALLOW, never ACCEPT.
    expect(labels).toEqual([
      "ALLOW",
      "CLAMP",
      "REJECT",
      "RECOVERY",
      "FAULT",
      "EMERGENCY STOP",
    ]);
  });

  it("calls onSelect with the row's tick when a row is clicked", () => {
    const onSelect = vi.fn();
    render(
      <DecisionTraceTable rows={[row({ tick: 7 })]} selectedTick={null} onSelect={onSelect} />,
    );
    fireEvent.click(screen.getByTestId("trace-row"));
    expect(onSelect).toHaveBeenCalledWith(7);
  });

  it("marks the selected row via data-selected / aria-selected", () => {
    render(<DecisionTraceTable rows={[row({ tick: 7 })]} selectedTick={7} onSelect={() => {}} />);
    const tr = screen.getByTestId("trace-row");
    expect(tr).toHaveAttribute("data-selected", "true");
    expect(tr).toHaveAttribute("aria-selected", "true");
  });

  it("expands a truncated rationale without (de)selecting the row", () => {
    const onSelect = vi.fn();
    const long = "a very long rationale ".repeat(8);
    render(
      <DecisionTraceTable rows={[row({ rationale: long })]} selectedTick={null} onSelect={onSelect} />,
    );
    const toggle = screen.getByTestId("trace-rationale-toggle");
    expect(toggle).toHaveTextContent("more");
    fireEvent.click(toggle);
    expect(toggle).toHaveTextContent("less");
    // The expand click is stopped from bubbling to the row's select handler.
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("shows the recommended and executed control values", () => {
    render(
      <DecisionTraceTable
        rows={[row({ recommendedHeat: 105, executedHeat: 100 })]}
        selectedTick={null}
        onSelect={() => {}}
      />,
    );
    const tr = screen.getByTestId("trace-row");
    expect(within(tr).getByText("105 %")).toBeInTheDocument();
    expect(within(tr).getByText("100 %")).toBeInTheDocument();
  });

  it("renders an empty state when there are no rows", () => {
    render(<DecisionTraceTable rows={[]} selectedTick={null} onSelect={() => {}} />);
    expect(screen.getByTestId("decision-trace-empty")).toBeInTheDocument();
  });
});
