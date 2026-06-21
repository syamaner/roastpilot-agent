/**
 * Saved bean-profile picker for the Start-Roast form (#303, D45).
 *
 * A dropdown of the saved library (`GET /api/bean-profiles`) plus an Add button
 * (opens the add modal) and an Edit pencil (opens the edit modal for the selected
 * profile). Selecting a profile FILLS the Start form (the parent applies it). The
 * built-in Ethiopia Koke seed is selectable like any other entry.
 *
 * Presentation only: the parent owns the query + the mutations and passes the
 * library in. The SPA renders + mutates from the server (the dropdown is the saved
 * library, never a client-fabricated list).
 */

import { cn } from "@/lib/cn";
import type { BeanProfile } from "@/lib/types";

export interface BeanProfilePickerProps {
  /** The saved library (from `useBeanProfiles`). */
  profiles: BeanProfile[];
  /** The selected profile id, or "" for none (manual entry). */
  selectedId: string;
  /** Select a profile by id ("" → none); the parent fills the form. */
  onSelect: (id: string) => void;
  /** Open the add-profile modal. */
  onAdd: () => void;
  /** Open the edit-profile modal for the selected profile. */
  onEdit: () => void;
  /** Whether the library is still loading (disables the controls). */
  loading?: boolean;
}

export function BeanProfilePicker({
  profiles,
  selectedId,
  onSelect,
  onAdd,
  onEdit,
  loading = false,
}: BeanProfilePickerProps): React.JSX.Element {
  const hasSelection = selectedId !== "";
  return (
    <div className="flex flex-col gap-1" data-testid="bean-profile-picker">
      <label
        htmlFor="bean-profile-select"
        className="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
      >
        Saved bean profile
      </label>
      <div className="flex items-center gap-2">
        <select
          id="bean-profile-select"
          value={selectedId}
          onChange={(e) => onSelect(e.target.value)}
          disabled={loading}
          data-testid="bean-profile-select"
          className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition-colors focus:ring-1 focus:ring-ring disabled:opacity-60"
        >
          <option value="">
            {loading ? "Loading saved profiles…" : "— Manual entry —"}
          </option>
          {profiles.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={onEdit}
          disabled={!hasSelection || loading}
          aria-label="Edit selected profile"
          title="Edit selected profile"
          data-testid="bean-profile-edit-button"
          className={cn(
            "inline-flex items-center justify-center rounded-md border border-input px-3 py-2 text-sm transition-colors",
            !hasSelection || loading
              ? "cursor-not-allowed opacity-50"
              : "hover:bg-muted/40",
          )}
        >
          {/* Pencil glyph — keeps the bundle lean (no icon dep). */}
          <span aria-hidden="true">✎</span>
        </button>
        <button
          type="button"
          onClick={onAdd}
          disabled={loading}
          data-testid="bean-profile-add-button"
          className={cn(
            "inline-flex items-center justify-center rounded-md border border-roast-coffee/60 bg-roast-coffee/15 px-3 py-2 text-sm font-semibold uppercase tracking-wide text-roast-coffee transition-colors",
            loading ? "cursor-not-allowed opacity-60" : "hover:bg-roast-coffee/25",
          )}
        >
          Add
        </button>
      </div>
      <span className="text-xs text-muted-foreground">
        Pick a saved profile to fill the form, or enter the bean details manually.
      </span>
    </div>
  );
}
