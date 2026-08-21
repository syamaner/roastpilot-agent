/**
 * Read-only star-rating glyph run (#794).
 *
 * Shared component exception (contract-authorized): `RoastRating`'s
 * read-only headline and `RoastTastings`' read-only entry stars both
 * hand-rolled the SAME `"★".repeat(n) + "☆".repeat(5 - n)` pattern with no
 * clamp on the input — `TastingEntry` fed a raw persisted `entry.stars`
 * straight into `"★".repeat(5 - entry.stars)`, so any malformed stored value
 * above 5 threw a `RangeError` (negative `repeat` count) and unmounted the
 * whole tasting list. `StarGlyphs` centralizes the derivation with a
 * fail-closed clamp so no caller can repeat that hazard: a non-finite input
 * (`NaN`/`Infinity`/`-Infinity`) fails closed to 0 filled stars, and any
 * other value is rounded then clamped to the 0–5 range before rendering.
 *
 * Deliberately minimal: one text run (no inner `<span>`, no opacity/color
 * distinction between filled and empty glyphs, no alpha-modifier class) —
 * callers that want visual styling apply it via `className` on the root.
 */

export interface StarGlyphsProps {
  /** Raw rating value to render as filled/empty glyphs (clamped 0–5). */
  rating: number;
  className?: string;
}

/** Derive the filled-star count from a raw rating: fail closed to 0 for a
 *  non-finite input, otherwise round and clamp to the 0–5 range. */
function deriveFilled(rating: number): number {
  if (!Number.isFinite(rating)) return 0;
  const rounded = Math.round(rating);
  if (rounded < 0) return 0;
  if (rounded > 5) return 5;
  return rounded;
}

export function StarGlyphs({ rating, className }: StarGlyphsProps): React.JSX.Element {
  const filled = deriveFilled(rating);
  return (
    <span
      data-testid="star-glyphs"
      role="img"
      aria-label={`${filled} of 5 stars`}
      className={className}
    >
      {"★".repeat(filled)}
      {"☆".repeat(5 - filled)}
    </span>
  );
}
