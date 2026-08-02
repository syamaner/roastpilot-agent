import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { roastKeys } from "@/hooks/queries";
import { DetailView } from "./DetailView";
import {
  FIXTURE_DETAIL,
  FIXTURE_DETAIL_FAILED,
  FIXTURE_DETAIL_LONG,
  FIXTURE_TELEMETRY,
  FIXTURE_TELEMETRY_FAILED,
  FIXTURE_TELEMETRY_LONG,
  FIXTURE_TIMELINE,
  FIXTURE_TIMELINE_FAILED,
  FIXTURE_TIMELINE_LONG,
} from "./fixture";
import { __resetPartialFailureLocksForTests } from "./useSaveRating";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function renderView() {
  return render(
    <DetailView
      detail={FIXTURE_DETAIL}
      telemetry={FIXTURE_TELEMETRY}
      timeline={FIXTURE_TIMELINE}
    />,
    { wrapper: wrapper() },
  );
}

/** The decision-trace table's row for a verdict (scoped, since the advisor
 * timeline now also renders verdict badges on the page — #170). */
function traceRow(verdict: string): HTMLElement {
  const table = screen.getByTestId("decision-trace-table");
  return within(table)
    .getByText(verdict)
    .closest("tr")!;
}

describe("DetailView trace-row → curve highlight", () => {
  it("highlights the row's timestamp on the shared LiveCurve, and toggles off on re-click", () => {
    renderView();
    // The shared LiveCurve exposes highlightTime on the window.__chart hook (D24)
    // — we assert the cross-component wiring through the REAL chart, not a stub.
    expect(window.__chart?.highlightTime).toBeNull();

    // The CLAMP row is tick 8 → 240 s in the fixture telemetry.
    const clampRow = traceRow("CLAMP");
    fireEvent.click(clampRow);
    expect(window.__chart?.highlightTime).toBe(240);
    expect(clampRow).toHaveAttribute("data-selected", "true");

    // Re-clicking the same row clears the highlight (toggle-off on re-click).
    fireEvent.click(clampRow);
    expect(window.__chart?.highlightTime).toBeNull();
    expect(clampRow).toHaveAttribute("data-selected", "false");
  });

  it("moves the highlight when a different row is selected", () => {
    renderView();
    fireEvent.click(traceRow("CLAMP"));
    expect(window.__chart?.highlightTime).toBe(240);
    // REJECT is tick 12 → 360 s.
    fireEvent.click(traceRow("REJECT"));
    expect(window.__chart?.highlightTime).toBe(360);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  // See RoastTastings.test.tsx's afterEach — this suite also drives a
  // partial-failure state (module-scoped, not per-test QueryClient) on the
  // shared FIXTURE_DETAIL.id, which could otherwise bleed into a later spec.
  __resetPartialFailureLocksForTests();
});

describe("DetailView composition", () => {
  it("renders the retained D96 recovery summary", () => {
    renderView();
    const summary = screen.getByTestId("post-fc-recovery-summary");
    expect(summary).toHaveTextContent("Recovery armed");
    expect(summary).toHaveTextContent("Cycles1");
    expect(summary).toHaveTextContent("First recovery06:00");
    expect(summary).toHaveTextContent("Max ceiling75 %");
    expect(summary).toHaveTextContent("Recovering00:30");
    expect(summary).toHaveTextContent("Exit glide00:30");
    expect(summary).toHaveTextContent("Glide retriggers0");
  });

  it("mounts ChargeWeight wired to the detail's frozen charge weight (#520) — the data-flows-to-the-render-tree check", () => {
    renderView();
    const frozen = screen.getByTestId("charge-weight-frozen");
    expect(frozen).toHaveTextContent(`${FIXTURE_DETAIL.profile.bean_weight_grams} g`);
  });

  it("wires RoastedWeight to the EFFECTIVE charge weight, not the frozen default, when a correction exists (#520 round-2 P2)", () => {
    // The server's set_roasted_weight bound now checks against the corrected
    // charge when present (the safety-medium fold), so the widget's own
    // client-side bound must match — a mismatch would show a value as
    // client-invalid that the server accepts, or vice versa.
    render(
      <DetailView
        detail={{ ...FIXTURE_DETAIL, corrected_charge_grams: 255 }}
        telemetry={FIXTURE_TELEMETRY}
        timeline={FIXTURE_TIMELINE}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.getByTestId("roasted-weight")).toHaveTextContent("255 g in →");
  });

  it("mounts RoastTastings wired to the detail's own run id (#522) — the data-flows-to-the-render-tree check: a dropped import or wrong runId prop would pass every other test here", async () => {
    const spy = vi
      .spyOn(api, "tastings")
      .mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    renderView();
    expect(screen.getByTestId("roast-tastings")).toBeInTheDocument();
    // Proves the runId PROP actually reached the mounted widget, not just that
    // some <RoastTastings> rendered: the query only fires with the fixture's
    // own run id if DetailView passed detail.id through, not a stale/wrong one.
    await waitFor(() => expect(spy).toHaveBeenCalledWith(FIXTURE_DETAIL.id));
  });

  it("#568 Codex round 1 (PRRT_kwDOSzMG_c6RdllD), direction A: RoastRating's own Edit is blocked while RoastTastings' one-gesture rating save is in flight — the two entry points never race a rating write", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    let resolveRate: (() => void) | undefined;
    vi.spyOn(api, "rate").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRate = () => resolve({ id: FIXTURE_DETAIL.id } as Awaited<ReturnType<typeof api.rate>>);
        }),
    );
    renderView();
    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());

    // Fire a tasting save — its rating POST stalls, mid-flight.
    fireEvent.click(screen.getByTestId("tasting-star-4"));
    fireEvent.click(screen.getByTestId("tasting-save"));
    await waitFor(() => expect(resolveRate).toBeDefined());

    // RoastRating's own "Edit" must be disabled for the duration — an
    // operator opening it here could otherwise clobber the in-flight
    // tasting-triggered save with a stale direct edit (last-write-wins).
    expect(screen.getByTestId("rating-edit")).toBeDisabled();
    expect(screen.getByTestId("rating-edit-blocked")).toBeInTheDocument();

    resolveRate?.();
    await waitFor(() => expect(screen.getByTestId("rating-edit")).not.toBeDisabled());
    expect(screen.queryByTestId("rating-edit-blocked")).not.toBeInTheDocument();
  });

  it("#568 round 2 (PRRT_kwDOSzMG_c6ReetO), direction B: RoastTastings' own 'Add tasting' is blocked while a DIRECT RoastRating save is in flight — the reverse of direction A, which round 1 left unguarded", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    const addSpy = vi
      .spyOn(api, "addTasting")
      .mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    let resolveRate: (() => void) | undefined;
    vi.spyOn(api, "rate").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRate = () => resolve({ id: FIXTURE_DETAIL.id } as Awaited<ReturnType<typeof api.rate>>);
        }),
    );
    renderView();
    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());

    // Choose a tasting star BEFORE the direct edit fires — the tasting
    // star buttons themselves are also disabled while a rating write is in
    // flight, so this must happen first for `stars` to be non-null once
    // unblocked (this test is probing the SAVE button's guard specifically,
    // not the inputs' own disabled state — that's covered elsewhere).
    fireEvent.click(screen.getByTestId("tasting-star-4"));

    // Fire a DIRECT edit on RoastRating — its rating POST stalls, mid-flight.
    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("star-3"));
    fireEvent.click(screen.getByTestId("rating-save"));
    await waitFor(() => expect(resolveRate).toBeDefined());

    // RoastTastings' own save must be disabled for the duration — clicking
    // "Add tasting" here would otherwise fire a SECOND, concurrent api.rate
    // call for the same run while the direct edit is still in flight.
    expect(screen.getByTestId("tasting-save")).toBeDisabled();

    resolveRate?.();
    await waitFor(() => expect(screen.getByTestId("tasting-save")).not.toBeDisabled());
    // Never fired while blocked.
    expect(addSpy).not.toHaveBeenCalled();
  });

  it("#568 round 4 (PRRT_kwDOSzMG_c6RflF1): RoastRating's Edit stays blocked for the WHOLE partial-failure window, not just while a rating write is literally in flight — an external edit can no longer land unobserved and be silently overwritten by RoastTastings' next retry", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    vi.spyOn(api, "rate").mockRejectedValue(new Error("rating endpoint down"));
    renderView();
    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());

    // A tasting save's rating side fails and SETTLES (not pending — genuinely
    // failed) — the partial-failure window is now open.
    fireEvent.click(screen.getByTestId("tasting-star-2"));
    fireEvent.click(screen.getByTestId("tasting-save"));
    await waitFor(() => expect(screen.getByTestId("rating-partial-error")).toBeInTheDocument());

    // Nothing is PENDING right now (both mutations have settled), so the
    // narrower round-1/round-2 in-flight guard alone would NOT block this —
    // round 4's shared partial-failure lock must.
    expect(screen.getByTestId("rating-edit")).toBeDisabled();
    expect(screen.getByTestId("rating-edit-blocked")).toBeInTheDocument();

    // Resolving via RoastTastings' own retry (not Start over) reopens Edit
    // once the cycle fully succeeds.
    vi.spyOn(api, "rate").mockResolvedValue({ id: FIXTURE_DETAIL.id } as Awaited<ReturnType<typeof api.rate>>);
    fireEvent.click(screen.getByTestId("tasting-save"));
    await waitFor(() => expect(screen.getByTestId("tasting-saved")).toBeInTheDocument());
    expect(screen.getByTestId("rating-edit")).not.toBeDisabled();
  });

  it("#568 round 5 (PRRT_kwDOSzMG_c6RgNHJ): a concurrent widget's BROAD (non-exact) invalidation of roastKeys.detail/history — exactly what RoastedWeight.tsx and ChargeWeight.tsx do routinely — does NOT clear an active partial-failure lock; RoastRating's Edit stays disabled across it", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    vi.spyOn(api, "addTasting").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    vi.spyOn(api, "rate").mockRejectedValue(new Error("rating endpoint down"));

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <DetailView detail={FIXTURE_DETAIL} telemetry={FIXTURE_TELEMETRY} timeline={FIXTURE_TIMELINE} />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("tasting-star-2"));
    fireEvent.click(screen.getByTestId("tasting-save"));
    await waitFor(() => expect(screen.getByTestId("rating-partial-error")).toBeInTheDocument());
    expect(screen.getByTestId("rating-edit")).toBeDisabled();

    // Simulate a concurrent RoastedWeight/ChargeWeight save landing on the
    // SAME detail page — both invalidate these two keys WITHOUT `exact`,
    // which is precisely the co-action that clobbered the round-5-broken
    // lock implementation (it lived under the same roasts/{runId} prefix, so
    // the invalidation's own refetch resolved to `false` and cleared it —
    // asynchronously, hence `waitFor` below rather than an immediate assert).
    await client.invalidateQueries({ queryKey: roastKeys.detail(FIXTURE_DETAIL.id) });
    await client.invalidateQueries({ queryKey: roastKeys.history });

    // The lock must survive — it has no query key at all now, so neither
    // invalidation (nor the refetch either triggers) can touch it.
    await waitFor(() => expect(screen.getByTestId("rating-edit")).toBeDisabled());
    expect(screen.getByTestId("rating-edit-blocked")).toBeInTheDocument();
    expect(screen.getByTestId("rating-partial-error")).toBeInTheDocument();
  });

  it("#568 round 6 (PRRT_kwDOSzMG_c6Rg5YO): RoastRating's Edit stays blocked in the TRANSIENT window where the rating side has already rejected but the tasting side is still pending — not just after both have settled", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    // The tasting mutation stalls, mid-flight — never settles until we
    // resolve it explicitly.
    let resolveTasting: (() => void) | undefined;
    vi.spyOn(api, "addTasting").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTasting = () => resolve({ run_id: FIXTURE_DETAIL.id, tastings: [] });
        }),
    );
    // The rating mutation rejects FAST — settled well before the tasting
    // mutation above even gets a chance to.
    vi.spyOn(api, "rate").mockRejectedValue(new Error("rating endpoint down"));
    renderView();
    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("tasting-star-2"));
    fireEvent.click(screen.getByTestId("tasting-save"));

    // Wait specifically for the RATING side to have settled into an error —
    // at this instant the tasting mutation is STILL pending (never resolved
    // above), so `hasUnresolvedPartial` (tastingOnlyFailed/ratingOnlyFailed/
    // bothFailed — all require BOTH sides to have settled) is false, and the
    // narrower round-1/round-2 `ratingWriteInFlight` in-flight guard has
    // ALSO already gone false (the rating write itself is no longer
    // pending). Only `isFrozen` (which also considers the tasting mutation's
    // own `isPending`) is true here — this is the exact gap round 6 closes.
    await waitFor(() => expect(resolveTasting).toBeDefined());
    // Give the rating mutation's rejection a chance to actually land (it's
    // mocked to reject immediately, but still asynchronously).
    await new Promise((r) => setTimeout(r, 0));

    expect(screen.getByTestId("tasting-save")).toHaveTextContent(/saving/i);
    expect(screen.getByTestId("rating-edit")).toBeDisabled();
    expect(screen.getByTestId("rating-edit-blocked")).toBeInTheDocument();

    // Let the tasting settle too — the cycle reaches its normal
    // ratingOnlyFailed partial-failure state, still frozen (re-confirms the
    // round-4 behavior is undisturbed by this round-6 change).
    resolveTasting?.();
    await waitFor(() => expect(screen.getByTestId("rating-partial-error")).toBeInTheDocument());
    expect(screen.getByTestId("rating-edit")).toBeDisabled();
  });

  it("resets the RoastTastings draft when navigating between two different runs (#522 Codex P2): run A's unsaved draft must never leak into a POST against run B", async () => {
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    const { rerender } = render(
      <DetailView detail={FIXTURE_DETAIL} telemetry={FIXTURE_TELEMETRY} timeline={FIXTURE_TIMELINE} />,
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());

    // Draft an unsaved tasting on run A — never saved.
    fireEvent.click(screen.getByTestId("tasting-star-4"));
    fireEvent.change(screen.getByTestId("tasting-notes"), { target: { value: "run A draft" } });
    expect(screen.getByTestId("tasting-star-4")).toHaveAttribute("data-filled", "true");

    // Simulate a client-side route change to a DIFFERENT run (the same
    // re-render TanStack Router/React Router performs on a param change —
    // DetailPage re-renders DetailView with a new `detail` prop, it does not
    // unmount/remount the page tree itself).
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: FIXTURE_DETAIL_LONG.id, tastings: [] });
    rerender(
      <DetailView
        detail={FIXTURE_DETAIL_LONG}
        telemetry={FIXTURE_TELEMETRY_LONG}
        timeline={FIXTURE_TIMELINE_LONG}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());
    // The draft must be gone — run A's stars/notes must not survive onto run B.
    expect(screen.getByTestId("tasting-star-4")).toHaveAttribute("data-filled", "false");
    expect(screen.getByTestId("tasting-notes")).toHaveValue("");
  });

  it("#568 round 7 (PRRT_kwDOSzMG_c6RhcxP): opening run A's editor never leaks into run B's render after navigating away — the key={detail.id} remount (mirroring ChargeWeight/RoastTastings) mounts a FRESH RoastRating instance per run, never reusing A's `editing` state on B", async () => {
    // Run B shares the IDENTICAL rating/notes as run A — isolates the
    // cross-instance-reuse hazard from RoastRating's OWN (unrelated)
    // props-sync effect (which resets `editing` on a genuine rating/notes
    // CHANGE regardless of the key fix, and would mask this test either way
    // if B's values differed from A's).
    const detailB = { ...FIXTURE_DETAIL_LONG, rating: FIXTURE_DETAIL.rating, notes: FIXTURE_DETAIL.notes };

    // Run A's rating save stalls — kept pending for the duration of this
    // test; its own resolution isn't what this test is probing (round 4/5/6
    // cover the in-flight/partial-failure lock windows already).
    vi.spyOn(api, "rate").mockImplementation(() => new Promise(() => undefined));
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: FIXTURE_DETAIL.id, tastings: [] });
    const { rerender } = render(
      <DetailView detail={FIXTURE_DETAIL} telemetry={FIXTURE_TELEMETRY} timeline={FIXTURE_TIMELINE} />,
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());

    // Open run A's editor and pick a star (an in-progress DRAFT, not yet
    // saved) — the state a stale reused instance would otherwise carry over.
    fireEvent.click(screen.getByTestId("rating-edit"));
    fireEvent.click(screen.getByTestId("star-3"));
    expect(screen.getByTestId("star-3")).toHaveAttribute("data-filled", "true");

    // Navigate to run B (the same re-render DetailPage performs on a route
    // param change) WITHOUT ever closing A's editor first — the exact
    // interleaving the round-lead described: "navigate to run B → open B's
    // editor before A resolves." WITHOUT the key fix, React reuses the same
    // RoastRating instance (detailB's rating/notes are unchanged from A's,
    // so nothing else forces a reset), landing on run B's render STILL
    // showing A's in-progress editing session — an operator would see A's
    // own draft (star-3 selected) presented as if it were B's rating.
    vi.spyOn(api, "tastings").mockResolvedValue({ run_id: detailB.id, tastings: [] });
    rerender(
      <DetailView detail={detailB} telemetry={FIXTURE_TELEMETRY_LONG} timeline={FIXTURE_TIMELINE_LONG} />,
    );
    await waitFor(() => expect(screen.getByTestId("roast-tastings")).toBeInTheDocument());

    // Run B's OWN (fresh) instance must start read-only, exactly like every
    // other newly-viewed completed run — never inheriting run A's
    // still-open, still-mid-draft editing session.
    expect(screen.getByTestId("rating-headline")).toBeInTheDocument();
    expect(screen.queryByTestId("star-3")).not.toBeInTheDocument();
  });

  it("resets the RoastDiscard confirm step when navigating between two different runs (#582 Codex): run A's open confirm must never leak into run B's render", () => {
    const { rerender } = render(
      <DetailView detail={FIXTURE_DETAIL} telemetry={FIXTURE_TELEMETRY} timeline={FIXTURE_TIMELINE} />,
      { wrapper: wrapper() },
    );

    // Open run A's discard confirm — never clicked through. Both fixtures
    // are un-excluded, so RoastDiscard's OWN props (excluded=false on both)
    // would not force a different render on navigation — only the
    // key={detail.id} remount (mirroring RoastRating/ChargeWeight/
    // RoastTastings) closes this leak. Without it, React would reuse the
    // same instance across the runId change, landing on run B's render
    // still showing A's "Yes, discard" confirm — clicking it would call
    // discardRoast(B) while the operator believes they are confirming A.
    fireEvent.click(screen.getByTestId("roast-discard-button"));
    expect(screen.getByTestId("roast-discard-confirm")).toBeInTheDocument();

    // Simulate a client-side route change to a different run (DetailPage
    // re-renders DetailView with a new `detail` prop, not a fresh mount).
    rerender(
      <DetailView
        detail={FIXTURE_DETAIL_LONG}
        telemetry={FIXTURE_TELEMETRY_LONG}
        timeline={FIXTURE_TIMELINE_LONG}
      />,
    );

    // Run B's OWN (fresh) instance must start un-confirmed — never inheriting
    // run A's still-open confirm step.
    expect(screen.getByTestId("roast-discard-button")).toBeInTheDocument();
    expect(screen.queryByTestId("roast-discard-confirm")).not.toBeInTheDocument();
  });

  it("resets the ChargeWeight correction draft when navigating between two different runs (#520 round-2 P4): run A's unsaved draft must never leak into a POST against run B", () => {
    const { rerender } = render(
      <DetailView detail={FIXTURE_DETAIL} telemetry={FIXTURE_TELEMETRY} timeline={FIXTURE_TIMELINE} />,
      { wrapper: wrapper() },
    );

    // Draft an unsaved correction on run A — never saved. Both fixtures have
    // `corrected_charge_grams: null`, so the widget's own re-sync effect
    // (keyed on that prop) would NOT fire on navigation between them — only
    // the key={detail.id} remount closes this leak.
    fireEvent.change(screen.getByTestId("charge-weight-input"), { target: { value: "999" } });
    expect(screen.getByTestId("charge-weight-input")).toHaveValue(999);

    // Simulate a client-side route change to a different run (DetailPage
    // re-renders DetailView with a new `detail` prop, not a fresh mount).
    rerender(
      <DetailView
        detail={FIXTURE_DETAIL_LONG}
        telemetry={FIXTURE_TELEMETRY_LONG}
        timeline={FIXTURE_TIMELINE_LONG}
      />,
    );

    // The draft must be gone — run A's unsaved "999" must not survive onto run B.
    expect(screen.getByTestId("charge-weight-input")).toHaveValue(null);
  });

  it("feeds the full persisted curve to the shared LiveCurve with event markers", () => {
    renderView();
    // Six columns (x + five series), all fixture points present.
    expect(window.__chart?.columns[0]).toHaveLength(FIXTURE_TELEMETRY.points.length);
    expect(window.__chart?.markers.map((m) => m.kind).sort()).toEqual([
      "drop",
      "first_crack",
      "t0",
    ]);
  });

  it("renders export links resolving to the three artifact URLs", () => {
    renderView();
    expect(screen.getByTestId("export-jsonl")).toHaveAttribute(
      "href",
      `/api/roasts/${FIXTURE_DETAIL.id}/log/jsonl`,
    );
    expect(screen.getByTestId("export-csv")).toHaveAttribute(
      "href",
      `/api/roasts/${FIXTURE_DETAIL.id}/log/csv`,
    );
    expect(screen.getByTestId("export-summary")).toHaveAttribute(
      "href",
      `/api/roasts/${FIXTURE_DETAIL.id}/log/summary`,
    );
  });

  it("shows the outcome chip and headline stats from the persisted snapshot", () => {
    renderView();
    expect(screen.getByTestId("outcome-chip")).toHaveTextContent("COMPLETED");
    expect(screen.getByTestId("stat-total")).toBeInTheDocument();
    expect(screen.getByTestId("stat-fc")).toBeInTheDocument();
  });

  it("lists milestone events on the timeline (FC with its audio source + confidence)", () => {
    renderView();
    const timeline = screen.getByTestId("event-timeline");
    const fc = within(timeline)
      .getByText("First crack")
      .closest("[data-testid='timeline-event']")!;
    expect(fc).toHaveTextContent("audio_model");
    expect(fc).toHaveTextContent("0.91");
  });

  it("renders the charge-time 'Roast conditions' widget from RoastDetail's ambient triad (#464)", () => {
    renderView();
    expect(screen.getByTestId("roast-conditions-temp")).toHaveTextContent("29.7 °C");
    expect(screen.getByTestId("roast-conditions-humidity")).toHaveTextContent("41 %");
    expect(screen.getByTestId("roast-conditions-pressure")).toHaveTextContent("1008 hPa");
  });

  it("shows the uncaptured note when a run has no ambient triad (back-compat)", () => {
    render(
      <DetailView
        detail={{ ...FIXTURE_DETAIL, ambient_temp_c: null, ambient_humidity_pct: null, ambient_pressure_hpa: null }}
        telemetry={FIXTURE_TELEMETRY}
        timeline={FIXTURE_TIMELINE}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.getByTestId("roast-conditions-uncaptured")).toBeInTheDocument();
  });
});

