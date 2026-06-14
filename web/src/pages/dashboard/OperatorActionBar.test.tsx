import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OperatorAction } from "@/lib/types";
import { OperatorActionBar } from "./OperatorActionBar";

afterEach(cleanup);

describe("OperatorActionBar", () => {
  it("enables a button ONLY when its action is in the server's enabledActions", () => {
    const enabled: OperatorAction[] = ["drop_beans", "pause_advisory"];
    render(
      <OperatorActionBar enabledActions={enabled} phase="development" onAction={() => {}} />,
    );
    // In enabledActions → enabled.
    expect(screen.getByTestId("action-drop_beans")).toHaveAttribute("data-enabled", "true");
    expect(screen.getByTestId("action-drop_beans")).not.toBeDisabled();
    // NOT in enabledActions → disabled (mirrors server, never a client matrix).
    expect(screen.getByTestId("action-stop_cooling")).toHaveAttribute("data-enabled", "false");
    expect(screen.getByTestId("action-stop_cooling")).toBeDisabled();
  });

  it("does not dispatch a disabled action when clicked", () => {
    const onAction = vi.fn();
    render(<OperatorActionBar enabledActions={[]} phase="cooling" onAction={onAction} />);
    fireEvent.click(screen.getByTestId("action-drop_beans"));
    expect(onAction).not.toHaveBeenCalled();
  });

  it("dispatches an enabled action on click", () => {
    const onAction = vi.fn();
    render(
      <OperatorActionBar
        enabledActions={["drop_beans"]}
        phase="development"
        onAction={onAction}
      />,
    );
    fireEvent.click(screen.getByTestId("action-drop_beans"));
    expect(onAction).toHaveBeenCalledWith("drop_beans");
  });

  it("requires a confirm second press before firing emergency stop", () => {
    const onAction = vi.fn();
    render(<OperatorActionBar enabledActions={[]} phase="preheating" onAction={onAction} />);
    const estop = screen.getByTestId("action-emergency_stop");

    // First press ARMS — does not fire.
    fireEvent.click(estop);
    expect(onAction).not.toHaveBeenCalled();
    expect(estop).toHaveAttribute("data-armed", "true");
    expect(estop).toHaveTextContent(/confirm/i);

    // Second press fires.
    fireEvent.click(estop);
    expect(onAction).toHaveBeenCalledWith("emergency_stop");
    expect(estop).toHaveAttribute("data-armed", "false");
  });

  it("keeps emergency stop available even when enabledActions is empty/null", () => {
    render(<OperatorActionBar enabledActions={null} phase={null} onAction={() => {}} />);
    expect(screen.getByTestId("action-emergency_stop")).toBeEnabled();
  });

  it("hides the permitted-but-meaningless advisory toggles on a terminal phase", () => {
    // pause/resume are enabled in EVERY phase (the server mirror), but on a
    // terminal roast they're meaningless — the page hides them (presentation).
    render(
      <OperatorActionBar
        enabledActions={["pause_advisory", "resume_advisory"]}
        phase="complete"
        onAction={() => {}}
      />,
    );
    expect(screen.queryByTestId("action-pause_advisory")).toBeNull();
    expect(screen.queryByTestId("action-resume_advisory")).toBeNull();
    // E-stop still present on a terminal phase.
    expect(screen.getByTestId("action-emergency_stop")).toBeInTheDocument();
  });

  it("shows the advisory toggles on a non-terminal phase when enabled", () => {
    render(
      <OperatorActionBar
        enabledActions={["pause_advisory", "resume_advisory"]}
        phase="development"
        onAction={() => {}}
      />,
    );
    expect(screen.getByTestId("action-pause_advisory")).toHaveAttribute("data-enabled", "true");
  });

  it("surfaces START/STOP COOLING + EMERGENCY STOP while faulted (#206)", () => {
    // Post-#206 a faulted run stays operable: the server's enabledActions include
    // the cooling controls (matrix-derived), so the operator can cool a still-
    // running machine without a power cycle. The bar mirrors the server set — no
    // client matrix decides this. Heat-bearing actions (drop) stay disabled.
    const onAction = vi.fn();
    render(
      <OperatorActionBar
        enabledActions={["start_cooling", "stop_cooling", "emergency_stop"]}
        phase="faulted"
        onAction={onAction}
      />,
    );
    expect(screen.getByTestId("action-start_cooling")).toHaveAttribute("data-enabled", "true");
    expect(screen.getByTestId("action-stop_cooling")).toHaveAttribute("data-enabled", "true");
    expect(screen.getByTestId("action-emergency_stop")).toBeEnabled();
    // Not in the server set → disabled (mirrors server, never re-derived).
    expect(screen.getByTestId("action-drop_beans")).toHaveAttribute("data-enabled", "false");
    // The cooling controls dispatch through the same typed path.
    fireEvent.click(screen.getByTestId("action-stop_cooling"));
    expect(onAction).toHaveBeenCalledWith("stop_cooling");
  });

  it("surfaces the server's typed reason on a rejected result", () => {
    render(
      <OperatorActionBar
        enabledActions={["drop_beans"]}
        phase="preheating"
        onAction={() => {}}
        lastResult={{
          action: "drop_beans",
          result: "rejected",
          reason: "drop_beans invalid in phase preheating",
        }}
      />,
    );
    const result = screen.getByTestId("action-result");
    expect(result).toHaveAttribute("data-result", "rejected");
    expect(result).toHaveTextContent(/invalid in phase preheating/);
  });

  it("does not show a result line for an accepted action", () => {
    render(
      <OperatorActionBar
        enabledActions={["drop_beans"]}
        phase="development"
        onAction={() => {}}
        lastResult={{ action: "drop_beans", result: "accepted", reason: "ok" }}
      />,
    );
    expect(screen.queryByTestId("action-result")).toBeNull();
  });
});
