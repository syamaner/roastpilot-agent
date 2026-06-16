/**
 * Shared list overlay for the roast-detail page (#271).
 *
 * The detail page is the REVIEW surface: the advisor-decisions list and the
 * decision-trace table both grow one row per consult/tick, so a long roast pushes
 * the page down without bound. The fix (operator decision, 16 Jun 2026) caps each
 * list to the last 5 rows inline and moves the COMPLETE, scrollable history into
 * this modal — older rows are never dropped, just relocated.
 *
 * Accessibility follows the existing modal precedent (`RecoveryModal`): a
 * `role="dialog"` / `aria-modal` overlay with a backdrop, Escape-to-close, and a
 * focus trap that returns focus to the trigger on close. This is page-local (not
 * a shared component) per the E10 page-ownership model; it is deliberately minimal
 * and content-agnostic — the caller supplies the full-history body.
 */

import { useEffect, useRef } from "react";

import { cn } from "@/lib/cn";

export interface ListModalProps {
  /** When false the modal is not rendered. */
  open: boolean;
  /** Accessible dialog title (also the visible header). */
  title: string;
  /** Stable testid for the dialog container (so each list's modal is addressable). */
  testId: string;
  /** Close the modal (backdrop click, Escape, or the close button). */
  onClose: () => void;
  /** The complete, scrollable history body. */
  children: React.ReactNode;
}

/** The selector matching every natively-focusable element inside the dialog. */
const FOCUSABLE =
  'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])';

export function ListModal({
  open,
  title,
  testId,
  onClose,
  children,
}: ListModalProps): React.JSX.Element | null {
  const dialogRef = useRef<HTMLDivElement>(null);
  // Remember whatever had focus when we opened, to restore it on close.
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement as HTMLElement | null;

    // Move focus into the dialog (the close button is first in the DOM).
    const dialog = dialogRef.current;
    const first = dialog?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      // Trap focus: wrap between the first and last focusable element.
      const focusable = dialog
        ? Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE))
        : [];
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const firstEl = focusable[0];
      const lastEl = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && active === firstEl) {
        event.preventDefault();
        lastEl.focus();
      } else if (!event.shiftKey && active === lastEl) {
        event.preventDefault();
        firstEl.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      // Return focus to the trigger that opened the modal.
      restoreFocusRef.current?.focus();
    };
  }, [open, onClose]);

  if (!open) return null;

  const titleId = `${testId}-title`;

  return (
    <div
      data-testid={`${testId}-backdrop`}
      // Backdrop click closes; clicks inside the panel are stopped below.
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-6"
    >
      <div
        ref={dialogRef}
        data-testid={testId}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onClick={(event) => event.stopPropagation()}
        className={cn(
          "flex max-h-[80vh] w-full max-w-3xl flex-col gap-3 rounded-lg border border-border",
          "bg-card p-5 shadow-2xl",
        )}
      >
        <header className="flex items-center justify-between gap-4">
          <h2 id={titleId} className="text-sm font-semibold tracking-tight">
            {title}
          </h2>
          <button
            type="button"
            data-testid={`${testId}-close`}
            onClick={onClose}
            aria-label="Close"
            className="rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary"
          >
            Close
          </button>
        </header>
        {/* The full history scrolls inside the panel; the panel itself is bounded. */}
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}
