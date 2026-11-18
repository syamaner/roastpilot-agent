import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { applyEvent, hydrate, initialRoastStreamState } from "@/hooks/roastStreamReducer";
import type { HumanProtectionProjection, OperatorAction } from "@/lib/types";

import { RecoveryModal } from "./RecoveryModal";

afterEach(cleanup);

const READOUT = { beanTempC: 198, envTempC: 215, heatPercent: 60, fanPercent: 55 };

const AUTOMATIC_PROTECTION = {
  human_endpoint: null,
  human_command_disposition: null,
  physical_intervention_required: null,
  emergency_fallback_disclosure: null,
} satisfies HumanProtectionProjection;

describe("RecoveryModal", () => {
  it("renders nothing when closed", () => {
    render(<RecoveryModal open={false} {...READOUT} enabledActions={[]} onAction={() => {}} />);
    expect(screen.queryByTestId("recovery-modal")).toBeNull();
  });

  it("preserves legacy recovery labels and copy for an explicit all-null projection", () => {
    const onAction = vi.fn();
    render(
      <RecoveryModal
        open
        {...READOUT}
        humanProtection={AUTOMATIC_PROTECTION}
        enabledActions={["acknowledge_recovery", "start_cooling"]}
        onAction={onAction}
      />,
    );
    const copy = screen.getByTestId("recovery-no-auto-resume");
    expect(copy).toHaveTextContent(/no auto-resume/i);
    expect(copy).toHaveTextContent(/will not touch heat or fan until you choose/i);
    expect(screen.getByText(/service restarted during an active roast/i)).toBeVisible();
    expect(screen.getByText("Heat (locked)")).toBeVisible();
    expect(screen.getByText("Fan (locked)")).toBeVisible();
    expect(screen.getByText("Halt all systems immediately.")).toBeVisible();
    expect(screen.getByText("Cut heat, max fan, begin cooldown.")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Human-mode protection" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /resume monitoring only/i }));
    expect(onAction).toHaveBeenCalledWith("acknowledge_recovery");
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

describe("recovery disclosure copy", () => {
  const actions: OperatorAction[] = ["acknowledge_recovery", "start_cooling"];

  function renderProjection(protection: HumanProtectionProjection | null | undefined) {
    const onAction = vi.fn();
    render(
      <RecoveryModal
        open
        {...READOUT}
        humanProtection={protection}
        enabledActions={actions}
        onAction={onAction}
      />,
    );
    return onAction;
  }

  function expectConservativeWording() {
    expect(screen.getByTestId("recovery-no-auto-resume")).toHaveTextContent(
      "Routine heating does not resume automatically.",
    );
    expect(screen.getByText("Heat command")).toBeVisible();
    expect(screen.getByText("Fan command")).toBeVisible();
    expect(screen.queryByText(/service restarted|will not touch heat or fan|\(locked\)/i)).toBeNull();
    expect(screen.queryByText(/halt all systems|cut heat, max fan|heat\/fan stay at safe defaults/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /resume monitoring only/i })).toBeNull();
  }

  it.each([undefined, null])("keeps wording conservative for %s disclosure", (protection) => {
    const onAction = renderProjection(protection);

    expectConservativeWording();
    expect(screen.queryByRole("region", { name: "Human-mode protection" })).toBeNull();
    expect(screen.queryByTestId("recovery-drop_beans")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge recovery" }));
    fireEvent.click(screen.getByRole("button", { name: "Start Cooling" }));
    fireEvent.click(screen.getByRole("button", { name: "Emergency Stop" }));
    expect(onAction.mock.calls).toEqual([
      ["acknowledge_recovery"], ["start_cooling"], ["emergency_stop"],
    ]);
  });

  it.each([
    ["partial", { human_endpoint: null }],
    ["invalid endpoint", { ...AUTOMATIC_PROTECTION, human_endpoint: "not_a_disposition" }],
    ["invalid intervention", { ...AUTOMATIC_PROTECTION, physical_intervention_required: "false" }],
  ] as const)("keeps wording conservative after a %s first snapshot", (_label, fields) => {
    const stream = hydrate(initialRoastStreamState, {
      ...fields,
      id: "recovering-run",
      agent_phase: "operator_recovery_required",
      enabled_actions: actions,
    } as Parameters<typeof hydrate>[1]);
    renderProjection(stream.humanProtection?.data ?? null);

    expectConservativeWording();
    expect(screen.queryByRole("region", { name: "Human-mode protection" })).toBeNull();
  });

  it("updates one history row without changing dialog focus or server-owned recovery actions", () => {
    const runId = "recovering-run";
    let stream = hydrate(initialRoastStreamState, {
      ...AUTOMATIC_PROTECTION,
      id: runId,
      agent_phase: "operator_recovery_required",
      enabled_actions: actions,
      human_history_durability: "complete",
    });
    const onAction = vi.fn();
    const modal = () => (
      <RecoveryModal
        open
        {...READOUT}
        humanProtection={stream.humanProtection?.data}
        humanHistoryDurability={stream.humanProtection?.historyDurability}
        historyIdentifiesHuman={stream.humanProtection?.historyIdentifiesHuman}
        enabledActions={stream.enabledActions}
        onAction={onAction}
      />
    );
    const { rerender } = render(modal());
    const dialog = within(screen.getByRole("dialog"));
    const cooling = dialog.getByRole("button", { name: "Start Cooling" });
    cooling.focus();
    fireEvent.click(cooling);
    expect(dialog.getByText("Run history complete")).toBeVisible();
    expectConservativeWording();

    function receiveHeartbeat(data: object) {
      stream = applyEvent(stream, { event: "heartbeat", data: {
        run_id: runId, ...AUTOMATIC_PROTECTION, ...data,
      } }, runId);
      rerender(modal());
    }

    receiveHeartbeat({ human_history_durability: "incomplete" });
    const disclosure = dialog.getByRole("region", { name: "Human-mode protection" });
    expect(within(disclosure).getByRole("term")).toHaveTextContent("History durability");
    expect(within(disclosure).getByRole("definition")).toHaveTextContent("Run history incomplete");
    expect(dialog.getAllByText("History durability")).toHaveLength(1);
    expect(cooling).toHaveFocus();

    receiveHeartbeat({});
    expect(dialog.queryByText("History durability")).toBeNull();
    expectConservativeWording();
    expect(cooling).toHaveFocus();
    expect(cooling).toBeEnabled();
    expect(dialog.queryByRole("button", { name: "Drop Beans Now" })).toBeNull();
    expect(dialog.queryByText(/cooling confirmed|beans dropped|evacuation confirmed/i)).toBeNull();
    fireEvent.click(cooling);
    fireEvent.click(dialog.getByRole("button", { name: "Emergency Stop" }));
    expect(onAction.mock.calls).toEqual([["start_cooling"], ["start_cooling"], ["emergency_stop"]]);

    receiveHeartbeat({ human_history_durability: null });
    expect(dialog.queryByText("History durability")).toBeNull();
    expect(dialog.getByText("Heat (locked)")).toBeVisible();
  });

  it("discloses only positive HUMAN fields and keeps acknowledge neutral", () => {
    const onAction = renderProjection({
      ...AUTOMATIC_PROTECTION,
      human_endpoint: "withheld",
      physical_intervention_required: false,
    });

    expectConservativeWording();
    const disclosure = screen.getByRole("region", { name: "Human-mode protection" });
    expect(disclosure).toHaveTextContent("Drop withheld");
    expect(disclosure).not.toHaveTextContent(/Physical intervention required|Command outcome unknown/);
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge recovery" }));
    expect(onAction).toHaveBeenCalledWith("acknowledge_recovery");
  });
});
