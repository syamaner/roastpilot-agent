/**
 * Dev/test-only cold-characterisation harness route (`/__cold-characterisation-harness`).
 *
 * NOT a product page, and never observation evidence. It drives the REAL cold hook
 * and view through a scripted in-memory event source with fixed, synthetic frames,
 * so the Playwright snapshot suite has a deterministic target. The page shows a
 * banner stating the data is synthetic. Like the other harness routes it ships in
 * the built bundle; the banner keeps it truthful.
 *
 * The scripted source queues its emissions with `queueMicrotask` after construction,
 * so they reach the hook only after its listeners are registered; `close()` before
 * the microtask runs suppresses every emission (so a StrictMode double-mount leaves
 * exactly one emitting source). It uses no timers.
 */

import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import type { ColdEventSourceLike } from "@/hooks/useColdObservationStream";

import { ColdCharacterisationPage } from "./ColdCharacterisationPage";

/** One scripted emission: an `open`, or a named event with data and an ID. */
export type HarnessEmission =
  | { kind: "open" }
  | { kind: "event"; type: string; data: string; lastEventId: string };

const EPOCH = "00000000000000aa";

/** Fixed synthetic observation data (canonical server key order and shape). */
function frameData(fields: Record<string, unknown>): string {
  return JSON.stringify(fields);
}

/** Two canonical synthetic observations and one hostile (extra-key) frame. */
const HARNESS_FRAMES: readonly HarnessEmission[] = [
  { kind: "open" },
  {
    kind: "event",
    type: "observation",
    lastEventId: `${EPOCH}-1`,
    data: frameData({
      bean_temp_c: 21.5,
      cold_phase: "recording_off",
      cooling_on: false,
      device_reported: true,
      env_temp_c: 22.25,
      fan_percent: null,
      heat_percent: 0,
      recorded_at_utc: "2026-01-01T00:00:01.000000+00:00",
      roast_fan_outcome: "not_eligible",
      roast_fan_percent: null,
      schema_version: 1,
    }),
  },
  {
    kind: "event",
    type: "observation",
    lastEventId: `${EPOCH}-2`,
    data: frameData({
      bean_temp_c: 21.75,
      cold_phase: "recording_on",
      cooling_on: false,
      device_reported: true,
      env_temp_c: 22.5,
      fan_percent: null,
      heat_percent: 0,
      recorded_at_utc: "2026-01-01T00:00:02.000000+00:00",
      roast_fan_outcome: "observed",
      roast_fan_percent: 40,
      schema_version: 1,
    }),
  },
  {
    kind: "event",
    type: "observation",
    lastEventId: `${EPOCH}-3`,
    data: frameData({
      bean_temp_c: 99.5,
      cold_phase: "recording_on",
      cooling_on: true,
      device_reported: true,
      env_temp_c: 99.5,
      fan_percent: null,
      heat_percent: 99,
      recorded_at_utc: "2026-01-01T00:00:03.000000+00:00",
      roast_fan_outcome: "observed",
      roast_fan_percent: 99,
      schema_version: 1,
      unexpected: "HARNESS-HOSTILE-CANARY",
    }),
  },
];

/** The awaiting state: an open connection and one heartbeat, no observation. */
const HARNESS_AWAITING_FRAMES: readonly HarnessEmission[] = [
  { kind: "open" },
  { kind: "event", type: "heartbeat", data: "{}", lastEventId: "" },
];

/** A scripted, timer-free stand-in for `EventSource` (dev/test harness only). */
export class HarnessEventSource implements ColdEventSourceLike {
  onopen: ColdEventSourceLike["onopen"] = null;
  onerror: ColdEventSourceLike["onerror"] = null;
  private readonly listeners = new Map<string, ((ev: MessageEvent) => void)[]>();
  private closed = false;

  constructor(emissions: readonly HarnessEmission[]) {
    queueMicrotask(() => this.emit(emissions));
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    const held = this.listeners.get(type) ?? [];
    held.push(listener);
    this.listeners.set(type, held);
  }

  close(): void {
    this.closed = true;
  }

  private emit(emissions: readonly HarnessEmission[]): void {
    for (const emission of emissions) {
      if (this.closed) return;
      if (emission.kind === "open") {
        this.onopen?.call(this, new Event("open"));
        continue;
      }
      const event = new MessageEvent(emission.type, {
        data: emission.data,
        lastEventId: emission.lastEventId,
      });
      for (const listener of this.listeners.get(emission.type) ?? []) listener(event);
    }
  }
}

/** The harness page: the real cold page over a scripted synthetic source. */
export function ColdCharacterisationHarnessPage(): React.JSX.Element {
  const [params] = useSearchParams();
  const awaiting = params.get("state") === "awaiting";
  const streamOptions = useMemo(() => {
    const emissions = awaiting ? HARNESS_AWAITING_FRAMES : HARNESS_FRAMES;
    return { createEventSource: () => new HarnessEventSource(emissions) };
  }, [awaiting]);
  return <ColdCharacterisationPage streamOptions={streamOptions} synthetic />;
}
