/**
 * Closed screenshot policy for the pinned Linux visual-regression suite.
 *
 * The uPlot canvas remains unmasked. Canvas-containing targets receive their
 * own calibrated budget; stable DOM-only pages and material regions use tighter
 * page-ratio and locator-pixel allowances respectively.
 */

import { expect, type Locator, type Page } from "@playwright/test";

/** The four target classes permitted in the visual-regression inventory. */
export const SCREENSHOT_CLASSES = {
  CANVAS_PAGE: "CANVAS_PAGE",
  DOM_PAGE: "DOM_PAGE",
  DOM_LOCATOR: "DOM_LOCATOR",
  CANVAS_LOCATOR: "CANVAS_LOCATOR",
} as const;

/** Closed screenshot target vocabulary. */
export type ScreenshotClass = (typeof SCREENSHOT_CLASSES)[keyof typeof SCREENSHOT_CLASSES];

/** Minimum page area guaranteed by the pinned 1600 × 1000 viewport. */
export const MINIMUM_PAGE_AREA_PIXELS = 1600 * 1000;

/** Canvas pages are the sole ratio-based loose class. */
export const CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO = 0.01;
/** DOM-only pages are at least ten times tighter than canvas pages. */
export const DOM_PAGE_MAX_DIFF_PIXEL_RATIO = 0.001;
/** Locator budgets are absolute so page area cannot dilute a material region. */
export const DOM_LOCATOR_MAX_DIFF_PIXELS = 512;
/** Chart locators remain tighter than the equivalent canvas-page allowance. */
export const CANVAS_LOCATOR_MAX_DIFF_PIXELS = 1024;

/** Named, immutable visual-comparison budgets. */
export const SCREENSHOT_BUDGETS = {
  CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO,
  DOM_PAGE_MAX_DIFF_PIXEL_RATIO,
  DOM_LOCATOR_MAX_DIFF_PIXELS,
  CANVAS_LOCATOR_MAX_DIFF_PIXELS,
} as const;

/** Snapshot inventory entry, derived structurally from each e2e callsite. */
export interface ScreenshotInventoryEntry {
  specFile: string;
  snapshotName: string;
  screenshotClass: ScreenshotClass;
}

