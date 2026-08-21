import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StarGlyphs } from "./StarGlyphs";

/** Every glyph string is EXACTLY five characters (filled + empty) — never
 *  more, never fewer, regardless of how malformed the raw input is. */
function expectFiveGlyphs(text: string): void {
  expect(text).toHaveLength(5);
  expect(text.replace(/[★☆]/g, "")).toBe("");
}

describe("StarGlyphs (#794)", () => {
  it.each([
    [0, "☆☆☆☆☆", "0 of 5 stars"],
    [3, "★★★☆☆", "3 of 5 stars"],
    [5, "★★★★★", "5 of 5 stars"],
  ] as const)("renders a rating of %s as %s with a matching aria-label", (rating, expectedText, label) => {
    render(<StarGlyphs rating={rating} />);
    const glyphs = screen.getByTestId("star-glyphs");
    expect(glyphs.textContent).toBe(expectedText);
    expectFiveGlyphs(glyphs.textContent ?? "");
    expect(glyphs).toHaveAttribute("aria-label", label);
    expect(glyphs).toHaveAttribute("role", "img");
  });

  it("clamps a finite out-of-range HIGH value (e.g. a malformed stored 6) to 5 filled stars, never throwing", () => {
    render(<StarGlyphs rating={6} />);
    const glyphs = screen.getByTestId("star-glyphs");
    expect(glyphs.textContent).toBe("★★★★★");
    expectFiveGlyphs(glyphs.textContent ?? "");
    expect(glyphs).toHaveAttribute("aria-label", "5 of 5 stars");
  });

  it("clamps a finite out-of-range LOW (negative) value to 0 filled stars, never throwing", () => {
    render(<StarGlyphs rating={-3} />);
    const glyphs = screen.getByTestId("star-glyphs");
    expect(glyphs.textContent).toBe("☆☆☆☆☆");
    expectFiveGlyphs(glyphs.textContent ?? "");
    expect(glyphs).toHaveAttribute("aria-label", "0 of 5 stars");
  });

  it("rounds a fractional rating before clamping", () => {
    render(<StarGlyphs rating={2.6} />);
    const glyphs = screen.getByTestId("star-glyphs");
    expect(glyphs.textContent).toBe("★★★☆☆");
    expectFiveGlyphs(glyphs.textContent ?? "");
    expect(glyphs).toHaveAttribute("aria-label", "3 of 5 stars");
  });

  it("fails closed to 0 filled stars for NaN, never throwing", () => {
    render(<StarGlyphs rating={NaN} />);
    const glyphs = screen.getByTestId("star-glyphs");
    expect(glyphs.textContent).toBe("☆☆☆☆☆");
    expectFiveGlyphs(glyphs.textContent ?? "");
    expect(glyphs).toHaveAttribute("aria-label", "0 of 5 stars");
  });

  it("fails closed to 0 filled stars for Infinity, never throwing", () => {
    render(<StarGlyphs rating={Infinity} />);
    const glyphs = screen.getByTestId("star-glyphs");
    expect(glyphs.textContent).toBe("☆☆☆☆☆");
    expectFiveGlyphs(glyphs.textContent ?? "");
    expect(glyphs).toHaveAttribute("aria-label", "0 of 5 stars");
  });

  it("fails closed to 0 filled stars for -Infinity, never throwing", () => {
    render(<StarGlyphs rating={-Infinity} />);
    const glyphs = screen.getByTestId("star-glyphs");
    expect(glyphs.textContent).toBe("☆☆☆☆☆");
    expectFiveGlyphs(glyphs.textContent ?? "");
    expect(glyphs).toHaveAttribute("aria-label", "0 of 5 stars");
  });

  it("applies the caller className to the root element", () => {
    render(<StarGlyphs rating={3} className="text-roast-caution" />);
    expect(screen.getByTestId("star-glyphs")).toHaveClass("text-roast-caution");
  });
});
