import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PostFcRecoveryStatus } from "./PostFcRecoveryStatus";

afterEach(cleanup);

describe("PostFcRecoveryStatus", () => {
  it("renders the armed state before a post-FC control output exists", () => {
    render(
      <PostFcRecoveryStatus
        phase="development"
        trace={{
          recoveryEnabled: true,
          heatAuthorityState: null,
          rorSetpointCPerMin: null,
          smoothedRorCPerMin: null,
          effectiveHeatCeilingPercent: null,
          atChargeElapsedSeconds: 300,
        }}
      />,
    );

    const status = screen.getByTestId("post-fc-recovery-status");
    expect(status).toHaveAttribute("data-state", "armed");
    expect(status).toHaveTextContent("Recovery armed");
    expect(status).toHaveTextContent("RoR — °C/min");
    expect(status).toHaveTextContent("Target — °C/min");
    expect(status).toHaveTextContent("Heat ceiling — %");
  });

  it("distinguishes the disabled feature from an armed HOLDING state", () => {
    const { rerender } = render(
      <PostFcRecoveryStatus
        phase="development"
        trace={{
          recoveryEnabled: false,
          heatAuthorityState: "holding",
          rorSetpointCPerMin: 6,
          smoothedRorCPerMin: 6.2,
          effectiveHeatCeilingPercent: 60,
          atChargeElapsedSeconds: 500,
        }}
      />,
    );
    expect(screen.getByText("Recovery off")).toBeInTheDocument();
    expect(screen.getByTestId("post-fc-recovery-status")).toHaveAttribute(
      "data-state",
      "off",
    );

    rerender(
      <PostFcRecoveryStatus
        phase="development"
        trace={{
          recoveryEnabled: true,
          heatAuthorityState: "holding",
          rorSetpointCPerMin: 6,
          smoothedRorCPerMin: 6.2,
          effectiveHeatCeilingPercent: 60,
          atChargeElapsedSeconds: 501,
        }}
      />,
    );
    expect(screen.getByText("Armed · Holding")).toBeInTheDocument();
    expect(screen.getByTestId("post-fc-recovery-status")).toHaveAttribute(
      "data-state",
      "holding",
    );
  });

  it("labels entry and exit glide and renders controller diagnostics", () => {
    const { rerender } = render(
      <PostFcRecoveryStatus
        phase="development"
        trace={{
          recoveryEnabled: true,
          heatAuthorityState: "recovering",
          rorSetpointCPerMin: 6.4,
          smoothedRorCPerMin: 4.8,
          effectiveHeatCeilingPercent: 75,
          atChargeElapsedSeconds: 510,
        }}
      />,
    );
    expect(screen.getByText("Recovery entry")).toBeInTheDocument();
    expect(screen.getByTestId("post-fc-recovery-status")).toHaveTextContent(
      "4.8 °C/min",
    );
    expect(screen.getByTestId("post-fc-recovery-status")).toHaveTextContent("75 %");

    rerender(
      <PostFcRecoveryStatus
        phase="development"
        trace={{
          recoveryEnabled: true,
          heatAuthorityState: "gliding",
          rorSetpointCPerMin: 6,
          smoothedRorCPerMin: 6.1,
          effectiveHeatCeilingPercent: 70,
          atChargeElapsedSeconds: 515,
        }}
      />,
    );
    expect(screen.getByText("Exit glide")).toBeInTheDocument();
  });

  it("does not present persisted authority as live during restart recovery", () => {
    render(
      <PostFcRecoveryStatus
        phase="operator_recovery_required"
        trace={{
          recoveryEnabled: true,
          heatAuthorityState: "recovering",
          rorSetpointCPerMin: 6.4,
          smoothedRorCPerMin: 4.8,
          effectiveHeatCeilingPercent: 75,
          atChargeElapsedSeconds: 510,
        }}
      />,
    );

    const status = screen.getByTestId("post-fc-recovery-status");
    expect(status).toHaveAttribute("data-state", "armed");
    expect(status).toHaveTextContent("Recovery armed");
    expect(status).not.toHaveTextContent("Recovery entry");
    expect(status).toHaveTextContent("RoR — °C/min");
    expect(status).toHaveTextContent("Target — °C/min");
    expect(status).toHaveTextContent("Heat ceiling — %");
  });
});
