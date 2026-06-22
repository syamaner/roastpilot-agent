import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RoastPhase } from "@/lib/types";
import { isClockFrozen, useFrozenElapsed } from "./preheatClock";

describe("isClockFrozen", () => {
  it("freezes on terminal / faulted / e-stopped / post-drop phases (#330)", () => {
    const frozen: RoastPhase[] = [
      "faulted",
      "operator_recovery_required",
      "cooling",
      "complete",
    ];
    for (const phase of frozen) {
      expect(isClockFrozen(phase)).toBe(true);
    }
  });

  it("does not freeze while live or pre-run", () => {
    const live: (RoastPhase | null)[] = [
      null,
      "idle",
      "starting",
      "preheating",
      "roasting_pre_first_crack",
      "development",
    ];
    for (const phase of live) {
      expect(isClockFrozen(phase)).toBe(false);
    }
  });
});

describe("useFrozenElapsed", () => {
  it("tracks the server elapsed while the phase is live", () => {
    const { result, rerender } = renderHook(
      ({ s, p }: { s: number | null; p: RoastPhase | null }) => useFrozenElapsed(s, p),
      { initialProps: { s: 40, p: "preheating" as RoastPhase | null } },
    );
    expect(result.current).toBe(40);
    rerender({ s: 95, p: "preheating" });
    expect(result.current).toBe(95);
  });

  it("holds the last LIVE value once the phase becomes terminal", () => {
    const { result, rerender } = renderHook(
      ({ s, p }: { s: number | null; p: RoastPhase | null }) => useFrozenElapsed(s, p),
      { initialProps: { s: 95, p: "preheating" as RoastPhase | null } },
    );
    expect(result.current).toBe(95);
    // Fault: the server keeps advancing the run clock, but we hold the last live 95.
    rerender({ s: 200, p: "faulted" });
    expect(result.current).toBe(95);
    rerender({ s: 260, p: "faulted" });
    expect(result.current).toBe(95);
  });

  it("falls back to the server value when terminal on first paint (no live value seen)", () => {
    // A device joining a faulted run mid-fault has no captured live value; the
    // server value is the best available (and no longer advancing in practice).
    const { result } = renderHook(() => useFrozenElapsed(180, "faulted"));
    expect(result.current).toBe(180);
  });

  it("does not overwrite the captured live value with a transient null frame (#330)", () => {
    // A null elapsed during a live phase (e.g. reconnect gap / first hydrate frame)
    // must not erase the last non-null live value, otherwise the freeze would show
    // "--:--" instead of the last known clock reading.
    const { result, rerender } = renderHook(
      ({ s, p }: { s: number | null; p: RoastPhase | null }) => useFrozenElapsed(s, p),
      { initialProps: { s: 95, p: "preheating" as RoastPhase | null } },
    );
    act(() => rerender({ s: null, p: "preheating" })); // transient null while still live
    act(() => rerender({ s: 200, p: "faulted" })); // terminal: must hold 95, not null/200
    expect(result.current).toBe(95);
  });

  it("captures within an act() commit so the held value is the last live frame", () => {
    const { result, rerender } = renderHook(
      ({ s, p }: { s: number | null; p: RoastPhase | null }) => useFrozenElapsed(s, p),
      { initialProps: { s: 10, p: "preheating" as RoastPhase | null } },
    );
    act(() => rerender({ s: 88, p: "preheating" }));
    act(() => rerender({ s: 999, p: "cooling" }));
    expect(result.current).toBe(88);
  });
});