describe("DetailView completed-run widget visibility (#533)", () => {
  // #533 (accepted #527 round-1 gap, repo-wide pass): RoastRating /
  // RoastedWeight / ChargeWeight / RoastTastings each POST a completed-run-
  // only endpoint. On an in-progress run's detail view (reachable via a
  // direct /roasts/:id visit while the roast is still live), rendering
  // these forms offers 409-doomed saves. ONE gate at DetailView, not five
  // per-widget checks. RoastDiscard (#582) joins the same gate — a discard
  // is likewise a completed-run immutability exception.
  it("hides all five completed-run-only widgets on an in-progress run (completed_at_utc: null)", () => {
    render(
      <DetailView
        detail={{ ...FIXTURE_DETAIL, completed_at_utc: null }}
        telemetry={FIXTURE_TELEMETRY}
        timeline={FIXTURE_TIMELINE}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.queryByTestId("roast-rating")).toBeNull();
    expect(screen.queryByTestId("roasted-weight")).toBeNull();
    expect(screen.queryByTestId("charge-weight")).toBeNull();
    expect(screen.queryByTestId("roast-tastings")).toBeNull();
    expect(screen.queryByTestId("roast-discard")).toBeNull();
  });

  it("shows all five completed-run-only widgets on a completed run (completed_at_utc set)", () => {
    renderView();
    expect(screen.getByTestId("roast-rating")).toBeInTheDocument();
    expect(screen.getByTestId("roasted-weight")).toBeInTheDocument();
    expect(screen.getByTestId("charge-weight")).toBeInTheDocument();
    expect(screen.getByTestId("roast-tastings")).toBeInTheDocument();
    expect(screen.getByTestId("roast-discard")).toBeInTheDocument();
  });

  it("still renders RoastConditions and ExportOptions on an in-progress run — they are read-outs, not forms", () => {
    render(
      <DetailView
        detail={{ ...FIXTURE_DETAIL, completed_at_utc: null }}
        telemetry={FIXTURE_TELEMETRY}
        timeline={FIXTURE_TIMELINE}
      />,
      { wrapper: wrapper() },
    );
    expect(screen.getByTestId("export-options")).toBeInTheDocument();
    // RoastConditions renders EITHER the triad or the uncaptured note —
    // either way it must still be present (not gated by this fix).
    const hasTriad = screen.queryByTestId("roast-conditions-temp") !== null;
    const hasUncaptured = screen.queryByTestId("roast-conditions-uncaptured") !== null;
    expect(hasTriad || hasUncaptured).toBe(true);
  });
});

