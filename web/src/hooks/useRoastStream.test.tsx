import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RoastPhase } from "@/lib/types";
import {
  useRoastStream,
  type EventSourceLike,
  type UseRoastStreamResult,
} from "./useRoastStream";

/** A controllable fake EventSource for driving connect/error/event in tests. */
class FakeEventSource implements EventSourceLike {
  onopen: ((this: EventSourceLike, ev: Event) => unknown) | null = null;
  onerror: ((this: EventSourceLike, ev: Event) => unknown) | null = null;
  listeners = new Map<string, (ev: MessageEvent) => void>();
  closed = false;
  static last: FakeEventSource | null = null;

  constructor() {
    FakeEventSource.last = this;
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    this.listeners.set(type, listener);
  }
  close(): void {
    this.closed = true;
  }

  open(): void {
    this.onopen?.call(this, new Event("open"));
  }
  error(): void {
    this.onerror?.call(this, new Event("error"));
  }
  emit(type: string, data: unknown, id?: number): void {
    const ev = { data: JSON.stringify(data), lastEventId: id ? String(id) : "" } as MessageEvent;
    this.listeners.get(type)?.(ev);
  }
}

function Probe({
  runId,
  snapshotPhase,
  onResult,
}: {
  runId: string;
  snapshotPhase: RoastPhase;
  onResult: (r: UseRoastStreamResult) => void;
}) {
  const result = useRoastStream(runId, {
    heartbeatSeconds: 1,
    createEventSource: () => new FakeEventSource(),
    fetchSnapshot: async () => ({ agent_phase: snapshotPhase }),
  });
  onResult(result);
  return <span data-testid="phase">{result.phase ?? "none"}</span>;
}

beforeEach(() => {
  FakeEventSource.last = null;
});
afterEach(() => vi.useRealTimers());

describe("useRoastStream", () => {
  it("hydrates phase from the snapshot before opening the stream", async () => {
    let latest: UseRoastStreamResult | null = null;
    await act(async () => {
      render(<Probe runId="r1" snapshotPhase="preheating" onResult={(r) => (latest = r)} />);
    });
    // Let the snapshot promise resolve.
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId("phase")).toHaveTextContent("preheating");
    expect(latest!.phase).toBe("preheating");
  });

  it("goes live on open and applies phase_changed but not telemetry phase", async () => {
    let latest: UseRoastStreamResult | null = null;
    await act(async () => {
      render(<Probe runId="r1" snapshotPhase="preheating" onResult={(r) => (latest = r)} />);
    });
    await act(async () => {
      await Promise.resolve();
    });
    const es = FakeEventSource.last!;
    await act(async () => es.open());
    expect(latest!.status).toBe("live");

    // A telemetry frame claiming a different phase must NOT move state.phase.
    await act(async () =>
      es.emit(
        "telemetry",
        {
          agent_phase: "development",
          bean_temp_c: 150,
          env_temp_c: 180,
          bean_ror_c_per_min: null,
          env_ror_c_per_min: null,
          heat_percent: 80,
          fan_percent: 40,
          cooling_on: false,
          elapsed_seconds: 10,
          t0_detected: true,
          first_crack_detected: false,
        },
        1,
      ),
    );
    expect(latest!.phase).toBe("preheating");
    expect(latest!.telemetry?.bean_temp_c).toBe(150);

    // A phase_changed frame is the only thing that moves phase.
    await act(async () => es.emit("phase_changed", { agent_phase: "development" }, 2));
    expect(latest!.phase).toBe("development");
  });

  it("reports stale when no frame arrives within the stale window", async () => {
    vi.useFakeTimers();
    let latest: UseRoastStreamResult | null = null;
    render(<Probe runId="r1" snapshotPhase="preheating" onResult={(r) => (latest = r)} />);
    // Resolve the snapshot promise + open.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const es = FakeEventSource.last!;
    act(() => es.open());
    expect(latest!.status).toBe("live");

    // heartbeat=1s → stale window is 2s; advance past it with no frame.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(latest!.status).toBe("stale");
  });
});
