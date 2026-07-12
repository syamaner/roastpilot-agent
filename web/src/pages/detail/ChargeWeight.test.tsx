import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { ChargeWeight } from "./ChargeWeight";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

afterEach(() => vi.restoreAllMocks());

describe("ChargeWeight (#520)", () => {
  it("shows only the frozen charge and no correction when never corrected", () => {
    render(
      <ChargeWeight
        runId="r1"
        frozenChargeGrams={250}
        correctedChargeGrams={null}
        roastedWeightGrams={221}
        weightLossPercent={11.6}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.getByTestId("charge-weight-frozen")).toHaveTextContent("250 g");
    expect(screen.queryByTestId("charge-weight-corrected")).not.toBeInTheDocument();
    // The frozen value drives the % when uncorrected — explicit, not implied.
    expect(screen.getByTestId("charge-weight-driving")).toHaveTextContent("250 g");
    expect(screen.getByTestId("charge-weight-input")).toHaveValue(null);
  });

  it("shows BOTH the frozen and corrected charge, with the corrected value driving the % (roast 13's worked example)", () => {
    render(
      <ChargeWeight
        runId="r1"
        frozenChargeGrams={250}
        correctedChargeGrams={255}
        roastedWeightGrams={223}
        weightLossPercent={12.55}
      />,
      { wrapper: wrapper() },
    );
    // Both values visible — never a silent swap of the frozen number.
    expect(screen.getByTestId("charge-weight-frozen")).toHaveTextContent("250 g");
    expect(screen.getByTestId("charge-weight-corrected")).toHaveTextContent("255 g");
    expect(screen.getByTestId("charge-weight-driving")).toHaveTextContent("255 g");
    expect(screen.getByTestId("charge-weight-loss")).toHaveTextContent("12.6%");
    expect(screen.getByTestId("charge-weight-input")).toHaveValue(255);
  });

  it("saves the entered correction via api.setChargeWeight", async () => {
    const spy = vi
      .spyOn(api, "setChargeWeight")
      .mockResolvedValue({ id: "r1" } as Awaited<ReturnType<typeof api.setChargeWeight>>);
    render(
      <ChargeWeight
        runId="r1"
        frozenChargeGrams={250}
        correctedChargeGrams={null}
        roastedWeightGrams={null}
        weightLossPercent={null}
      />,
      { wrapper: wrapper() },
    );
    fireEvent.change(screen.getByTestId("charge-weight-input"), { target: { value: "255" } });
    fireEvent.click(screen.getByTestId("charge-weight-save"));
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith("r1", { corrected_charge_grams: 255 }),
    );
    await waitFor(() => expect(screen.getByTestId("charge-weight-saved")).toBeInTheDocument());
  });

  it("disables save until a positive correction is entered", () => {
    render(
      <ChargeWeight
        runId="r1"
        frozenChargeGrams={250}
        correctedChargeGrams={null}
        roastedWeightGrams={null}
        weightLossPercent={null}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.getByTestId("charge-weight-save")).toBeDisabled();
    fireEvent.change(screen.getByTestId("charge-weight-input"), { target: { value: "0" } });
    expect(screen.getByTestId("charge-weight-save")).toBeDisabled();
    fireEvent.change(screen.getByTestId("charge-weight-input"), { target: { value: "255" } });
    expect(screen.getByTestId("charge-weight-save")).toBeEnabled();
  });

  it("disables save and shows a hint when the correction is below the roasted-out weight", () => {
    render(
      <ChargeWeight
        runId="r1"
        frozenChargeGrams={250}
        correctedChargeGrams={null}
        roastedWeightGrams={221}
        weightLossPercent={11.6}
      />,
      { wrapper: wrapper() },
    );
    fireEvent.change(screen.getByTestId("charge-weight-input"), { target: { value: "200" } });
    expect(screen.getByTestId("charge-weight-save")).toBeDisabled();
    expect(screen.getByTestId("charge-weight-invalid")).toBeInTheDocument();
  });

  it("allows any positive correction when no roasted weight has been entered yet", () => {
    render(
      <ChargeWeight
        runId="r1"
        frozenChargeGrams={250}
        correctedChargeGrams={null}
        roastedWeightGrams={null}
        weightLossPercent={null}
      />,
      { wrapper: wrapper() },
    );
    fireEvent.change(screen.getByTestId("charge-weight-input"), { target: { value: "5" } });
    expect(screen.getByTestId("charge-weight-save")).toBeEnabled();
    expect(screen.queryByTestId("charge-weight-invalid")).not.toBeInTheDocument();
  });

  it("surfaces a save error", async () => {
    vi.spyOn(api, "setChargeWeight").mockRejectedValue(new Error("nope"));
    render(
      <ChargeWeight
        runId="r1"
        frozenChargeGrams={250}
        correctedChargeGrams={255}
        roastedWeightGrams={223}
        weightLossPercent={12.55}
      />,
      { wrapper: wrapper() },
    );
    fireEvent.click(screen.getByTestId("charge-weight-save"));
    await waitFor(() => expect(screen.getByTestId("charge-weight-error")).toBeInTheDocument());
  });
});
