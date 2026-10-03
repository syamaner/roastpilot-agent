/**
 * Hook tests for the read-only cold observation stream (#954 U3: H1-H5).
 *
 * A fake event source records its URL, listeners and close state and lets each
 * test fire open/error/named events by hand; fake timers drive the backoff.
 */

import { StrictMode, type ReactNode } from "react";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  useColdObservationStream,
  type ColdEventSourceLike,
} from "./useColdObservationStream";

const EPOCH = "0123456789abcdef";

function observationData(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    bean_temp_c: 21.5,
    cold_phase: "recording_on",
    cooling_on: false,
    device_reported: true,
    env_temp_c: 22.25,
    fan_percent: null,
    heat_percent: 0,
    recorded_at_utc: "2026-01-01T00:00:01.250000+00:00",
    roast_fan_outcome: "observed",
    roast_fan_percent: 40,
    schema_version: 1,
    ...overrides,
  });
}

class FakeSource implements ColdEventSourceLike {
  onopen: ColdEventSourceLike["onopen"] = null;
  onerror: ColdEventSourceLike["onerror"] = null;
  readonly listeners = new Map<string, ((ev: MessageEvent) => void)[]>();
  closed = false;

  constructor(readonly url: string) {}

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  open(): void {
    act(() => {
      this.onopen?.call(this, new Event("open"));
    });
  }

  error(): void {
    act(() => {
      this.onerror?.call(this, new Event("error"));
    });
  }

  emit(type: string, data: string, lastEventId = ""): void {
    act(() => {
      const event = new MessageEvent(type, { data, lastEventId });
      for (const listener of this.listeners.get(type) ?? []) listener(event);
    });
  }

  observation(sequence: number, overrides: Record<string, unknown> = {}): void {
    this.emit("observation", observationData(overrides), `${EPOCH}-${sequence}`);
  }
}

function setup(options: { maxBackoffSeconds?: number; wrapper?: (p: { children: ReactNode }) => ReactNode } = {}) {
  const sources: FakeSource[] = [];
  const createEventSource = (url: string) => {
    const source = new FakeSource(url);
    sources.push(source);
    return source;
  };
  const hook = renderHook(
    () =>
      useColdObservationStream({
        createEventSource,
        maxBackoffSeconds: options.maxBackoffSeconds,
      }),
    { wrapper: options.wrapper },
  );
  const latest = () => {
    const source = sources.at(-1);
    if (source === undefined) throw new Error("no source constructed");
    return source;
  };
  return { ...hook, sources, latest };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("H1 transport status and backoff", () => {
  it("opens one source at the bare path and shows connected on open", () => {
    const { result, sources, latest } = setup();
    expect(sources).toHaveLength(1);
    expect(latest().url).toBe("/api/cold-characterisation/events");
    expect(result.current.status).toBe("connecting");
    latest().open();
    expect(result.current.status).toBe("connected");
  });

  it("on error: reconnecting, closes the source, backs off 1, 2, 4 … 30 s", () => {
    const { result, sources, latest } = setup();
    const expectedDelays = [1, 2, 4, 8, 16, 30, 30];
    for (const delay of expectedDelays) {
      const current = latest();
      const before = sources.length;
      current.error();
      expect(current.closed).toBe(true);
      expect(result.current.status).toBe("reconnecting");
      act(() => {
        vi.advanceTimersByTime(delay * 1000 - 1);
      });
      expect(sources).toHaveLength(before);
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(sources).toHaveLength(before + 1);
      expect(result.current.status).toBe("reconnecting");
    }
  });

  it("a repeated open while connected keeps the same state object", () => {
    const { result, latest } = setup();
    latest().emit("heartbeat", "{}");
    const connected = result.current;
    expect(connected.status).toBe("connected");
    latest().open();
    expect(result.current).toBe(connected);
  });

  it("an open resets the backoff to 1 s", () => {
    const { sources, latest } = setup();
    latest().error();
    act(() => vi.advanceTimersByTime(1000));
    latest().error();
    act(() => vi.advanceTimersByTime(2000));
    latest().open();
    latest().error();
    act(() => vi.advanceTimersByTime(1000));
    expect(sources).toHaveLength(4);
  });

  it("the reconnect URL carries the exact last ID string", () => {
    const { result, latest } = setup({ maxBackoffSeconds: 30 });
    latest().open();
    // A sequence beyond 2^53: any numeric coercion would round it to ...992.
    latest().emit("observation", observationData(), `${EPOCH}-9007199254740993`);
    expect(result.current.lastId).toBe(`${EPOCH}-9007199254740993`);
    latest().error();
    act(() => vi.advanceTimersByTime(1000));
    expect(latest().url).toBe(
      `/api/cold-characterisation/events?last_event_id=${EPOCH}-9007199254740993`,
    );
  });

  it("honours a lower backoff ceiling", () => {
    const { sources, latest } = setup({ maxBackoffSeconds: 2 });
    for (let i = 0; i < 3; i += 1) {
      latest().error();
      act(() => vi.advanceTimersByTime(2000));
    }
    expect(sources).toHaveLength(4);
  });
});

describe("H1b lazy admission lost after HTTP 200", () => {
  it("a stream that ends after open with no frame shows reconnecting with one timer", () => {
    const { result, sources, latest } = setup();
    latest().open();
    expect(result.current.status).toBe("connected");
    latest().error();
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.observation).toBeNull();
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(1000));
    expect(sources).toHaveLength(2);
    expect(sources[1]?.url).toBe("/api/cold-characterisation/events");
  });
});

