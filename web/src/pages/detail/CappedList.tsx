/**
 * Last-N cap + "View all" modal wrapper for the detail-page lists (#271).
 *
 * The advisor-decisions list and the decision-trace table both grow unbounded with
 * roast length. This wraps either one: it renders only the most-recent `cap` rows
 * inline (preserving the source ordering), and when there are more than `cap` rows
 * it shows a "View all (N)" affordance that opens the COMPLETE, scrollable list in
 * a `ListModal`. Older rows are never dropped — they remain reachable in the modal
 * (the detail page is the review surface, unlike the live dashboard panel).
 *
 * It is render-prop driven so both lists reuse it with their own table component:
 * `renderRows(rows)` is called once for the inline slice and once for the full set
 * inside the modal. The affordance appears iff `rows.length > cap`.
 */

import { useCallback, useState } from "react";

import { ListModal } from "./ListModal";

export interface CappedListProps<Row> {
  /** Full, ordered row set (most-recent last, matching the source ordering). */
  rows: Row[];
  /** Inline cap — at most this many most-recent rows show inline (default 5). */
  cap?: number;
  /** Modal title + accessible label. */
  modalTitle: string;
  /** Stable testid prefix for the affordance + modal (so each list is addressable). */
  testId: string;
  /**
   * Render a table/body for a given row slice. `ctx.inModal` distinguishes the
   * modal (full set) from the inline (capped) render; `ctx.close` closes the modal
   * so a caller can dismiss it after the operator interacts with a modal row (e.g.
   * selecting a row that only exists in the modal — keeps the curve highlight in
   * frame, #126). `tableTestId` is a distinct id for the modal copy so duplicate
   * table testids never collide with the #253 guarded selector.
   */
  renderRows: (
    rows: Row[],
    ctx: { inModal: boolean; close: () => void; tableTestId?: string },
  ) => React.ReactNode;
  /** The `data-testid` for the table rendered INSIDE the modal (#271). */
  modalTableTestId?: string;
}

export function CappedList<Row>({
  rows,
  cap = 5,
  modalTitle,
  testId,
  renderRows,
  modalTableTestId,
}: CappedListProps<Row>): React.JSX.Element {
  const [open, setOpen] = useState(false);
  // Stable identity: `close` is passed to `ListModal` as `onClose`, which lists it
  // in a `useEffect` dep array. An inline closure would change every render and
  // re-fire the modal's focus-trap effect (premature focus restore) on any parent
  // re-render while the modal is open — reviewer-confirmed correctness hazard.
  const close = useCallback(() => setOpen(false), []);

  const total = rows.length;
  const overflows = total > cap;
  // "Last N" = the most recent rows, preserving the current ordering.
  const inlineRows = overflows ? rows.slice(total - cap) : rows;

  return (
    <div className="flex flex-col gap-2">
      {renderRows(inlineRows, { inModal: false, close })}

      {overflows && (
        <div className="flex justify-end">
          <button
            type="button"
            data-testid={`${testId}-view-all`}
            onClick={() => setOpen(true)}
            aria-haspopup="dialog"
            className="rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-secondary"
          >
            View all ({total})
          </button>
        </div>
      )}

      {overflows && (
        <ListModal
          open={open}
          title={modalTitle}
          testId={`${testId}-modal`}
          onClose={close}
        >
          {/* Build the full-history tree only while open — when closed the modal
              renders nothing, so there is no point reconciling N rows every tick. */}
          {open && renderRows(rows, { inModal: true, close, tableTestId: modalTableTestId })}
        </ListModal>
      )}
    </div>
  );
}
