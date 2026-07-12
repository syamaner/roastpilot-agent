import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
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

afterEach(() => vi.restoreAllMocks());

describe("DetailView composition", () => {
  it("mounts ChargeWeight wired to the detail's frozen charge weight (#520) — the data-flows-to-the-render-tree check", () => {
    renderView();
    const frozen = screen.getByTestId("charge-weight-frozen");
    expect(frozen).toHaveTextContent(`${FIXTURE_DETAIL.profile.bean_weight_grams} g`);
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
