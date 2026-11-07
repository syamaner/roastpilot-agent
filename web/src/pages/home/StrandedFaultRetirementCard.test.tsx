import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StrandedFaultRetirementCard, StrandedFaultRetirementOutcomeNotice, StrandedFaultRetirementPendingNotice } from "./StrandedFaultRetirementCard";

const boxes = ["stranded-fault-stopped-empty", "stranded-fault-heat-off", "stranded-fault-fan-cooling-off", "stranded-fault-independent-stop"];

describe("StrandedFaultRetirementCard", () => {
  it("TC6a-d/TC7: requires all four fresh checks and consumes a submit gesture synchronously", () => {
    const submit = vi.fn();
    render(<StrandedFaultRetirementCard onSubmit={submit} />);
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    expect(screen.getByTestId("stranded-fault-stopped-empty")).toHaveFocus();
    const button = screen.getByTestId("stranded-fault-submit");
    for (const id of boxes) { expect(button).toBeDisabled(); fireEvent.click(screen.getByTestId(id)); }
    expect(button).toBeEnabled();
    fireEvent.click(button); fireEvent.click(button);
    expect(submit).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    expect(screen.getByTestId("stranded-fault-submit")).toBeDisabled();
    for (const id of boxes) fireEvent.click(screen.getByTestId(id));
    fireEvent.click(screen.getByTestId("stranded-fault-submit"));
    expect(submit).toHaveBeenCalledTimes(2);
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
  it("TC9: never submits from timers and renders only fixed non-diagnostic card copy", () => {
    vi.useFakeTimers();
    const submit = vi.fn();
    render(<StrandedFaultRetirementCard onSubmit={submit} />);
    act(() => vi.advanceTimersByTime(60_000));
    expect(submit).not.toHaveBeenCalled();
    const text = screen.getByTestId("stranded-fault-retirement-card").textContent ?? "";
    expect(text).not.toMatch(/retry|safe-zero|D212|process-1|run-abc|\d+/i);
    vi.useRealTimers();
  });
  it("TC11/TC12: renders fixed pending and outcome copy without server detail", () => {
    const refresh = vi.fn();
    const { rerender } = render(<StrandedFaultRetirementPendingNotice />);
    expect(screen.getByTestId("stranded-fault-retirement-pending")).toHaveAttribute("role", "status");
    expect(screen.getByTestId("stranded-fault-retirement-pending")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByTestId("stranded-fault-retirement-pending").querySelector("button")).toBeNull();
    rerender(<StrandedFaultRetirementOutcomeNotice kind="confirmed" onRefreshStatus={refresh} />);
    fireEvent.click(screen.getByTestId("stranded-fault-refresh-status"));
    expect(refresh).toHaveBeenCalledTimes(1);
    rerender(<StrandedFaultRetirementOutcomeNotice kind="error" onRefreshStatus={refresh} />);
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).toHaveAttribute("role", "status");
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).toHaveTextContent(/could not be confirmed/i);
    expect(screen.queryByTestId("stranded-fault-refresh-status")).toBeNull();
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).not.toHaveTextContent(/run-abc|\/var\/db|process-1/i);
  });

  it.each(["unconfirmed", "rejected", "error"] as const)("TC11: renders fixed %s outcome without a control", (kind) => {
    render(<StrandedFaultRetirementOutcomeNotice kind={kind} onRefreshStatus={vi.fn()} />);
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).toBeInTheDocument();
    expect(screen.queryByTestId("stranded-fault-refresh-status")).toBeNull();
  });
});
