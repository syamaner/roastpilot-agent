import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RoastHeader } from "./RoastHeader";

afterEach(cleanup);

const BASE = {
  phase: "development" as const,
  elapsedSeconds: 582,
  developmentSeconds: 72,
  beanRorCPerMin: 8.4,
  profileName: "Ethiopian Yirgacheffe — Medium",
  firstCrack: null,
  mcpChild: "running" as const,
};

describe("RoastHeader", () => {
  it("renders the phase badge from the server phase (operator-facing label)", () => {
    render(<RoastHeader {...BASE} />);
    const badge = screen.getByTestId("phase-badge");
    expect(badge).toHaveAttribute("data-phase", "development");
    expect(badge).toHaveTextContent("DEVELOPMENT");
  });

  it("formats the roast + development timers as mm:ss (tabular)", () => {
    render(<RoastHeader {...BASE} />);
    expect(screen.getByTestId("roast-timer")).toHaveTextContent("09:42");
    expect(screen.getByTestId("development-timer")).toHaveTextContent("01:12");
  });

  it("shows the live bean RoR readout in °C/min (#165)", () => {
    render(<RoastHeader {...BASE} beanRorCPerMin={8.4} />);
    const ror = screen.getByTestId("ror-readout");
    expect(ror).toHaveTextContent("8.4 °C/min");
  });

  it("shows the RoR readout from the start incl. preheat (real data, not hidden) (#165)", () => {
    // Operator clarification: pre-charge RoR is real probe data and stays shown;
    // the charge (T0) marker on the curve flags the meaningful turning point.
    render(
      <RoastHeader
        {...BASE}
        phase="preheating"
        developmentSeconds={null}
        beanRorCPerMin={14.2}
      />,
    );
    expect(screen.getByTestId("ror-readout")).toHaveTextContent("14.2 °C/min");
  });

  it("renders the RoR readout as a placeholder when no rate yet (null-safe)", () => {
    render(<RoastHeader {...BASE} beanRorCPerMin={null} />);
    expect(screen.getByTestId("ror-readout")).toHaveTextContent("— °C/min");
  });

  it("omits the development timer before first crack (GAP A — no dev% invented)", () => {
    render(<RoastHeader {...BASE} developmentSeconds={null} />);
    expect(screen.queryByTestId("development-timer")).toBeNull();
  });

  it("shows FC 'listening' while roasting pre-first-crack, no mock audio dot", () => {
    render(<RoastHeader {...BASE} phase="roasting_pre_first_crack" developmentSeconds={null} />);
    const fc = screen.getByTestId("fc-status");
    expect(fc).toHaveAttribute("data-detected", "false");
    expect(fc).toHaveTextContent(/listening/i);
  });

  it("shows the real FC detection (temp + source) once it fires", () => {
    render(
      <RoastHeader {...BASE} firstCrack={{ source: "mcp", bean_temp_c: 201.2 }} />,
    );
    const fc = screen.getByTestId("fc-status");
    expect(fc).toHaveAttribute("data-detected", "true");
    expect(fc).toHaveTextContent("201.2 °C");
    expect(fc).toHaveTextContent("mcp");
  });

  it("reflects the MCP child health on the roaster-link dot", () => {
    render(<RoastHeader {...BASE} mcpChild="stopped" />);
    expect(screen.getByTestId("roaster-link")).toHaveAttribute("data-status", "stopped");
  });

  it("opens the diagnostics drawer over real signals only", () => {
    render(<RoastHeader {...BASE} />);
    expect(screen.queryByTestId("diagnostics-drawer")).toBeNull();
    fireEvent.click(screen.getByTestId("diagnostics-toggle"));
    const drawer = screen.getByTestId("diagnostics-drawer");
    expect(drawer).toHaveTextContent("development");
    expect(drawer).toHaveTextContent("running");
  });
});
