/**
 * Home / landing hub (#324, updated #423 D81, #473) — all entry points render and
 * route correctly. "Start a new roast" points to `/live` (idle /live shows the
 * start form; if a roast is active the operator lands on the dashboard). "Settings"
 * (#473) points to `/config`, previously reachable only by typing the URL.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { HomePage } from "./HomePage";

function renderHome() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/live" element={<div data-testid="live-landing" />} />
        <Route path="/roasts" element={<div data-testid="history-landing" />} />
        <Route path="/config" element={<div data-testid="config-landing" />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("HomePage (#324 / #423 / #473)", () => {
  it("renders all entry points: Start → /live (D81), View → /roasts, Settings → /config (#473)", () => {
    renderHome();
    expect(screen.getByTestId("home-page")).toBeInTheDocument();

    // #423 D81: the start tile now points to /live (idle /live = start form;
    // active /live = live dashboard — one entry point for both cases).
    const start = screen.getByTestId("home-start-roast");
    expect(start).toHaveAttribute("href", "/live");
    expect(start).toHaveTextContent(/start a new roast/i);

    const view = screen.getByTestId("home-view-roasts");
    expect(view).toHaveAttribute("href", "/roasts");
    expect(view).toHaveTextContent(/view .* roasts/i);

    // #473: the Settings tile is the third home entry point, into the Config UI
    // (previously reachable only by typing /config directly).
    const settings = screen.getByTestId("home-settings");
    expect(settings).toHaveAttribute("href", "/config");
    expect(settings).toHaveTextContent(/settings/i);
  });

  it("navigates to /config when the Settings tile is clicked (#473)", async () => {
    renderHome();
    await userEvent.click(screen.getByTestId("home-settings"));
    expect(screen.getByTestId("config-landing")).toBeInTheDocument();
  });
});
