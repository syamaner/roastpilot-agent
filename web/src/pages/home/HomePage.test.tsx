/**
 * Home / landing hub (#324, updated #423 D81) — both entry points render and
 * route correctly. "Start a new roast" now points to `/live` (idle /live shows
 * the start form; if a roast is active the operator lands on the dashboard).
 */

import { render, screen } from "@testing-library/react";
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
      </Routes>
    </MemoryRouter>,
  );
}

describe("HomePage (#324 / #423)", () => {
  it("renders both entry points: Start → /live (D81), View → /roasts", () => {
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
  });
});
