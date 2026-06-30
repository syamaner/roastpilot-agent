/**
 * Pure-launcher `/` (#423, D81): HomeGate now always renders `HomePage` —
 * unconditionally, no server-state read, no loading hold. The active→dashboard
 * branch that lived here (#324) moved to LivePage (#403 / #423), which is the
 * single live-roast home. This spec guards that invariant.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HomeGate } from "./HomeGate";

// No health hook needed — HomeGate does not read server state any more.
// Stub the only destination so the test asserts the import, not the internals.
vi.mock("./HomePage", () => ({
  HomePage: () => <div data-testid="home-stub" />,
}));

afterEach(cleanup);

describe("HomeGate pure launcher (D81 / #423)", () => {
  it("always renders HomePage — no loading hold, no active-run branch", () => {
    render(<HomeGate />);
    expect(screen.getByTestId("home-stub")).toBeInTheDocument();
    // The old loading placeholder must never appear.
    expect(screen.queryByTestId("home-gate-loading")).toBeNull();
    // The dashboard must never render at /; that lives at /live now.
    expect(screen.queryByTestId("dashboard-stub")).toBeNull();
  });
});
