/**
 * `runConfirmRetry` (#513): the shared confirm-with-retry loop + unmount guard
 * every "latch on a proven mutation result, poll to confirm" call site needs.
 */
import { describe, expect, it, vi } from "vitest";

import { runConfirmRetry } from "./confirmRetry";

describe("runConfirmRetry", () => {
  it("returns 'confirmed' on the first successful attempt", async () => {
    const attempt = vi.fn().mockResolvedValue("ok");
    const onResult = vi.fn();
    const result = await runConfirmRetry({
      attempt,
      isSuccess: (v) => v === "ok",
      onResult,
      isMounted: () => true,
    });
    expect(result).toBe("confirmed");
    expect(attempt).toHaveBeenCalledTimes(1);
    expect(onResult).toHaveBeenCalledWith("ok");
  });

  it("retries a throwing attempt, then confirms", async () => {
    const attempt = vi
      .fn()
      .mockRejectedValueOnce(new Error("transient"))
      .mockResolvedValueOnce("ok");
    const result = await runConfirmRetry({
      attempt,
      isSuccess: (v) => v === "ok",
      isMounted: () => true,
      retryDelayMs: 1,
    });
    expect(result).toBe("confirmed");
    expect(attempt).toHaveBeenCalledTimes(2);
  });

  it("calls onResult on every resolved attempt, even ones that don't satisfy isSuccess", async () => {
    const attempt = vi
      .fn()
      .mockResolvedValueOnce("pending")
      .mockResolvedValueOnce("ok");
    const onResult = vi.fn();
    await runConfirmRetry({
      attempt,
      isSuccess: (v) => v === "ok",
      onResult,
      isMounted: () => true,
      retryDelayMs: 1,
    });
    expect(onResult).toHaveBeenNthCalledWith(1, "pending");
    expect(onResult).toHaveBeenNthCalledWith(2, "ok");
  });

  it("returns 'failed' after maxAttempts with no success, never permanently stuck", async () => {
    const attempt = vi.fn().mockResolvedValue("still-pending");
    const result = await runConfirmRetry({
      attempt,
      isSuccess: () => false,
      isMounted: () => true,
      maxAttempts: 3,
      retryDelayMs: 1,
    });
    expect(result).toBe("failed");
    expect(attempt).toHaveBeenCalledTimes(3);
  });

  it("#513: never calls onResult once isMounted goes false, even mid-loop", async () => {
    let mounted = true;
    const onResult = vi.fn();
    const attempt = vi.fn().mockImplementation(async () => {
      // Unmount happens WHILE this attempt is in flight — simulates a
      // component unmounting during an awaited fetch.
      mounted = false;
      return "ok";
    });
    const result = await runConfirmRetry({
      attempt,
      isSuccess: (v) => v === "ok",
      onResult,
      isMounted: () => mounted,
    });
    expect(result).toBe("unmounted");
    // The attempt itself resolved successfully, but the mounted check runs
    // BEFORE onResult/isSuccess — an orphaned loop must never write state or
    // the query cache after its owning component is gone.
    expect(onResult).not.toHaveBeenCalled();
  });

  it("#513: stops between attempts if unmounted during the inter-attempt delay", async () => {
    let mounted = true;
    const attempt = vi.fn().mockResolvedValue("pending");
    const promise = runConfirmRetry({
      attempt,
      isSuccess: () => false,
      isMounted: () => mounted,
      maxAttempts: 5,
      retryDelayMs: 5,
    });
    // Unmount shortly after the first attempt starts, during its delay window.
    await new Promise((r) => setTimeout(r, 1));
    mounted = false;
    const result = await promise;
    expect(result).toBe("unmounted");
    // Must not have run every attempt — the unmount check during the delay
    // short-circuits the loop instead of burning the full retry budget.
    expect(attempt.mock.calls.length).toBeLessThan(5);
  });

  it("does not call onResult for a throwing attempt (nothing to store)", async () => {
    const onResult = vi.fn();
    const attempt = vi.fn().mockRejectedValue(new Error("boom"));
    await runConfirmRetry({
      attempt,
      isSuccess: () => true,
      onResult,
      isMounted: () => true,
      maxAttempts: 1,
    });
    expect(onResult).not.toHaveBeenCalled();
  });
});