/** Complete, closed visual-regression inventory. */
export const SCREENSHOT_INVENTORY = [
  { specFile: "config.spec.ts", snapshotName: "config.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
  { specFile: "config.spec.ts", snapshotName: "config-safety.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
  { specFile: "dashboard.spec.ts", snapshotName: "dashboard-live.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "dashboard.spec.ts", snapshotName: "dashboard-fault.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "dashboard.spec.ts", snapshotName: "dashboard-recovery.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "dashboard.spec.ts", snapshotName: "bean-temperature-developed.png", screenshotClass: SCREENSHOT_CLASSES.DOM_LOCATOR },
  { specFile: "dashboard.spec.ts", snapshotName: "dashboard-developed.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "dashboard.spec.ts", snapshotName: "dashboard-charge-window.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "detail.spec.ts", snapshotName: "roast-detail.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "detail.spec.ts", snapshotName: "roast-detail-selected.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_LOCATOR },
  { specFile: "detail.spec.ts", snapshotName: "roast-detail-capped.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "detail.spec.ts", snapshotName: "roast-detail-advisor-failed.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "foundation.spec.ts", snapshotName: "foundation-chrome.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "foundation.spec.ts", snapshotName: "foundation-d96-recovering.png", screenshotClass: SCREENSHOT_CLASSES.DOM_LOCATOR },
  { specFile: "hardware-clear.spec.ts", snapshotName: "hardware-clear-required.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
  { specFile: "history.spec.ts", snapshotName: "history.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
  { specFile: "history.spec.ts", snapshotName: "history-empty.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
  { specFile: "home.spec.ts", snapshotName: "home.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
  { specFile: "live-finished.spec.ts", snapshotName: "live-finished.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "mic-status.spec.ts", snapshotName: "mic-green.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "mic-status.spec.ts", snapshotName: "mic-error.png", screenshotClass: SCREENSHOT_CLASSES.CANVAS_PAGE },
  { specFile: "start-roast.spec.ts", snapshotName: "start-roast.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
  { specFile: "start-roast.spec.ts", snapshotName: "start-roast-add-modal.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
  { specFile: "start-roast.spec.ts", snapshotName: "start-roast-add-modal-draft-panel.png", screenshotClass: SCREENSHOT_CLASSES.DOM_LOCATOR },
  { specFile: "start-roast.spec.ts", snapshotName: "start-roast-add-modal-catalogue-results.png", screenshotClass: SCREENSHOT_CLASSES.DOM_LOCATOR },
  { specFile: "stream-smoke.spec.ts", snapshotName: "stream-smoke-preheating.png", screenshotClass: SCREENSHOT_CLASSES.DOM_PAGE },
] as const satisfies readonly ScreenshotInventoryEntry[];

type ScreenshotOptions =
  | { animations: "disabled"; fullPage: true; maxDiffPixelRatio: number }
  | { animations: "disabled"; maxDiffPixels: number };

/** Return whether the explicit calibration mode is active. */
export function isVisualCalibrationEnabled(environment: NodeJS.ProcessEnv = process.env): boolean {
  return environment.RP_VISUAL_CALIBRATE === "1";
}

/**
 * Resolve comparison options for a closed screenshot class.
 *
 * Calibration only tightens: its exact opt-in value zeroes every allowance so
 * Playwright reports observed differences without changing committed baselines.
 */
export function resolveScreenshotOptions(
  screenshotClass: ScreenshotClass,
  environment: NodeJS.ProcessEnv = process.env,
): ScreenshotOptions {
  const allowance = isVisualCalibrationEnabled(environment) ? 0 : undefined;

  switch (screenshotClass) {
    case SCREENSHOT_CLASSES.CANVAS_PAGE:
      return {
        animations: "disabled",
        fullPage: true,
        maxDiffPixelRatio: allowance ?? CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO,
      };
    case SCREENSHOT_CLASSES.DOM_PAGE:
      return {
        animations: "disabled",
        fullPage: true,
        maxDiffPixelRatio: allowance ?? DOM_PAGE_MAX_DIFF_PIXEL_RATIO,
      };
    case SCREENSHOT_CLASSES.DOM_LOCATOR:
      return { animations: "disabled", maxDiffPixels: allowance ?? DOM_LOCATOR_MAX_DIFF_PIXELS };
    case SCREENSHOT_CLASSES.CANVAS_LOCATOR:
      return { animations: "disabled", maxDiffPixels: allowance ?? CANVAS_LOCATOR_MAX_DIFF_PIXELS };
    default: {
      const unknownClass: never = screenshotClass;
      throw new Error(`Unknown screenshot class: ${String(unknownClass)}`);
    }
  }
}

/** Identify a Playwright Page without relying on browser-global constructors. */
function isPage(target: Page | Locator): target is Page {
  return "context" in target;
}

/** Verify that a target's uPlot-canvas membership agrees with its class. */
async function assertCanvasMembership(
  target: Page | Locator,
  screenshotClass: ScreenshotClass,
): Promise<void> {
  const expectsPage =
    screenshotClass === SCREENSHOT_CLASSES.CANVAS_PAGE || screenshotClass === SCREENSHOT_CLASSES.DOM_PAGE;
  if (expectsPage !== isPage(target)) {
    throw new Error(`${screenshotClass} requires a ${expectsPage ? "Page" : "Locator"} target`);
  }

  const canvas = target.locator("[data-testid='live-curve'] canvas");
  const canvasCount = await canvas.count();
  const expectsCanvas =
    screenshotClass === SCREENSHOT_CLASSES.CANVAS_PAGE ||
    screenshotClass === SCREENSHOT_CLASSES.CANVAS_LOCATOR;
  if (expectsCanvas) {
    if (canvasCount === 0 || !(await canvas.first().isVisible())) {
      throw new Error(`${screenshotClass} requires a visible live-curve canvas`);
    }
    return;
  }
  if (canvasCount !== 0) {
    throw new Error(`${screenshotClass} must not contain a live-curve canvas`);
  }
}

/**
 * Capture a registered visual-regression target through the sole sanctioned surface.
 */
export async function expectScreenshot(
  target: Page | Locator,
  name: string,
  screenshotClass: ScreenshotClass,
): Promise<void> {
  await assertCanvasMembership(target, screenshotClass);
  await expect(target).toHaveScreenshot(name, resolveScreenshotOptions(screenshotClass));
}
