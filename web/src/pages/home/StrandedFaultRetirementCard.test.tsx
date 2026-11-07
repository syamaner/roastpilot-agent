import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StrandedFaultRetirementCard, StrandedFaultRetirementOutcomeNotice, StrandedFaultRetirementPendingNotice } from "./StrandedFaultRetirementCard";

const boxes = ["stranded-fault-stopped-empty", "stranded-fault-heat-off", "stranded-fault-fan-cooling-off", "stranded-fault-independent-stop"];

describe("StrandedFaultRetirementCard", () => {
  it("requires all four fresh physical checks and consumes a submit gesture synchronously", () => {
    const submit = vi.fn();
    render(<StrandedFaultRetirementCard onSubmit={submit} />);
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    const button = screen.getByTestId("stranded-fault-submit");
    for (const id of boxes) { expect(button).toBeDisabled(); fireEvent.click(screen.getByTestId(id)); }
    expect(button).toBeEnabled();
    fireEvent.click(button); fireEvent.click(button);
    expect(submit).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    expect(screen.getByTestId("stranded-fault-submit")).toBeDisabled();
  });
  it("cancels fresh ticks and always carries the physical-stop notice", () => {
    render(<StrandedFaultRetirementCard onSubmit={vi.fn()} />);
    expect(screen.getByTestId("stranded-fault-physical-stop-operative")).toHaveTextContent(/operative stop/i);
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    fireEvent.click(screen.getByTestId(boxes[0]));
    fireEvent.click(screen.getByTestId("stranded-fault-cancel"));
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    expect(screen.getByTestId(boxes[0])).not.toBeChecked();
  });
  it("renders fixed pending and outcome copy without server detail", () => {
    const refresh = vi.fn();
    const { rerender } = render(<StrandedFaultRetirementPendingNotice />);
    expect(screen.getByTestId("stranded-fault-retirement-pending").querySelector("button")).toBeNull();
    rerender(<StrandedFaultRetirementOutcomeNotice kind="confirmed" onRefreshStatus={refresh} />);
    fireEvent.click(screen.getByTestId("stranded-fault-refresh-status"));
    expect(refresh).toHaveBeenCalledTimes(1);
    rerender(<StrandedFaultRetirementOutcomeNotice kind="error" onRefreshStatus={refresh} />);
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).toHaveTextContent(/could not be confirmed/i);
    expect(screen.queryByTestId("stranded-fault-refresh-status")).toBeNull();
  });

  it.each(["unconfirmed", "rejected", "error"] as const)("renders fixed %s outcome without a control", (kind) => {
    render(<StrandedFaultRetirementOutcomeNotice kind={kind} onRefreshStatus={vi.fn()} />);
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).toBeInTheDocument();
    expect(screen.queryByTestId("stranded-fault-refresh-status")).toBeNull();
  });
});
