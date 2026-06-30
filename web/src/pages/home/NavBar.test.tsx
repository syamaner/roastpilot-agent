/**
 * Persistent nav (#324, updated #403): Home + History are always present; the
 * "Live roast" link appears ONLY when the server reports an active run and points
 * to `/live` (the stable reload-safe route, #403). Active-run presence is read
 * from `useHealth` (server state) — never inferred.
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
        <Route path="/live" element={<div data-testid="live-landing" />} />
        <Route path="/roasts" element={<div data-testid="history-landing" />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("NavBar (#324 / #403)", () => {
  it("shows Home (not Live roast) + History when the server reports no active run", () => {
    healthState.data = { active_run_id: null };
    renderNav();
    expect(screen.getByTestId("nav-home")).toHaveAttribute("href", "/");
    expect(screen.getByTestId("nav-history")).toHaveAttribute("href", "/roasts");
    // The first slot is Home when idle — the Live-roast label is not shown.
    expect(screen.queryByTestId("nav-live-roast")).toBeNull();
  });

  it("swaps the first slot to Live roast (→ /live, not Home or /) when a run is active (#403)", () => {
    healthState.data = { active_run_id: "run-42" };
    renderNav("/roasts");
    const live = screen.getByTestId("nav-live-roast");
    // #403: the stable live-roast URL is /live, not /. This means the active-link
    // highlight is never ambiguous between the home hub and the live roast.
    expect(live).toHaveAttribute("href", "/live");
    // Home is not also rendered while a run is active.
    expect(screen.queryByTestId("nav-home")).toBeNull();
    expect(screen.getByTestId("nav-history")).toHaveAttribute("href", "/roasts");
  });

  it("navigates to /live when the Live roast link is clicked", async () => {
    healthState.data = { active_run_id: "run-42" };
    renderNav("/");
    expect(screen.getByTestId("home-landing")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("nav-live-roast"));
    expect(screen.getByTestId("live-landing")).toBeInTheDocument();
  });

  it("navigates to /roasts when the History link is clicked", async () => {
    healthState.data = { active_run_id: null };
    renderNav("/");
    expect(screen.getByTestId("home-landing")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("nav-history"));
    expect(screen.getByTestId("history-landing")).toBeInTheDocument();
  });
});
