/**
 * Persistent nav (#324): Home + History are always present; the "Live roast" link
 * appears ONLY when the server reports an active run; clicking a link navigates.
 * Active-run presence is read from `useHealth` (server state) — never inferred.
 */

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NavBar } from "./NavBar";

// Mutable health stub so each test can toggle the active-run signal.
const healthState: { data: { active_run_id: string | null } | undefined } = {
  data: { active_run_id: null },
};
vi.mock("@/hooks/queries", () => ({
  useHealth: () => healthState,
}));

function renderNav(initialPath = "/") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <NavBar />
      <Routes>
        <Route path="/" element={<div data-testid="home-landing" />} />
        <Route path="/roasts" element={<div data-testid="history-landing" />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("NavBar (#324)", () => {
  it("shows Home (not Live roast) + History when the server reports no active run", () => {
    healthState.data = { active_run_id: null };
    renderNav();
    expect(screen.getByTestId("nav-home")).toHaveAttribute("href", "/");
    expect(screen.getByTestId("nav-history")).toHaveAttribute("href", "/roasts");
    // The first slot is Home when idle — the Live-roast label is not shown.
    expect(screen.queryByTestId("nav-live-roast")).toBeNull();
  });

  it("swaps the first slot to Live roast (→ /, not Home) when a run is active", () => {
    healthState.data = { active_run_id: "run-42" };
    renderNav("/roasts");
    const live = screen.getByTestId("nav-live-roast");
    expect(live).toHaveAttribute("href", "/");
    // The first slot's label tracks server state, so `/` is never claimed by two
    // active links at once — Home is not also rendered while a run is active.
    expect(screen.queryByTestId("nav-home")).toBeNull();
    expect(screen.getByTestId("nav-history")).toHaveAttribute("href", "/roasts");
  });

  it("navigates when a link is clicked", async () => {
    healthState.data = { active_run_id: null };
    renderNav("/");
    expect(screen.getByTestId("home-landing")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("nav-history"));
    expect(screen.getByTestId("history-landing")).toBeInTheDocument();
  });
});
