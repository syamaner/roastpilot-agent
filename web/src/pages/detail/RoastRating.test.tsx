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

describe("RoastRating (#566: read-only headline + edit affordance)", () => {
  it("renders a read-only headline from the persisted rating and notes, with no edit form visible", () => {
    render(<RoastRating runId="r1" rating={4} notes="bright" />, { wrapper: wrapper() });
    const headline = screen.getByTestId("rating-headline");
    expect(headline).toHaveTextContent("★★★★");
    expect(headline).toHaveTextContent("bright");
    expect(screen.queryByTestId("star-4")).not.toBeInTheDocument();
    expect(screen.queryByTestId("rating-notes")).not.toBeInTheDocument();
    expect(screen.getByTestId("rating-edit")).toBeInTheDocument();
  });

  it("shows a 'not yet rated' placeholder when there is no persisted rating", () => {
    render(<RoastRating runId="r1" rating={null} notes={null} />, { wrapper: wrapper() });
    expect(screen.getByTestId("rating-headline")).toHaveTextContent("Not yet rated.");
  });

  it("reveals the editable form on 'Edit', pre-filled from the persisted values", () => {
    render(<RoastRating runId="r1" rating={4} notes="bright" />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("rating-edit"));

    expect(screen.queryByTestId("rating-headline")).not.toBeInTheDocument();
    expect(screen.getByTestId("star-4")).toHaveAttribute("data-filled", "true");
    expect(screen.getByTestId("star-5")).toHaveAttribute("data-filled", "false");
    expect(screen.getByTestId("rating-notes")).toHaveValue("bright");
  });

  it("saves a direct edit via api.rate and returns to the read-only headline", async () => {
    const spy = vi
      .spyOn(api, "rate")
      .mockResolvedValue({ id: "r1" } as Awaited<ReturnType<typeof api.rate>>);
    render(<RoastRating runId="r1" rating={null} notes={null} />, { wrapper: wrapper() });

    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("star-3"));
    fireEvent.change(screen.getByTestId("rating-notes"), { target: { value: " nice " } });
    fireEvent.click(screen.getByTestId("rating-save"));

    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1", { stars: 3, notes: "nice" }));
    await waitFor(() => expect(screen.queryByTestId("star-3")).not.toBeInTheDocument());
  });

  it("normalizes empty notes to null", async () => {
    const spy = vi
      .spyOn(api, "rate")
      .mockResolvedValue({ id: "r1" } as Awaited<ReturnType<typeof api.rate>>);
    render(<RoastRating runId="r1" rating={2} notes={null} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("rating-save"));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("r1", { stars: 2, notes: null }));
  });

  it("disables save until a rating is chosen", () => {
    render(<RoastRating runId="r1" rating={null} notes={null} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("rating-edit"));
    expect(screen.getByTestId("rating-save")).toBeDisabled();
  });

  it("cancel discards the draft and returns to the read-only headline unchanged", () => {
    render(<RoastRating runId="r1" rating={4} notes="bright" />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("star-1"));
    fireEvent.change(screen.getByTestId("rating-notes"), { target: { value: "changed" } });

    fireEvent.click(screen.getByTestId("rating-cancel"));

    expect(screen.queryByTestId("star-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("rating-headline")).toHaveTextContent("★★★★");
    expect(screen.getByTestId("rating-headline")).toHaveTextContent("bright");
  });

  it("surfaces a save error and stays in edit mode (draft not lost)", async () => {
    vi.spyOn(api, "rate").mockRejectedValue(new Error("nope"));
    render(<RoastRating runId="r1" rating={5} notes={null} />, { wrapper: wrapper() });
    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("rating-save"));
    await waitFor(() => expect(screen.getByTestId("rating-error")).toBeInTheDocument());
    // Still in edit mode — the failed draft is not silently discarded.
    expect(screen.getByTestId("rating-save")).toBeInTheDocument();
  });

  it("re-syncs from a later prop change (e.g. a tasting save updating the headline) and drops out of edit mode", () => {
    const { rerender } = render(<RoastRating runId="r1" rating={null} notes={null} />, {
      wrapper: wrapper(),
    });
    fireEvent.click(screen.getByTestId("rating-edit"));
    expect(screen.getByTestId("star-3")).toBeInTheDocument();

    rerender(<RoastRating runId="r1" rating={5} notes="from a tasting" />);

    expect(screen.queryByTestId("star-3")).not.toBeInTheDocument();
    expect(screen.getByTestId("rating-headline")).toHaveTextContent("★★★★★");
    expect(screen.getByTestId("rating-headline")).toHaveTextContent("from a tasting");
  });
});