function renderLongView() {
  return render(
    <DetailView
      detail={FIXTURE_DETAIL_LONG}
      telemetry={FIXTURE_TELEMETRY_LONG}
      timeline={FIXTURE_TIMELINE_LONG}
    />,
    { wrapper: wrapper() },
  );
}

describe("DetailView list caps (#271)", () => {
  it("caps the inline decision-trace table to 5 rows and offers 'View all (N)'", () => {
    renderLongView();
    const inlineTable = screen.getByTestId("decision-trace-table");
    expect(within(inlineTable).getAllByTestId("trace-row")).toHaveLength(5);
    // N = 24 trace rows → the affordance appears with the full count.
    expect(screen.getByTestId("trace-view-all")).toHaveTextContent(
      `View all (${FIXTURE_TIMELINE_LONG.safety_evaluations.length})`,
    );
  });

  it("caps the inline advisor timeline to 5 rows and offers 'View all (N)'", () => {
    renderLongView();
    const inlineTimeline = screen.getByTestId("advisor-timeline");
    expect(within(inlineTimeline).getAllByTestId("advisor-row")).toHaveLength(5);
    expect(screen.getByTestId("advisor-view-all")).toHaveTextContent(
      `View all (${FIXTURE_TIMELINE_LONG.advisor_decisions.length})`,
    );
  });

  it("does NOT show 'View all' when a list is at or below the cap", () => {
    // The short fixture has 3 trace rows / 3 advisor rows.
    renderView();
    expect(screen.queryByTestId("trace-view-all")).toBeNull();
    expect(screen.queryByTestId("advisor-view-all")).toBeNull();
  });

  it("keeps the #253 trace-table header selector unambiguous when the modal is open", () => {
    renderLongView();
    fireEvent.click(screen.getByTestId("trace-view-all"));
    // The inline table keeps the guarded testid; the modal copy uses a distinct one.
    expect(screen.getAllByTestId("decision-trace-table")).toHaveLength(1);
    expect(screen.getByTestId("decision-trace-table-modal")).toBeInTheDocument();
  });

  it("opens the trace modal with the COMPLETE history and closes it", () => {
    renderLongView();
    fireEvent.click(screen.getByTestId("trace-view-all"));
    const modal = screen.getByTestId("trace-modal");
    expect(within(modal).getAllByTestId("trace-row")).toHaveLength(
      FIXTURE_TIMELINE_LONG.safety_evaluations.length,
    );
    fireEvent.click(screen.getByTestId("trace-modal-close"));
    expect(screen.queryByTestId("trace-modal")).toBeNull();
  });

  it("selecting a trace row that only lives in the modal sets the curve highlight and closes the modal (#126)", () => {
    renderLongView();
    expect(window.__chart?.highlightTime).toBeNull();
    fireEvent.click(screen.getByTestId("trace-view-all"));

    // Tick 0 is well outside the last-5 inline window — modal-only.
    const modal = screen.getByTestId("trace-modal");
    const firstRow = within(modal)
      .getAllByTestId("trace-row")
      .find((r) => r.getAttribute("data-tick") === "0")!;
    fireEvent.click(firstRow);

    // The highlight is set (tick 0 → 0 s in the telemetry) and the modal closed so
    // the highlighted curve at the top of the page is back in frame.
    expect(window.__chart?.highlightTime).toBe(0);
    expect(screen.queryByTestId("trace-modal")).toBeNull();
  });

  it("selecting an inline trace row still highlights the curve (inline view works)", () => {
    renderLongView();
    const inlineTable = screen.getByTestId("decision-trace-table");
    // The CLAMP row is engineered to fall in the last-5 inline window.
    const clamp = within(inlineTable)
      .getAllByTestId("trace-row")
      .find((r) => r.getAttribute("data-verdict") === "clamp")!;
    fireEvent.click(clamp);
    expect(window.__chart?.highlightTime).not.toBeNull();
  });
});

