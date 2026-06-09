import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { RoastHeader } from "./RoastHeader";

afterEach(cleanup);

const BASE = {
  phase: "development" as const,
  elapsedSeconds: 582,
  developmentSeconds: 72,
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
