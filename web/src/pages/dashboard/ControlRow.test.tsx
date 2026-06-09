import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ControlRow } from "./ControlRow";

afterEach(cleanup);

describe("ControlRow", () => {
  it("renders the applied heat/fan values (%)", () => {
    render(
      <ControlRow heatPercent={65} fanPercent={70} targetHeatPercent={null} targetFanPercent={null} />,
    );
    expect(screen.getByTestId("control-heat-value")).toHaveTextContent("65 %");
    expect(screen.getByTestId("control-fan-value")).toHaveTextContent("70 %");
  });

  it("renders ghost markers at the advisor targets", () => {
    render(
      <ControlRow heatPercent={65} fanPercent={70} targetHeatPercent={60} targetFanPercent={75} />,
    );
    expect(screen.getByTestId("control-heat-ghost")).toHaveAttribute("data-target", "60");
    expect(screen.getByTestId("control-fan-ghost")).toHaveAttribute("data-target", "75");
  });

  it("omits the ghost marker when there is no advisor target", () => {
    render(
      <ControlRow heatPercent={65} fanPercent={70} targetHeatPercent={null} targetFanPercent={null} />,
    );
    expect(screen.queryByTestId("control-heat-ghost")).toBeNull();
    expect(screen.queryByTestId("control-fan-ghost")).toBeNull();
  });

  it("shows a placeholder when a value is unknown", () => {
    render(
      <ControlRow heatPercent={null} fanPercent={null} targetHeatPercent={null} targetFanPercent={null} />,
    );
    expect(screen.getByTestId("control-heat-value")).toHaveTextContent("— %");
  });
});
