import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// jsdom gaps that uPlot (and resize-aware components) need. We assert chart DATA
// via the window.__chart hook, never canvas pixels (D24), so a no-op 2d context
// is sufficient — these stubs just let the component mount under jsdom.

if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

if (!("ResizeObserver" in window)) {
  (window as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}

// uPlot builds line geometry with Path2D, which jsdom lacks. A no-op shim is
// enough — we never read back canvas geometry (data is asserted via __chart).
if (!("Path2D" in globalThis)) {
  globalThis.Path2D = class {
    addPath(): void {}
    moveTo(): void {}
    lineTo(): void {}
    rect(): void {}
    arc(): void {}
    closePath(): void {}
  } as never;
}

// A no-op canvas 2d context so uPlot can construct.
const noopCtx = new Proxy(
  {},
  {
    get: (_t, prop) => {
      if (prop === "canvas") return document.createElement("canvas");
      return () => undefined;
    },
  },
);
HTMLCanvasElement.prototype.getContext = vi.fn(() => noopCtx) as never;
