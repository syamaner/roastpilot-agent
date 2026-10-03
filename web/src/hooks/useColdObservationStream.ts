/**
 * Read-only cold-characterisation stream hook.
 *
 * Opens exactly one native `EventSource` at the cold events path and admits only
 * the two closed event names through the pure adapter in `@/lib/coldObservation`.
 * It makes no REST request and touches no storage, `window` hook or clock: the only
 * retained state is the latest admitted observation, its opaque ID and two integer
 * counters.
 *
 * Transport status describes the event connection only (`connecting`, `connected`,
 * `reconnecting`). It is never a freshness, safety or readiness signal: a heartbeat
 * never changes the observation or its time, and "connected" says nothing about
 * progress while the producer's clock stalls.
 *
 * Connection generations: each connect captures its own generation number, and every
 * callback first checks it is still the active one. An error invalidates its
 * generation at once (before the source is closed and the delayed reconnect is
 * scheduled), so late callbacks from a closed source change nothing. Reconnects use
 * an explicit capped backoff of 1, 2, 4 … seconds and carry the exact last ID.
 */

import { useEffect, useRef, useState } from "react";

import {
  admitHeartbeat,
  admitObservation,
  coldEventsUrl,
  type ColdObservation,
} from "@/lib/coldObservation";

/** Transport status of the cold event connection (never a freshness claim). */
export type ColdTransportStatus = "connecting" | "connected" | "reconnecting";

/** The narrowed slice of `EventSource` this hook uses (a test/harness seam). */
export interface ColdEventSourceLike {
  onopen: ((this: ColdEventSourceLike, ev: Event) => unknown) | null;
  onerror: ((this: ColdEventSourceLike, ev: Event) => unknown) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

export interface UseColdObservationStreamOptions {
  /** Seam: construct the event source (defaults to the native `EventSource`). */
  createEventSource?: (url: string) => ColdEventSourceLike;
  /** Reconnect backoff ceiling in seconds (default 30). */
  maxBackoffSeconds?: number;
}

/** Everything the hook retains: the latest observation plus integer counters. */
export interface ColdObservationStreamState {
  status: ColdTransportStatus;
  /** The last retained display observation, or `null` before the first one. */
  observation: ColdObservation | null;
  /** The exact opaque ID of `observation` (kept as a string, never ordered). */
  lastId: string | null;
  /** Observation frames admitted and shown. */
  shownCount: number;
  /** Supported frames refused (malformed, oversize, duplicate ID). */
  refusedCount: number;
}

const DEFAULT_MAX_BACKOFF_SECONDS = 30;

const INITIAL_STATE: ColdObservationStreamState = {
  status: "connecting",
  observation: null,
  lastId: null,
  shownCount: 0,
  refusedCount: 0,
};

function defaultCreateEventSource(url: string): ColdEventSourceLike {
  return new EventSource(url) as unknown as ColdEventSourceLike;
}

/**
 * Subscribe to the cold observation stream.
 *
 * @param options Optional event-source factory and backoff ceiling.
 * @returns The transport status, the last retained display observation and counters.
 */
export function useColdObservationStream(
  options: UseColdObservationStreamOptions = {},
): ColdObservationStreamState {
  const maxBackoff = options.maxBackoffSeconds ?? DEFAULT_MAX_BACKOFF_SECONDS;
  const [state, setState] = useState<ColdObservationStreamState>(INITIAL_STATE);

  // The factory is held in a ref so a fresh inline factory does not re-subscribe.
  const factoryRef = useRef<(url: string) => ColdEventSourceLike>(defaultCreateEventSource);
  factoryRef.current = options.createEventSource ?? defaultCreateEventSource;

  useEffect(() => {
    let cancelled = false;
    let generation = 0;
    let active = 0;
    let source: ColdEventSourceLike | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let lastId: string | null = null;

    const refuse = () => {
      setState((prev) => ({ ...prev, refusedCount: prev.refusedCount + 1 }));
    };

    const connect = () => {
      generation += 1;
      active = generation;
      const myGen = active;
      const stale = () => cancelled || myGen !== active;

      const es = factoryRef.current(coldEventsUrl(lastId));
      source = es;

      es.onopen = () => {
        if (stale()) return;
        attempt = 0;
        setState((prev) => (prev.status === "connected" ? prev : { ...prev, status: "connected" }));
      };

      es.onerror = () => {
        if (stale()) return;
        // Invalidate this generation first: nothing from this source counts after
        // this point, including callbacks before the delayed reconnect runs.
        active = 0;
        es.close();
        source = null;
        setState((prev) =>
          prev.status === "reconnecting" ? prev : { ...prev, status: "reconnecting" },
        );
        // Defensive: with the immediate invalidation above, a second error from this
        // source returns early, so a timer can never already be pending here.
        /* v8 ignore else -- defensive one-timer guard, unreachable while invalidation is immediate */
        if (timer === null) {
          const delayMs = Math.min(2 ** attempt, maxBackoff) * 1000;
          attempt += 1;
          timer = setTimeout(() => {
            timer = null;
            /* v8 ignore else -- defensive: cleanup clears this timer, so it never fires after cancel */
            if (!cancelled) connect();
          }, delayMs);
        }
      };

      // Exactly two listeners. Any other event name has no listener, so the native
      // EventSource never delivers it here; it is ignored and not counted.
      es.addEventListener("observation", (ev) => {
        if (stale()) return;
        const id: unknown = ev.lastEventId;
        if (id === lastId) {
          refuse();
          return;
        }
        const observation = admitObservation(ev.data, id);
        if (observation === null) {
          refuse();
          return;
        }
        const admittedId = id as string;
        lastId = admittedId;
        setState((prev) => ({
          ...prev,
          observation,
          lastId: admittedId,
          shownCount: prev.shownCount + 1,
        }));
      });

      es.addEventListener("heartbeat", (ev) => {
        if (stale()) return;
        if (!admitHeartbeat(ev.data)) {
          refuse();
          return;
        }
        setState((prev) => (prev.status === "connected" ? prev : { ...prev, status: "connected" }));
      });
    };

    connect();

    return () => {
      cancelled = true;
      active = 0;
      if (source !== null) source.close();
      source = null;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    };
  }, [maxBackoff]);

  return state;
}
