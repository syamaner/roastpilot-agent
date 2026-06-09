import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StarRating } from "./StarRating";

describe("StarRating", () => {
  it("renders five stars and an aria-label for a rated run", () => {
    render(<StarRating rating={4} />);
    const el = screen.getByTestId("star-rating");
    expect(el).toHaveAttribute("data-rating", "4");
    expect(el).toHaveAttribute("aria-label", "4 of 5 stars");
    expect(el.querySelectorAll("svg")).toHaveLength(5);
  });

  it("renders an em dash for an unrated run", () => {
    render(<StarRating rating={null} />);
    const el = screen.getByTestId("star-rating");
    expect(el).toHaveAttribute("data-rating", "none");
    expect(el).toHaveTextContent("—");
    expect(el.querySelector("svg")).toBeNull();
  });
});
