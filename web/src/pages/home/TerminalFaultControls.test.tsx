import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FaultControlsActionResult, OperatorAction } from "@/lib/types";
import { TerminalFaultControls } from "./TerminalFaultControls";

describe("TerminalFaultControls", () => {
  it("renders only server-enumerated terminal actions and fails closed after admission", async () => {
    const onAction = vi.fn(async (action: OperatorAction): Promise<FaultControlsActionResult> => ({
      action,
      result: "accepted" as const,
      reason: "queued",
      queued: true,
    }));
    render(
      <TerminalFaultControls
        controls={{
          status: "open",
          generation: 4,
          enabled_actions: ["emergency_stop", "stop_cooling_and_acknowledge"],
        }}
        onAction={onAction}
      />,
    );

    expect(screen.getByTestId("terminal-fault-action-emergency_stop")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-fault-action-start_cooling")).toBeNull();
    fireEvent.click(screen.getByTestId("terminal-fault-action-emergency_stop"));
    await waitFor(() => expect(onAction).toHaveBeenCalledWith("emergency_stop", undefined));
    expect(screen.getByTestId("terminal-fault-controls-result")).toHaveTextContent(
      /admitted.*completion is not confirmed/i,
    );
    expect(screen.getByTestId("terminal-fault-controls-result")).toHaveTextContent(
      /no later execution outcome/i,
    );
    expect(screen.getByTestId("terminal-fault-controls-result")).not.toHaveTextContent(
      /waiting for the server/i,
    );
  });

  it("requires a second explicit confirmation for stop cooling and acknowledge", async () => {
    const onAction = vi.fn(async (
      action: OperatorAction,
      _confirmation?: true,
    ): Promise<FaultControlsActionResult> => ({
      action,
      result: "confirmed" as const,
      reason: "server verified cooling is off",
      queued: false,
    }));
    render(
      <TerminalFaultControls
        controls={{
          status: "open",
          generation: 4,
          enabled_actions: ["stop_cooling_and_acknowledge"],
        }}
        onAction={onAction}
      />,
    );

    const action = screen.getByTestId("terminal-fault-action-stop_cooling_and_acknowledge");
    fireEvent.click(action);
    expect(onAction).not.toHaveBeenCalled();
    expect(screen.getByTestId("terminal-fault-controls-confirmation")).toHaveTextContent(
      /heat 0, main fan 0, and cooling off/i,
    );
    fireEvent.click(action);
    await waitFor(() =>
      expect(onAction).toHaveBeenCalledWith("stop_cooling_and_acknowledge", true),
    );
  });

  it("renders UNKNOWN with no actuator other than the server-admitted e-stop", () => {
    render(
      <TerminalFaultControls
        controls={{
          status: "unknown",
          generation: null,
          enabled_actions: ["emergency_stop", "start_cooling"],
        }}
        onAction={vi.fn()}
      />,
    );

    expect(screen.getByTestId("terminal-fault-controls")).toHaveAttribute("data-status", "unknown");
    expect(screen.getByTestId("terminal-fault-action-emergency_stop")).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-fault-action-start_cooling")).toBeNull();
  });
});
