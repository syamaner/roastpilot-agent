import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ConnectionStatus } from "@/hooks/useRoastStream";
import { ConnectionIndicator } from "./ConnectionIndicator";

describe("ConnectionIndicator", () => {
  it.each([
    ["connecting", "Connecting"],
    ["live", "Live"],
    ["reconnecting", "Reconnecting"],
    ["stale", "Stale"],
  ] as [ConnectionStatus, string][])(
    "renders the %s state with its label",
    (status, label) => {
      render(<ConnectionIndicator status={status} />);
      const el = screen.getByTestId("connection-indicator");
      expect(el).toHaveAttribute("data-status", status);
      expect(el).toHaveTextContent(label);
    },
  );
});
