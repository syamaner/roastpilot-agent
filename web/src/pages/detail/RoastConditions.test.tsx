import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RoastConditions } from "./RoastConditions";

afterEach(cleanup);

describe("RoastConditions (#464 — the charge-time 'Roast conditions' widget)", () => {
  it("renders the charge-time triad with units when every field is present", () => {
    render(
      <RoastConditions ambientTempC={29.7} ambientHumidityPct={41.2} ambientPressureHpa={1008.3} />,
    );
    expect(screen.getByTestId("roast-conditions-temp")).toHaveTextContent("29.7 °C");
    expect(screen.getByTestId("roast-conditions-humidity")).toHaveTextContent("41 %");
    expect(screen.getByTestId("roast-conditions-pressure")).toHaveTextContent("1008 hPa");
    expect(screen.queryByTestId("roast-conditions-uncaptured")).not.toBeInTheDocument();
  });

  it("shows an em dash per field and the uncaptured note when never captured", () => {
    render(<RoastConditions ambientTempC={null} ambientHumidityPct={null} ambientPressureHpa={null} />);
    expect(screen.getByTestId("roast-conditions-temp")).toHaveTextContent("—");
    expect(screen.getByTestId("roast-conditions-humidity")).toHaveTextContent("—");
    expect(screen.getByTestId("roast-conditions-pressure")).toHaveTextContent("—");
    expect(screen.getByTestId("roast-conditions-uncaptured")).toBeInTheDocument();
    const el = screen.getByTestId("roast-conditions");
    expect(el.textContent).not.toMatch(/NaN|undefined|null/i);
  });

  it("treats undefined the same as null (pre-#342 back-compat run)", () => {
    render(
      <RoastConditions
        ambientTempC={undefined}
        ambientHumidityPct={undefined}
        ambientPressureHpa={undefined}
      />,
    );
    expect(screen.getByTestId("roast-conditions-temp")).toHaveTextContent("—");
    expect(screen.getByTestId("roast-conditions-uncaptured")).toBeInTheDocument();
  });

  it("handles a PARTIAL null gracefully and does not show the uncaptured note", () => {
    render(
      <RoastConditions ambientTempC={29.7} ambientHumidityPct={null} ambientPressureHpa={null} />,
    );
    expect(screen.getByTestId("roast-conditions-temp")).toHaveTextContent("29.7 °C");
    expect(screen.getByTestId("roast-conditions-humidity")).toHaveTextContent("—");
    expect(screen.getByTestId("roast-conditions-pressure")).toHaveTextContent("—");
    // One real field means the roast DID capture ambient — don't show the
    // "not captured" note alongside a real reading.
    expect(screen.queryByTestId("roast-conditions-uncaptured")).not.toBeInTheDocument();
  });

  it("renders an em dash for a non-finite value (Infinity), not 'Infinity °C' (ui-reviewer fold)", () => {
    // formatAmbientTemp/Humidity/Pressure guard `!Number.isFinite`, not just
    // `Number.isNaN` — a stray Infinity must never leak into the DOM.
    render(
      <RoastConditions
        ambientTempC={Infinity}
        ambientHumidityPct={-Infinity}
        ambientPressureHpa={Infinity}
      />,
    );
    expect(screen.getByTestId("roast-conditions-temp")).toHaveTextContent("—");
    expect(screen.getByTestId("roast-conditions-humidity")).toHaveTextContent("—");
    expect(screen.getByTestId("roast-conditions-pressure")).toHaveTextContent("—");
    const el = screen.getByTestId("roast-conditions");
    expect(el.textContent).not.toMatch(/infinity/i);
  });
});
