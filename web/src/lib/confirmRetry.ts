/**
 * Shared confirm-with-retry helper for a proven mutation result (#513).
 *
 * TanStack Query's `refetchQueries`/`invalidateQueries` RESOLVE even when the
 * underlying fetch genuinely failed — confirmed empirically against this
 * project's TanStack Query version, `throwOnError` included (see
 * `docs/recent-fixes.md`, "A query-cache refetch resolving is not proof the
 * underlying fetch succeeded"). So a caller cannot detect a failed refetch by
 * awaiting one, which makes it unsafe to gate control flow (navigation, a
 * state latch, the only user-visible feedback of an operation) on it.
 *
 * The fix pattern: after a mutation is PROVEN to have succeeded (e.g. a 201),
 * poll the resource directly with a bounded retry budget, and write successes
 * into the query cache with `queryClient.setQueryData` rather than trusting a
 * refetch/invalidate to signal failure to its awaiter.
 *
 * `runConfirmRetry` centralises the retry loop AND the unmount guard every
 * call site needs: a component's confirm loop keeps running in its closure
 * after `cleanup()`/unmount (React does not cancel in-flight promises), so
 * every await must be followed by a mounted check before any state write —
 * otherwise an unmounted component's loop can still call `setQueryData` on
 * the (still-alive, app-wide) query cache, and a remount that starts a NEW
 * confirm loop can race the orphaned one, both writing the same cache key.
 */

/** Confirm-retry attempts after a proven mutation result before giving up and
 *  reporting failure to the caller. Each attempt is a couple of seconds apart
 *  — enough to ride out a brief post-restart/MCP-respawn blip (#513) without
 *  leaving the operator staring at a pending state for long. */
export const CONFIRM_RETRY_MAX_ATTEMPTS = 5;
export const CONFIRM_RETRY_DELAY_MS = 1500;

/** Resolve after `ms` milliseconds. */
export function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface RunConfirmRetryOptions<T> {
  /** One confirm attempt — e.g. `api.health()`. Throwing is treated as a
   *  transient failure and retried; the caller decides success by returning
   *  a value for which `isSuccess` is true. */
  attempt: () => Promise<T>;
  /** Whether the resolved value counts as confirmed (stops the loop). */
  isSuccess: (value: T) => boolean;
  /** Called with every resolved attempt (success or not) — typically
   *  `queryClient.setQueryData(key, value)` so an observing `useQuery` picks
   *  up the latest known-good snapshot even on an attempt that doesn't (yet)
   *  satisfy `isSuccess`. Skipped for a throwing attempt (nothing to store).
   *  NOT called if the component has unmounted since the attempt started. */
  onResult?: (value: T) => void;
  /** Returns `false` once the owning component has unmounted. Checked after
   *  EVERY await (each attempt and each inter-attempt delay) — an unmounted
   *  loop returns immediately, before any `onResult`/state write, so an
   *  orphaned closure never mutates shared state after its component is gone. */
  isMounted: () => boolean;
  maxAttempts?: number;
  retryDelayMs?: number;
}

export type ConfirmRetryResult = "confirmed" | "unmounted" | "failed";

/**
 * Run the confirm-retry loop described above. Returns `"confirmed"` once
 * `isSuccess` passes, `"unmounted"` if the component unmounts mid-loop (the
 * caller must not touch component state after this — the guard already
 * skipped any pending `onResult`), or `"failed"` once `maxAttempts` is
 * exhausted with no success.
 */
export async function runConfirmRetry<T>({
  attempt,
  isSuccess,
  onResult,
  isMounted,
  maxAttempts = CONFIRM_RETRY_MAX_ATTEMPTS,
  retryDelayMs = CONFIRM_RETRY_DELAY_MS,
}: RunConfirmRetryOptions<T>): Promise<ConfirmRetryResult> {
  for (let i = 0; i < maxAttempts; i += 1) {
    let value: T | undefined;
    let threw = false;
    try {
      value = await attempt();
    } catch {
      threw = true;
    }
    if (!isMounted()) return "unmounted";

    if (!threw && value !== undefined) {
      onResult?.(value);
      if (isSuccess(value)) return "confirmed";
    }

    if (i < maxAttempts - 1) {
      await delay(retryDelayMs);
      if (!isMounted()) return "unmounted";
    }
  }
  return "failed";
}
