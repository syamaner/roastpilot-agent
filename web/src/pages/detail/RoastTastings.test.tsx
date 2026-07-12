import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { TastingList } from "@/lib/types";
import { RoastTastings } from "./RoastTastings";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function emptyList(runId = "r1"): TastingList {
  return { run_id: runId, tastings: [] };
}

afterEach(() => vi.restoreAllMocks());

describe("RoastTastings", () => {
  it("renders no entries and a blank form when the run has no tastings yet", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });

    await waitFor(() => expect(api.tastings).toHaveBeenCalledWith("r1"));
    expect(screen.queryByTestId("tasting-entries")).not.toBeInTheDocument();
    expect(screen.getByTestId("tasting-save")).toBeDisabled();
  });

  it("renders persisted tasting entries", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue({
      run_id: "r1",
      tastings: [
        {
          id: 1,
          tasted_at_utc: "2026-07-12T18:00:00+00:00",
          recorded_at_utc: "2026-07-12T18:05:00+00:00",
          stars: 2,
          notes: "flat",
          brew_method: null,
          grind_note: null,
          attributes: [],
          defects: ["flat"],
        },
      ],
    });
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });

    await waitFor(() => expect(screen.getByTestId("tasting-entry-1")).toBeInTheDocument());
    expect(screen.getByTestId("tasting-entry-1")).toHaveTextContent("flat");
  });

  it("saves a stars-only entry (every other field optional)", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    const addSpy = vi.spyOn(api, "addTasting").mockResolvedValue({
      run_id: "r1",
      tastings: [
        {
          id: 1,
          tasted_at_utc: null,
          recorded_at_utc: "now",
          stars: 4,
          notes: null,
          brew_method: null,
          grind_note: null,
          attributes: [],
          defects: [],
        },
      ],
    });
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("tasting-star-4"));
    fireEvent.click(screen.getByTestId("tasting-save"));

    await waitFor(() =>
      expect(addSpy).toHaveBeenCalledWith("r1", {
        stars: 4,
        notes: null,
        tasted_at_utc: null,
        brew_method: null,
        grind_note: null,
        attributes: [],
        defects: [],
      }),
    );
    await waitFor(() => expect(screen.getByTestId("tasting-saved")).toBeInTheDocument());
  });

  it("submits brew context and attribute/defect tags", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    const addSpy = vi
      .spyOn(api, "addTasting")
      .mockResolvedValue({ run_id: "r1", tastings: [] });
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("tasting-star-5"));
    fireEvent.change(screen.getByTestId("tasting-notes"), { target: { value: " sweet " } });
    fireEvent.change(screen.getByTestId("tasting-brew-method"), {
      target: { value: "pour_over" },
    });
    fireEvent.change(screen.getByTestId("tasting-grind-note"), {
      target: { value: " medium " },
    });
    fireEvent.click(screen.getByTestId("tasting-attribute-sweetness"));
    fireEvent.click(screen.getByTestId("tasting-defect-bitter"));
    fireEvent.click(screen.getByTestId("tasting-save"));

    await waitFor(() =>
      expect(addSpy).toHaveBeenCalledWith(
        "r1",
        expect.objectContaining({
          stars: 5,
          notes: "sweet",
          brew_method: "pour_over",
          grind_note: "medium",
          attributes: ["sweetness"],
          defects: ["bitter"],
        }),
      ),
    );
  });

  it("resets the form after a successful save", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: "r1", tastings: [] });
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("tasting-star-3"));
    fireEvent.change(screen.getByTestId("tasting-notes"), { target: { value: "draft" } });
    fireEvent.click(screen.getByTestId("tasting-save"));

    await waitFor(() => expect(screen.getByTestId("tasting-saved")).toBeInTheDocument());
    expect(screen.getByTestId("tasting-notes")).toHaveValue("");
    expect(screen.getByTestId("tasting-star-3")).toHaveAttribute("data-filled", "false");
    expect(screen.getByTestId("tasting-save")).toBeDisabled();
  });

  it("disables save until a star rating is chosen", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());
    expect(screen.getByTestId("tasting-save")).toBeDisabled();
  });

  it("surfaces a save error", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    vi.spyOn(api, "addTasting").mockRejectedValue(new Error("nope"));
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("tasting-star-1"));
    fireEvent.click(screen.getByTestId("tasting-save"));
    await waitFor(() => expect(screen.getByTestId("tasting-error")).toBeInTheDocument());
  });
});
