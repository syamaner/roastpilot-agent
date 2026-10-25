/** Behavioural contract for the real screenshot helper at its matcher boundary. */

import type { Locator, Page } from "@playwright/test";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ expectSpy: vi.fn(), matcherSpy: vi.fn() }));
vi.mock("@playwright/test", () => ({ expect: mocks.expectSpy }));

import { expectScreenshot, resolveScreenshotOptions, SCREENSHOT_CLASSES } from "../e2e/visualBudgets";

function canvas(count: number) {
  return {
    count: async () => count,
    first: () => ({ isVisible: async () => true }),
  };
}

function page(canvasCount = 0): Page {
  return {
    context: () => undefined,
    locator: (_selector: string) => canvas(canvasCount),
  } as unknown as Page;
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flushMicrotasks(): Promise<void> {
  for (let turn = 0; turn < 50; turn += 1) await Promise.resolve();
}

beforeEach(() => {
  mocks.expectSpy.mockReset();
  mocks.matcherSpy.mockReset();
  mocks.expectSpy.mockImplementation(() => ({ toHaveScreenshot: mocks.matcherSpy }));
  mocks.matcherSpy.mockImplementation(() => Promise.resolve());
});

afterEach(() => {
  mocks.expectSpy.mockReset();
  mocks.matcherSpy.mockReset();
});

describe("expectScreenshot behaviour", () => {
  test("forwards the page identity, name and resolved options", async () => {
    const target = page();
    await expect(expectScreenshot(target, "forward-page.png", SCREENSHOT_CLASSES.DOM_PAGE))
      .resolves.toBeUndefined();
    expect(mocks.matcherSpy).toHaveBeenCalledExactlyOnceWith(
      "forward-page.png", resolveScreenshotOptions(SCREENSHOT_CLASSES.DOM_PAGE),
    );
    expect(mocks.expectSpy).toHaveBeenCalledTimes(1);
    expect(mocks.expectSpy.mock.calls[0]![0]).toBe(target);
  });

  test("forwards the locator identity, name and resolved options", async () => {
    const target = {
      getAttribute: async () => "live-curve",
      locator: (_selector: string) => canvas(1),
    } as unknown as Locator;
    await expect(expectScreenshot(target, "forward-locator.png", SCREENSHOT_CLASSES.CANVAS_LOCATOR))
      .resolves.toBeUndefined();
    expect(mocks.matcherSpy).toHaveBeenCalledExactlyOnceWith(
      "forward-locator.png", resolveScreenshotOptions(SCREENSHOT_CLASSES.CANVAS_LOCATOR),
    );
    expect(mocks.expectSpy).toHaveBeenCalledTimes(1);
    expect(mocks.expectSpy.mock.calls[0]![0]).toBe(target);
  });

  test("remains pending until the matcher settles", async () => {
    const pending = deferred<void>();
    mocks.matcherSpy.mockReturnValue(pending.promise);
    let settled = false;
    const helperPromise = expectScreenshot(page(), "pending.png", SCREENSHOT_CLASSES.DOM_PAGE);
    const settlement = helperPromise.then(
      () => { settled = true; },
      () => { settled = true; },
    );
    try {
      await flushMicrotasks();
      expect(mocks.matcherSpy).toHaveBeenCalledTimes(1);
      expect(settled).toBe(false);
      pending.resolve();
      await expect(helperPromise).resolves.toBeUndefined();
      await settlement;
      expect(settled).toBe(true);
    } finally {
      pending.resolve();
      await helperPromise.catch(() => undefined);
      await settlement;
    }
  });

  test("propagates the matcher failure by identity", async () => {
    const failure = new Error("matcher sentinel");
    const rejection = Promise.reject(failure);
    void rejection.catch(() => undefined);
    mocks.matcherSpy.mockReturnValue(rejection);
    await expect(expectScreenshot(page(), "failure.png", SCREENSHOT_CLASSES.DOM_PAGE))
      .rejects.toBe(failure);
    expect(mocks.matcherSpy).toHaveBeenCalledTimes(1);
  });

  test("rejects a target kind mismatch before invoking the matcher", async () => {
    await expect(expectScreenshot(page(), "invalid-kind.png", SCREENSHOT_CLASSES.DOM_LOCATOR))
      .rejects.toThrow(/DOM_LOCATOR requires a Locator target/);
    expect(mocks.matcherSpy).not.toHaveBeenCalled();
    expect(mocks.expectSpy).not.toHaveBeenCalled();
  });

  test("rejects a canvas mismatch before invoking the matcher", async () => {
    await expect(expectScreenshot(page(1), "invalid-canvas.png", SCREENSHOT_CLASSES.DOM_PAGE))
      .rejects.toThrow(/must not contain a live-curve canvas/);
    expect(mocks.matcherSpy).not.toHaveBeenCalled();
    expect(mocks.expectSpy).not.toHaveBeenCalled();
  });
});
