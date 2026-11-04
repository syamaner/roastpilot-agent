import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FaultControls, FaultControlsActionResult, OperatorAction } from "@/lib/types";
import { TerminalFaultControls } from "./TerminalFaultControls";

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe("TerminalFaultControls", () => {
  it("renders only server-enumerated terminal actions and fails closed after admission", async () => {
    const onAction = vi.fn(async (action: OperatorAction): Promise<FaultControlsActionResult> => ({
      action,
      result: "accepted" as const,
      reason: "queued",
      queued: true,
      fault_controls: {
        status: "open",
        generation: 4,
        enabled_actions: ["emergency_stop", "stop_cooling_and_acknowledge"],
      },
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
    const emergencyStop = screen.getByTestId("terminal-fault-action-emergency_stop");
    fireEvent.click(emergencyStop);
    expect(onAction).not.toHaveBeenCalled();
    expect(emergencyStop).toHaveAttribute("data-armed", "true");
    expect(screen.getByTestId("terminal-fault-controls-emergency-stop-confirmation")).toHaveTextContent(
      /press Confirm emergency stop.*independent physical emergency stop/i,
    );
    expect(emergencyStop).toHaveAttribute(
      "aria-describedby",
      "terminal-fault-controls-emergency-stop-confirmation",
    );
    fireEvent.click(emergencyStop);
    await waitFor(() => expect(onAction).toHaveBeenCalledWith("emergency_stop", undefined));
    expect(screen.getByTestId("terminal-fault-controls-result")).toHaveTextContent(
      /accepted.*not hardware completion/i,
    );
    expect(screen.getByTestId("terminal-fault-controls-result")).toHaveTextContent(
      /request-scoped historical status/i,
    );
    expect(screen.getByTestId("terminal-fault-controls-result")).toHaveTextContent(
      /lease at request time/i,
    );
    expect(screen.getByTestId("terminal-fault-controls-result")).not.toHaveTextContent(
      /waiting for the server/i,
    );
  });

  it("labels a confirmed e-stop as server software readback rather than physical proof", async () => {
    const onAction = vi.fn(async (action: OperatorAction): Promise<FaultControlsActionResult> => ({
      action,
      result: "confirmed" as const,
      reason: "typed command result",
      queued: false,
      fault_controls: { status: "open", generation: 4, enabled_actions: ["emergency_stop"] },
    }));
    render(
      <TerminalFaultControls
        controls={{ status: "open", generation: 4, enabled_actions: ["emergency_stop"] }}
        onAction={onAction}
      />,
    );

    const emergencyStop = screen.getByTestId("terminal-fault-action-emergency_stop");
    fireEvent.click(emergencyStop);
    fireEvent.click(emergencyStop);
    await waitFor(() => expect(onAction).toHaveBeenCalledWith("emergency_stop", undefined));
    expect(screen.getByTestId("terminal-fault-controls-result")).toHaveTextContent(
      /software readback was confirmed by the server.*not physical proof/i,
    );
  });

  it("keeps the e-stop available while restart clearance disarms routine controls", () => {
    render(
      <TerminalFaultControls
        controls={{
          status: "open",
          generation: 4,
          enabled_actions: [
            "emergency_stop",
            "start_cooling",
            "stop_cooling_and_acknowledge",
          ],
        }}
        onAction={vi.fn()}
        routineControlsDisabled
      />,
    );

    expect(screen.getByTestId("terminal-fault-controls-restart-clearance-required")).toHaveTextContent(
      /restart clearance.*routine controls/i,
    );
    expect(screen.getByTestId("terminal-fault-action-emergency_stop")).toBeEnabled();
    expect(screen.getByTestId("terminal-fault-action-start_cooling")).toBeDisabled();
    expect(
      screen.getByTestId("terminal-fault-action-stop_cooling_and_acknowledge"),
    ).toBeDisabled();
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
      fault_controls: { status: "closed", generation: 4, enabled_actions: [] },
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
    const confirmation = screen.getByTestId("terminal-fault-controls-confirmation");
    expect(confirmation).toHaveTextContent(
      /heat 0, main fan 0, and cooling off/i,
    );
    expect(action).toHaveAttribute("aria-describedby", confirmation.id);
    fireEvent.click(action);
    await waitFor(() =>
      expect(onAction).toHaveBeenCalledWith("stop_cooling_and_acknowledge", true, 4),
    );
  });

  it("lets the operator cancel a D212 confirmation before it dispatches", () => {
    const onAction = vi.fn();
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
    fireEvent.click(screen.getByTestId("terminal-fault-controls-confirmation-cancel"));

    expect(screen.queryByTestId("terminal-fault-controls-confirmation")).toBeNull();
    expect(action).toHaveTextContent("Stop cooling and acknowledge");
    expect(document.activeElement).toBe(action);
    expect(onAction).not.toHaveBeenCalled();
  });

  it("announces a pending D212 request and keeps its acknowledgement control described", async () => {
    const acknowledgement = deferred<FaultControlsActionResult>();
    const onAction = vi.fn().mockReturnValue(acknowledgement.promise);
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
    fireEvent.click(action);

    await waitFor(() =>
      expect(onAction).toHaveBeenCalledWith("stop_cooling_and_acknowledge", true, 4),
    );
    expect(screen.getByTestId("terminal-fault-controls-acknowledgement-pending")).toHaveTextContent(
      /request is pending.*server must still verify heat 0, main fan 0, and cooling off/i,
    );
    expect(action).toHaveAttribute(
      "aria-describedby",
      "terminal-fault-controls-acknowledgement-pending",
    );

    acknowledgement.resolve({
      action: "stop_cooling_and_acknowledge",
      result: "confirmed",
      reason: "server verified cooling is off",
      queued: false,
      fault_controls: { status: "closed", generation: null, enabled_actions: [] },
    });
    await waitFor(() => expect(action).toBeEnabled());
  });

  it("does not submit D212 without a positive server lease generation", () => {
    const onAction = vi.fn();
    render(
      <TerminalFaultControls
        controls={{
          status: "open",
          generation: 0,
          enabled_actions: ["stop_cooling_and_acknowledge"],
        }}
        onAction={onAction}
      />,
    );

    expect(screen.getByTestId("terminal-fault-action-stop_cooling_and_acknowledge")).toBeDisabled();
    expect(screen.getByTestId("terminal-fault-controls-generation-required")).toHaveTextContent(
      /valid positive lease generation/i,
    );
    fireEvent.click(screen.getByTestId("terminal-fault-action-stop_cooling_and_acknowledge"));
    expect(onAction).not.toHaveBeenCalled();
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

  it("keeps repeated emergency-stop attempts dispatchable while D212 is pending and keeps its newer result", async () => {
    const acknowledge = deferred<FaultControlsActionResult>();
    const emergencyStop = deferred<FaultControlsActionResult>();
    const onAction = vi
      .fn<
        (action: OperatorAction, confirmation?: true) => Promise<FaultControlsActionResult>
      >()
      .mockReturnValueOnce(acknowledge.promise)
      .mockReturnValueOnce(emergencyStop.promise);
    const controls: FaultControls = {
      status: "open",
      generation: 4,
      enabled_actions: ["emergency_stop", "stop_cooling_and_acknowledge"],
    };
    render(<TerminalFaultControls controls={controls} onAction={onAction} />);

    const acknowledgeButton = screen.getByTestId(
      "terminal-fault-action-stop_cooling_and_acknowledge",
    );
    fireEvent.click(acknowledgeButton);
    fireEvent.click(acknowledgeButton);
    await waitFor(() =>
      expect(onAction).toHaveBeenCalledWith("stop_cooling_and_acknowledge", true, 4),
    );

    const emergencyStopButton = screen.getByTestId("terminal-fault-action-emergency_stop");
    expect(emergencyStopButton).toBeEnabled();
    fireEvent.click(emergencyStopButton);
    fireEvent.click(emergencyStopButton);
    await waitFor(() => expect(onAction).toHaveBeenCalledWith("emergency_stop", undefined));

    emergencyStop.resolve({
      action: "emergency_stop",
      result: "accepted",
      reason: "admitted",
      queued: true,
      fault_controls: controls,
    });
    await waitFor(() =>
      expect(screen.getByTestId("terminal-fault-controls-result")).toHaveTextContent(
        /accepted.*not hardware completion/i,
      ),
    );

    acknowledge.resolve({
      action: "stop_cooling_and_acknowledge",
      result: "confirmed",
      reason: "late acknowledgement",
      queued: false,
      fault_controls: controls,
    });
    await waitFor(() => expect(acknowledgeButton).toBeEnabled());
    expect(acknowledgeButton).toHaveTextContent("Stop cooling and acknowledge");
    expect(screen.getByTestId("terminal-fault-controls-result")).not.toHaveTextContent(
      "late acknowledgement",
    );
  });

  it("immediately disarms D212 while an emergency-stop attempt is pending", async () => {
    const emergencyStop = deferred<FaultControlsActionResult>();
    const onAction = vi
      .fn<(action: OperatorAction, confirmation?: true) => Promise<FaultControlsActionResult>>()
      .mockReturnValue(emergencyStop.promise);
    const controls: FaultControls = {
      status: "open",
      generation: 4,
      enabled_actions: ["emergency_stop", "stop_cooling_and_acknowledge"],
    };
    render(<TerminalFaultControls controls={controls} onAction={onAction} />);

    const acknowledgement = screen.getByTestId(
      "terminal-fault-action-stop_cooling_and_acknowledge",
    );
    fireEvent.click(acknowledgement);
    expect(screen.getByTestId("terminal-fault-controls-confirmation")).toBeInTheDocument();

    const emergencyStopButton = screen.getByTestId("terminal-fault-action-emergency_stop");
    fireEvent.click(emergencyStopButton);
    fireEvent.click(emergencyStopButton);
    await waitFor(() => expect(onAction).toHaveBeenCalledWith("emergency_stop", undefined));
    expect(screen.queryByTestId("terminal-fault-controls-confirmation")).toBeNull();
    expect(acknowledgement).toBeDisabled();
    expect(screen.getByTestId("terminal-fault-controls-emergency-stop-pending")).toHaveTextContent(
      /pending.*not physical proof.*independent physical emergency stop/i,
    );
    expect(emergencyStopButton).toHaveAttribute(
      "aria-describedby",
      "terminal-fault-controls-emergency-stop-pending",
    );
    fireEvent.click(acknowledgement);
    expect(onAction).toHaveBeenCalledTimes(1);

    emergencyStop.resolve({
      action: "emergency_stop",
      result: "failed",
      reason: "software path did not confirm",
      queued: false,
      fault_controls: controls,
    });
    await waitFor(() => expect(acknowledgement).toBeEnabled());
    expect(screen.queryByTestId("terminal-fault-controls-confirmation")).toBeNull();
  });

  it("disables every ordinary terminal action while an emergency-stop request is pending", async () => {
    const emergencyStop = deferred<FaultControlsActionResult>();
    const onAction = vi
      .fn<(action: OperatorAction, confirmation?: true) => Promise<FaultControlsActionResult>>()
      .mockReturnValue(emergencyStop.promise);
    const controls: FaultControls = {
      status: "open",
      generation: 4,
      enabled_actions: ["emergency_stop", "start_cooling", "stop_cooling"],
    };
    render(<TerminalFaultControls controls={controls} onAction={onAction} />);

    const emergencyStopButton = screen.getByTestId("terminal-fault-action-emergency_stop");
    fireEvent.click(emergencyStopButton);
    fireEvent.click(emergencyStopButton);
    await waitFor(() => expect(onAction).toHaveBeenCalledWith("emergency_stop", undefined));

    expect(screen.getByTestId("terminal-fault-action-start_cooling")).toBeDisabled();
    expect(screen.getByTestId("terminal-fault-action-stop_cooling")).toBeDisabled();

    emergencyStop.resolve({
      action: "emergency_stop",
      result: "failed",
      reason: "not confirmed",
      queued: false,
      fault_controls: controls,
    });
    await waitFor(() => expect(screen.getByTestId("terminal-fault-action-start_cooling")).toBeEnabled());
  });

  it("keeps the newest concurrent emergency-stop result when an older request settles last", async () => {
    const first = deferred<FaultControlsActionResult>();
    const second = deferred<FaultControlsActionResult>();
    const onAction = vi
      .fn<(action: OperatorAction, confirmation?: true) => Promise<FaultControlsActionResult>>()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const controls: FaultControls = {
      status: "open",
      generation: 4,
      enabled_actions: ["emergency_stop"],
    };
    render(<TerminalFaultControls controls={controls} onAction={onAction} />);

    const emergencyStop = screen.getByTestId("terminal-fault-action-emergency_stop");
    fireEvent.click(emergencyStop);
    fireEvent.click(emergencyStop);
    fireEvent.click(emergencyStop);
    fireEvent.click(emergencyStop);
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(2));

    second.resolve({
      action: "emergency_stop",
      result: "accepted",
      reason: "newer request accepted",
      queued: true,
      fault_controls: controls,
    });
    await waitFor(() =>
      expect(screen.getByTestId("terminal-fault-controls-result")).toHaveAttribute(
        "data-result",
        "accepted",
      ),
    );
    first.resolve({
      action: "emergency_stop",
      result: "failed",
      reason: "older failure",
      queued: false,
      fault_controls: controls,
    });
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("terminal-fault-controls-result")).not.toHaveTextContent(
      /older failure/i,
    );
  });

  it("keeps an e-stop failure after the authoritative lease advances before it settles", async () => {
    const emergencyStop = deferred<FaultControlsActionResult>();
    const onAction = vi
      .fn<(action: OperatorAction, confirmation?: true) => Promise<FaultControlsActionResult>>()
      .mockReturnValue(emergencyStop.promise);
    const generationFour: FaultControls = {
      status: "open",
      generation: 4,
      enabled_actions: ["emergency_stop", "stop_cooling_and_acknowledge"],
    };
    const { rerender } = render(
      <TerminalFaultControls controls={generationFour} onAction={onAction} />,
    );

    const emergencyStopButton = screen.getByTestId("terminal-fault-action-emergency_stop");
    fireEvent.click(emergencyStopButton);
    fireEvent.click(emergencyStopButton);
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(1));
    const generationFive = { ...generationFour, generation: 5 };
    rerender(<TerminalFaultControls controls={generationFive} onAction={onAction} />);
    expect(
      screen.getByTestId("terminal-fault-action-stop_cooling_and_acknowledge"),
    ).toBeDisabled();
    emergencyStop.resolve({
      action: "emergency_stop",
      result: "failed",
      reason: "not confirmed after the lease advanced",
      queued: false,
      fault_controls: generationFive,
    });

    await waitFor(() =>
      expect(screen.getByTestId("terminal-fault-controls-result")).toHaveTextContent(
        /not confirmed after the lease advanced/i,
      ),
    );
    expect(screen.getByTestId("terminal-fault-controls")).toHaveAttribute(
      "data-lease-generation",
      "5",
    );
    expect(
      screen.getByTestId("terminal-fault-action-stop_cooling_and_acknowledge"),
    ).toBeEnabled();
  });

  it("resets D212 confirmation for a new lease and suppresses late prior-lease results", async () => {
    const firstAcknowledgement = deferred<FaultControlsActionResult>();
    const lateAcknowledgement = deferred<FaultControlsActionResult>();
    const onAction = vi
      .fn<
        (action: OperatorAction, confirmation?: true) => Promise<FaultControlsActionResult>
      >()
      .mockReturnValueOnce(firstAcknowledgement.promise)
      .mockReturnValueOnce(lateAcknowledgement.promise);
    const generationSeven: FaultControls = {
      status: "open",
      generation: 7,
      enabled_actions: ["stop_cooling_and_acknowledge"],
    };
    const { rerender } = render(
      <TerminalFaultControls controls={generationSeven} onAction={onAction} />,
    );

    const acknowledgementButton = () =>
      screen.getByTestId("terminal-fault-action-stop_cooling_and_acknowledge");
    fireEvent.click(acknowledgementButton());
    fireEvent.click(acknowledgementButton());
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(1));
    firstAcknowledgement.resolve({
      action: "stop_cooling_and_acknowledge",
      result: "confirmed",
      reason: "current acknowledgement",
      queued: false,
      fault_controls: generationSeven,
    });
    await waitFor(() => expect(acknowledgementButton()).toBeEnabled());
    expect(acknowledgementButton()).toHaveTextContent("Stop cooling and acknowledge");

    fireEvent.click(acknowledgementButton());
    expect(screen.getByTestId("terminal-fault-controls-confirmation")).toBeInTheDocument();
    const generationEight = { ...generationSeven, generation: 8 };
    rerender(<TerminalFaultControls controls={generationEight} onAction={onAction} />);
    expect(screen.queryByTestId("terminal-fault-controls-confirmation")).toBeNull();
    expect(acknowledgementButton()).toHaveTextContent("Stop cooling and acknowledge");

    fireEvent.click(acknowledgementButton());
    fireEvent.click(acknowledgementButton());
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(2));
    rerender(
      <TerminalFaultControls
        controls={{ status: "unknown", generation: null, enabled_actions: [] }}
        onAction={onAction}
      />,
    );
    lateAcknowledgement.resolve({
      action: "stop_cooling_and_acknowledge",
      result: "confirmed",
      reason: "late acknowledgement",
      queued: false,
      fault_controls: generationEight,
    });
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("terminal-fault-controls-result")).toBeNull();
  });
});
