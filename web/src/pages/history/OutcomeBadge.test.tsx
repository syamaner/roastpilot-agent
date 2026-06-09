import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RoastOutcome } from "@/lib/types";

import { OutcomeBadge } from "./OutcomeBadge";

describe("OutcomeBadge", () => {
  it.each([
    ["completed", "COMPLETED", "nominal"],
    ["aborted", "ABORTED", "caution"],
    ["faulted", "FAULTED", "fault"],
  ] as [RoastOutcome, string, string][])(
    "renders %s with its label and tone",
    (outcome, label, tone) => {
      render(<OutcomeBadge outcome={outcome} />);
      const el = screen.getByTestId("outcome-badge");
      expect(el).toHaveTextContent(label);
      expect(el).toHaveAttribute("data-tone", tone);
    },
  );

  it("renders an in-progress state for a null outcome", () => {
    render(<OutcomeBadge outcome={null} />);
    const el = screen.getByTestId("outcome-badge");
    expect(el).toHaveTextContent("IN PROGRESS");
    expect(el).toHaveAttribute("data-outcome", "in_progress");
  });
});
