import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { RestartClearanceResult } from "@/lib/types";
import { RestartClearanceCard } from "./RestartClearanceCard";

describe("RestartClearanceCard", () => {
  it("requires each fresh physical confirmation before it records clearance", async () => {
    const acknowledge = vi.fn(async (): Promise<RestartClearanceResult> => ({
      cleared: true,
      restart_clearance_required: false,
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
  });
});
