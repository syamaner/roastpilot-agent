import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PostFcRecoveryStatus } from "./PostFcRecoveryStatus";

afterEach(cleanup);

describe("PostFcRecoveryStatus", () => {
  it("distinguishes the disabled feature from an armed HOLDING state", () => {
    const { rerender } = render(
      <PostFcRecoveryStatus
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

    rerender(
      <PostFcRecoveryStatus
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
  });

  it("labels entry and exit glide and renders controller diagnostics", () => {
    const { rerender } = render(
      <PostFcRecoveryStatus
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
});
