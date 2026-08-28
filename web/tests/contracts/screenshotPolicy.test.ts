/** Structural guard for the closed visual-regression screenshot policy. */

import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { basename, dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { describe, expect, test } from "vitest";

import {
  CANVAS_LOCATOR_MAX_DIFF_PIXELS,
  CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO,
  DOM_LOCATOR_MAX_DIFF_PIXELS,
  DOM_PAGE_MAX_DIFF_PIXEL_RATIO,
  MINIMUM_PAGE_AREA_PIXELS,
  SCREENSHOT_CLASSES,
  SCREENSHOT_INVENTORY,
  expectScreenshot,
  isVisualCalibrationEnabled,
  resolveScreenshotOptions,
  type ScreenshotClass,
  type ScreenshotInventoryEntry,
} from "../e2e/visualBudgets";

const MODULE_PATH = import.meta.url.startsWith("file:")
  ? fileURLToPath(import.meta.url)
  : import.meta.url.replace(/^\/@fs/, "");
const WEB_ROOT = resolve(dirname(MODULE_PATH), "../..");
const TESTS_ROOT = join(WEB_ROOT, "tests");
const E2E_ROOT = join(WEB_ROOT, "tests", "e2e");
const HELPER_PATH = join(E2E_ROOT, "visualBudgets.ts");
const CONFIG_PATH = join(WEB_ROOT, "playwright.config.ts");

const TEST_SOURCE_EXTENSIONS = new Set([".ts", ".tsx"]);
const EXCLUDED_TEST_SOURCE_SUFFIXES = [".d.ts", ".generated.ts", ".generated.tsx"];
const EXCLUDED_TEST_SOURCE_DIRECTORIES = new Set(["__screenshots__", "generated"]);

type HelperCall = ScreenshotInventoryEntry;

/** Recursively discover all screenshot spec files and fail closed when absent. */
function discoverSpecFiles(directory = E2E_ROOT): string[] {
  const children = readdirSync(directory, { withFileTypes: true });
  return children.flatMap((child) => {
    const childPath = join(directory, child.name);
    if (child.isDirectory()) return discoverSpecFiles(childPath);
    return child.isFile() && child.name.endsWith(".spec.ts") ? [childPath] : [];
  });
}

/** Return whether a path is an admitted TypeScript/TSX test source. */
function isTestSourceFile(path: string): boolean {
  return (
    TEST_SOURCE_EXTENSIONS.has(extname(path)) &&
    !EXCLUDED_TEST_SOURCE_SUFFIXES.some((suffix) => path.endsWith(suffix))
  );
}

/** Recursively discover all admitted test source files across the test tree. */
function discoverTestSourceFiles(directory = TESTS_ROOT): string[] {
  const children = readdirSync(directory, { withFileTypes: true });
  return children.flatMap((child) => {
    const childPath = join(directory, child.name);
    if (child.isDirectory()) {
      return EXCLUDED_TEST_SOURCE_DIRECTORIES.has(child.name) ? [] : discoverTestSourceFiles(childPath);
    }
    return child.isFile() && isTestSourceFile(childPath) ? [childPath] : [];
  });
}

/** Parse a TypeScript source file from disk. */
function sourceFile(path: string): ts.SourceFile {
  return ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.ESNext, true);
}

/** Return a property name when it is statically identifiable. */
function propertyName(name: ts.PropertyName): string | undefined {
  if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name)) return name.text;
  return undefined;
}

