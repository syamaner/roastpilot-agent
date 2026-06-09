/**
 * Read-only 5-star rating for the history table (E10-S4).
 *
 * Renders the operator self-rating (1–5) from `RoastSummary.rating`. Unrated
 * runs (`rating: null`) show an em dash rather than five empty stars, so "not
 * rated" reads differently from "rated zero". Inline SVG (the foundation ships
 * no icon dep — match the hand-rolled convention from S2's shared components).
 */

import { cn } from "@/lib/cn";

const STARS = [1, 2, 3, 4, 5] as const;

export interface StarRatingProps {
  /** 1–5, or `null` for an unrated run. */
  rating: number | null;
  className?: string;
}

export function StarRating({ rating, className }: StarRatingProps): React.JSX.Element {
  if (rating === null) {
    return (
      <span
        data-testid="star-rating"
        data-rating="none"
        className={cn("font-mono text-sm text-muted-foreground", className)}
      >
        —
      </span>
    );
  }
  return (
    <span
      data-testid="star-rating"
      data-rating={rating}
      aria-label={`${rating} of 5 stars`}
      className={cn("inline-flex items-center gap-0.5", className)}
    >
      {STARS.map((star) => (
        <Star key={star} filled={star <= rating} />
      ))}
    </span>
  );
}

function Star({ filled }: { filled: boolean }): React.JSX.Element {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      aria-hidden="true"
      className={filled ? "fill-roast-caution text-roast-caution" : "fill-transparent text-border"}
    >
      <path
        d="M12 2.5l2.9 5.9 6.5.95-4.7 4.58 1.1 6.47L12 17.9l-5.8 3.07 1.1-6.47-4.7-4.58 6.5-.95L12 2.5z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}
