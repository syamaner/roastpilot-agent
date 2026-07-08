import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AmbientConditions } from "./AmbientConditions";

afterEach(cleanup);

describe("AmbientConditions (#464 — the live 'Room' readout)", () => {
  it("renders the latest triad with units when every field is present", () => {
    render(
      <AmbientConditions
        ambientTempC={29.7}
        ambientHumidityPct={41}
        ambientPressureHpa={1008}
      />,
    );
    expect(screen.getByTestId("ambient-temp")).toHaveTextContent("29.7 °C");
    expect(screen.getByTestId("ambient-humidity")).toHaveTextContent("41 % RH");
    expect(screen.getByTestId("ambient-pressure")).toHaveTextContent("1008 hPa");
  });

  it("rounds humidity/pressure to whole numbers and keeps one decimal for temp", () => {
    render(
      <AmbientConditions
        ambientTempC={22.449}
        ambientHumidityPct={40.6}
        ambientPressureHpa={1013.2}
      />,
    );
    expect(screen.getByTestId("ambient-temp")).toHaveTextContent("22.4 °C");
    expect(screen.getByTestId("ambient-humidity")).toHaveTextContent("41 % RH");
    expect(screen.getByTestId("ambient-pressure")).toHaveTextContent("1013 hPa");
  });

  it("shows an em dash per field when every field is null (uncaptured/disabled)", () => {
    render(
      <AmbientConditions ambientTempC={null} ambientHumidityPct={null} ambientPressureHpa={null} />,
    );
    expect(screen.getByTestId("ambient-temp")).toHaveTextContent("— °C");
    expect(screen.getByTestId("ambient-humidity")).toHaveTextContent("— % RH");
    expect(screen.getByTestId("ambient-pressure")).toHaveTextContent("— hPa");
    // No leaking NaN/undefined/null text into the DOM (back-compat invariant).
    const el = screen.getByTestId("ambient-conditions");
    expect(el.textContent).not.toMatch(/NaN|undefined|null/i);
  });

  it("treats undefined the same as null (pre-frame / pre-#342 back-compat)", () => {
    render(
      <AmbientConditions
        ambientTempC={undefined}
        ambientHumidityPct={undefined}
        ambientPressureHpa={undefined}
      />,
    );
    expect(screen.getByTestId("ambient-temp")).toHaveTextContent("— °C");
    expect(screen.getByTestId("ambient-humidity")).toHaveTextContent("— % RH");
    expect(screen.getByTestId("ambient-pressure")).toHaveTextContent("— hPa");
  });

  it("handles a PARTIAL null gracefully — one field present, others null", () => {
    render(
      <AmbientConditions ambientTempC={29.7} ambientHumidityPct={null} ambientPressureHpa={1008} />,
    );
    expect(screen.getByTestId("ambient-temp")).toHaveTextContent("29.7 °C");
    expect(screen.getByTestId("ambient-humidity")).toHaveTextContent("— % RH");
    expect(screen.getByTestId("ambient-pressure")).toHaveTextContent("1008 hPa");
  });

  it("labels the readout 'Room' so it never reads as the bean probe", () => {
    render(<AmbientConditions ambientTempC={29.7} ambientHumidityPct={41} ambientPressureHpa={1008} />);
    const el = screen.getByTestId("ambient-conditions");
    expect(el).toHaveTextContent(/room/i);
    expect(el).toHaveAttribute("title", expect.stringMatching(/ambient/i));
  });

  it("renders an em dash for a non-finite value (Infinity), not 'Infinity °C'", () => {
    render(
      <AmbientConditions
        ambientTempC={Infinity}
        ambientHumidityPct={-Infinity}
        ambientPressureHpa={Infinity}
      />,
    );
    expect(screen.getByTestId("ambient-temp")).toHaveTextContent("— °C");
    expect(screen.getByTestId("ambient-humidity")).toHaveTextContent("— % RH");
    expect(screen.getByTestId("ambient-pressure")).toHaveTextContent("— hPa");
    const el = screen.getByTestId("ambient-conditions");
    expect(el.textContent).not.toMatch(/infinity/i);
  });
});
