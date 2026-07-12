/**
 * Home / landing hub (#324, updated #423 D81, #473, #523) — always the same
 * four entry points, never a form and never a dashboard. "Start a new roast"
 * points to `/start` (the ONLY start-form surface under the #523 IA).
 * "Live/last roast" points to `/live` (the roaster's permanent state
 * address). "Settings" (#473) points to `/config`. The header chip reflects
 * active-run presence without turning this page into a dashboard.
 */

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HomePage } from "./HomePage";

// Mutable health stub so each test can toggle the active-run signal — the same
// pattern NavBar.test.tsx uses (server-derived, never inferred).
const healthState: { data: { active_run_id: string | null } | undefined } = {
  data: { active_run_id: null },
};
vi.mock("@/hooks/queries", () => ({
  useHealth: () => healthState,
}));

function renderHome() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/start" element={<div data-testid="start-landing" />} />
        <Route path="/live" element={<div data-testid="live-landing" />} />
        <Route path="/roasts" element={<div data-testid="history-landing" />} />
        <Route path="/config" element={<div data-testid="config-landing" />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("HomePage (#324 / #423 / #473 / #523)", () => {
  it("renders all entry points: Start → /start, Live/last → /live, View → /roasts, Settings → /config", () => {
    healthState.data = { active_run_id: null };
    renderHome();
    expect(screen.getByTestId("home-page")).toBeInTheDocument();

    // #523: the start tile is the ONLY start-form entry point — it points to
    // /start, never /live (which is never a form under the new IA).
    const start = screen.getByTestId("home-start-roast");
    expect(start).toHaveAttribute("href", "/start");
    expect(start).toHaveTextContent(/start a new roast/i);

    const live = screen.getByTestId("home-live-roast");
    expect(live).toHaveAttribute("href", "/live");

    const view = screen.getByTestId("home-view-roasts");
    expect(view).toHaveAttribute("href", "/roasts");
    expect(view).toHaveTextContent(/view .* roasts/i);

    // #473: the Settings tile is a home entry point, into the Config UI
    // (previously reachable only by typing /config directly).
    const settings = screen.getByTestId("home-settings");
    expect(settings).toHaveAttribute("href", "/config");
    expect(settings).toHaveTextContent(/settings/i);
  });

  it("navigates to /start when the Start tile is clicked (#523)", async () => {
    healthState.data = { active_run_id: null };
    renderHome();
    await userEvent.click(screen.getByTestId("home-start-roast"));
    expect(screen.getByTestId("start-landing")).toBeInTheDocument();
  });

  it("navigates to /config when the Settings tile is clicked (#473)", async () => {
    healthState.data = { active_run_id: null };
    renderHome();
    await userEvent.click(screen.getByTestId("home-settings"));
    expect(screen.getByTestId("config-landing")).toBeInTheDocument();
  });

  it("shows an Idle header chip and 'Last roast' copy when no run is active (#523)", () => {
    healthState.data = { active_run_id: null };
    renderHome();
    expect(screen.queryByTestId("home-live-status-chip")).toBeNull();
    expect(screen.getByTestId("home-live-roast")).toHaveTextContent(/last roast/i);
  });

  it("shows the live-status chip and 'View live roast' copy when a run is active (#523)", () => {
    healthState.data = { active_run_id: "run-42" };
    renderHome();
    expect(screen.getByTestId("home-live-status-chip")).toHaveTextContent(/roast in progress/i);
    expect(screen.getByTestId("home-live-roast")).toHaveTextContent(/view live roast/i);
  });

  it("does NOT show the live-status chip while health is still pending (data: undefined) — never flashes active before the read resolves", () => {
    // Guards against a future refactor (e.g. deriving hasActiveRun from
    // isSuccess/isLoading instead of `data?.active_run_id`) accidentally
    // reading a pending fetch as "active" and flashing the chip. This page
    // is a non-gating useHealth() consumer by design (unlike /live and
    // /start's useFreshHealthGate()) — it renders stale-then-update — but
    // "no data yet" must still resolve to the idle copy, not the active one.
    healthState.data = undefined;
    renderHome();
    expect(screen.queryByTestId("home-live-status-chip")).toBeNull();
    expect(screen.getByTestId("home-live-roast")).toHaveTextContent(/last roast/i);
  });
});
