import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { RoastedWeight } from "./RoastedWeight";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

afterEach(() => vi.restoreAllMocks());

describe("RoastedWeight (#388)", () => {
  it("pre-fills the persisted roasted weight and shows the derived loss %", () => {
    render(
      <RoastedWeight
        runId="r1"
        chargeWeightGrams={250}
        roastedWeightGrams={221}
        weightLossPercent={11.6}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.getByTestId("roasted-weight-input")).toHaveValue(221);
    expect(screen.getByTestId("weight-loss")).toHaveTextContent("11.6%");
  });

  it("shows a not-weighed state when no roasted weight is set", () => {
    render(
      <RoastedWeight
        runId="r1"
        chargeWeightGrams={250}
        roastedWeightGrams={null}
        weightLossPercent={null}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.getByTestId("roasted-weight-input")).toHaveValue(null);
    expect(screen.getByTestId("weight-loss")).toHaveTextContent("not yet weighed");
  });

  it("saves the entered weight via api.setRoastedWeight", async () => {
    const spy = vi
      .spyOn(api, "setRoastedWeight")
      .mockResolvedValue({ id: "r1" } as Awaited<ReturnType<typeof api.setRoastedWeight>>);
    render(
      <RoastedWeight
        runId="r1"
        chargeWeightGrams={250}
        roastedWeightGrams={null}
        weightLossPercent={null}
      />,
      { wrapper: wrapper() },
    );
    fireEvent.change(screen.getByTestId("roasted-weight-input"), { target: { value: "221" } });
    fireEvent.click(screen.getByTestId("roasted-weight-save"));
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith("r1", { roasted_weight_grams: 221 }),
    );
    await waitFor(() => expect(screen.getByTestId("roasted-weight-saved")).toBeInTheDocument());
  });

  it("disables save until a positive weight is entered", () => {
    render(
      <RoastedWeight
        runId="r1"
        chargeWeightGrams={250}
        roastedWeightGrams={null}
        weightLossPercent={null}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.getByTestId("roasted-weight-save")).toBeDisabled();
    fireEvent.change(screen.getByTestId("roasted-weight-input"), { target: { value: "0" } });
    expect(screen.getByTestId("roasted-weight-save")).toBeDisabled();
    fireEvent.change(screen.getByTestId("roasted-weight-input"), { target: { value: "221" } });
    expect(screen.getByTestId("roasted-weight-save")).toBeEnabled();
  });

  it("surfaces a save error", async () => {
    vi.spyOn(api, "setRoastedWeight").mockRejectedValue(new Error("nope"));
    render(
      <RoastedWeight
        runId="r1"
        chargeWeightGrams={250}
        roastedWeightGrams={221}
        weightLossPercent={11.6}
      />,
      { wrapper: wrapper() },
    );
    fireEvent.click(screen.getByTestId("roasted-weight-save"));
    await waitFor(() => expect(screen.getByTestId("roasted-weight-error")).toBeInTheDocument());
  });
});
