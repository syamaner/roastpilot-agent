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
  fetchSnapshot,
  onResult,
}: {
  runId: string | null;
  snapshotPhase?: RoastPhase;
  /** Override the snapshot fetch (e.g. to reject, exercising the reconnect path). */
  fetchSnapshot?: () => Promise<{ agent_phase: RoastPhase }>;
  onResult: (r: UseRoastStreamResult) => void;
}) {
  const result = useRoastStream(runId, {
    heartbeatSeconds: 1,
    createEventSource: () => new FakeEventSource(),
    fetchSnapshot: fetchSnapshot ?? (async () => ({ agent_phase: snapshotPhase ?? "preheating" })),
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

    // A phase_changed frame is the only thing that moves phase — wire field is
    // `phase` (not agent_phase, which is the snapshot's field).
    await act(async () => es.emit("phase_changed", { phase: "development" }, 2));
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

  it("does not flip straight to stale on a slow reconnect (lastFrameAt reset)", async () => {
    // Regression for the reconnect false-stale bug: a reconnect that takes longer
    // than the stale window must reset the freshness clock at connect(), so the
    // watchdog cannot fire `stale` the instant the new stream opens.
    vi.useFakeTimers();
    let latest: UseRoastStreamResult | null = null;
    render(<Probe runId="r1" snapshotPhase="preheating" onResult={(r) => (latest = r)} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const first = FakeEventSource.last!;
    act(() => first.open());
    expect(latest!.status).toBe("live");

    // Drop the stream → backoff scheduled; let a long gap pass (> stale window).
    act(() => first.error());
    expect(latest!.status).toBe("reconnecting");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000); // covers backoff + reopen + a stale-tick
    });

    // The new EventSource opened; on open the status must be live — NOT stale —
    // because connect() reset lastFrameAt for the fresh attempt.
    const second = FakeEventSource.last!;
    expect(second).not.toBe(first);
    act(() => second.open());
    expect(latest!.status).toBe("live");
  });

  it("does nothing when runId is null (no stream, status connecting)", () => {
    let latest: UseRoastStreamResult | null = null;
    render(<Probe runId={null} onResult={(r) => (latest = r)} />);
    // The early-return branch: no EventSource is ever constructed.
    expect(FakeEventSource.last).toBeNull();
    expect(latest!.status).toBe("connecting");
    expect(latest!.phase).toBeNull();
  });

  it("backs off and re-tries with a NEW EventSource when the snapshot fetch fails", async () => {
    // The fetchSnapshot-failure → reconnect branch: a failed hydrate is treated
    // as a dropped connection (back off, retry) — must NOT open a stream on the
    // failed attempt, and must reconnect after the backoff delay.
    vi.useFakeTimers();
    let latest: UseRoastStreamResult | null = null;
    let calls = 0;
    const fetchSnapshot = vi.fn(async () => {
      calls += 1;
      if (calls === 1) throw new Error("snapshot 503");
      return { agent_phase: "preheating" as RoastPhase };
    });

    render(<Probe runId="r1" fetchSnapshot={fetchSnapshot} onResult={(r) => (latest = r)} />);
    // First attempt: the snapshot rejects → no stream opened, status reconnecting.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(FakeEventSource.last).toBeNull();
    expect(latest!.status).toBe("reconnecting");

    // After the first backoff (2^0 = 1s), connect() retries; the 2nd snapshot
    // resolves and a real EventSource is created.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(fetchSnapshot).toHaveBeenCalledTimes(2);
    expect(FakeEventSource.last).not.toBeNull();
  });

  it("exposes the latest raw frame on lastEvent (page-level pub/sub)", async () => {
    // advisory/fault/recovery_required frames are consumed by pages off lastEvent;
    // assert the pass-through works for an advisory frame.
    let latest: UseRoastStreamResult | null = null;
    await act(async () => {
      render(<Probe runId="r1" snapshotPhase="development" onResult={(r) => (latest = r)} />);
    });
    await act(async () => {
      await Promise.resolve();
    });
    const es = FakeEventSource.last!;
    await act(async () => es.open());
    await act(async () =>
      es.emit("advisory", { recommended_heat_percent: 60, verdict: "clamp" }, 3),
    );
    expect(latest!.lastEvent?.event).toBe("advisory");
    expect(latest!.lastEvent?.data).toMatchObject({ verdict: "clamp" });
  });

  it("buffers EVERY frame of a synchronous burst — non-lossy, unlike lastEvent (#122)", async () => {
    // The regression: a replay `advance-to` flushes a whole run's frames into the
    // EventSource in one synchronous batch. The single `lastEvent` slot coalesces to
    // the LAST frame, but the non-lossy `frames`/`frameCount` buffer must hold them
    // all, so a cursored drain delivers every one (the dropped-fault-banner bug).
    let latest: UseRoastStreamResult | null = null;
    await act(async () => {
      render(<Probe runId="r1" snapshotPhase="development" onResult={(r) => (latest = r)} />);
    });
    await act(async () => {
      await Promise.resolve();
    });
    const es = FakeEventSource.last!;
    await act(async () => es.open());

    // Emit a burst within ONE act() — telemetry frames then a trailing fault, exactly
    // the shape `advance-to fault` produces.
    await act(async () => {
      for (let i = 1; i <= 208; i += 1) {
        es.emit("telemetry", { elapsed_seconds: i, bean_temp_c: 100 + i }, i);
      }
      es.emit("fault", { rule: "max_env_temp", verdict: "emergency_stop", reason: "over ceiling" }, 209);
    });

    // The non-lossy buffer captured all 209 frames (208 telemetry + 1 fault)...
    expect(latest!.frameCount).toBe(209);
    expect(latest!.frames).toHaveLength(209);
    expect(latest!.frames[208]?.event).toBe("fault");
    expect(latest!.frames.filter((f) => f.event === "fault")).toHaveLength(1);

    // ...while `lastEvent` only retained the final frame — the loss the buffer fixes.
    expect(latest!.lastEvent?.event).toBe("fault");
  });

  it("clears the frame buffer on a run change (no cross-run replay)", async () => {
    let latest: UseRoastStreamResult | null = null;
    const { rerender } = render(
      <Probe runId="r1" snapshotPhase="development" onResult={(r) => (latest = r)} />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    const es = FakeEventSource.last!;
    await act(async () => es.open());
    await act(async () => es.emit("advisory", { verdict: "allow" }, 1));
    expect(latest!.frameCount).toBe(1);

    // A new run resubscribes; the buffer must reset to empty so the new run never
    // folds the previous run's frames.
    await act(async () => {
      rerender(<Probe runId="r2" snapshotPhase="development" onResult={(r) => (latest = r)} />);
      await Promise.resolve();
    });
    expect(latest!.frameCount).toBe(0);
    expect(latest!.frames).toHaveLength(0);
  });
});
