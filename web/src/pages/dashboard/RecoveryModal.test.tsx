import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RecoveryModal } from "./RecoveryModal";

afterEach(cleanup);

const READOUT = { beanTempC: 198, envTempC: 215, heatPercent: 60, fanPercent: 55 };

describe("RecoveryModal", () => {
  it("renders nothing when closed", () => {
    render(<RecoveryModal open={false} {...READOUT} enabledActions={[]} onAction={() => {}} />);
    expect(screen.queryByTestId("recovery-modal")).toBeNull();
  });

  it("states the no-auto-resume guarantee (required copy)", () => {
    render(<RecoveryModal open {...READOUT} enabledActions={[]} onAction={() => {}} />);
    const copy = screen.getByTestId("recovery-no-auto-resume");
    expect(copy).toHaveTextContent(/no auto-resume/i);
    expect(copy).toHaveTextContent(/will not touch heat or fan until you choose/i);
  });

  it("shows the current hardware readout", () => {
    render(<RecoveryModal open {...READOUT} enabledActions={[]} onAction={() => {}} />);
    const modal = screen.getByTestId("recovery-modal");
    expect(modal).toHaveTextContent("198.0 °C");
    expect(modal).toHaveTextContent("215.0 °C");
  });

  it("gates the recovery actions by the server's enabledActions", () => {
    // start_cooling + acknowledge_recovery are the actions the server permits in
    // operator_recovery_required; drop_beans is NOT.
    render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={["start_cooling", "acknowledge_recovery"]}
        onAction={() => {}}
      />,
    );
    expect(screen.getByTestId("recovery-acknowledge_recovery")).toHaveAttribute("data-enabled", "true");
    expect(screen.getByTestId("recovery-start_cooling")).toHaveAttribute("data-enabled", "true");
    expect(screen.getByTestId("recovery-drop_beans")).toHaveAttribute("data-enabled", "false");
    expect(screen.getByTestId("recovery-drop_beans")).toBeDisabled();
  });

  it("keeps emergency stop available regardless of the enabled set", () => {
    render(<RecoveryModal open {...READOUT} enabledActions={[]} onAction={() => {}} />);
    expect(screen.getByTestId("recovery-emergency_stop")).toBeEnabled();
  });

  it("dispatches the chosen recovery action", () => {
    const onAction = vi.fn();
    render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={["acknowledge_recovery"]}
        onAction={onAction}
      />,
    );
    fireEvent.click(screen.getByTestId("recovery-acknowledge_recovery"));
    expect(onAction).toHaveBeenCalledWith("acknowledge_recovery");
  });

  it("does not dispatch a disabled recovery action", () => {
    const onAction = vi.fn();
    render(<RecoveryModal open {...READOUT} enabledActions={[]} onAction={onAction} />);
    fireEvent.click(screen.getByTestId("recovery-drop_beans"));
    expect(onAction).not.toHaveBeenCalled();
  });
});
