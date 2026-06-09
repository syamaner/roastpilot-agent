import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { VerdictBadge } from "./VerdictBadge";

describe("VerdictBadge", () => {
  it.each([
    ["allow", "ALLOW", "nominal"],
    ["clamp", "CLAMP", "caution"],
    ["reject", "REJECT", "fault"],
  ] as const)("renders %s as the %s badge", (verdict, label, tone) => {
    render(<VerdictBadge verdict={verdict} />);
    const badge = screen.getByTestId("verdict-badge");
    expect(badge).toHaveTextContent(label);
    expect(badge).toHaveAttribute("data-tone", tone);
    expect(badge).toHaveAttribute("data-verdict", verdict);
  });

  it.each(["recovery", "fault", "emergency_stop"] as const)(
    "renders nothing for %s (not an advisory badge)",
    (verdict) => {
      const { container } = render(<VerdictBadge verdict={verdict} />);
      expect(container).toBeEmptyDOMElement();
      expect(screen.queryByTestId("verdict-badge")).toBeNull();
    },
  );
});
