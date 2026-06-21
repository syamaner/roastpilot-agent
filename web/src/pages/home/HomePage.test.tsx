/**
 * Home / landing hub (#324) — both entry points render and route correctly.
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
        <Route path="/start" element={<div data-testid="start-landing" />} />
        <Route path="/roasts" element={<div data-testid="history-landing" />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("HomePage (#324)", () => {
  it("renders both entry points pointing at the Start form and the history list", () => {
    renderHome();
    expect(screen.getByTestId("home-page")).toBeInTheDocument();

    const start = screen.getByTestId("home-start-roast");
    expect(start).toHaveAttribute("href", "/start");
    expect(start).toHaveTextContent(/start a new roast/i);

    const view = screen.getByTestId("home-view-roasts");
    expect(view).toHaveAttribute("href", "/roasts");
    expect(view).toHaveTextContent(/view .* roasts/i);
  });
});
