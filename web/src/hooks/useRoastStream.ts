/**
 * Live roast SSE hook (E10 kickoff §6, plan §11.4).
 *
 * Native `EventSource` over `GET /api/roasts/{id}/events`. On every (re)connect
 * it HYDRATES from `GET /api/roasts/{id}` THEN applies live frames, so phase and
 * state always re-base on the server's snapshot — never replay-from-zero, never
 * trust stale client state. PHASE COMES FROM THE SERVER ONLY (see the reducer).
 *
 * Liveness is a safety-relevant UI state: the operator must know whether the
 * data on screen is fresh. We surface `live | reconnecting | stale`:
 *   - live         : connected and a frame (incl. the 15 s heartbeat) arrived recently
 *   - reconnecting : the EventSource dropped; we're backing off to reopen
 *   - stale        : connected but no frame within the stale window (≈2× heartbeat)
 */

import { useEffect, useReducer, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { OperatorAction, RoastPhase, SseEvent, TelemetryEventData } from "@/lib/types";
import {
  applyEvent,
  hydrate,
  initialRoastStreamState,
  type RoastStreamState,
} from "./roastStreamReducer";

declare global {
  interface Window {
    /** Highest applied SSE event id (test hook, D24): the Playwright global-setup
     *  waits until this catches up to the replay step's `last_event_id` before
     *  screenshotting — a deterministic settle signal with no arbitrary sleep. */
    __lastEventId?: number;
  }
}

export type ConnectionStatus = "connecting" | "live" | "reconnecting" | "stale";

export interface UseRoastStreamOptions {
  /** Server heartbeat cadence (api default 15 s). Used to size the stale window. */
  heartbeatSeconds?: number;
  /** Reconnect backoff ceiling (seconds). */
  maxBackoffSeconds?: number;
  /** Test seam: construct an EventSource (defaults to the global). */
  createEventSource?: (url: string) => EventSourceLike;
  /** Test seam: snapshot fetch (defaults to the REST client). */
  fetchSnapshot?: (runId: string) => Promise<{
    agent_phase: RoastPhase;
    enabled_actions?: OperatorAction[];
  }>;
}

/** The slice of EventSource we use — narrowed so tests can supply a fake. */
export interface EventSourceLike {
  onopen: ((this: EventSourceLike, ev: Event) => unknown) | null;
  onerror: ((this: EventSourceLike, ev: Event) => unknown) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

export interface UseRoastStreamResult {
  status: ConnectionStatus;
  phase: RoastPhase | null;
  telemetry: TelemetryEventData | null;
  enabledActions: OperatorAction[] | null;
  /**
   * The most recent raw frame. CONVENIENCE ONLY — it is a single slot and is
   * LOSSY under a burst: when several frames arrive in one React batch (e.g. a
   * replay `advance-to` flushing a whole run, or reconnect-hydration), every
   * `setState` but the last coalesces, so a `[lastEvent]` effect sees only the
   * final frame and drops the rest (#122). Use it only where missing
   * intermediate frames is harmless. For page-local event folding that must NOT
   * drop a frame (advisory / fault / recovery / markers), drain `frames` via the
   * monotonic `frameCount` cursor instead — see `useFrameDrain`.
   */
  lastEvent: SseEvent | null;
  /**
   * Append-only buffer of every applied frame, in arrival order — the NON-LOSSY
   * channel (#122). Paired with `frameCount`: a consumer keeps a cursor and
   * folds `frames.slice(cursor, frameCount)` so a burst that coalesces into one
   * render still delivers every frame. The array identity is stable (the same
   * ref is appended in place); react to `frameCount`, not the array.
   */
  frames: readonly SseEvent[];
  /** Monotonic count of applied frames — the cursor/settle signal for `frames`. */
  frameCount: number;
}

const DEFAULT_HEARTBEAT = 15;
const DEFAULT_MAX_BACKOFF = 30;

type Action =
  | { kind: "hydrate"; snapshot: { agent_phase: RoastPhase; enabled_actions?: OperatorAction[] } }
  | { kind: "event"; event: SseEvent }
  | { kind: "reset" };

function reduce(state: RoastStreamState, action: Action): RoastStreamState {
  if (action.kind === "reset") {
    // A run change (or clearing the run): drop ALL per-run derived state — phase,
    // telemetry, enabledActions, lastEventId — back to the pre-first-frame null so
    // the next run starts clean (#215 FIX H). Without this, `useRoastStream` keeps
    // the PRIOR run's last telemetry (hydrate only updates phase), so a new preheat
    // could combine the new `preheating` phase with a stale bean temp and briefly
    // render a false "CHARGE NOW" before the first real frame.
    return initialRoastStreamState;
  }
  if (action.kind === "hydrate") {
    return hydrate(state, {
      // The reducer only reads agent_phase + enabled_actions off the snapshot.
      agent_phase: action.snapshot.agent_phase,
      enabled_actions: action.snapshot.enabled_actions,
    } as Parameters<typeof hydrate>[1]);
  }
  return applyEvent(state, action.event);
}

/** Capped exponential backoff: 1, 2, 4, … up to `maxSeconds`. */
function backoffSeconds(attempt: number, maxSeconds: number): number {
  return Math.min(2 ** attempt, maxSeconds);
}

/** The production EventSource factory (test seam default). */
function defaultCreateEventSource(url: string): EventSourceLike {
  return new EventSource(url) as unknown as EventSourceLike;
}

export function useRoastStream(
  runId: string | null,
  options: UseRoastStreamOptions = {},
): UseRoastStreamResult {
  const heartbeat = options.heartbeatSeconds ?? DEFAULT_HEARTBEAT;
  const maxBackoff = options.maxBackoffSeconds ?? DEFAULT_MAX_BACKOFF;

  const [state, dispatch] = useReducer(reduce, initialRoastStreamState);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [lastEvent, setLastEvent] = useState<SseEvent | null>(null);

  // The non-lossy frame channel (#122): every applied frame is appended to this
  // ref synchronously inside the listener (never coalesced), and `frameCount`
  // mirrors its length so consumers can drain new frames with a cursor. A burst
  // that batches N `setFrameCount` calls into one render still leaves all N frames
  // in the buffer; the consumer's drain reads them all. The buffer is reset on a
  // run change so a new run never replays the previous run's frames.
  const framesRef = useRef<SseEvent[]>([]);
  const [frameCount, setFrameCount] = useState(0);

  // Injected seams are held in refs (updated each render) so the connect effect
  // depends only on the primitive inputs — supplying a fresh inline factory does
  // not re-subscribe the stream.
  const createEventSourceRef = useRef<(url: string) => EventSourceLike>(defaultCreateEventSource);
  const fetchSnapshotRef = useRef<NonNullable<UseRoastStreamOptions["fetchSnapshot"]>>(api.roast);
  createEventSourceRef.current = options.createEventSource ?? defaultCreateEventSource;
  fetchSnapshotRef.current = options.fetchSnapshot ?? api.roast;

  // Refs hold transport state the effect mutates without re-subscribing.
  const lastFrameAt = useRef<number>(Date.now());

  // Reset per-run state the instant the run changes (or clears), BEFORE the
  // connect effect below re-subscribes (#215 FIX H). This runs in render-commit
  // order ahead of the connect effect (declared earlier), so the new run's first
  // hydrate/frames apply on a clean slate — never on the prior run's stale
  // telemetry. The pure reducer's `reset` returns the shared initial reference, so
  // a no-op reset on first mount doesn't churn renders. `lastEvent` is cleared too
  // so a `[lastEvent]` consumer can't read the previous run's final frame, and the
  // non-lossy append buffer (`framesRef`/`frameCount`) is cleared here as well
  // (#215 FIX I) so `frames` never exposes the prior run's frames on a run change
  // OR when idle (`runId === null`) — the connect effect's clear below only ran for
  // a non-null run, leaving the buffer stale on the idle transition. The cursor
  // consumer (useFrameDrain) resets in lockstep on the same runId change, so
  // frameCount going back to 0 is consistent.
  useEffect(() => {
    dispatch({ kind: "reset" });
    setLastEvent(null);
    framesRef.current = [];
    setFrameCount(0);
  }, [runId]);

  useEffect(() => {
    if (runId === null) {
      setStatus("connecting");
      return;
    }

    let cancelled = false;
    let source: EventSourceLike | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let staleTimer: ReturnType<typeof setInterval> | null = null;
    let attempt = 0;

    const markFrame = () => {
      lastFrameAt.current = Date.now();
      if (!cancelled) setStatus("live");
    };

    const connect = () => {
      if (cancelled) return;
      setStatus(attempt === 0 ? "connecting" : "reconnecting");
      // Reset the freshness clock on every (re)connect: otherwise a reconnect
      // that takes longer than the stale window leaves `lastFrameAt` at the old
      // timestamp, so the watchdog can fire `stale` the instant the new stream
      // opens — before the first frame/heartbeat has had a chance to arrive.
      lastFrameAt.current = Date.now();

      // Snapshot-first: hydrate from REST, THEN open the stream and apply frames.
      // A failed snapshot is treated as a dropped connection (back off, retry).
      void fetchSnapshotRef
        .current(runId)
        .then((snapshot) => {
          if (cancelled) return;
          dispatch({ kind: "hydrate", snapshot });
          openStream();
        })
        .catch(() => {
          if (!cancelled) scheduleReconnect();
        });
    };

    const openStream = () => {
      if (cancelled) return;
      const es = createEventSourceRef.current(api.eventsUrl(runId));
      source = es;

      es.onopen = () => {
        attempt = 0;
        markFrame();
      };
      es.onerror = () => {
        // EventSource will try its own reconnect, but we close and drive an
        // explicit capped backoff so the snapshot re-hydrates each attempt and
        // the indicator reflects reconnecting state.
        es.close();
        if (source === es) source = null;
        scheduleReconnect();
      };

      // One listener per known event type so the typed `event:` field is honored
      // (EventSource's `onmessage` only fires for the default/unnamed event).
      for (const type of SSE_EVENT_TYPES) {
        es.addEventListener(type, (ev) => {
          markFrame();
          const id = ev.lastEventId ? Number(ev.lastEventId) : null;
          const frame: SseEvent = {
            event: type,
            data: parseData(ev.data),
            id,
          };
          dispatch({ kind: "event", event: frame });
          setLastEvent(frame);
          // Non-lossy channel (#122): append every frame to the buffer (in place,
          // synchronously) and bump the count. Even if N frames arrive in one batch
          // and the N setFrameCount calls coalesce to a single render, all N frames
          // are already in framesRef for the consumer's cursored drain.
          framesRef.current.push(frame);
          setFrameCount((n) => n + 1);
          // Test hook (D24): the Playwright global-setup waits until this catches
          // up to the replay step's `last_event_id` before screenshotting — a
          // deterministic settle signal, no arbitrary sleep. Same monotonic
          // sequence the broadcaster stamps + the reducer dedups on.
          if (id !== null && typeof window !== "undefined") {
            window.__lastEventId = Math.max(window.__lastEventId ?? 0, id);
          }
        });
      }
    };

    const scheduleReconnect = () => {
      if (cancelled || reconnectTimer) return;
      setStatus("reconnecting");
      const delay = backoffSeconds(attempt, maxBackoff) * 1000;
      attempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    // Freshness watchdog: if no frame (incl. heartbeat) within the stale window
    // while we believe we're connected, surface `stale`.
    const staleWindowMs = heartbeat * 2 * 1000;
    staleTimer = setInterval(() => {
      if (cancelled || source === null) return;
      if (Date.now() - lastFrameAt.current > staleWindowMs) {
        setStatus((prev) => (prev === "reconnecting" ? prev : "stale"));
      }
    }, heartbeat * 1000);

    connect();

    return () => {
      cancelled = true;
      if (source) source.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (staleTimer) clearInterval(staleTimer);
    };
  }, [runId, heartbeat, maxBackoff]);

  return {
    status,
    phase: state.phase,
    telemetry: state.telemetry,
    enabledActions: state.enabledActions,
    lastEvent,
    frames: framesRef.current,
    frameCount,
  };
}

/**
 * Drain every new frame from `useRoastStream`'s non-lossy buffer into a per-frame
 * handler, exactly once each, in arrival order (#122).
 *
 * This is the burst-safe replacement for a `[lastEvent]` effect: it keeps a cursor
 * and, whenever `frameCount` advances, replays `frames.slice(cursor, frameCount)`,
 * so a burst that coalesces into a single render still delivers ALL its frames. The
 * cursor resets when `frameCount` drops below it (a run change cleared the buffer),
 * keeping the drain consistent with the hook's per-run reset.
 *
 * `onFrame` is held in a ref, so passing a fresh inline closure each render does not
 * re-drain already-seen frames.
 */
export function useFrameDrain(
  frames: readonly SseEvent[],
  frameCount: number,
  onFrame: (frame: SseEvent) => void,
): void {
  const onFrameRef = useRef(onFrame);
  onFrameRef.current = onFrame;
  const cursorRef = useRef(0);

  useEffect(() => {
    // A run change reset the buffer (frameCount < cursor): restart the cursor so we
    // don't index past the now-shorter buffer or skip the new run's first frames.
    if (frameCount < cursorRef.current) cursorRef.current = 0;
    for (let i = cursorRef.current; i < frameCount; i += 1) {
      const frame = frames[i];
      if (frame !== undefined) onFrameRef.current(frame);
    }
    cursorRef.current = frameCount;
  }, [frames, frameCount]);
}

const SSE_EVENT_TYPES: SseEvent["event"][] = [
  "run_started",
  "phase_changed",
  "charge_guidance",
  "t0_detected",
  "first_crack",
  "advisory",
  "command_executed",
  "command_failed",
  "safety_alert",
  "fault",
  "recovery_required",
  "recovery_acknowledged",
  "logs_exported",
  "run_completed",
  "telemetry",
  "heartbeat",
];

function parseData(raw: string): Record<string, unknown> {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return {};
  }
}
