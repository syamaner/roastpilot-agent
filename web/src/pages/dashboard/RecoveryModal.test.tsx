import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    expect(screen.queryByTestId("recovery-drop_beans")).toBeNull();
  });

  it("keeps emergency stop available regardless of the enabled set", () => {
    render(<RecoveryModal open {...READOUT} enabledActions={[]} onAction={() => {}} />);
    expect(screen.getByTestId("recovery-emergency_stop")).toBeEnabled();
  });

  it("disables routine recovery actions while health is stale but retains emergency stop", () => {
    render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={["start_cooling", "acknowledge_recovery"]}
        onAction={() => {}}
        healthStale
      />,
    );
    expect(screen.queryByTestId("recovery-start_cooling")).toBeNull();
    expect(screen.queryByTestId("recovery-acknowledge_recovery")).toBeNull();
    expect(screen.getByTestId("recovery-emergency_stop")).toBeEnabled();
    expect(screen.getByTestId("recovery-health-stale")).toHaveTextContent(
      /recovery actions are disabled.*emergency stop remains available/i,
    );
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

  it("does not offer a server-disabled recovery action", () => {
    const onAction = vi.fn();
    render(<RecoveryModal open {...READOUT} enabledActions={[]} onAction={onAction} />);
    expect(screen.queryByTestId("recovery-drop_beans")).toBeNull();
    expect(onAction).not.toHaveBeenCalled();
  });

  it("renders closure-only confirmation only from the server projection and resets its checks", async () => {
    const onRequestClosureOnly = vi.fn(async () => ({
      outcome: "failed" as const,
      closure_only: false,
      restart_clearance: {
        state: "required" as const,
        eligible: false,
        enabled_actions: [],
        closure_only_available: true,
      },
      fault_controls: { status: "closed" as const, generation: null, enabled_actions: [] },
    }));
    const { rerender } = render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        onRequestClosureOnly={onRequestClosureOnly}
      />,
    );
    expect(screen.queryByTestId("closure-only-clearance")).toBeNull();

    rerender(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        closureOnlyAvailable
        onRequestClosureOnly={onRequestClosureOnly}
      />,
    );
    fireEvent.click(screen.getByTestId("closure-only-open"));
    const submit = screen.getByTestId("closure-only-submit");
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByTestId("closure-only-stopped"));
    fireEvent.click(screen.getByTestId("closure-only-empty"));
    fireEvent.click(screen.getByTestId("closure-only-independent-stop"));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(onRequestClosureOnly).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("closure-only-error")).toBeVisible());
    expect(screen.getByTestId("closure-only-stopped")).not.toBeChecked();
    expect(screen.getByTestId("closure-only-empty")).not.toBeChecked();
    expect(screen.getByTestId("closure-only-independent-stop")).not.toBeChecked();
  });

  it("shows recovered-run End only when the server projects it and latches success", async () => {
    const onEndRecoveredRun = vi.fn(async () => ({
      run_id: "run-recovered",
      outcome: "aborted" as const,
      restart_clearance: {
        state: "required" as const,
        eligible: false,
        enabled_actions: [],
        closure_only_available: false,
      },
      fault_controls: { status: "closed" as const, generation: null, enabled_actions: [] },
    }));
    const { rerender } = render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        onEndRecoveredRun={onEndRecoveredRun}
      />,
    );
    expect(screen.queryByTestId("end-recovered-run")).toBeNull();

    rerender(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        endRecoveredRunAvailable
        onEndRecoveredRun={onEndRecoveredRun}
      />,
    );
    fireEvent.click(screen.getByTestId("end-recovered-run-submit"));
    expect(onEndRecoveredRun).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByTestId("end-recovered-run-submit")).toBeDisabled());
    expect(screen.getByTestId("end-recovered-run-submit")).toHaveTextContent("End requested");
  });

  it("resets physical checks across a stale-to-fresh health transition while retaining e-stop", async () => {
    const { rerender } = render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        closureOnlyAvailable
        onRequestClosureOnly={async () => {
          throw new Error("not reached");
        }}
      />,
    );
    fireEvent.click(screen.getByTestId("closure-only-open"));
    fireEvent.click(screen.getByTestId("closure-only-stopped"));
    expect(screen.getByTestId("closure-only-stopped")).toBeChecked();

    rerender(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        closureOnlyAvailable
        healthStale
        onRequestClosureOnly={async () => {
          throw new Error("not reached");
        }}
      />,
    );
    expect(screen.queryByTestId("closure-only-clearance")).toBeNull();
    expect(screen.getByTestId("recovery-emergency_stop")).toBeEnabled();

    rerender(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        closureOnlyAvailable
        onRequestClosureOnly={async () => {
          throw new Error("not reached");
        }}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("closure-only-open")).toBeVisible());
    fireEvent.click(screen.getByTestId("closure-only-open"));
    expect(screen.getByTestId("closure-only-stopped")).not.toBeChecked();
  });

  it("keeps emergency stop available and submits End only once while End is in flight", async () => {
    let resolveEnd: (() => void) | undefined;
    const onEndRecoveredRun = vi.fn(
      () =>
        new Promise<{
          run_id: string;
          outcome: "aborted";
          restart_clearance: {
            state: "required";
            eligible: boolean;
            enabled_actions: [];
            closure_only_available: boolean;
          };
          fault_controls: { status: "closed"; generation: null; enabled_actions: [] };
        }>((resolve) => {
          resolveEnd = () =>
            resolve({
              run_id: "run-recovered",
              outcome: "aborted",
              restart_clearance: {
                state: "required",
                eligible: false,
                enabled_actions: [],
                closure_only_available: false,
              },
              fault_controls: { status: "closed", generation: null, enabled_actions: [] },
            });
        }),
    );
    const onAction = vi.fn();
    render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={onAction}
        endRecoveredRunAvailable
        onEndRecoveredRun={onEndRecoveredRun}
      />,
    );

    const end = screen.getByTestId("end-recovered-run-submit");
    fireEvent.click(end);
    fireEvent.click(end);
    expect(onEndRecoveredRun).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("recovery-emergency_stop")).toBeEnabled();
    fireEvent.click(screen.getByTestId("recovery-emergency_stop"));
    expect(onAction).toHaveBeenCalledWith("emergency_stop");

    resolveEnd?.();
    await waitFor(() => expect(screen.getByTestId("end-recovered-run-submit")).toBeDisabled());
    expect(screen.getByTestId("end-recovered-run-submit")).toHaveTextContent("End requested");
  });

  it("clears an End error when its server authority disappears", async () => {
    const onEndRecoveredRun = vi.fn(async () => {
      throw new Error("authority changed");
    });
    const { rerender } = render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        endRecoveredRunAvailable
        onEndRecoveredRun={onEndRecoveredRun}
      />,
    );
    fireEvent.click(screen.getByTestId("end-recovered-run-submit"));
    await waitFor(() => expect(screen.getByTestId("end-recovered-run-error")).toHaveTextContent("authority changed"));

    rerender(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        onEndRecoveredRun={onEndRecoveredRun}
      />,
    );
    expect(screen.queryByTestId("end-recovered-run")).toBeNull();

    rerender(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        endRecoveredRunAvailable
        onEndRecoveredRun={onEndRecoveredRun}
      />,
    );
    expect(screen.queryByTestId("end-recovered-run-error")).toBeNull();
  });

  it("clears an old closure error when a new checklist is opened", async () => {
    const onRequestClosureOnly = vi.fn(async () => {
      throw new Error("authority changed");
    });
    render(
      <RecoveryModal
        open
        {...READOUT}
        enabledActions={[]}
        onAction={() => {}}
        closureOnlyAvailable
        onRequestClosureOnly={onRequestClosureOnly}
      />,
    );
    fireEvent.click(screen.getByTestId("closure-only-open"));
    fireEvent.click(screen.getByTestId("closure-only-stopped"));
    fireEvent.click(screen.getByTestId("closure-only-empty"));
    fireEvent.click(screen.getByTestId("closure-only-independent-stop"));
    fireEvent.click(screen.getByTestId("closure-only-submit"));
    await waitFor(() => expect(screen.getByTestId("closure-only-error")).toHaveTextContent("authority changed"));

    fireEvent.click(screen.getByTestId("closure-only-cancel"));
    fireEvent.click(screen.getByTestId("closure-only-open"));
    expect(screen.queryByTestId("closure-only-error")).toBeNull();
  });
});
