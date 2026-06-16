import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CappedList } from "./CappedList";

/** A trivial body renderer: one button per row, tagged inline vs modal. */
function renderRows(
  rows: number[],
  ctx: { inModal: boolean; close: () => void },
  onSelect?: (n: number) => void,
): React.ReactNode {
  return (
    <ul data-testid={ctx.inModal ? "body-modal" : "body-inline"}>
      {rows.map((n) => (
        <li key={n}>
          <button
            type="button"
            data-testid={`${ctx.inModal ? "modal" : "inline"}-row-${n}`}
            onClick={() => {
              if (ctx.inModal) ctx.close();
              onSelect?.(n);
            }}
          >
            row {n}
          </button>
        </li>
      ))}
    </ul>
  );
}

function rows(n: number): number[] {
  return Array.from({ length: n }, (_, i) => i);
}

describe("CappedList", () => {
  it("renders every row inline when at or below the cap, with no 'View all' affordance", () => {
    render(
      <CappedList
        rows={rows(5)}
        modalTitle="All rows"
        testId="x"
        renderRows={(r, ctx) => renderRows(r, ctx)}
      />,
    );
    const inline = screen.getByTestId("body-inline");
    expect(within(inline).getAllByRole("listitem")).toHaveLength(5);
    expect(screen.queryByTestId("x-view-all")).toBeNull();
  });

  it("caps the inline list to the last 5 rows (most recent, preserving order) when N > 5", () => {
    render(
      <CappedList
        rows={rows(9)} // 0..8
        modalTitle="All rows"
        testId="x"
        renderRows={(r, ctx) => renderRows(r, ctx)}
      />,
    );
    const inline = screen.getByTestId("body-inline");
    expect(within(inline).getAllByRole("listitem")).toHaveLength(5);
    // The last 5 are 4..8, in source order.
    for (const n of [4, 5, 6, 7, 8]) {
      expect(within(inline).getByTestId(`inline-row-${n}`)).toBeInTheDocument();
    }
    expect(within(inline).queryByTestId("inline-row-3")).toBeNull();
  });

  it("shows the 'View all (N)' affordance iff N > cap, with the full count", () => {
    const { rerender } = render(
      <CappedList rows={rows(5)} modalTitle="All" testId="x" renderRows={renderRows} />,
    );
    expect(screen.queryByTestId("x-view-all")).toBeNull();

    rerender(
      <CappedList rows={rows(6)} modalTitle="All" testId="x" renderRows={renderRows} />,
    );
    expect(screen.getByTestId("x-view-all")).toHaveTextContent("View all (6)");
  });

  it("opens the modal with the COMPLETE list, then closes via the close button", () => {
    render(
      <CappedList rows={rows(12)} modalTitle="All rows" testId="x" renderRows={renderRows} />,
    );
    // Closed initially.
    expect(screen.queryByTestId("x-modal")).toBeNull();

    fireEvent.click(screen.getByTestId("x-view-all"));
    const modal = screen.getByTestId("x-modal");
    expect(modal).toHaveAttribute("role", "dialog");
    // The modal body carries all 12 rows.
    expect(within(modal).getAllByRole("listitem")).toHaveLength(12);

    fireEvent.click(screen.getByTestId("x-modal-close"));
    expect(screen.queryByTestId("x-modal")).toBeNull();
  });

  it("closes the modal on Escape and on a backdrop click", () => {
    render(
      <CappedList rows={rows(8)} modalTitle="All" testId="x" renderRows={renderRows} />,
    );
    fireEvent.click(screen.getByTestId("x-view-all"));
    expect(screen.getByTestId("x-modal")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("x-modal")).toBeNull();

    // Re-open and click the backdrop.
    fireEvent.click(screen.getByTestId("x-view-all"));
    fireEvent.click(screen.getByTestId("x-modal-backdrop"));
    expect(screen.queryByTestId("x-modal")).toBeNull();
  });

  it("a click inside the panel does not close the modal (backdrop-only)", () => {
    render(
      <CappedList rows={rows(8)} modalTitle="All" testId="x" renderRows={renderRows} />,
    );
    fireEvent.click(screen.getByTestId("x-view-all"));
    fireEvent.click(screen.getByTestId("x-modal"));
    expect(screen.getByTestId("x-modal")).toBeInTheDocument();
  });

  it("invokes onSelect for an inline row (inline view keeps working)", () => {
    const onSelect = vi.fn();
    render(
      <CappedList
        rows={rows(9)}
        modalTitle="All"
        testId="x"
        renderRows={(r, ctx) => renderRows(r, ctx, onSelect)}
      />,
    );
    fireEvent.click(screen.getByTestId("inline-row-8"));
    expect(onSelect).toHaveBeenCalledWith(8);
  });

  it("invokes onSelect AND closes the modal when a modal-only row is selected (#126)", () => {
    const onSelect = vi.fn();
    render(
      <CappedList
        rows={rows(9)}
        modalTitle="All"
        testId="x"
        renderRows={(r, ctx) => renderRows(r, ctx, onSelect)}
      />,
    );
    fireEvent.click(screen.getByTestId("x-view-all"));
    // Row 0 only exists in the modal (inline shows 4..8).
    const modal = screen.getByTestId("x-modal");
    fireEvent.click(within(modal).getByTestId("modal-row-0"));
    expect(onSelect).toHaveBeenCalledWith(0);
    // The modal closed so the selection target (e.g. the curve) is back in frame.
    expect(screen.queryByTestId("x-modal")).toBeNull();
  });
});
