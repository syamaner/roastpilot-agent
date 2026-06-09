import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AppFrame } from "./AppFrame";

describe("AppFrame", () => {
  it("renders the brand, the header-right slot, and the page body", () => {
    render(
      <AppFrame headerRight={<span data-testid="slot">indicator</span>}>
        <p data-testid="body">page</p>
      </AppFrame>,
    );
    expect(screen.getByText("RoastPilot")).toBeInTheDocument();
    expect(screen.getByTestId("header-right")).toContainElement(screen.getByTestId("slot"));
    expect(screen.getByTestId("body")).toHaveTextContent("page");
  });
});
