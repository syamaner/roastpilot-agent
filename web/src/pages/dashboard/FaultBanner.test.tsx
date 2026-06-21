import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { SafetyEvaluationData } from "./events";
import { FaultBanner } from "./FaultBanner";
import type { SafetyTrailEntry } from "./useDashboardEvents";

afterEach(cleanup);

const FAULT: SafetyEvaluationData = {
  rule: "env_temp_ceiling",
  verdict: "fault",
  input_heat: null,
  input_fan: null,
  adjusted_heat: 0,
  adjusted_fan: 100,
  reason: "Environment temp exceeded the 240 °C ceiling",
};

const TRAIL: SafetyTrailEntry[] = [
  {
    kind: "safety_alert",
    evaluation: { ...FAULT, verdict: "emergency_stop", reason: "env ceiling tripped emergency stop" },
  },
  { kind: "fault", evaluation: FAULT },
];

describe("FaultBanner", () => {
  it("renders nothing when there is no fault", () => {
    render(<FaultBanner fault={null} trail={[]} />);
    expect(screen.queryByTestId("fault-banner")).toBeNull();
  });

  it("states what the safety layer did (the fault reason)", () => {
    render(<FaultBanner fault={FAULT} trail={[]} />);
    expect(screen.getByTestId("fault-reason")).toHaveTextContent(/exceeded the 240 °C ceiling/);
  });

  it("shows the forced-safe state (heat forced to 0, fan safe)", () => {
    render(<FaultBanner fault={FAULT} trail={[]} />);
    const banner = screen.getByTestId("fault-banner");
    expect(banner).toHaveTextContent(/0 % \(forced\)/);
    expect(banner).toHaveTextContent(/100 % \(safe\)/);
  });

  it("accumulates the safety event trail with verdict labels", () => {
    render(<FaultBanner fault={FAULT} trail={TRAIL} />);
    const rows = screen.getAllByTestId("safety-trail-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("EMERGENCY STOP");
    expect(rows[1]).toHaveTextContent("FAULT");
  });

  it("bounds the trail height + scrolls so a long trail can't push the page away", () => {
    // Roast-2 post-mortem: a re-emitted-every-tick fault spammed the trail with
    // dozens of identical rows, growing it unbounded and scrolling the rest of
    // the page out of view. The container must be height-capped + scrollable.
    const longTrail: SafetyTrailEntry[] = Array.from({ length: 60 }, () => ({
      kind: "fault" as const,
      evaluation: FAULT,
    }));
    render(<FaultBanner fault={FAULT} trail={longTrail} />);
    const list = screen.getByTestId("safety-trail");
    expect(list.className).toMatch(/max-h-/);
    expect(list.className).toContain("overflow-y-auto");
    // Every row still renders (reachable via scroll, none dropped).
    expect(screen.getAllByTestId("safety-trail-row")).toHaveLength(60);
  });

  it("is informational + persistent — NO server-dispatching button on the banner", () => {
    // A fault must not be hidden by the operator, and the banner must not re-issue
    // a roaster command under a misleading 'acknowledge' label (button honesty).
    render(<FaultBanner fault={FAULT} trail={TRAIL} />);
    expect(screen.queryByTestId("acknowledge-fault")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders the optional forward-nav affordance (a fault is terminal)", () => {
    render(
      <FaultBanner
        fault={FAULT}
        trail={[]}
        acknowledgeAffordance={<a data-testid="start-new">Start New Roast</a>}
      />,
    );
    expect(screen.getByTestId("start-new")).toBeInTheDocument();
  });

  it("renders no action affordance when acknowledgeAffordance is omitted", () => {
    render(<FaultBanner fault={FAULT} trail={[]} />);
    expect(screen.queryByTestId("start-new")).toBeNull();
  });
});