describe("DetailView advisor timeline (#170)", () => {
  it("renders the advisor decision timeline with one row per consult + summary", () => {
    renderView();
    const advisor = screen.getByTestId("advisor-timeline");
    // Three consults (ticks 4/8/12) in the fixture.
    expect(within(advisor).getAllByTestId("advisor-row")).toHaveLength(3);
    // Summary chips reflect the one CLAMP and one REJECT in the fixture.
    expect(screen.getByTestId("advisor-summary-consults")).toHaveTextContent("3 consults");
    expect(screen.getByTestId("advisor-summary-clamped")).toHaveTextContent("1 clamped");
    expect(screen.getByTestId("advisor-summary-rejected")).toHaveTextContent("1 rejected");
  });

  it("a roast where every advisor consult failed renders the failures, not a blank panel", () => {
    render(
      <DetailView
        detail={FIXTURE_DETAIL_FAILED}
        telemetry={FIXTURE_TELEMETRY_FAILED}
        timeline={FIXTURE_TIMELINE_FAILED}
      />,
      { wrapper: wrapper() },
    );
    // The advisor timeline is present (not the empty panel).
    expect(screen.queryByTestId("advisor-timeline-empty")).toBeNull();
    const rows = screen.getAllByTestId("advisor-row");
    expect(rows).toHaveLength(3);
    // Each row shows its failure status.
    for (const status of screen.getAllByTestId("advisor-status")) {
      expect(status).toHaveTextContent("PROVIDER ERROR");
    }
    // The summary calls out the failures.
    expect(screen.getByTestId("advisor-summary-failed")).toHaveTextContent("3 failed");
    // The old safety-spined decision-trace table IS empty here (no verdicts) — the
    // advisor timeline is what saves the page from a blank advisor panel.
    expect(screen.getByTestId("decision-trace-empty")).toBeInTheDocument();
  });
});
