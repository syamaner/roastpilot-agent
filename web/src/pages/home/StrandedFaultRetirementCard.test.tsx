import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StrandedFaultRetirementCard, StrandedFaultRetirementOutcomeNotice, StrandedFaultRetirementPendingNotice } from "./StrandedFaultRetirementCard";

const boxes = ["stranded-fault-stopped-empty", "stranded-fault-heat-off", "stranded-fault-fan-cooling-off", "stranded-fault-independent-stop"];

describe("StrandedFaultRetirementCard", () => {
  it.each(boxes)("TC6: requires the independently missing %s check", (missing) => {
    const submit = vi.fn();
    render(<StrandedFaultRetirementCard onSubmit={submit} />);
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    for (const id of boxes.filter((id) => id !== missing)) fireEvent.click(screen.getByTestId(id));
    const button = screen.getByTestId("stranded-fault-submit");
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(submit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId(missing));
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(submit).toHaveBeenCalledTimes(1);
  });
  it("TC7/MC38: synchronously consumes a re-entrant submit while its button is still mounted", () => {
    let reentered = false;
    const submit = vi.fn(() => {
      if (!reentered) {
        reentered = true;
        fireEvent.click(screen.getByTestId("stranded-fault-submit"));
      }
    });
    render(<StrandedFaultRetirementCard onSubmit={submit} />);
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    expect(screen.getByTestId("stranded-fault-stopped-empty")).toHaveFocus();
    for (const id of boxes) fireEvent.click(screen.getByTestId(id));
    fireEvent.click(screen.getByTestId("stranded-fault-submit"));
    expect(submit).toHaveBeenCalledTimes(1);
    for (const id of boxes) expect(screen.queryByTestId(id)).toBeNull();
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    for (const id of boxes) expect(screen.getByTestId(id)).not.toBeChecked();
  });
  it("cancels fresh ticks and always carries the physical-stop notice", () => {
    render(<StrandedFaultRetirementCard onSubmit={vi.fn()} />);
    expect(screen.getByTestId("stranded-fault-physical-stop-operative")).toHaveTextContent(/operative stop/i);
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    for (const id of boxes) fireEvent.click(screen.getByTestId(id));
    fireEvent.click(screen.getByTestId("stranded-fault-cancel"));
    for (const id of boxes) expect(screen.queryByTestId(id)).toBeNull();
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    for (const id of boxes) expect(screen.getByTestId(id)).not.toBeChecked();
    expect(screen.getByTestId("stranded-fault-physical-stop-operative")).toBeInTheDocument();
  });
  it("TC10: keeps the physical-stop notice in closed, open, submitted and cancelled states", () => {
    const submit = vi.fn();
    render(<StrandedFaultRetirementCard onSubmit={submit} />);
    const notice = () => expect(screen.getByTestId("stranded-fault-physical-stop-operative")).toBeInTheDocument();
    notice();
    fireEvent.click(screen.getByTestId("stranded-fault-open")); notice();
    fireEvent.click(screen.getByTestId("stranded-fault-cancel")); notice();
    fireEvent.click(screen.getByTestId("stranded-fault-open"));
    for (const id of boxes) fireEvent.click(screen.getByTestId(id));
    fireEvent.click(screen.getByTestId("stranded-fault-submit"));
    expect(submit).toHaveBeenCalledTimes(1); notice();
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
    expect(screen.getByTestId("stranded-fault-retirement-pending")).toHaveTextContent("A stranded-fault retirement request from this page is still pending. Starting remains blocked. The independent physical emergency stop is the operative stop.");
    rerender(<StrandedFaultRetirementOutcomeNotice kind="confirmed" onRefreshStatus={refresh} />);
    fireEvent.click(screen.getByTestId("stranded-fault-refresh-status"));
    expect(refresh).toHaveBeenCalledTimes(1);
    rerender(<StrandedFaultRetirementOutcomeNotice kind="error" onRefreshStatus={refresh} />);
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).toHaveAttribute("role", "status");
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).toHaveTextContent(/could not be confirmed/i);
    expect(screen.queryByTestId("stranded-fault-refresh-status")).toBeNull();
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).not.toHaveTextContent(/run-abc|\/var\/db|process-1/i);
  });

  it.each([
    ["confirmed", "Stranded fault record retired by your attestation. This is not proof that the roaster is in a safe state. Starting remains blocked until you complete a new, separate restart clearance with a fresh physical confirmation."],
    ["unconfirmed", "Retirement is unconfirmed. Starting remains blocked. Check refreshed status before any further action."],
    ["rejected", "Retirement was rejected. Check current status."],
    ["error", "Retirement result could not be confirmed. Check current status."],
  ] as const)("TC11: renders exact fixed %s outcome without diagnostic detail", (kind, text) => {
    render(<StrandedFaultRetirementOutcomeNotice kind={kind} onRefreshStatus={vi.fn()} />);
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).toHaveTextContent(text);
    expect(screen.getByTestId("stranded-fault-retirement-outcome")).not.toHaveTextContent(/run-abc|\/var\/db/i);
    if (kind === "confirmed") expect(screen.getByTestId("stranded-fault-refresh-status")).toBeInTheDocument();
    else expect(screen.queryByTestId("stranded-fault-refresh-status")).toBeNull();
  });
});
