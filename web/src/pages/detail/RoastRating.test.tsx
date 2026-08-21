import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { RoastRating } from "./RoastRating";
import { __resetPartialFailureLocksForTests } from "./useSaveRating";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

/** Asserts the EXACT `StarGlyphs` text (not a substring match — a naive
 *  `toHaveTextContent("★★★★")` also passes for "★★★★☆", so this reads the
 *  glyph run's own `textContent` directly and compares it for equality). */
function expectStarGlyphsText(container: HTMLElement, expected: string): void {
  expect(within(container).getByTestId("star-glyphs").textContent).toBe(expected);
}

afterEach(() => {
  vi.restoreAllMocks();
  // Defensive: this file only READS the module-scoped partial-failure lock
  // (#568 round 5), never sets it, but resetting keeps every spec in this
  // suite starting from a clean, known state regardless.
  __resetPartialFailureLocksForTests();
});

describe("RoastRating (#566: read-only headline + edit affordance)", () => {
  it("renders a read-only headline from the persisted rating and notes, with no edit form visible", () => {
    render(<RoastRating runId="r1" rating={4} notes="bright" />, { wrapper: wrapper() });
    const headline = screen.getByTestId("rating-headline");
    expectStarGlyphsText(headline, "★★★★☆");
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
    const headline = screen.getByTestId("rating-headline");
    expectStarGlyphsText(headline, "★★★★☆");
    expect(headline).toHaveTextContent("bright");
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

  it("#568 Codex (P3, PRRT_kwDOSzMG_c6RdxDY): reopening the editor after a failed save does not flash the PRIOR attempt's status", async () => {
    vi.spyOn(api, "rate").mockRejectedValue(new Error("nope"));
    render(<RoastRating runId="r1" rating={4} notes="bright" />, { wrapper: wrapper() });

    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("rating-save"));
    await waitFor(() => expect(screen.getByTestId("rating-error")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("rating-cancel"));
    fireEvent.click(screen.getByTestId("rating-edit"));

    // The reopened editor must not carry over the PREVIOUS attempt's error —
    // this is a fresh session, no save has been attempted yet.
    expect(screen.queryByTestId("rating-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("rating-saved")).not.toBeInTheDocument();
  });

  it("#568 Codex (P3, PRRT_kwDOSzMG_c6RdxDY): reopening the editor after a SUCCESSFUL save does not flash a stale 'Saved.'", async () => {
    vi.spyOn(api, "rate").mockResolvedValue({ id: "r1" } as Awaited<ReturnType<typeof api.rate>>);
    const { rerender } = render(<RoastRating runId="r1" rating={4} notes="bright" />, {
      wrapper: wrapper(),
    });

    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("rating-save"));
    // The editor auto-closes to the read-only headline on success; simulate
    // the parent NOT yet re-rendering with fresh props (the exact stale
    // window #568's other fix closes at the cache layer — here we're
    // specifically probing the mutation object's own carried-over status).
    await waitFor(() => expect(screen.queryByTestId("rating-headline")).toBeInTheDocument());
    rerender(<RoastRating runId="r1" rating={4} notes="bright" />);

    fireEvent.click(screen.getByTestId("rating-edit"));
    expect(screen.queryByTestId("rating-saved")).not.toBeInTheDocument();
    expect(screen.queryByTestId("rating-error")).not.toBeInTheDocument();
  });

  it("re-syncs from a later prop change (e.g. a tasting save updating the headline) and drops out of edit mode", () => {
    const { rerender } = render(<RoastRating runId="r1" rating={null} notes={null} />, {
      wrapper: wrapper(),
    });
    fireEvent.click(screen.getByTestId("rating-edit"));
    expect(screen.getByTestId("star-3")).toBeInTheDocument();

    rerender(<RoastRating runId="r1" rating={5} notes="from a tasting" />);

    expect(screen.queryByTestId("star-3")).not.toBeInTheDocument();
    const headline = screen.getByTestId("rating-headline");
    expectStarGlyphsText(headline, "★★★★★");
    expect(headline).toHaveTextContent("from a tasting");
  });

  it("renders exactly ★★★☆☆ for a rating of 3", () => {
    render(<RoastRating runId="r1" rating={3} notes={null} />, { wrapper: wrapper() });
    expectStarGlyphsText(screen.getByTestId("rating-headline"), "★★★☆☆");
  });

  it("passes an out-of-range persisted rating (7) straight through to StarGlyphs, which saturates it to ★★★★★ — the caller must not pre-clamp an out-of-range value to zero (that would be a second, inconsistent normalization boundary vs RoastTastings' own direct pass-through)", () => {
    render(<RoastRating runId="r1" rating={7} notes={null} />, { wrapper: wrapper() });
    const headline = screen.getByTestId("rating-headline");
    expectStarGlyphsText(headline, "★★★★★");
    expect(within(headline).getByTestId("star-glyphs")).toHaveAttribute(
      "aria-label",
      "5 of 5 stars",
    );
  });
});