/** Collect all Playwright screenshot matcher accesses from a source file. */
function directScreenshotCalls(file: ts.SourceFile): ts.PropertyAccessExpression[] {
  const calls: ts.PropertyAccessExpression[] = [];
  const visit = (node: ts.Node): void => {
    if (
      ts.isPropertyAccessExpression(node) &&
      node.name.text === "toHaveScreenshot"
    ) {
      calls.push(node);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return calls;
}

/** Collect structurally valid helper calls, rejecting dynamic names/classes. */
function helperCalls(path: string): HelperCall[] {
  const calls: HelperCall[] = [];
  const file = sourceFile(path);
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "expectScreenshot") {
      const [target, snapshotName, screenshotClass, ...extra] = node.arguments;
      expect(target).toBeDefined();
      expect(extra).toHaveLength(0);
      expect(snapshotName).toBeDefined();
      expect(ts.isStringLiteral(snapshotName!)).toBe(true);
      expect(screenshotClass).toBeDefined();
      expect(ts.isPropertyAccessExpression(screenshotClass!)).toBe(true);
      if (
        !ts.isStringLiteral(snapshotName!) ||
        !ts.isPropertyAccessExpression(screenshotClass!) ||
        !ts.isIdentifier(screenshotClass!.expression) ||
        screenshotClass!.expression.text !== "SCREENSHOT_CLASSES" ||
        !(screenshotClass!.name.text in SCREENSHOT_CLASSES)
      ) {
        throw new Error(`Invalid expectScreenshot call in ${path}`);
      }
      calls.push({
        specFile: basename(path),
        snapshotName: snapshotName!.text,
        screenshotClass: SCREENSHOT_CLASSES[
          screenshotClass!.name.text as keyof typeof SCREENSHOT_CLASSES
        ],
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return calls;
}

/** Return structurally declared property names from a TypeScript file. */
function declaredPropertyNames(path: string): string[] {
  const names: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isPropertyAssignment(node) || ts.isPropertyDeclaration(node)) {
      const name = propertyName(node.name);
      if (name) names.push(name);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile(path));
  return names;
}

/** Normalize inventory entries for order-independent equality checks. */
function inventoryKeys(entries: readonly ScreenshotInventoryEntry[]): string[] {
  return entries
    .map((entry) => `${entry.specFile}:${entry.snapshotName}:${entry.screenshotClass}`)
    .sort();
}

describe("visual screenshot policy", () => {
  test("resolves all four classes with page/locator shapes and closed failures", () => {
    const canvasPage = resolveScreenshotOptions(SCREENSHOT_CLASSES.CANVAS_PAGE);
    const domPage = resolveScreenshotOptions(SCREENSHOT_CLASSES.DOM_PAGE);
    const domLocator = resolveScreenshotOptions(SCREENSHOT_CLASSES.DOM_LOCATOR);
    const canvasLocator = resolveScreenshotOptions(SCREENSHOT_CLASSES.CANVAS_LOCATOR);

    expect(canvasPage).toMatchObject({ fullPage: true, maxDiffPixelRatio: CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO });
    expect(domPage).toMatchObject({ fullPage: true, maxDiffPixelRatio: DOM_PAGE_MAX_DIFF_PIXEL_RATIO });
    expect(domLocator).toMatchObject({ maxDiffPixels: DOM_LOCATOR_MAX_DIFF_PIXELS });
    expect(canvasLocator).toMatchObject({ maxDiffPixels: CANVAS_LOCATOR_MAX_DIFF_PIXELS });
    expect(domLocator).not.toHaveProperty("fullPage");
    expect(canvasLocator).not.toHaveProperty("fullPage");
    expect(domLocator).not.toHaveProperty("maxDiffPixelRatio");
    expect(canvasLocator).not.toHaveProperty("maxDiffPixelRatio");
    expect(() => resolveScreenshotOptions("UNKNOWN" as ScreenshotClass)).toThrow("Unknown screenshot class");
  });

  test("calibration only activates for its exact value and only tightens", () => {
    expect(isVisualCalibrationEnabled({ RP_VISUAL_CALIBRATE: "1" })).toBe(true);
    expect(isVisualCalibrationEnabled({ RP_VISUAL_CALIBRATE: "" })).toBe(false);
    expect(isVisualCalibrationEnabled({ RP_VISUAL_CALIBRATE: "true" })).toBe(false);

    for (const screenshotClass of Object.values(SCREENSHOT_CLASSES)) {
      const committed = resolveScreenshotOptions(screenshotClass);
      const calibrated = resolveScreenshotOptions(screenshotClass, { RP_VISUAL_CALIBRATE: "1" });
      if ("maxDiffPixelRatio" in committed && "maxDiffPixelRatio" in calibrated) {
        expect(calibrated.maxDiffPixelRatio).toBe(0);
        expect(calibrated.maxDiffPixelRatio).toBeLessThanOrEqual(committed.maxDiffPixelRatio);
      }
      if ("maxDiffPixels" in committed && "maxDiffPixels" in calibrated) {
        expect(calibrated.maxDiffPixels).toBe(0);
        expect(calibrated.maxDiffPixels).toBeLessThanOrEqual(committed.maxDiffPixels);
      }
    }
  });

  test("recursively discovers all eleven non-empty e2e specs", () => {
    const specs = discoverSpecFiles();
    expect(specs).not.toHaveLength(0);
    expect(specs.map((path) => basename(path)).sort()).toEqual([
      "config.spec.ts",
      "dashboard.spec.ts",
      "detail.spec.ts",
      "foundation.spec.ts",
      "hardware-clear.spec.ts",
      "history.spec.ts",
      "home.spec.ts",
      "live-finished.spec.ts",
      "mic-status.spec.ts",
      "start-roast.spec.ts",
      "stream-smoke.spec.ts",
    ]);
  });

  test("permits direct screenshot matching only in the sanctioned helper", () => {
    const testSources = discoverTestSourceFiles();
    expect(testSources).not.toHaveLength(0);
    for (const path of testSources) {
      if (path !== HELPER_PATH) expect(directScreenshotCalls(sourceFile(path))).toHaveLength(0);
    }
    expect(directScreenshotCalls(sourceFile(HELPER_PATH))).toHaveLength(1);
  });

  test("detects raw matchers in e2e helpers and non-e2e test sources", () => {
    const e2eFixtureDirectory = mkdtempSync(join(E2E_ROOT, ".screenshot-policy-"));
    const contractFixtureDirectory = mkdtempSync(join(TESTS_ROOT, ".screenshot-policy-"));
    const e2eHelper = join(e2eFixtureDirectory, "raw-helper.ts");
    const contractSource = join(contractFixtureDirectory, "raw-contract.tsx");
    const rawMatcher = "expect(target).toHaveScreenshot('escape.png');";

    try {
      writeFileSync(e2eHelper, rawMatcher);
      writeFileSync(contractSource, rawMatcher);
      const discovered = discoverTestSourceFiles();

      expect(discovered).toEqual(expect.arrayContaining([e2eHelper, contractSource]));
      expect(directScreenshotCalls(sourceFile(e2eHelper))).toHaveLength(1);
      expect(directScreenshotCalls(sourceFile(contractSource))).toHaveLength(1);
    } finally {
      rmSync(e2eFixtureDirectory, { recursive: true, force: true });
      rmSync(contractFixtureDirectory, { recursive: true, force: true });
    }
  });

  test("derives an exact helper-call inventory with literal names and closed classes", () => {
    const discovered = discoverSpecFiles().flatMap(helperCalls);
    expect(inventoryKeys(discovered)).toEqual(inventoryKeys(SCREENSHOT_INVENTORY));
  });

  test("has no project-wide screenshot tolerance", () => {
    const properties = declaredPropertyNames(CONFIG_PATH);
    expect(properties).not.toContain("maxDiffPixelRatio");
    expect(properties).not.toContain("maxDiffPixels");
    expect(properties).not.toContain("threshold");
  });

  test("enforces class counts, numeric ladders, and locator caps", () => {
    const counts = Object.values(SCREENSHOT_CLASSES).map((screenshotClass) => [
      screenshotClass,
      SCREENSHOT_INVENTORY.filter((entry) => entry.screenshotClass === screenshotClass).length,
    ]);
    expect(counts).toEqual([
      [SCREENSHOT_CLASSES.CANVAS_PAGE, 12],
      [SCREENSHOT_CLASSES.DOM_PAGE, 9],
      [SCREENSHOT_CLASSES.DOM_LOCATOR, 4],
      [SCREENSHOT_CLASSES.CANVAS_LOCATOR, 1],
    ]);
    expect(CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO).toBeGreaterThan(0);
    expect(CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO).toBeLessThanOrEqual(0.01);
    expect(DOM_PAGE_MAX_DIFF_PIXEL_RATIO).toBeGreaterThanOrEqual(0);
    expect(DOM_PAGE_MAX_DIFF_PIXEL_RATIO).toBeLessThanOrEqual(CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO / 5);
    expect(DOM_LOCATOR_MAX_DIFF_PIXELS).toBeLessThan(DOM_PAGE_MAX_DIFF_PIXEL_RATIO * MINIMUM_PAGE_AREA_PIXELS);
    expect(CANVAS_LOCATOR_MAX_DIFF_PIXELS).toBeLessThan(CANVAS_PAGE_MAX_DIFF_PIXEL_RATIO * MINIMUM_PAGE_AREA_PIXELS);
  });

  test("retains every material-region locator contract", () => {
    const locatorNames = SCREENSHOT_INVENTORY.filter(
      (entry) => entry.screenshotClass === SCREENSHOT_CLASSES.DOM_LOCATOR,
    ).map((entry) => entry.snapshotName);
    expect(locatorNames.sort()).toEqual([
      "bean-temperature-developed.png",
      "foundation-d96-recovering.png",
      "start-roast-add-modal-catalogue-results.png",
      "start-roast-add-modal-draft-panel.png",
    ]);
  });

  test("exports the sole screenshot helper surface", () => {
    expect(expectScreenshot).toBeTypeOf("function");
  });
});
