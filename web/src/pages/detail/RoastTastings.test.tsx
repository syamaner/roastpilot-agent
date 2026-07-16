import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import type { RoastDetail, TastingList } from "@/lib/types";
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

/** api.rate resolves to the full RoastDetail; only `id` matters to callers here. */
function fakeRatedDetail(runId = "r1"): RoastDetail {
  return { id: runId } as RoastDetail;
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

  it("shows the grind note even when brew_method is null (#522 Codex P2): the two fields render independently, not gated on brew_method alone", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue({
      run_id: "r1",
      tastings: [
        {
          id: 2,
          tasted_at_utc: null,
          recorded_at_utc: "2026-07-12T18:05:00+00:00",
          stars: 4,
          notes: null,
          brew_method: null,
          grind_note: "medium-fine, 22g/380g",
          attributes: [],
          defects: [],
        },
      ],
    });
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });

    await waitFor(() => expect(screen.getByTestId("tasting-entry-2")).toBeInTheDocument());
    expect(screen.getByTestId("tasting-entry-2")).toHaveTextContent("medium-fine, 22g/380g");
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
    vi.spyOn(api, "rate").mockResolvedValue(fakeRatedDetail());
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
    vi.spyOn(api, "rate").mockResolvedValue(fakeRatedDetail());
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
    vi.spyOn(api, "rate").mockResolvedValue(fakeRatedDetail());
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

  it("#533: disables every input (not just the save button) while a save is in flight, so a mid-save draft cannot be typed then silently wiped", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    let resolveSave: (() => void) | undefined;
    vi.spyOn(api, "addTasting").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = () => resolve({ run_id: "r1", tastings: [] });
        }),
    );
    vi.spyOn(api, "rate").mockResolvedValue(fakeRatedDetail());
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("tasting-star-3"));
    fireEvent.click(screen.getByTestId("tasting-save"));

    // The save is now in flight (stalled) — every input must be disabled,
    // not just the save button, so the operator cannot draft a NEW entry
    // that this save's onSuccess would then silently wipe.
    await waitFor(() => expect(screen.getByTestId("tasting-save")).toHaveTextContent(/saving/i));
    expect(screen.getByTestId("tasting-notes")).toBeDisabled();
    expect(screen.getByTestId("tasting-tasted-at")).toBeDisabled();
    expect(screen.getByTestId("tasting-brew-method")).toBeDisabled();
    expect(screen.getByTestId("tasting-grind-note")).toBeDisabled();
    expect(screen.getByTestId("tasting-star-4")).toBeDisabled();
    expect(screen.getByTestId("tasting-attribute-sweetness")).toBeDisabled();
    expect(screen.getByTestId("tasting-defect-bitter")).toBeDisabled();

    resolveSave?.();
    await waitFor(() => expect(screen.getByTestId("tasting-saved")).toBeInTheDocument());
    // Once the save settles, the form (now reset) is editable again.
    expect(screen.getByTestId("tasting-notes")).not.toBeDisabled();
  });

  it("disables save until a star rating is chosen", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());
    expect(screen.getByTestId("tasting-save")).toBeDisabled();
  });

  it("surfaces a save error when BOTH records fail to save", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
    vi.spyOn(api, "addTasting").mockRejectedValue(new Error("nope"));
    vi.spyOn(api, "rate").mockRejectedValue(new Error("nope"));
    render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
    await waitFor(() => expect(api.tastings).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("tasting-star-1"));
    fireEvent.click(screen.getByTestId("tasting-save"));
    await waitFor(() => expect(screen.getByTestId("tasting-error")).toBeInTheDocument());
    expect(screen.getByTestId("tasting-error")).toHaveTextContent("Save failed");
  });

  describe("#566: one gesture updates both the tasting corpus and the headline rating", () => {
    it("fires both api.addTasting and api.rate from a single save, seeded with the tasting's own stars/notes", async () => {
      vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
      const addSpy = vi
        .spyOn(api, "addTasting")
        .mockResolvedValue({ run_id: "r1", tastings: [] });
      const rateSpy = vi.spyOn(api, "rate").mockResolvedValue(fakeRatedDetail());
      render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
      await waitFor(() => expect(api.tastings).toHaveBeenCalled());

      fireEvent.click(screen.getByTestId("tasting-star-4"));
      fireEvent.change(screen.getByTestId("tasting-notes"), { target: { value: "great cup" } });
      fireEvent.click(screen.getByTestId("tasting-save"));

      await waitFor(() =>
        expect(addSpy).toHaveBeenCalledWith(
          "r1",
          expect.objectContaining({ stars: 4, notes: "great cup" }),
        ),
      );
      await waitFor(() =>
        expect(rateSpy).toHaveBeenCalledWith("r1", { stars: 4, notes: "great cup" }),
      );
      await waitFor(() => expect(screen.getByTestId("tasting-saved")).toBeInTheDocument());
    });

    it("surfaces a partial failure honestly when the tasting saves but the rating update fails — never a false 'Saved.'", async () => {
      vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
      vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: "r1", tastings: [] });
      vi.spyOn(api, "rate").mockRejectedValue(new Error("rating endpoint down"));
      render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
      await waitFor(() => expect(api.tastings).toHaveBeenCalled());

      fireEvent.click(screen.getByTestId("tasting-star-5"));
      fireEvent.click(screen.getByTestId("tasting-save"));

      await waitFor(() =>
        expect(screen.getByTestId("rating-partial-error")).toBeInTheDocument(),
      );
      // Points the operator at the real recovery path: RoastRating's own
      // "Edit" affordance (the tasting form's own draft is gone — the
      // tasting itself is append-only and already saved, so it must not be
      // resubmitted; a direct rating retry belongs on RoastRating).
      expect(screen.getByTestId("rating-partial-error")).toHaveTextContent("Edit");
      expect(screen.queryByTestId("tasting-saved")).not.toBeInTheDocument();
      expect(screen.queryByTestId("tasting-error")).not.toBeInTheDocument();
    });

    it("surfaces a partial failure honestly when the rating saves but the tasting fails — never a false 'Saved.'", async () => {
      vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
      vi.spyOn(api, "addTasting").mockRejectedValue(new Error("tastings endpoint down"));
      vi.spyOn(api, "rate").mockResolvedValue(fakeRatedDetail());
      render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
      await waitFor(() => expect(api.tastings).toHaveBeenCalled());

      fireEvent.click(screen.getByTestId("tasting-star-2"));
      fireEvent.click(screen.getByTestId("tasting-save"));

      await waitFor(() => expect(screen.getByTestId("tasting-error")).toBeInTheDocument());
      expect(screen.getByTestId("tasting-error")).toHaveTextContent("Rating saved");
      expect(screen.queryByTestId("tasting-saved")).not.toBeInTheDocument();
      expect(screen.queryByTestId("rating-partial-error")).not.toBeInTheDocument();
    });

    it("is disabled while EITHER mutation is pending, not just the tasting save", async () => {
      vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
      vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: "r1", tastings: [] });
      let resolveRate: (() => void) | undefined;
      vi.spyOn(api, "rate").mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveRate = () => resolve(fakeRatedDetail());
          }),
      );
      render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
      await waitFor(() => expect(api.tastings).toHaveBeenCalled());

      fireEvent.click(screen.getByTestId("tasting-star-3"));
      fireEvent.click(screen.getByTestId("tasting-save"));

      // The tasting write resolves quickly; the rating write is still in
      // flight — the form must stay locked (and unsaved) until BOTH settle.
      await waitFor(() => expect(screen.getByTestId("tasting-save")).toHaveTextContent(/saving/i));
      expect(screen.queryByTestId("tasting-saved")).not.toBeInTheDocument();

      resolveRate?.();
      await waitFor(() => expect(screen.getByTestId("tasting-saved")).toBeInTheDocument());
    });

    it("#568 Codex (PRRT_kwDOSzMG_c6RdllH): never claims a record 'saved' while the OTHER mutation is still pending — settled success is required, not merely 'hasn't failed yet'", async () => {
      vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
      // The tasting write resolves immediately; the rating write stalls —
      // `!ratingFailed` is true THIS WHOLE TIME the rating is pending, so a
      // naive "not failed" gate would wrongly show "Rating saved" before the
      // rating mutation has actually settled.
      vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: "r1", tastings: [] });
      let resolveRate: (() => void) | undefined;
      vi.spyOn(api, "rate").mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveRate = () => resolve(fakeRatedDetail());
          }),
      );
      render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
      await waitFor(() => expect(api.tastings).toHaveBeenCalled());

      fireEvent.click(screen.getByTestId("tasting-star-3"));
      fireEvent.click(screen.getByTestId("tasting-save"));

      // The tasting mutation has settled (success); the rating mutation is
      // still in flight. No "X saved" claim of ANY kind may render yet.
      await waitFor(() => expect(screen.getByTestId("tasting-save")).toHaveTextContent(/saving/i));
      expect(screen.queryByTestId("tasting-saved")).not.toBeInTheDocument();
      expect(screen.queryByTestId("tasting-error")).not.toBeInTheDocument();
      expect(screen.queryByTestId("rating-partial-error")).not.toBeInTheDocument();

      resolveRate?.();
      await waitFor(() => expect(screen.getByTestId("tasting-saved")).toBeInTheDocument());
    });

    it("#568 Codex (PRRT_kwDOSzMG_c6RdxDJ): preserves the star rating + notes draft when the tasting succeeds but the rating write fails, so the operator's attempted values aren't lost", async () => {
      vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
      vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: "r1", tastings: [] });
      vi.spyOn(api, "rate").mockRejectedValue(new Error("rating endpoint down"));
      render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
      await waitFor(() => expect(api.tastings).toHaveBeenCalled());

      fireEvent.click(screen.getByTestId("tasting-star-5"));
      fireEvent.change(screen.getByTestId("tasting-notes"), { target: { value: "sweet, floral" } });
      fireEvent.click(screen.getByTestId("tasting-save"));

      await waitFor(() => expect(screen.getByTestId("rating-partial-error")).toBeInTheDocument());
      // The draft must survive this partial failure — the tasting record
      // itself is append-only and already safely saved, so nothing is lost
      // by NOT clearing the form; clearing it here would strand the
      // attempted stars/notes with no way to recover them (RoastRating's
      // "Edit" pre-fills from the OLD persisted rating, not these values).
      expect(screen.getByTestId("tasting-star-5")).toHaveAttribute("data-filled", "true");
      expect(screen.getByTestId("tasting-notes")).toHaveValue("sweet, floral");
    });

    it("a retry after a rating-only partial failure resubmits ONLY the rating, never a duplicate tasting entry", async () => {
      vi.spyOn(api, "tastings").mockResolvedValue(emptyList());
      const addSpy = vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: "r1", tastings: [] });
      const rateSpy = vi
        .spyOn(api, "rate")
        .mockRejectedValueOnce(new Error("rating endpoint down"))
        .mockResolvedValueOnce(fakeRatedDetail());
      render(<RoastTastings runId="r1" />, { wrapper: wrapper() });
      await waitFor(() => expect(api.tastings).toHaveBeenCalled());

      fireEvent.click(screen.getByTestId("tasting-star-4"));
      fireEvent.click(screen.getByTestId("tasting-save"));
      await waitFor(() => expect(screen.getByTestId("rating-partial-error")).toBeInTheDocument());
      expect(addSpy).toHaveBeenCalledTimes(1);

      // Retry from the SAME (preserved) draft.
      fireEvent.click(screen.getByTestId("tasting-save"));
      await waitFor(() => expect(screen.getByTestId("tasting-saved")).toBeInTheDocument());

      // The tasting was NOT resubmitted — still exactly one call — while the
      // rating retried and succeeded.
      expect(addSpy).toHaveBeenCalledTimes(1);
      expect(rateSpy).toHaveBeenCalledTimes(2);
    });
  });
});