describe("H2 heartbeat inertness", () => {
  it("a heartbeat never changes the observation or its ID", () => {
    const { result, latest } = setup();
    latest().open();
    latest().observation(1);
    const observation = result.current.observation;
    const lastId = result.current.lastId;
    expect(observation).not.toBeNull();
    latest().emit("heartbeat", "{}", "ignored-by-design");
    latest().emit("heartbeat", "{}");
    expect(Object.is(result.current.observation, observation)).toBe(true);
    expect(result.current.lastId).toBe(lastId);
    expect(result.current.shownCount).toBe(1);
    expect(result.current.refusedCount).toBe(0);
  });

  it("a heartbeat after reconnect shows connected; a malformed one is refused", () => {
    const { result, latest } = setup();
    latest().emit("heartbeat", "{}");
    expect(result.current.status).toBe("connected");
    latest().emit("heartbeat", '{"t":1}');
    expect(result.current.refusedCount).toBe(1);
  });
});

describe("H3 duplicate IDs", () => {
  it("refuses and counts a repeated ID, even with different data", () => {
    const { result, latest } = setup();
    latest().observation(1);
    latest().observation(1, { bean_temp_c: 99.5 });
    expect(result.current.shownCount).toBe(1);
    expect(result.current.refusedCount).toBe(1);
    expect(result.current.observation?.bean_temp_c).toBe(21.5);
    latest().observation(2, { bean_temp_c: 23.5 });
    expect(result.current.observation?.bean_temp_c).toBe(23.5);
    expect(result.current.lastId).toBe(`${EPOCH}-2`);
  });

  it("refuses a malformed observation without displaying it", () => {
    const { result, latest } = setup();
    latest().observation(1);
    const shown = result.current.observation;
    latest().emit("observation", observationData({ extra: 1 }), `${EPOCH}-2`);
    latest().emit("observation", observationData(), "not-an-id");
    expect(result.current.observation).toBe(shown);
    expect(result.current.refusedCount).toBe(2);
    expect(result.current.lastId).toBe(`${EPOCH}-1`);
  });
});

describe("H4 listener set", () => {
  it("registers exactly the observation and heartbeat listeners", () => {
    const { latest } = setup();
    expect([...latest().listeners.keys()].sort()).toEqual(["heartbeat", "observation"]);
    expect(latest().listeners.get("observation")).toHaveLength(1);
    expect(latest().listeners.get("heartbeat")).toHaveLength(1);
  });
});

describe("H5 connection generations", () => {
  it("(a) callbacks between an error and the reconnect change nothing", () => {
    const { result, latest } = setup();
    const first = latest();
    first.open();
    first.observation(1);
    first.error();
    const snapshot = result.current;
    first.observation(2, { bean_temp_c: 50.5 });
    first.emit("heartbeat", "{}");
    first.emit("heartbeat", "bad");
    first.open();
    expect(result.current).toBe(snapshot);
    expect(result.current.status).toBe("reconnecting");
    expect(vi.getTimerCount()).toBe(1);
  });

  it("(b) callbacks from an old source after the reconnect change nothing", () => {
    const { result, sources, latest } = setup();
    const first = latest();
    first.observation(1);
    first.error();
    act(() => vi.advanceTimersByTime(1000));
    expect(sources).toHaveLength(2);
    const snapshot = result.current;
    first.open();
    first.observation(2, { bean_temp_c: 50.5 });
    first.emit("heartbeat", "{}");
    first.emit("heartbeat", "bad");
    first.error();
    expect(result.current).toBe(snapshot);
    expect(vi.getTimerCount()).toBe(0);
    expect(sources).toHaveLength(2);
    latest().open();
    expect(result.current.status).toBe("connected");
  });

  it("(c) repeated errors on one source schedule one timer", () => {
    const { sources, latest } = setup();
    const first = latest();
    first.error();
    first.error();
    first.error();
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(1000));
    expect(sources).toHaveLength(2);
  });

  it("(d) unmount closes the source, clears the timer, and later callbacks do nothing", () => {
    const consoleError = vi.spyOn(console, "error");
    const { result, unmount, sources, latest } = setup();
    const first = latest();
    first.error();
    expect(vi.getTimerCount()).toBe(1);
    act(() => vi.advanceTimersByTime(1000));
    const second = latest();
    second.open();
    const snapshot = result.current;
    unmount();
    expect(second.closed).toBe(true);
    second.observation(5);
    second.emit("heartbeat", "{}");
    second.open();
    second.error();
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(60_000));
    expect(sources).toHaveLength(2);
    expect(result.current).toBe(snapshot);
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("(d) unmount with a pending reconnect clears the timer", () => {
    const { unmount, sources, latest } = setup();
    latest().error();
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(60_000));
    expect(sources).toHaveLength(1);
  });

  it("(e) StrictMode ends with exactly one open source", () => {
    const { sources, result, latest } = setup({ wrapper: StrictMode });
    expect(sources.length).toBeGreaterThanOrEqual(1);
    expect(sources.filter((source) => !source.closed)).toHaveLength(1);
    latest().open();
    latest().observation(1);
    expect(result.current.shownCount).toBe(1);
  });
});
