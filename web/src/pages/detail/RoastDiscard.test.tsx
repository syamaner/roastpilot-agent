import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { RoastDiscard } from "./RoastDiscard";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

afterEach(() => vi.restoreAllMocks());

describe("RoastDiscard (#582)", () => {
  it("shows the Discard action (no indicator) for an included run", () => {
    render(<RoastDiscard runId="r1" excluded={false} />, { wrapper: wrapper() });
    expect(screen.getByTestId("roast-discard-button")).toBeInTheDocument();
    expect(screen.queryByTestId("roast-discard-indicator")).not.toBeInTheDocument();
    expect(screen.queryByTestId("roast-restore-button")).not.toBeInTheDocument();
  });

  it("requires a confirm step before posting the discard", async () => {
    const spy = vi
      .spyOn(api, "discardRoast")
      .mockResolvedValue({ id: "r1", excluded: true } as Awaited<
        ReturnType<typeof api.discardRoast>
      >);
    render(<RoastDiscard runId="r1" excluded={false} />, { wrapper: wrapper() });

    // The API is not called until the confirm step is clicked.
    fireEvent.click(screen.getByTestId("roast-discard-button"));
    expect(spy).not.toHaveBeenCalled();
    expect(screen.getByTestId("roast-discard-confirm")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("roast-discard-confirm"));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1"));
  });

  it("cancel dismisses the confirm step without posting", () => {
    const spy = vi.spyOn(api, "discardRoast");
    render(<RoastDiscard runId="r1" excluded={false} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("roast-discard-button"));
    fireEvent.click(screen.getByTestId("roast-discard-cancel"));
    expect(screen.queryByTestId("roast-discard-confirm")).not.toBeInTheDocument();
    expect(screen.getByTestId("roast-discard-button")).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it("surfaces a discard save error", async () => {
    vi.spyOn(api, "discardRoast").mockRejectedValue(new Error("nope"));
    render(<RoastDiscard runId="r1" excluded={false} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("roast-discard-button"));
    fireEvent.click(screen.getByTestId("roast-discard-confirm"));
    await waitFor(() => expect(screen.getByTestId("roast-discard-error")).toBeInTheDocument());
  });

  it("shows the discarded indicator + a one-click restore for an excluded run", async () => {
    const spy = vi
      .spyOn(api, "restoreRoast")
      .mockResolvedValue({ id: "r1", excluded: false } as Awaited<
        ReturnType<typeof api.restoreRoast>
      >);
    render(<RoastDiscard runId="r1" excluded={true} />, { wrapper: wrapper() });
    expect(screen.getByTestId("roast-discard-indicator")).toHaveTextContent("Discarded");
    expect(screen.queryByTestId("roast-discard-button")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("roast-restore-button"));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1"));
  });

  it("surfaces a restore save error", async () => {
    vi.spyOn(api, "restoreRoast").mockRejectedValue(new Error("nope"));
    render(<RoastDiscard runId="r1" excluded={true} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("roast-restore-button"));
    await waitFor(() => expect(screen.getByTestId("roast-discard-error")).toBeInTheDocument());
  });
});
