import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { RestartClearanceResult } from "@/lib/types";
import { RestartClearanceCard } from "./RestartClearanceCard";

describe("RestartClearanceCard", () => {
  it("requires each fresh physical confirmation before it records clearance", async () => {
    const acknowledge = vi.fn(async (): Promise<RestartClearanceResult> => ({
      outcome: "confirmed",
      cleared: true,
      restart_clearance_required: false,
      restart_clearance: { state: "cleared", eligible: true },
      fault_controls: { status: "closed", generation: null, enabled_actions: [] },
    }));
    render(<RestartClearanceCard onAcknowledge={acknowledge} />);

    expect(screen.getByTestId("restart-clearance-required")).toHaveTextContent(
      /does not inspect the Hottop.*prove its physical state.*turn heat, fan, or cooling on/i,
    );
    fireEvent.click(screen.getByTestId("restart-clearance-open"));
    const submit = screen.getByTestId("restart-clearance-submit");
    expect(submit).toBeDisabled();

    fireEvent.click(screen.getByTestId("restart-clearance-stopped"));
    fireEvent.click(screen.getByTestId("restart-clearance-empty"));
    expect(submit).toBeDisabled();

    fireEvent.click(screen.getByTestId("restart-clearance-independent-stop"));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(acknowledge).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("restart-clearance-reported")).toHaveTextContent(
      /recorded.*refresh current server status before starting/i,
    );
    expect(screen.getByTestId("restart-clearance-refresh-status")).toBeEnabled();
  });

  it("shows the durable confirmation before it requests a health refresh", async () => {
    const refresh = vi.fn();
    render(
      <RestartClearanceCard
        onAcknowledge={async () => ({
          outcome: "confirmed",
          cleared: true,
          restart_clearance_required: false,
          restart_clearance: { state: "cleared", eligible: true },
          fault_controls: { status: "closed", generation: null, enabled_actions: [] },
        })}
        onRefreshStatus={refresh}
      />,
    );

    fireEvent.click(screen.getByTestId("restart-clearance-open"));
    fireEvent.click(screen.getByTestId("restart-clearance-stopped"));
    fireEvent.click(screen.getByTestId("restart-clearance-empty"));
    fireEvent.click(screen.getByTestId("restart-clearance-independent-stop"));
    fireEvent.click(screen.getByTestId("restart-clearance-submit"));

    expect(await screen.findByTestId("restart-clearance-reported")).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("restart-clearance-refresh-status"));
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does not claim clearance after a rejected recording", async () => {
    const acknowledge = vi.fn(async () => {
      throw new Error("restart clearance could not be stored");
    });
    render(<RestartClearanceCard onAcknowledge={acknowledge} />);

    fireEvent.click(screen.getByTestId("restart-clearance-open"));
    fireEvent.click(screen.getByTestId("restart-clearance-stopped"));
    fireEvent.click(screen.getByTestId("restart-clearance-empty"));
    fireEvent.click(screen.getByTestId("restart-clearance-independent-stop"));
    fireEvent.click(screen.getByTestId("restart-clearance-submit"));

    expect(
      await screen.findByTestId("restart-clearance-error"),
    ).toHaveTextContent("restart clearance could not be stored");
    expect(screen.getByTestId("restart-clearance-required")).toBeInTheDocument();
    expect(screen.getByTestId("restart-clearance-stopped")).not.toBeChecked();
    expect(screen.getByTestId("restart-clearance-empty")).not.toBeChecked();
    expect(screen.getByTestId("restart-clearance-independent-stop")).not.toBeChecked();
  });

  it("requires fresh physical checks when the server returns an unconfirmed result", async () => {
    const acknowledge = vi.fn(async (): Promise<RestartClearanceResult> => ({
      outcome: "failed",
      cleared: false,
      restart_clearance_required: true,
      restart_clearance: { state: "required", eligible: false },
      fault_controls: { status: "open", generation: 4, enabled_actions: ["emergency_stop"] },
    }));
    render(<RestartClearanceCard onAcknowledge={acknowledge} />);

    fireEvent.click(screen.getByTestId("restart-clearance-open"));
    fireEvent.click(screen.getByTestId("restart-clearance-stopped"));
    fireEvent.click(screen.getByTestId("restart-clearance-empty"));
    fireEvent.click(screen.getByTestId("restart-clearance-independent-stop"));
    fireEvent.click(screen.getByTestId("restart-clearance-submit"));

    expect(await screen.findByTestId("restart-clearance-error")).toHaveTextContent(
      /not confirmed/i,
    );
    expect(screen.getByTestId("restart-clearance-stopped")).not.toBeChecked();
    expect(screen.getByTestId("restart-clearance-empty")).not.toBeChecked();
    expect(screen.getByTestId("restart-clearance-independent-stop")).not.toBeChecked();
    expect(screen.getByTestId("restart-clearance-submit")).toBeDisabled();
  });
});
