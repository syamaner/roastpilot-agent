import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { DetailView } from "./DetailView";
import { FIXTURE_DETAIL, FIXTURE_TELEMETRY, FIXTURE_TIMELINE } from "./fixture";

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return render(
    <DetailView
      detail={FIXTURE_DETAIL}
      telemetry={FIXTURE_TELEMETRY}
      timeline={FIXTURE_TIMELINE}
    />,
    { wrapper: Wrapper },
  );
}

describe("DetailView trace-row → curve highlight", () => {
  it("highlights the row's timestamp on the shared LiveCurve, and toggles off on re-click", () => {
    renderView();
    // The shared LiveCurve exposes highlightTime on the window.__chart hook (D24)
    // — we assert the cross-component wiring through the REAL chart, not a stub.
    expect(window.__chart?.highlightTime).toBeNull();

    // The CLAMP row is tick 8 → 240 s in the fixture telemetry.
    const clampRow = screen.getByText("CLAMP").closest("tr")!;
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
    fireEvent.click(screen.getByText("CLAMP").closest("tr")!);
    expect(window.__chart?.highlightTime).toBe(240);
    // REJECT is tick 12 → 360 s.
    fireEvent.click(screen.getByText("REJECT").closest("tr")!);
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
