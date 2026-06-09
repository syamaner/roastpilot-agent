import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { RoastRating } from "./RoastRating";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

afterEach(() => vi.restoreAllMocks());

describe("RoastRating", () => {
  it("pre-fills the persisted rating and notes", () => {
    render(<RoastRating runId="r1" rating={4} notes="bright" />, { wrapper: wrapper() });
    expect(screen.getByTestId("star-4")).toHaveAttribute("data-filled", "true");
    expect(screen.getByTestId("star-5")).toHaveAttribute("data-filled", "false");
    expect(screen.getByTestId("rating-notes")).toHaveValue("bright");
  });

  it("saves the selected stars + notes via api.rate", async () => {
    const spy = vi
      .spyOn(api, "rate")
      .mockResolvedValue({ id: "r1" } as Awaited<ReturnType<typeof api.rate>>);
    render(<RoastRating runId="r1" rating={null} notes={null} />, { wrapper: wrapper() });

    fireEvent.click(screen.getByTestId("star-3"));
    fireEvent.change(screen.getByTestId("rating-notes"), { target: { value: " nice " } });
    fireEvent.click(screen.getByTestId("rating-save"));

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith("r1", { stars: 3, notes: "nice" }),
    );
    await waitFor(() => expect(screen.getByTestId("rating-saved")).toBeInTheDocument());
  });

  it("normalizes empty notes to null", async () => {
    const spy = vi
      .spyOn(api, "rate")
      .mockResolvedValue({ id: "r1" } as Awaited<ReturnType<typeof api.rate>>);
    render(<RoastRating runId="r1" rating={2} notes={null} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("rating-save"));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1", { stars: 2, notes: null }));
  });

  it("disables save until a rating is chosen", () => {
    render(<RoastRating runId="r1" rating={null} notes={null} />, { wrapper: wrapper() });
    expect(screen.getByTestId("rating-save")).toBeDisabled();
  });

  it("surfaces a save error", async () => {
    vi.spyOn(api, "rate").mockRejectedValue(new Error("nope"));
    render(<RoastRating runId="r1" rating={5} notes={null} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("rating-save"));
    await waitFor(() => expect(screen.getByTestId("rating-error")).toBeInTheDocument());
  });
});
