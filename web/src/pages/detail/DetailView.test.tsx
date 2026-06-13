import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { DetailView } from "./DetailView";
import {
  FIXTURE_DETAIL,
  FIXTURE_DETAIL_FAILED,
  FIXTURE_TELEMETRY,
  FIXTURE_TELEMETRY_FAILED,
  FIXTURE_TIMELINE,
  FIXTURE_TIMELINE_FAILED,
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

describe("DetailView composition", () => {
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
