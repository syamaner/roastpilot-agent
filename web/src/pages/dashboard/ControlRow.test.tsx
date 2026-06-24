import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { RoastPhase } from "@/lib/types";

import { ControlRow } from "./ControlRow";

afterEach(cleanup);

describe("ControlRow", () => {
  it("renders the applied heat/fan values (%) in any phase", () => {
    render(
      <ControlRow
        phase="development"
        heatPercent={65}
        fanPercent={70}
        targetHeatPercent={null}
        targetFanPercent={null}
      />,
    );
    expect(screen.getByTestId("control-heat-value")).toHaveTextContent("65 %");
    expect(screen.getByTestId("control-fan-value")).toHaveTextContent("70 %");
  });

  describe("post-first-crack (interactive presentation)", () => {
    it("renders the slider-style bars and the advisor-target ghost markers", () => {
      render(
        <ControlRow
          phase="development"
          heatPercent={65}
          fanPercent={70}
          targetHeatPercent={60}
          targetFanPercent={75}
        />,
      );
      expect(screen.getByTestId("control-heat")).toHaveAttribute("data-mode", "interactive");
      expect(screen.getByTestId("control-fan")).toHaveAttribute("data-mode", "interactive");
      expect(screen.getByTestId("control-heat-ghost")).toHaveAttribute("data-target", "60");
      expect(screen.getByTestId("control-fan-ghost")).toHaveAttribute("data-target", "75");
      // Interactive mode shows the bar scale, not the read-out note.
      expect(screen.queryByTestId("control-heat-readout-note")).toBeNull();
    });

    it("omits the ghost marker when there is no advisor target", () => {
      render(
        <ControlRow
          phase="development"
          heatPercent={65}
          fanPercent={70}
          targetHeatPercent={null}
          targetFanPercent={null}
        />,
      );
      expect(screen.queryByTestId("control-heat-ghost")).toBeNull();
      expect(screen.queryByTestId("control-fan-ghost")).toBeNull();
    });

    it("shows a placeholder when a value is unknown", () => {
      render(
        <ControlRow
          phase="development"
          heatPercent={null}
          fanPercent={null}
          targetHeatPercent={null}
          targetFanPercent={null}
        />,
      );
      expect(screen.getByTestId("control-heat-value")).toHaveTextContent("— %");
    });
  });

  describe("pre-first-crack (read-out presentation, #318)", () => {
    it.each<RoastPhase>(["preheating", "roasting_pre_first_crack"])(
      "renders heat/fan as read-outs (no bar, no settable affordance) in %s",
      (phase) => {
        render(
          <ControlRow
            phase={phase}
            heatPercent={100}
            fanPercent={30}
            targetHeatPercent={null}
            targetFanPercent={null}
          />,
        );
        // Still shows the applied value...
        expect(screen.getByTestId("control-heat-value")).toHaveTextContent("100 %");
        expect(screen.getByTestId("control-fan-value")).toHaveTextContent("30 %");
        // ...but in read-out mode: the controller-driven note, no slider bar scale.
        expect(screen.getByTestId("control-heat")).toHaveAttribute("data-mode", "readout");
        expect(screen.getByTestId("control-fan")).toHaveAttribute("data-mode", "readout");
        expect(screen.getByTestId("control-heat-readout-note")).toBeInTheDocument();
        expect(screen.getByTestId("control-fan-readout-note")).toBeInTheDocument();
        // No settable affordance: the 0/50/100 slider scale is GONE pre-FC (it
        // only renders in the interactive bar). Asserting its absence proves we
        // removed the dial, not just added a note alongside it.
        expect(screen.getByTestId("control-heat")).not.toHaveTextContent("50");
        expect(screen.getByTestId("control-fan")).not.toHaveTextContent("50");
      },
    );

    it("suppresses the advisor-target ghost marker pre-FC even if a target is passed", () => {
      // The advisor is gated out pre-FC; a stale/passed-through target must NOT
      // render a ghost that implies a settable dial that would silently revert.
      render(
        <ControlRow
          phase="preheating"
          heatPercent={100}
          fanPercent={30}
          targetHeatPercent={80}
          targetFanPercent={20}
        />,
      );
      expect(screen.queryByTestId("control-heat-ghost")).toBeNull();
      expect(screen.queryByTestId("control-fan-ghost")).toBeNull();
    });
  });
});
